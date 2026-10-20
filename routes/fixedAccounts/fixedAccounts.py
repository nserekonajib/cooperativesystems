"""
Fixed Deposit Accounts blueprint.

Endpoints (all admin-only JSON unless noted):
    GET    /fixed/                                       -> page
    GET    /fixed/api/stats                              -> KPI cards
    GET    /fixed/api/list                               -> paginated list
    GET    /fixed/api/<uuid:fd_uuid>                     -> single deposit + member
    GET    /fixed/api/<uuid:fd_uuid>/transactions        -> paginated tx list
    GET    /fixed/api/<uuid:fd_uuid>/schedule            -> interest schedule
    GET    /fixed/api/<uuid:fd_uuid>/statement           -> CSV download

    POST   /fixed/api/create                             -> create deposit (cash: apply now; online: pawaPay first)
    POST   /fixed/api/<uuid:fd_uuid>/topup               -> add principal
    POST   /fixed/api/<uuid:fd_uuid>/withdraw            -> allowed only when matured (or admin early)
    POST   /fixed/api/payment/<ref>/verify               -> verify pawaPay deposit
    POST   /fixed/api/<uuid:fd_uuid>/accrue              -> apply interest accrual up to today

Design:
- Accounts are created by admin (NOT auto-created with members).
- Interest math is server-side; schedule is pure function of snapshotted terms.
- Online creation/top-ups stage a JSON file (like savings) until pawaPay confirms.
- Withdrawal blocked until maturity_date <= today unless admin passes
  {early: true} and the deposit's product allows early withdrawal.
"""

import os
import io
import csv
import json
import secrets
import logging
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from flask import (
    Blueprint, render_template, request, jsonify, session, redirect, url_for,
    current_app, Response,
)
from dotenv import load_dotenv
from supabase import create_client, Client

from routes.auth.auth import role_required
from routes.payments.pawapay import (
    deposit_mtn_ug,
    deposit_airtel_ug,
    verify_deposit,
)

load_dotenv()

log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] fixed: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

fixed_bp = Blueprint("fixed", __name__, url_prefix="/fixed")

PAWAPAY_MESSAGE = "SACCO fixed dep"
MONEY_Q = Decimal("0.01")

_supabase_admin: Client | None = None


def _admin() -> Client:
    global _supabase_admin
    if _supabase_admin is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY missing")
        _supabase_admin = create_client(url, key)
    return _supabase_admin


# ---------------------------------------------------------------------------
# Pending (online) staging
# ---------------------------------------------------------------------------
def _pending_dir() -> Path:
    p = Path(current_app.instance_path) / "pending_fixed"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _pending_file(deposit_id: str) -> Path:
    safe = "".join(c for c in (deposit_id or "") if c.isalnum() or c in "-_")
    return _pending_dir() / f"{safe}.json"


def _save_pending(deposit_id: str, payload: dict) -> None:
    path = _pending_file(deposit_id)
    tmp  = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_pending(deposit_id: str) -> dict | None:
    path = _pending_file(deposit_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("failed to read pending file %s", path)
        return None


def _delete_pending(deposit_id: str) -> None:
    path = _pending_file(deposit_id)
    try:
        if path.exists():
            path.unlink()
    except Exception:
        log.exception("failed to delete pending file %s", path)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _bad(msg: str, code: int = 400, reason: str | None = None):
    log.warning("REJECT [%s] %s (reason=%s)", code, msg, reason)
    return jsonify({"ok": False, "error": msg, "reason": reason}), code


def _org_id() -> str:
    return session["user"]["org_id"]


def _user_id() -> str:
    return session["user"]["id"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _to_decimal(v, default="0") -> Decimal:
    if v is None or v == "":
        return Decimal(default)
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid number: {v}")


def _q(v: Decimal) -> Decimal:
    return v.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _normalize_phone(p: str) -> str:
    digits = "".join(c for c in (p or "") if c.isdigit())
    if digits.startswith("0"):
        digits = "256" + digits[1:]
    elif not digits.startswith("256") and len(digits) == 9:
        digits = "256" + digits
    return digits


def _parse_date(v) -> date | None:
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# FD number generation
# ---------------------------------------------------------------------------
def _gen_fd_number(org_id: str, max_tries: int = 20) -> str:
    admin = _admin()
    for _ in range(max_tries):
        candidate = "FD-" + "".join(
            str(secrets.randbelow(10)) for _ in range(8)
        )
        resp = (
            admin.table("fixed_deposits")
            .select("id")
            .eq("org_id", org_id)
            .eq("fd_number", candidate)
            .limit(1)
            .execute()
        )
        if not resp.data:
            return candidate
    raise RuntimeError("Could not generate unique FD number")


# ---------------------------------------------------------------------------
# Interest math
# ---------------------------------------------------------------------------
def _add_months(d: date, months: int) -> date:
    """Add `months` to a date, clamping day to end of month if needed."""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    # clamp day
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return date(y, m, 28)


def _compounds_per_year(freq: str) -> int:
    return {"monthly": 12, "quarterly": 4, "annually": 1}.get(freq, 12)


def _matured(dep: dict) -> bool:
    md = _parse_date(dep.get("maturity_date"))
    return bool(md and md <= _today())


def _interest_to_date(dep: dict, as_of: date) -> Decimal:
    """
    Accrued interest from start_date to as_of (inclusive of whole periods).
    Simple:   P * r * t           (t in years)
    Compound: P * ((1 + r/n)^(n*t) - 1)
    """
    if not dep.get("interest_enabled"):
        return Decimal("0")

    P = _to_decimal(dep.get("principal"))
    r = _to_decimal(dep.get("interest_rate_percent")) / Decimal("100")
    if P <= 0 or r <= 0:
        return Decimal("0")

    start = _parse_date(dep.get("start_date")) or _today()
    end   = min(as_of, _parse_date(dep.get("maturity_date")) or as_of)
    if end <= start:
        return Decimal("0")

    days = (end - start).days
    years = Decimal(days) / Decimal("365")

    itype = (dep.get("interest_type") or "simple").lower()
    if itype == "simple":
        interest = P * r * years
    else:
        n = _compounds_per_year(dep.get("compound_frequency") or "monthly")
        # compound up to whole periods; prorate the remainder linearly
        periods = int((Decimal(days) * Decimal(n) / Decimal("365")).to_integral_value(rounding="ROUND_DOWN"))
        base = P * ((Decimal(1) + r / Decimal(n)) ** periods - Decimal(1))
        # remainder proration
        days_used = Decimal(periods) * Decimal("365") / Decimal(n)
        remaining_days = Decimal(days) - days_used
        extra = (P + base) * r * (remaining_days / Decimal("365"))
        interest = base + extra

    return _q(interest)


def _schedule(dep: dict) -> list[dict]:
    """
    Monthly schedule rows from start_date to maturity_date. Each row shows
    expected interest accrued to that month-end.
    """
    start = _parse_date(dep.get("start_date")) or _today()
    maturity = _parse_date(dep.get("maturity_date")) or start
    rows = []
    cur = start
    while cur < maturity:
        # next month-end
        nxt = _add_months(cur.replace(day=1), 1)
        nxt = date(nxt.year, nxt.month, 28)
        # find last day of that month
        last = _add_months(cur.replace(day=1), 1) - timedelta(days=1)
        cur = min(last, maturity)
        accrued = _interest_to_date(dep, cur)
        rows.append({
            "as_of":              cur.isoformat(),
            "months_elapsed":     (cur.year - start.year) * 12 + (cur.month - start.month),
            "accrued_interest":   float(accrued),
            "projected_balance":  float(_q(_to_decimal(dep.get("principal")) + accrued)),
        })
    return rows


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
@fixed_bp.route("/", methods=["GET"])
def page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    if session["user"].get("role") != "admin":
        return redirect(url_for("auth.dashboard"))
    return render_template("fixedAccounts/fixedAccounts.html")


# ---------------------------------------------------------------------------
# STATS
# ---------------------------------------------------------------------------
@fixed_bp.route("/api/stats", methods=["GET"])
@role_required("admin")
def api_stats():
    org_id = _org_id()
    try:
        rows = (
            _admin().table("fixed_deposits")
            .select("principal, accrued_interest, status, maturity_date")
            .eq("org_id", org_id)
            .execute()
        ).data or []

        active_count = 0
        matured_count = 0
        total_principal = Decimal("0")
        total_interest = Decimal("0")

        today = _today()
        for r in rows:
            total_principal += _to_decimal(r.get("principal"))
            total_interest  += _to_decimal(r.get("accrued_interest"))
            if r.get("status") == "active":
                md = _parse_date(r.get("maturity_date"))
                if md and md <= today:
                    matured_count += 1
                else:
                    active_count += 1
            elif r.get("status") == "matured":
                matured_count += 1

        # maturing in the next 30 days (still active)
        soon_cutoff = today + timedelta(days=30)
        upcoming = 0
        for r in rows:
            if r.get("status") != "active":
                continue
            md = _parse_date(r.get("maturity_date"))
            if md and today < md <= soon_cutoff:
                upcoming += 1

    except Exception as e:
        log.exception("fixed stats failed: %s", e)
        return _bad("Could not load stats", 500)

    return jsonify({
        "ok": True,
        "stats": {
            "total_principal":  float(_q(total_principal)),
            "total_interest":   float(_q(total_interest)),
            "active_count":     active_count,
            "matured_count":    matured_count,
            "maturing_30d":     upcoming,
        },
    })


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------
@fixed_bp.route("/api/list", methods=["GET"])
@role_required("admin")
def api_list():
    org_id = _org_id()
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
        search   = (request.args.get("q") or "").strip()
        status   = (request.args.get("status") or "").strip()
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    q = (
        _admin().table("fixed_deposits")
        .select(
            "id, fd_number, principal, target_amount, accrued_interest, currency, "
            "start_date, maturity_date, period_months, status, "
            "interest_enabled, interest_rate_percent, interest_type, "
            "members!inner(id, member_id, first_name, last_name, phone, email)",
            count="exact",
        )
        .eq("org_id", org_id)
        .order("created_at", desc=True)
        .range(start, end)
    )
    if status:
        q = q.eq("status", status)

    if search:
        like = f"%{search}%"

        # (1) Find member IDs whose name / member_id / phone match.
        #     PostgREST won't let us OR parent columns with embedded
        #     foreign-table columns in a single expression, so we do
        #     this as its own query (single round-trip, capped IN list).
        try:
            m_resp = (
                _admin().table("members")
                .select("id")
                .eq("org_id", org_id)
                .or_(
                    f"first_name.ilike.{like},"
                    f"last_name.ilike.{like},"
                    f"member_id.ilike.{like},"
                    f"phone.ilike.{like}"
                )
                .limit(500)
                .execute()
            )
            member_ids = [r["id"] for r in (m_resp.data or [])]
        except Exception as e:
            log.exception("member search failed: %s", e)
            member_ids = []

        # (2) Combine: fd_number matches OR member_id is in the matched set.
        if member_ids:
            id_list = ",".join(member_ids)   # UUIDs, safe to inline
            q = q.or_(f"fd_number.ilike.{like},member_id.in.({id_list})")
        else:
            q = q.ilike("fd_number", like)

    try:
        resp = q.execute()
    except Exception as e:
        log.exception("fixed list failed: %s", e)
        return _bad("Could not load deposits", 500)

    # Add computed progress + maturity flag per row
    deposits = resp.data or []
    today = _today()
    for d in deposits:
        principal = _to_decimal(d.get("principal"))
        target = _to_decimal(d.get("target_amount"))
        interest = _to_decimal(d.get("accrued_interest"))
        total = principal + interest
        progress = 0
        if target > 0:
            progress = float(min(Decimal("100"), (total / target) * Decimal("100")))
        d["progress_percent"] = round(progress, 2)
        d["total_value"]      = float(_q(total))
        d["matured"]          = bool(
            (_parse_date(d.get("maturity_date")) or today) <= today
        )

    return jsonify({
        "ok": True,
        "deposits": deposits,
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })

# ---------------------------------------------------------------------------
# DETAIL
# ---------------------------------------------------------------------------
@fixed_bp.route("/api/<uuid:fd_uuid>", methods=["GET"])
@role_required("admin")
def api_detail(fd_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("fixed_deposits")
            .select(
                "*, members!inner(id, member_id, first_name, last_name, "
                "phone, email, shares_units, shares_value), "
                "fixed_deposit_products(name, description, "
                "min_period_months, max_period_months, min_amount)"
            )
            .eq("org_id", org_id)
            .eq("id", str(fd_uuid))
            .limit(1)
            .execute()
        )
    except Exception as e:
        log.exception("fixed detail failed: %s", e)
        return _bad("Could not load deposit", 500)

    if not resp.data:
        return _bad("Deposit not found", 404)

    d = resp.data[0]
    principal = _to_decimal(d.get("principal"))
    target = _to_decimal(d.get("target_amount"))
    interest = _to_decimal(d.get("accrued_interest"))
    total = principal + interest
    progress = 0
    if target > 0:
        progress = float(min(Decimal("100"), (total / target) * Decimal("100")))

    d["progress_percent"] = round(progress, 2)
    d["total_value"]      = float(_q(total))
    d["matured"]          = _matured(d)
    d["interest_to_date"] = float(_interest_to_date(d, _today()))

    return jsonify({"ok": True, "deposit": d})


# ---------------------------------------------------------------------------
# TRANSACTIONS
# ---------------------------------------------------------------------------
@fixed_bp.route("/api/<uuid:fd_uuid>/transactions", methods=["GET"])
@role_required("admin")
def api_transactions(fd_uuid):
    org_id = _org_id()
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(200, max(10, int(request.args.get("per_page", 50))))
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    try:
        resp = (
            _admin().table("fixed_deposit_transactions")
            .select(
                "id, type, method, amount, currency, status, provider, "
                "provider_ref, phone, narration, principal_after, "
                "interest_after, total_after, created_at",
                count="exact",
            )
            .eq("org_id", org_id)
            .eq("deposit_id", str(fd_uuid))
            .order("created_at", desc=True)
            .range(start, end)
            .execute()
        )
    except Exception as e:
        log.exception("fixed tx list failed: %s", e)
        return _bad("Could not load transactions", 500)

    return jsonify({
        "ok": True,
        "transactions": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# SCHEDULE
# ---------------------------------------------------------------------------
@fixed_bp.route("/api/<uuid:fd_uuid>/schedule", methods=["GET"])
@role_required("admin")
def api_schedule(fd_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("fixed_deposits")
            .select("*")
            .eq("org_id", org_id)
            .eq("id", str(fd_uuid))
            .limit(1)
            .execute()
        )
    except Exception as e:
        log.exception("schedule fetch failed")
        return _bad("Could not load deposit", 500)

    if not resp.data:
        return _bad("Deposit not found", 404)

    return jsonify({"ok": True, "schedule": _schedule(resp.data[0])})


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------
@fixed_bp.route("/api/create", methods=["POST"])
@role_required("admin")
def api_create():
    """
    Body:
    {
      member_id,
      principal,
      period_months,
      target_amount?,
      interest_enabled? = true,
      interest_rate_percent? = 0,
      interest_type? = 'simple',
      compound_frequency? = 'monthly',
      early_withdrawal_allowed? = false,
      early_withdrawal_penalty_percent? = 0,
      notes?,
      payment: { method: 'cash'|'online', provider?, phone? }
    }
    """
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}

    # ---- validate ----
    member_id = (data.get("member_id") or "").strip()
    if not member_id:
        return _bad("member_id is required", reason="missing_member")

    try:
        principal = _to_decimal(data.get("principal"))
        if principal <= 0:
            return _bad("Principal must be greater than zero", reason="bad_principal")
    except ValueError:
        return _bad("Invalid principal", reason="bad_principal")

    try:
        period_months = int(data.get("period_months") or 0)
        if period_months < 1 or period_months > 120:
            return _bad("period_months must be 1..120", reason="bad_period")
    except (TypeError, ValueError):
        return _bad("Invalid period_months", reason="bad_period")

    try:
        target = _to_decimal(data.get("target_amount"))
        if target < 0:
            target = Decimal("0")
    except ValueError:
        target = Decimal("0")

    interest_enabled = bool(data.get("interest_enabled", True))
    try:
        rate = _to_decimal(data.get("interest_rate_percent"))
        if rate < 0 or rate > 100:
            return _bad("interest_rate_percent must be 0..100", reason="bad_rate")
    except ValueError:
        return _bad("Invalid interest_rate_percent", reason="bad_rate")

    itype = (data.get("interest_type") or "simple").lower()
    if itype not in ("simple", "compound"):
        return _bad("interest_type must be 'simple' or 'compound'", reason="bad_type")

    comp_freq = (data.get("compound_frequency") or "monthly").lower()
    if comp_freq not in ("monthly", "quarterly", "annually"):
        return _bad("compound_frequency must be 'monthly', 'quarterly', or 'annually'",
                    reason="bad_freq")

    early_allowed = bool(data.get("early_withdrawal_allowed", False))
    try:
        early_penalty = _to_decimal(data.get("early_withdrawal_penalty_percent"))
        if early_penalty < 0 or early_penalty > 100:
            return _bad("early_withdrawal_penalty_percent must be 0..100",
                        reason="bad_penalty")
    except ValueError:
        return _bad("Invalid early_withdrawal_penalty_percent", reason="bad_penalty")

    payment = data.get("payment") or {}
    method  = (payment.get("method") or "cash").lower()
    if method not in ("cash", "online"):
        return _bad("payment.method must be 'cash' or 'online'", reason="bad_method")

    provider  = None
    pay_phone = None
    if method == "online":
        provider  = (payment.get("provider") or "").upper()
        pay_phone = (payment.get("phone") or "").strip()
        if provider not in ("MTN", "AIRTEL"):
            return _bad("payment.provider must be 'MTN' or 'AIRTEL'", reason="bad_provider")
        if not pay_phone:
            return _bad("payment.phone required for online", reason="missing_phone")
        pay_phone = _normalize_phone(pay_phone)

    # ---- member exists? ----
    # (currency is NOT on members — it comes from institution_settings)
    try:
        m_resp = (
            _admin().table("members")
            .select("id, member_id, first_name, last_name")
            .eq("org_id", org_id)
            .eq("id", member_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        log.exception("member fetch failed")
        return _bad("Could not load member", 500, reason="db_error")

    if not m_resp.data:
        return _bad("Member not found", 404, reason="not_found")
    member = m_resp.data[0]

    # ---- currency from institution_settings ----
    currency = _org_currency(org_id)

    start_date    = _today()
    maturity_date = _add_months(start_date, period_months)

    # ---- ONLINE: call pawaPay first, then stage JSON ----
    deposit_id = None
    if method == "online":
        try:
            if provider == "MTN":
                pay_resp = deposit_mtn_ug(
                    phone_number=pay_phone,
                    amount=str(int(principal)),
                    client_reference_id=f"fd-{secrets.token_hex(6)}",
                    customer_message=PAWAPAY_MESSAGE,
                )
            else:
                pay_resp = deposit_airtel_ug(
                    phone_number=pay_phone,
                    amount=str(int(principal)),
                    client_reference_id=f"fd-{secrets.token_hex(6)}",
                    customer_message=PAWAPAY_MESSAGE,
                )
        except Exception as e:
            log.exception("pawaPay preflight failed")
            return _bad(f"Payment could not be initiated: {e}. Deposit NOT created.",
                        502, reason="pawapay_raised")

        deposit_id = pay_resp.get("depositId") or pay_resp.get("deposit_id")
        pay_status = (pay_resp.get("status") or "").upper()
        if not deposit_id or pay_status in ("REJECTED", "FAILED"):
            reason = (pay_resp.get("failureReason") or {}).get("failureMessage") \
                     or pay_status or "unknown"
            return _bad(f"Payment was rejected: {reason}", 400, reason="payment_rejected")

        spec = {
            "org_id": org_id,
            "member_id": member_id,
            "principal": str(principal),
            "target_amount": str(target),
            "period_months": period_months,
            "currency": currency,
            "start_date": start_date.isoformat(),
            "maturity_date": maturity_date.isoformat(),
            "interest_enabled": interest_enabled,
            "interest_rate_percent": str(rate),
            "interest_type": itype,
            "compound_frequency": comp_freq,
            "early_withdrawal_allowed": early_allowed,
            "early_withdrawal_penalty_percent": str(early_penalty),
            "notes": (data.get("notes") or "").strip() or None,
            "method": "online",
            "provider": provider,
            "pay_phone": pay_phone,
            "deposit_id": deposit_id,
            "created_by": _user_id(),
            "staged_at": _now_iso(),
        }
        try:
            _save_pending(deposit_id, spec)
        except Exception as e:
            log.exception("failed to stage fixed deposit")
            return _bad(f"Payment accepted but staging failed: {e}", 500,
                        reason="staging_failed")

        return jsonify({
            "ok": True,
            "pending": True,
            "payment": {
                "method": "online",
                "status": "pending",
                "provider": provider,
                "deposit_id": deposit_id,
                "phone": pay_phone,
                "amount": float(principal),
                "currency": currency,
                "instructions": "Approve the prompt on the customer's phone.",
            },
        }), 202

    # ---- CASH: create deposit + first tx immediately ----
    try:
        result = _create_fixed_deposit(org_id, {
            "member_id": member_id,
            "principal": str(principal),
            "target_amount": str(target),
            "period_months": period_months,
            "currency": currency,
            "start_date": start_date.isoformat(),
            "maturity_date": maturity_date.isoformat(),
            "interest_enabled": interest_enabled,
            "interest_rate_percent": str(rate),
            "interest_type": itype,
            "compound_frequency": comp_freq,
            "early_withdrawal_allowed": early_allowed,
            "early_withdrawal_penalty_percent": str(early_penalty),
            "notes": (data.get("notes") or "").strip() or None,
            "method": "cash",
            "created_by": _user_id(),
        })
    except Exception as e:
        log.exception("cash fixed deposit create failed")
        return _bad(f"Could not create deposit: {e}", 500, reason="create_failed")

    return jsonify({
        "ok": True,
        "deposit": result,
        "payment": {
            "method": "cash",
            "status": "completed",
            "amount": float(principal),
            "currency": currency,
        },
    }), 201
    
    
def _org_currency(org_id: str) -> str:
    """Currency lives on institution_settings (per-org), not members."""
    try:
        r = (
            _admin().table("institution_settings")
            .select("currency")
            .eq("org_id", org_id)
            .limit(1)
            .execute()
        )
        return (r.data or [{}])[0].get("currency") or "UGX"
    except Exception:
        log.exception("currency lookup failed")
        return "UGX"

def _create_fixed_deposit(org_id: str, spec: dict) -> dict:
    """
    Insert the deposit row + initial principal transaction.
    Then post to the chart of accounts:
        Dr  1000 Cash on hand / 1010 Bank account   (money in)
        Cr  2010 Fixed deposits payable             (liability up)
    """
    admin = _admin()
    fd_number = _gen_fd_number(org_id)

    principal = _to_decimal(spec["principal"])
    target    = _to_decimal(spec.get("target_amount") or "0")

    row = {
        "org_id":           org_id,
        "member_id":        spec["member_id"],
        "fd_number":        fd_number,
        "principal":        float(principal),
        "target_amount":    float(target),
        "accrued_interest": 0,
        "paid_interest":    0,
        "currency":         spec.get("currency") or "UGX",
        "start_date":       spec["start_date"],
        "maturity_date":    spec["maturity_date"],
        "period_months":    int(spec["period_months"]),
        "interest_enabled": bool(spec.get("interest_enabled", True)),
        "interest_rate_percent": float(_to_decimal(spec.get("interest_rate_percent") or "0")),
        "interest_type":    spec.get("interest_type") or "simple",
        "compound_frequency": spec.get("compound_frequency") or "monthly",
        "early_withdrawal_allowed": bool(spec.get("early_withdrawal_allowed", False)),
        "early_withdrawal_penalty_percent": float(
            _to_decimal(spec.get("early_withdrawal_penalty_percent") or "0")
        ),
        "status":           "active",
        "notes":            spec.get("notes"),
        "created_by":       spec.get("created_by") or _user_id(),
    }
    resp = admin.table("fixed_deposits").insert(row).execute()
    if not resp.data:
        raise RuntimeError("insert returned no data")
    dep = resp.data[0]

    # --- initial principal transaction (FD ledger) ---
    tx = {
        "org_id":     org_id,
        "deposit_id": dep["id"],
        "member_id":  dep["member_id"],
        "type":       "principal",
        "method":     spec.get("method") or "cash",
        "amount":     float(principal),
        "currency":   dep["currency"],
        "status":     "completed",
        "principal_after": float(principal),
        "interest_after":  0,
        "total_after":     float(principal),
        "narration":  "Initial fixed deposit principal",
        "created_by": spec.get("created_by") or _user_id(),
    }
    if spec.get("provider"):
        tx["provider"] = spec["provider"]
    if spec.get("deposit_id"):
        tx["provider_ref"] = spec["deposit_id"]
    if spec.get("pay_phone"):
        tx["phone"] = spec["pay_phone"]

    admin.table("fixed_deposit_transactions").insert(tx).execute()

    # --- post to chart of accounts ---
    # Fixed deposits are a LIABILITY: SACCO owes the principal back.
    # Money in: Dr asset (cash / bank),  Cr 2010 Fixed deposits payable
    try:
        from routes.accounts.accounts import record_liability_movement
        record_liability_movement(
            org_id=org_id,
            direction="in",
            amount=principal,
            method=spec.get("method") or "cash",
            counterpart_account_code="2010",   # Fixed deposits payable
            category="other_adjustment",
            narration=f"Fixed deposit · {dep['fd_number']}",
            reference=spec.get("deposit_id") or None,
            created_by=spec.get("created_by"),
            meta={
                "source":     "fixed_deposit_create",
                "deposit_id": dep["id"],
                "member_id":  dep["member_id"],
            },
        )
    except Exception:
        log.exception("chart post for FD create failed (non-fatal)")

    log.info("fixed deposit created: %s principal=%s", dep["fd_number"], principal)

    return {
        "id":            dep["id"],
        "fd_number":     dep["fd_number"],
        "member_id":     dep["member_id"],
        "principal":     float(principal),
        "currency":      dep["currency"],
        "start_date":    dep["start_date"],
        "maturity_date": dep["maturity_date"],
    }

# ---------------------------------------------------------------------------
# TOP-UP
# ---------------------------------------------------------------------------
@fixed_bp.route("/api/<uuid:fd_uuid>/topup", methods=["POST"])
@role_required("admin")
def api_topup(fd_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}

    try:
        amount = _to_decimal(data.get("amount"))
        if amount <= 0:
            return _bad("Amount must be greater than zero", reason="bad_amount")
    except ValueError:
        return _bad("Invalid amount", reason="bad_amount")

    method = (data.get("method") or "cash").lower()
    if method not in ("cash", "online"):
        return _bad("method must be 'cash' or 'online'", reason="bad_method")

    provider  = None
    pay_phone = None
    if method == "online":
        provider  = (data.get("provider") or "").upper()
        pay_phone = (data.get("phone") or "").strip()
        if provider not in ("MTN", "AIRTEL"):
            return _bad("provider must be 'MTN' or 'AIRTEL'", reason="bad_provider")
        if not pay_phone:
            return _bad("phone required for online", reason="missing_phone")
        pay_phone = _normalize_phone(pay_phone)

    # --- load deposit ---
    try:
        dep_resp = (
            _admin().table("fixed_deposits")
            .select("*")
            .eq("org_id", org_id)
            .eq("id", str(fd_uuid))
            .limit(1)
            .execute()
        )
    except Exception:
        log.exception("topup fetch failed")
        return _bad("Could not load deposit", 500)

    if not dep_resp.data:
        return _bad("Deposit not found", 404, reason="not_found")
    dep = dep_resp.data[0]
    if dep["status"] != "active":
        return _bad(f"Cannot top up a deposit in status '{dep['status']}'",
                    reason="bad_state")

    # --- online: pawaPay first ---
    if method == "online":
        try:
            if provider == "MTN":
                pay_resp = deposit_mtn_ug(
                    phone_number=pay_phone,
                    amount=str(int(amount)),
                    client_reference_id=f"fdt-{secrets.token_hex(6)}",
                    customer_message=PAWAPAY_MESSAGE,
                )
            else:
                pay_resp = deposit_airtel_ug(
                    phone_number=pay_phone,
                    amount=str(int(amount)),
                    client_reference_id=f"fdt-{secrets.token_hex(6)}",
                    customer_message=PAWAPAY_MESSAGE,
                )
        except Exception as e:
            return _bad(f"Payment could not be initiated: {e}.", 502,
                        reason="pawapay_raised")

        deposit_id = pay_resp.get("depositId") or pay_resp.get("deposit_id")
        pay_status = (pay_resp.get("status") or "").upper()
        if not deposit_id or pay_status in ("REJECTED", "FAILED"):
            reason = (pay_resp.get("failureReason") or {}).get("failureMessage") \
                     or pay_status or "unknown"
            return _bad(f"Payment was rejected: {reason}", 400,
                        reason="payment_rejected")

        spec = {
            "action": "topup",
            "org_id": org_id,
            "deposit_id_fk": dep["id"],
            "amount": str(amount),
            "currency": dep["currency"],
            "provider": provider,
            "pay_phone": pay_phone,
            "deposit_id": deposit_id,
            "created_by": _user_id(),
            "staged_at": _now_iso(),
        }
        _save_pending(deposit_id, spec)
        return jsonify({
            "ok": True, "pending": True,
            "payment": {
                "method": "online", "status": "pending",
                "provider": provider, "deposit_id": deposit_id,
                "phone": pay_phone, "amount": float(amount),
                "currency": dep["currency"],
            },
        }), 202

    # --- cash: apply immediately ---
    try:
        result = _apply_topup(org_id, dep, amount)
    except Exception as e:
        log.exception("cash topup failed")
        return _bad(f"Could not top up: {e}", 500, reason="apply_failed")

    return jsonify({"ok": True, "deposit": result}), 201


def _apply_topup(org_id: str, dep: dict, amount: Decimal) -> dict:
    """
    Cash top-up: increase principal, write FD ledger row, post to chart:
        Dr  1000 Cash on hand  /  1010 Bank account
        Cr  2010 Fixed deposits payable
    """
    admin = _admin()
    new_principal = _q(_to_decimal(dep["principal"]) + amount)
    accrued = _interest_to_date(dep, _today())
    new_interest = _q(accrued)
    total = _q(new_principal + new_interest)

    admin.table("fixed_deposits").update({
        "principal":        float(new_principal),
        "accrued_interest": float(new_interest),
        "updated_at":       _now_iso(),
    }).eq("id", dep["id"]).execute()

    tx = {
        "org_id":     org_id,
        "deposit_id": dep["id"],
        "member_id":  dep["member_id"],
        "type":       "topup",
        "method":     "cash",
        "amount":     float(amount),
        "currency":   dep["currency"],
        "status":     "completed",
        "principal_after": float(new_principal),
        "interest_after":  float(new_interest),
        "total_after":     float(total),
        "narration":  "Fixed deposit top-up (cash)",
        "created_by": _user_id(),
    }
    admin.table("fixed_deposit_transactions").insert(tx).execute()

    # --- post to chart of accounts ---
    try:
        from routes.accounts.accounts import record_liability_movement
        record_liability_movement(
            org_id=org_id,
            direction="in",
            amount=amount,
            method="cash",
            counterpart_account_code="2010",
            category="other_adjustment",
            narration=f"FD top-up (cash) · {dep['fd_number']}",
            created_by=_user_id(),
            meta={
                "source":     "fixed_deposit_topup",
                "deposit_id": dep["id"],
                "member_id":  dep["member_id"],
                "method":     "cash",
            },
        )
    except Exception:
        log.exception("chart post for FD cash top-up failed (non-fatal)")

    return {
        "id": dep["id"],
        "fd_number": dep["fd_number"],
        "principal": float(new_principal),
        "accrued_interest": float(new_interest),
        "total_value": float(total),
    }


# ---------------------------------------------------------------------------
# WITHDRAW
# ---------------------------------------------------------------------------
@fixed_bp.route("/api/<uuid:fd_uuid>/withdraw", methods=["POST"])
@role_required("admin")
def api_withdraw(fd_uuid):
    """
    Body: { amount?, early? (bool), reason?, method? ('cash'|'online') }
    Full withdrawal by default. Partial allowed if amount < total.
    Early withdrawal requires {early: true} AND the deposit allows it;
    a penalty is deducted from accrued interest.

    Chart-of-accounts posts:
      Principal + net interest out:  Dr 2010 Fixed deposits payable
                                     Cr 1000 Cash on hand / 1010 Bank
      Penalty (income to SACCO):     Dr 1000 / 1010
                                     Cr 4020 Penalty income

    SMS is sent to the member on success (if enabled).
    """
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    early  = bool(data.get("early", False))
    method = (data.get("method") or "cash").lower()
    if method not in ("cash", "online"):
        method = "cash"

    try:
        dep_resp = (
            _admin().table("fixed_deposits")
            .select("*")
            .eq("org_id", org_id)
            .eq("id", str(fd_uuid))
            .limit(1)
            .execute()
        )
    except Exception:
        log.exception("withdraw fetch failed")
        return _bad("Could not load deposit", 500)

    if not dep_resp.data:
        return _bad("Deposit not found", 404, reason="not_found")
    dep = dep_resp.data[0]

    if dep["status"] not in ("active", "matured"):
        return _bad(f"Cannot withdraw from a deposit in status '{dep['status']}'",
                    reason="bad_state")

    matured = _matured(dep)
    if not matured:
        if not early:
            return _bad(
                f"Deposit matures on {dep['maturity_date']}. "
                f"Pass {{early: true}} to withdraw early.",
                reason="not_matured",
            )
        if not dep.get("early_withdrawal_allowed"):
            return _bad("Early withdrawal is not permitted for this deposit.",
                        reason="early_not_allowed")

    principal = _to_decimal(dep["principal"])
    accrued   = _interest_to_date(dep, _today())

    # Penalty (only if early)
    penalty = Decimal("0")
    if not matured:
        pen_pct = _to_decimal(dep.get("early_withdrawal_penalty_percent"))
        penalty = _q(accrued * pen_pct / Decimal("100"))
    net_interest = _q(max(Decimal("0"), accrued - penalty))
    total_available = _q(principal + net_interest)

    try:
        amount = _to_decimal(data.get("amount"))
        if amount <= 0:
            amount = total_available
    except (ValueError, TypeError):
        amount = total_available

    if amount > total_available:
        return _bad(
            f"Amount ({amount}) exceeds available balance ({total_available}).",
            reason="insufficient_balance",
        )

    is_full = amount == total_available
    new_principal = Decimal("0") if is_full else _q(principal - amount)
    if new_principal < 0:
        new_principal = Decimal("0")

    status_new = "withdrawn" if is_full else dep["status"]

    try:
        admin = _admin()
        # 1. Update the deposit
        upd = {
            "principal":        float(new_principal),
            "accrued_interest": float(net_interest if not is_full else 0),
            "paid_interest":    float(_to_decimal(dep.get("paid_interest")) + net_interest),
            "status":           status_new,
            "updated_at":       _now_iso(),
        }
        admin.table("fixed_deposits").update(upd).eq("id", dep["id"]).execute()

        # 2. FD ledger row(s)
        txs = []
        txs.append({
            "org_id":     org_id,
            "deposit_id": dep["id"],
            "member_id":  dep["member_id"],
            "type":       "withdrawal",
            "method":     method,
            "amount":     float(amount),
            "currency":   dep["currency"],
            "status":     "completed",
            "principal_after": float(new_principal),
            "interest_after":  float(net_interest if not is_full else 0),
            "total_after":     float(_q(new_principal + (net_interest if not is_full else 0))),
            "narration":  data.get("reason") or (
                "Fixed deposit withdrawal (matured)" if matured
                else "Fixed deposit withdrawal (early)"
            ),
            "created_by": _user_id(),
        })
        if penalty > 0:
            txs.append({
                "org_id":     org_id,
                "deposit_id": dep["id"],
                "member_id":  dep["member_id"],
                "type":       "penalty",
                "method":     "internal",
                "amount":     float(penalty),
                "currency":   dep["currency"],
                "status":     "completed",
                "narration":  "Early withdrawal penalty",
                "created_by": _user_id(),
            })
        admin.table("fixed_deposit_transactions").insert(txs).execute()

        # 3. Chart of accounts
        try:
            from routes.accounts.accounts import (
                record_liability_movement,
                record_asset_movement,
            )

            record_liability_movement(
                org_id=org_id,
                direction="out",
                amount=amount,
                method=method,
                counterpart_account_code="2010",
                category="other_adjustment",
                narration=f"FD withdrawal · {dep['fd_number']}",
                created_by=_user_id(),
                meta={
                    "source":       "fixed_deposit_withdrawal",
                    "deposit_id":   dep["id"],
                    "member_id":    dep["member_id"],
                    "early":        not matured,
                },
            )

            if penalty > 0:
                record_liability_movement(
                    org_id=org_id,
                    direction="out",
                    amount=penalty,
                    method="internal",
                    counterpart_account_code="2010",
                    category="other_adjustment",
                    narration=f"FD early penalty · {dep['fd_number']}",
                    created_by=_user_id(),
                    meta={
                        "source":     "fixed_deposit_penalty",
                        "deposit_id": dep["id"],
                    },
                )
                record_asset_movement(
                    org_id=org_id,
                    direction="in",
                    amount=penalty,
                    method="internal",
                    counterpart_account_code="4020",
                    category="membership_penalty",
                    narration=f"FD early penalty income · {dep['fd_number']}",
                    created_by=_user_id(),
                    meta={
                        "source":     "fixed_deposit_penalty",
                        "deposit_id": dep["id"],
                    },
                )
        except Exception:
            log.exception("chart posts for FD withdrawal failed (non-fatal)")

        # 4. SMS notification (fire-and-forget) — NEW
        try:
            member_row = (
                admin.table("members")
                .select("phone, first_name")
                .eq("id", dep["member_id"]).limit(1).execute()
            ).data or [{}]
            member_row = member_row[0]
            member_phone = (member_row.get("phone") or "").strip()
            member_first = (member_row.get("first_name") or "").strip() or "Member"

            if member_phone:
                from routes.sms.sms import send_sms, clamp_sms

                currency = dep["currency"]

                if is_full:
                    msg = (
                        f"Hi {member_first}, fixed deposit {dep['fd_number']} "
                        f"has been closed. {amount:.0f} {currency} paid out."
                    )
                else:
                    msg = (
                        f"Hi {member_first}, withdrawal of {amount:.0f} {currency} "
                        f"from FD {dep['fd_number']} processed. "
                        f"Remaining: {new_principal:.0f} {currency}."
                    )

                if penalty > 0:
                    msg = (
                        f"Hi {member_first}, early withdrawal of {amount:.0f} {currency} "
                        f"from FD {dep['fd_number']} processed "
                        f"(penalty {penalty:.0f} {currency})."
                    )

                send_sms(
                    org_id=org_id,
                    phone=member_phone,
                    message=clamp_sms(msg),
                    member_id=dep["member_id"],
                    triggered_by=_user_id(),
                    meta={
                        "kind":       "fixed_withdrawal",
                        "deposit_id": dep["id"],
                        "early":      not matured,
                        "amount":     float(amount),
                        "penalty":    float(penalty),
                    },
                )
        except Exception:
            log.exception("SMS for FD withdrawal failed (non-fatal)")

    except Exception as e:
        log.exception("withdraw apply failed")
        return _bad(f"Could not process withdrawal: {e}", 500, reason="apply_failed")

    return jsonify({
        "ok": True,
        "withdrawn": float(amount),
        "penalty":   float(penalty),
        "net_interest": float(net_interest),
        "new_principal": float(new_principal),
        "status": status_new,
    })
# ---------------------------------------------------------------------------
# ACCRUE INTEREST (manual trigger; can also be a scheduled job)
# ---------------------------------------------------------------------------
@fixed_bp.route("/api/<uuid:fd_uuid>/accrue", methods=["POST"])
@role_required("admin")
def api_accrue(fd_uuid):
    """
    Apply interest accrual up to today. Chart-of-accounts post:
        Dr  5170 Interest expense        (cost of funds)
        Cr  2010 Fixed deposits payable  (we now owe more)
    """
    org_id = _org_id()
    try:
        dep_resp = (
            _admin().table("fixed_deposits")
            .select("*")
            .eq("org_id", org_id)
            .eq("id", str(fd_uuid))
            .limit(1)
            .execute()
        )
    except Exception:
        log.exception("accrue fetch failed")
        return _bad("Could not load deposit", 500)

    if not dep_resp.data:
        return _bad("Deposit not found", 404, reason="not_found")
    dep = dep_resp.data[0]

    accrued = _interest_to_date(dep, _today())
    prev    = _to_decimal(dep.get("accrued_interest"))
    delta   = _q(accrued - prev)
    if delta <= 0:
        return jsonify({"ok": True, "accrued": float(accrued), "delta": 0.0})

    admin = _admin()
    admin.table("fixed_deposits").update({
        "accrued_interest": float(accrued),
        "updated_at":       _now_iso(),
    }).eq("id", dep["id"]).execute()

    admin.table("fixed_deposit_transactions").insert({
        "org_id":     org_id,
        "deposit_id": dep["id"],
        "member_id":  dep["member_id"],
        "type":       "interest_accrual",
        "method":     "internal",
        "amount":     float(delta),
        "currency":   dep["currency"],
        "status":     "completed",
        "interest_after": float(accrued),
        "total_after":    float(_q(_to_decimal(dep["principal"]) + accrued)),
        "narration":  "Interest accrual",
        "created_by": _user_id(),
    }).execute()

    # --- post to chart of accounts ---
    # Interest expense is a cost; it also increases the FD liability.
    # Dr 5170 Interest expense,  Cr 2010 Fixed deposits payable
    try:
        from routes.accounts.accounts import _apply_txn
        admin = _admin()

        exp_row = (
            admin.table("accounts")
            .select("id").eq("org_id", org_id).eq("code", "5170").limit(1).execute()
        ).data
        fdp_row = (
            admin.table("accounts")
            .select("id").eq("org_id", org_id).eq("code", "2010").limit(1).execute()
        ).data

        if exp_row and fdp_row:
            _apply_txn(org_id, {
                "type":              "expense",
                "category":          "interest_expense"
                                     if "interest_expense" in (
                                         "interest_expense",) else "other_operating",
                "amount":            str(delta),
                "debit_account_id":  exp_row[0]["id"],   # 5170 Interest expense
                "credit_account_id": fdp_row[0]["id"],   # 2010 FD payable
                "method":            "internal",
                "narration":         f"FD interest accrual · {dep['fd_number']}",
                "created_by":        _user_id(),
                "meta": {
                    "source":     "fixed_deposit_interest",
                    "deposit_id": dep["id"],
                },
            })
        else:
            log.warning("interest expense or FD payable account missing; skipping post")
    except Exception:
        log.exception("chart post for FD interest accrual failed (non-fatal)")

    return jsonify({"ok": True, "accrued": float(accrued), "delta": float(delta)})


# ---------------------------------------------------------------------------
# VERIFY (pawaPay)
# ---------------------------------------------------------------------------
@fixed_bp.route("/api/payment/<deposit_id>/verify", methods=["POST"])
@role_required("admin")
def api_verify(deposit_id):
    org_id = _org_id()
    staged = _load_pending(deposit_id)
    if staged is None:
        return _bad("No pending fixed-deposit payment for this reference", 404,
                    reason="not_found")

    try:
        result = verify_deposit(deposit_id)
    except Exception as e:
        log.exception("verify_deposit failed")
        return _bad(f"Verification error: {e}", 502, reason="provider_error")

    status = result.get("status")

    if result.get("completed"):
        try:
            if staged.get("action") == "topup":
                dep_resp = (
                    _admin().table("fixed_deposits")
                    .select("*")
                    .eq("org_id", org_id)
                    .eq("id", staged["deposit_id_fk"])
                    .limit(1)
                    .execute()
                )
                if not dep_resp.data:
                    return _bad("Deposit disappeared", 500, reason="db_error")
                _apply_topup_online(org_id, dep_resp.data[0], staged)
            else:
                _create_fixed_deposit(org_id, staged)
        except Exception as e:
            log.exception("apply after payment failed")
            return _bad(f"Payment succeeded but apply failed: {e}",
                        500, reason="apply_failed")
        _delete_pending(deposit_id)
        return jsonify({"ok": True, "status": "completed", "provider_status": status})

    if result.get("failed"):
        _delete_pending(deposit_id)
        return jsonify({"ok": True, "status": "failed",
                        "provider_status": status,
                        "reason": result.get("failure_reason")})

    return jsonify({"ok": True, "status": "pending", "provider_status": status})


def _apply_topup_online(org_id: str, dep: dict, spec: dict) -> None:
    admin = _admin()
    amount = _to_decimal(spec["amount"])
    new_principal = _q(_to_decimal(dep["principal"]) + amount)
    accrued = _q(_interest_to_date(dep, _today()))
    admin.table("fixed_deposits").update({
        "principal":        float(new_principal),
        "accrued_interest": float(accrued),
        "updated_at":       _now_iso(),
    }).eq("id", dep["id"]).execute()

    admin.table("fixed_deposit_transactions").insert({
        "org_id":     org_id,
        "deposit_id": dep["id"],
        "member_id":  dep["member_id"],
        "type":       "topup",
        "method":     "online",
        "amount":     float(amount),
        "currency":   dep["currency"],
        "status":     "completed",
        "principal_after": float(new_principal),
        "interest_after":  float(accrued),
        "total_after":     float(_q(new_principal + accrued)),
        "provider":   spec.get("provider"),
        "provider_ref": spec.get("deposit_id"),
        "phone":      spec.get("pay_phone"),
        "narration":  "Fixed deposit top-up (online)",
        "created_by": spec.get("created_by"),
    }).execute()


# ---------------------------------------------------------------------------
# STATEMENT (CSV)
# ---------------------------------------------------------------------------
@fixed_bp.route("/api/<uuid:fd_uuid>/statement", methods=["GET"])
@role_required("admin")
def api_statement(fd_uuid):
    org_id = _org_id()

    try:
        dep_resp = (
            _admin().table("fixed_deposits")
            .select("*, members!inner(member_id, first_name, last_name, phone, email)")
            .eq("org_id", org_id)
            .eq("id", str(fd_uuid))
            .limit(1)
            .execute()
        )
    except Exception:
        log.exception("statement fetch failed")
        return _bad("Could not load deposit", 500)
    if not dep_resp.data:
        return _bad("Deposit not found", 404)
    dep = dep_resp.data[0]
    m = dep["members"]

    tx_resp = (
        _admin().table("fixed_deposit_transactions")
        .select("created_at, type, method, amount, currency, status, narration, "
                "principal_after, interest_after, total_after, provider_ref")
        .eq("org_id", org_id)
        .eq("deposit_id", str(fd_uuid))
        .order("created_at", asc=True)
        .execute()
    )
    rows = tx_resp.data or []

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["SACCO Pro — Fixed Deposit Statement"])
    w.writerow(["Member", f"{m['first_name']} {m['last_name']}"])
    w.writerow(["Member ID", m["member_id"]])
    w.writerow(["Phone", m.get("phone") or ""])
    w.writerow(["FD Number", dep["fd_number"]])
    w.writerow(["Principal", dep["principal"]])
    w.writerow(["Accrued Interest", dep["accrued_interest"]])
    w.writerow(["Currency", dep["currency"]])
    w.writerow(["Start Date", dep["start_date"]])
    w.writerow(["Maturity Date", dep["maturity_date"]])
    w.writerow(["Status", dep["status"]])
    w.writerow(["Interest Rate %", dep["interest_rate_percent"]])
    w.writerow(["Interest Type", dep["interest_type"]])
    w.writerow(["Generated", _now_iso()])
    w.writerow([])
    w.writerow(["Date", "Type", "Method", "Amount", "Currency", "Status",
                "Narration", "Principal After", "Interest After",
                "Total After", "Reference"])
    for r in rows:
        w.writerow([
            r.get("created_at") or "",
            r.get("type") or "",
            r.get("method") or "",
            r.get("amount") or 0,
            r.get("currency") or "",
            r.get("status") or "",
            (r.get("narration") or "").replace("\n", " "),
            r.get("principal_after") if r.get("principal_after") is not None else "",
            r.get("interest_after")  if r.get("interest_after")  is not None else "",
            r.get("total_after")     if r.get("total_after")     is not None else "",
            r.get("provider_ref") or "",
        ])

    filename = f"fd_statement_{m['member_id']}_{dep['fd_number']}.csv"
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/csv; charset=utf-8",
        },
    )