"""
Yearly Subscriptions blueprint.

Endpoints (admin-only JSON unless noted):
    GET    /subscriptions/                                    -> page
    GET    /subscriptions/api/stats                           -> KPI cards
    GET    /subscriptions/api/list                            -> paginated list
    GET    /subscriptions/api/<uuid:sub_uuid>                 -> detail (with payments)
    GET    /subscriptions/api/years                           -> available years
    GET    /subscriptions/api/members/search                  -> lookup for create modal

    POST   /subscriptions/api/create                          -> create for a member/year
    POST   /subscriptions/api/<uuid:sub_uuid>/pay             -> cash: apply now;
                                                                 online: pawaPay + stage
    POST   /subscriptions/api/payment/<ref>/verify            -> verify online payment
    POST   /subscriptions/api/<uuid:sub_uuid>/waive           -> mark waived
    POST   /subscriptions/api/<uuid:sub_uuid>/cancel          -> mark cancelled
    POST   /subscriptions/api/bulk-create                     -> create current-year rows
                                                                 for members who don't have one

    GET    /subscriptions/api/export                          -> CSV

Design notes:
- The ledger (member_transactions) remains the source of truth for money.
  yearly_subscriptions is the state layer: one row per (member, year) that
  tracks how much is owed, how much has been paid, and whether the year is
  settled.
- When a member is registered we already insert a member_transactions row
  for the current-year subscription. That can be linked by setting
  subscription_id when we create the yearly_subscriptions row.
- Online payments follow the pattern used in savings/fixed/repayments:
  call pawaPay, stage spec to instance/pending_subscriptions/<id>.json,
  apply only on /verify COMPLETED.
- No N+1: list = single joined query; detail = 2 queries.
"""

import os
import io
import csv
import json
import secrets
import logging
from pathlib import Path
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import (
    Blueprint, render_template, request, jsonify, session,
    redirect, url_for, Response, current_app,
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
        "%(asctime)s [%(levelname)s] subscriptions: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

yearly_subscriptions_bp = Blueprint(
    "yearly_subscriptions", __name__, url_prefix="/subscriptions",
)

PAWAPAY_MESSAGE = "SACCO subs"
MONEY_Q = Decimal("0.01")
ZERO    = Decimal("0.00")

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
# Helpers
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


def _q(v) -> Decimal:
    if isinstance(v, Decimal):
        d = v
    elif v is None:
        d = ZERO
    else:
        d = Decimal(str(v))
    return d.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _normalize_phone(p: str) -> str:
    digits = "".join(c for c in (p or "") if c.isdigit())
    if digits.startswith("0"):
        digits = "256" + digits[1:]
    elif not digits.startswith("256") and len(digits) == 9:
        digits = "256" + digits
    return digits

# ---------------------------------------------------------------------------
# Chart of accounts bridge (lazy import so this module never hard-fails
# if accounts aren't set up yet)
# ---------------------------------------------------------------------------
def _post_to_accounts(org_id: str, spec: dict) -> None:
    """
    Post a subscription payment to the chart of accounts.
    Never raises — the subscription ledger is the source of truth.
    """
    try:
        from routes.accounts.accounts import record_asset_movement
        method_key = "cash" if spec["method"] == "cash" else "online"
        record_asset_movement(
            org_id=org_id,
            direction="in",
            amount=spec["amount"],
            method=method_key,
            counterpart_account_code="4000",   # Membership subscription income
            category="other_income",
            narration=spec.get("narration")
                      or f"Subscription {spec.get('year','')}".strip(),
            reference=spec.get("reference"),
            created_by=spec.get("created_by"),
            meta={
                "source":          "subscription",
                "subscription_id": spec.get("subscription_id"),
            },
        )
    except Exception:
        log.exception("chart-of-accounts post for subscription failed (non-fatal)")
        
        
def _due_date_for_year(org_id: str, year: int) -> date | None:
    """Read subscription_due_month from institution_settings and build a date."""
    try:
        s = (
            _admin().table("institution_settings")
            .select("subscription_due_month")
            .eq("org_id", org_id).limit(1).execute()
        ).data
    except Exception:
        log.exception("due date fetch failed")
        return None
    if not s:
        return None
    month = int(s[0].get("subscription_due_month") or 1)
    month = max(1, min(12, month))
    return date(year, month, 1)


# ---------------------------------------------------------------------------
# Pending (online) staging
# ---------------------------------------------------------------------------
def _pending_path(deposit_id: str) -> Path:
    p = Path(current_app.instance_path) / "pending_subscriptions"
    p.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (deposit_id or "") if c.isalnum() or c in "-_")
    return p / f"{safe}.json"


def _save_pending(deposit_id: str, payload: dict) -> None:
    path = _pending_path(deposit_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    tmp.replace(path)
    log.info("pending subscription payment saved: %s", path)


def _load_pending(deposit_id: str) -> dict | None:
    path = _pending_path(deposit_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("failed to read pending file %s", path)
        return None


def _delete_pending(deposit_id: str) -> None:
    path = _pending_path(deposit_id)
    try:
        if path.exists():
            path.unlink()
    except Exception:
        log.exception("failed to delete pending file %s", path)


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
@yearly_subscriptions_bp.route("/", methods=["GET"])
def page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    if session["user"].get("role") != "admin":
        return redirect(url_for("auth.dashboard"))
    return render_template("yearlySubscriptions/yearlySubscriptions.html")


# ---------------------------------------------------------------------------
# STATS
# ---------------------------------------------------------------------------
@yearly_subscriptions_bp.route("/api/stats", methods=["GET"])
@role_required("admin")
def api_stats():
    org_id = _org_id()

    year_arg = request.args.get("year")
    try:
        year = int(year_arg) if year_arg else datetime.now(timezone.utc).year
    except ValueError:
        year = datetime.now(timezone.utc).year

    try:
        rows = (
            _admin().table("yearly_subscriptions")
            .select("amount_due, amount_paid, status, currency")
            .eq("org_id", org_id)
            .eq("subscription_year", year)
            .execute()
        ).data or []

        total_due    = ZERO
        total_paid   = ZERO
        pending      = 0
        partial      = 0
        paid         = 0
        waived       = 0
        overdue      = 0

        for r in rows:
            total_due  += _to_decimal(r.get("amount_due"))
            total_paid += _to_decimal(r.get("amount_paid"))
            s = r.get("status")
            if s == "pending":    pending  += 1
            elif s == "partial":  partial  += 1
            elif s == "paid":     paid     += 1
            elif s == "waived":   waived   += 1
            elif s == "overdue":  overdue  += 1

    except Exception as e:
        log.exception("subscription stats failed: %s", e)
        return _bad("Could not load stats", 500)

    collection_rate = 0.0
    if total_due > 0:
        collection_rate = float((total_paid / total_due * Decimal("100")).quantize(Decimal("0.01")))

    return jsonify({
        "ok": True,
        "year": year,
        "stats": {
            "total":             len(rows),
            "pending":           pending,
            "partial":           partial,
            "paid":              paid,
            "waived":            waived,
            "overdue":           overdue,
            "total_due":         float(_q(total_due)),
            "total_paid":        float(_q(total_paid)),
            "total_outstanding": float(_q(total_due - total_paid)),
            "collection_rate":   collection_rate,
        },
    })


# ---------------------------------------------------------------------------
# YEARS (distinct)
# ---------------------------------------------------------------------------
@yearly_subscriptions_bp.route("/api/years", methods=["GET"])
@role_required("admin")
def api_years():
    org_id = _org_id()
    try:
        rows = (
            _admin().table("yearly_subscriptions")
            .select("subscription_year")
            .eq("org_id", org_id)
            .execute()
        ).data or []
    except Exception as e:
        log.exception("years fetch failed: %s", e)
        return _bad("Could not load years", 500)

    years = sorted({int(r["subscription_year"]) for r in rows}, reverse=True)
    current = datetime.now(timezone.utc).year
    if current not in years:
        years.insert(0, current)

    return jsonify({"ok": True, "years": years, "current": current})


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------
@yearly_subscriptions_bp.route("/api/list", methods=["GET"])
@role_required("admin")
def api_list():
    org_id = _org_id()
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
        search   = (request.args.get("q") or "").strip()
        status   = (request.args.get("status") or "").strip()
        year_arg = request.args.get("year")
        year     = int(year_arg) if year_arg else datetime.now(timezone.utc).year
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    q = (
        _admin().table("yearly_subscriptions")
        .select(
            "id, subscription_year, amount_due, amount_paid, currency, "
            "status, source, due_date, last_payment_at, paid_at, "
            "created_at, "
            "members!inner(id, member_id, first_name, last_name, phone, email)",
            count="exact",
        )
        .eq("org_id", org_id)
        .eq("subscription_year", year)
        .order("created_at", desc=True)
        .range(start, end)
    )
    if status:
        q = q.eq("status", status)
    if search:
        like = f"%{search}%"
        q = q.or_(
            f"members.first_name.ilike.{like},"
            f"members.last_name.ilike.{like},"
            f"members.member_id.ilike.{like},"
            f"members.phone.ilike.{like}"
        )

    try:
        resp = q.execute()
    except Exception as e:
        log.exception("subscriptions list failed: %s", e)
        return _bad("Could not load subscriptions", 500)

    return jsonify({
        "ok": True,
        "subscriptions": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
        "year": year,
    })


# ---------------------------------------------------------------------------
# DETAIL (with payment history)
# ---------------------------------------------------------------------------
@yearly_subscriptions_bp.route("/api/<uuid:sub_uuid>", methods=["GET"])
@role_required("admin")
def api_detail(sub_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("yearly_subscriptions")
            .select(
                "*, members!inner(id, member_id, first_name, last_name, "
                "phone, email, status)"
            )
            .eq("org_id", org_id).eq("id", str(sub_uuid)).limit(1).execute()
        )
    except Exception as e:
        log.exception("subscription detail failed: %s", e)
        return _bad("Could not load subscription", 500)
    if not resp.data:
        return _bad("Subscription not found", 404)

    sub = resp.data[0]

    try:
        tx = (
            _admin().table("member_transactions")
            .select(
                "id, type, method, amount, currency, status, provider, "
                "provider_ref, phone, narration, created_at"
            )
            .eq("org_id", org_id)
            .eq("member_id", sub["member_id"])
            .or_(
                f"subscription_id.eq.{sub['id']},"
                f"and(type.eq.subscription,created_at.gte."
                f"{sub['subscription_year']}-01-01,"
                f"created_at.lte.{sub['subscription_year']}-12-31)"
            )
            .order("created_at", desc=True)
            .execute()
        ).data or []
    except Exception:
        log.exception("subscription tx fetch failed (non-fatal)")
        tx = []

    return jsonify({"ok": True, "subscription": sub, "payments": tx})


# ---------------------------------------------------------------------------
# MEMBER SEARCH (for create modal)
# ---------------------------------------------------------------------------
@yearly_subscriptions_bp.route("/api/members/search", methods=["GET"])
@role_required("admin")
def api_members_search():
    org_id = _org_id()
    q_str = (request.args.get("q") or "").strip()
    year_arg = request.args.get("year")
    try:
        year = int(year_arg) if year_arg else datetime.now(timezone.utc).year
    except ValueError:
        year = datetime.now(timezone.utc).year

    try:
        q = (
            _admin().table("members")
            .select("id, member_id, first_name, last_name, phone, email, status")
            .eq("org_id", org_id)
            .eq("status", "active")
            .limit(50)
        )
        if q_str:
            like = f"%{q_str}%"
            q = q.or_(
                f"first_name.ilike.{like},"
                f"last_name.ilike.{like},"
                f"member_id.ilike.{like},"
                f"phone.ilike.{like}"
            )
        members = q.execute().data or []
    except Exception as e:
        log.exception("member search failed: %s", e)
        return _bad("Could not search members", 500)

    # exclude members who already have a subscription for the year
    try:
        existing = (
            _admin().table("yearly_subscriptions")
            .select("member_id")
            .eq("org_id", org_id).eq("subscription_year", year)
            .execute()
        ).data or []
        taken = {r["member_id"] for r in existing}
        members = [m for m in members if m["id"] not in taken]
    except Exception:
        log.exception("existing check failed (non-fatal)")

    return jsonify({"ok": True, "members": members})


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------
@yearly_subscriptions_bp.route("/api/create", methods=["POST"])
@role_required("admin")
def api_create():
    """
    Body:
    {
      member_id, year?, amount_due?, due_date?, source? ('admin'|'member'|'system'),
      notes?
    }
    If amount_due omitted, uses institution_settings.annual_subscription_fee.
    """
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}

    member_id = (data.get("member_id") or "").strip()
    if not member_id:
        return _bad("member_id is required", reason="missing_member")

    try:
        year = int(data.get("year") or datetime.now(timezone.utc).year)
    except (ValueError, TypeError):
        return _bad("Invalid year", reason="bad_year")
    if year < 2000 or year > 2100:
        return _bad("year out of range", reason="bad_year")

    source = (data.get("source") or "admin").lower()
    if source not in ("admin", "member", "system"):
        return _bad("source must be admin, member, or system", reason="bad_source")

    # amount
    amount_due_raw = data.get("amount_due")
    if amount_due_raw not in (None, ""):
        try:
            amount_due = _to_decimal(amount_due_raw)
            if amount_due < 0:
                return _bad("amount_due cannot be negative", reason="bad_amount")
        except ValueError:
            return _bad("Invalid amount_due", reason="bad_amount")
    else:
        try:
            s = (
                _admin().table("institution_settings")
                .select("annual_subscription_fee, currency, "
                        "subscription_due_month")
                .eq("org_id", org_id).limit(1).execute()
            ).data or [{}]
            amount_due = _to_decimal(s[0].get("annual_subscription_fee"))
        except Exception:
            log.exception("settings fetch failed")
            return _bad("Could not read subscription fee", 500, reason="db_error")

    # confirm member exists
        # confirm member exists
    try:
        m = (
            _admin().table("members")
            .select("id")
            .eq("org_id", org_id).eq("id", member_id).limit(1).execute()
        ).data
    except Exception:
        log.exception("member fetch failed")
        return _bad("Could not load member", 500, reason="db_error")
    if not m:
        return _bad("Member not found", 404, reason="not_found")

    # currency comes from institution_settings
    try:
        s_cur = (
            _admin().table("institution_settings")
            .select("currency")
            .eq("org_id", org_id).limit(1).execute()
        ).data
    except Exception:
        log.exception("settings currency fetch failed (non-fatal)")
        s_cur = []
    org_currency = (s_cur or [{}])[0].get("currency") or "UGX"

    # due date
    due_date = data.get("due_date") or None
    if not due_date:
        dd = _due_date_for_year(org_id, year)
        due_date = dd.isoformat() if dd else None

    row = {
        "org_id":            org_id,
        "member_id":         member_id,
        "subscription_year": year,
        "amount_due":        float(_q(amount_due)),
        "amount_paid":       0.0,
        "currency":          org_currency,
        "status":            "paid" if amount_due == 0 else "pending",
        "source":            source,
        "due_date":          due_date,
        "notes":             (data.get("notes") or "").strip() or None,
        "created_by":        _user_id(),
    }

    try:
        resp = _admin().table("yearly_subscriptions").insert(row).execute()
    except Exception as e:
        log.exception("subscription insert failed: %s", e)
        if "23505" in str(e):
            return _bad("This member already has a subscription for that year",
                        409, reason="duplicate")
        return _bad(f"Could not create subscription: {e}", 500, reason="db_error")

    return jsonify({"ok": True, "subscription": resp.data[0]}), 201


# ---------------------------------------------------------------------------
# BULK CREATE — everyone who doesn't have one for the year
# ---------------------------------------------------------------------------
@yearly_subscriptions_bp.route("/api/bulk-create", methods=["POST"])
@role_required("admin")
def api_bulk_create():
    """
    Body: { year? }
    Creates pending rows for all active members missing a row for `year`.
    """
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    try:
        year = int(data.get("year") or datetime.now(timezone.utc).year)
    except (ValueError, TypeError):
        return _bad("Invalid year", reason="bad_year")

    try:
        s = (
            _admin().table("institution_settings")
            .select("annual_subscription_fee, subscription_due_month, currency")
            .eq("org_id", org_id).limit(1).execute()
        ).data or [{}]
        fee = _to_decimal(s[0].get("annual_subscription_fee"))
        due_month = int(s[0].get("subscription_due_month") or 1)
        due_month = max(1, min(12, due_month))
    except Exception:
        log.exception("settings fetch failed")
        return _bad("Could not read fee", 500, reason="db_error")

    due = date(year, due_month, 1)

    try:
            
        members = (
            _admin().table("members")
            .select("id")               # only what we need
            .eq("org_id", org_id).eq("status", "active")
            .execute()
        ).data or []

        # currency from settings (bulk path)
        s_cur = (
            _admin().table("institution_settings")
            .select("currency")
            .eq("org_id", org_id).limit(1).execute()
        ).data
        org_currency = (s_cur or [{}])[0].get("currency") or "UGX"

        existing = (
            _admin().table("yearly_subscriptions")
            .select("member_id")
            .eq("org_id", org_id).eq("subscription_year", year)
            .execute()
        ).data or []
        taken = {r["member_id"] for r in existing}
    except Exception:
        log.exception("bulk fetch failed")
        return _bad("Could not load members", 500, reason="db_error")

    rows = []
    for m in members:
        if m["id"] in taken:
            continue
        rows.append({
            "org_id":            org_id,
            "member_id":         m["id"],
            "subscription_year": year,
            "amount_due":        float(_q(fee)),
            "amount_paid":       0.0,
            "currency":          org_currency,
            "status":            "paid" if fee == 0 else "pending",
            "source":            "system",
            "due_date":          due.isoformat(),
            "created_by":        _user_id(),
        })

    if not rows:
        return jsonify({"ok": True, "created": 0,
                        "message": "All active members already have this year's subscription."})

    # Supabase python client doesn't batch across huge lists; chunk safely
    created = 0
    chunk_size = 500
    try:
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i:i + chunk_size]
            _admin().table("yearly_subscriptions").insert(chunk).execute()
            created += len(chunk)
    except Exception as e:
        log.exception("bulk insert failed")
        return _bad(f"Could not bulk create: {e}", 500, reason="db_error")

    return jsonify({"ok": True, "created": created})


# ---------------------------------------------------------------------------
# PAY
# ---------------------------------------------------------------------------
def _apply_subscription_payment(org_id: str, sub_id: str, spec: dict) -> dict:
    """
    spec: amount, method, provider?, pay_phone?, deposit_id?, narration?,
          created_by?

    Applies payment against a subscription row:
      1. Validates amount and current balance
      2. Writes a member_transactions ledger row (source of truth)
      3. Updates yearly_subscriptions.amount_paid / status / timestamps
      4. Posts the cash side to the chart of accounts
         (1000 cash / 1010 online, counterpart 4000 subscription income)
    """
    admin = _admin()

    # ------------------------------------------------------------------
    # 1. Validate
    # ------------------------------------------------------------------
    amount = _to_decimal(spec["amount"])
    if amount <= 0:
        raise ValueError("amount must be > 0")

    sub_resp = (
        admin.table("yearly_subscriptions")
        .select("*")
        .eq("org_id", org_id).eq("id", sub_id).limit(1).execute()
    )
    if not sub_resp.data:
        raise RuntimeError("Subscription not found")
    sub = sub_resp.data[0]

    if sub["status"] in ("waived", "cancelled"):
        raise RuntimeError(f"Subscription is {sub['status']}")
    if sub["status"] == "paid":
        raise RuntimeError("Subscription already fully paid")

    due     = _to_decimal(sub["amount_due"])
    paid    = _to_decimal(sub["amount_paid"])
    balance = _q(due - paid)
    if balance <= 0:
        raise RuntimeError("No outstanding balance")

    # Cap the applied amount at the outstanding balance; anything extra is
    # not accepted here (subscriptions don't support overpayment).
    applied     = _q(min(amount, balance))
    new_paid    = _q(paid + applied)
    fully_paid  = new_paid >= due

    # ------------------------------------------------------------------
    # 2. Write the ledger row (member_transactions)
    # ------------------------------------------------------------------
    tx = {
        "org_id":          org_id,
        "member_id":       sub["member_id"],
        "type":            "subscription",
        "method":          spec["method"],
        "amount":          float(applied),
        "currency":        sub["currency"],
        "status":          "completed",
        "narration":       spec.get("narration")
                           or f"Subscription {sub['subscription_year']}",
        "created_by":      spec.get("created_by") or _user_id(),
        "subscription_id": sub["id"],
    }
    if spec.get("provider"):
        tx["provider"] = spec["provider"]
    if spec.get("deposit_id"):
        tx["provider_ref"] = spec["deposit_id"]
    if spec.get("pay_phone"):
        tx["phone"] = spec["pay_phone"]

    tx_resp = admin.table("member_transactions").insert(tx).execute()
    if not tx_resp.data:
        raise RuntimeError("ledger insert failed")
    tx_row = tx_resp.data[0]

    # ------------------------------------------------------------------
    # 3. Update the subscription state row
    # ------------------------------------------------------------------
    upd = {
        "amount_paid":     float(new_paid),
        "last_payment_at": _now_iso(),
        "updated_at":      _now_iso(),
    }
    if fully_paid:
        upd["status"]  = "paid"
        upd["paid_at"] = _now_iso()
    else:
        upd["status"] = "partial"

    admin.table("yearly_subscriptions").update(upd).eq("id", sub["id"]).execute()

    # ------------------------------------------------------------------
    # 4. Post to chart of accounts (non-fatal if it fails)
    # ------------------------------------------------------------------
    _post_to_accounts(org_id, {
        "amount":          float(applied),
        "method":          spec["method"],           # 'cash' | 'online'
        "narration":       spec.get("narration")
                           or f"Subscription {sub['subscription_year']}",
        "reference":       tx_row["id"],             # links ledger to chart
        "created_by":      spec.get("created_by") or _user_id(),
        "subscription_id": sub["id"],
        "year":            sub["subscription_year"],
    })

    # ------------------------------------------------------------------
    # 5. Return a response the caller / UI can show
    # ------------------------------------------------------------------
    return {
        "subscription_id": sub["id"],
        "year":            sub["subscription_year"],
        "amount_applied":  float(applied),
        "amount_paid":     float(new_paid),
        "amount_due":      float(due),
        "balance":         float(_q(due - new_paid)),
        "status":          upd["status"],
        "fully_paid":      fully_paid,
        "receipt_ref":     tx_row["id"],
    }

@yearly_subscriptions_bp.route("/api/<uuid:sub_uuid>/pay", methods=["POST"])
@role_required("admin")
def api_pay(sub_uuid):
    """
    Body: { amount, method ('cash'|'online'), provider?, phone?, narration? }
    """
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}

    try:
        amount = _to_decimal(data.get("amount"))
        if amount <= 0:
            return _bad("Amount must be > 0", reason="bad_amount")
    except ValueError:
        return _bad("Invalid amount", reason="bad_amount")

    method = (data.get("method") or "cash").lower()
    if method not in ("cash", "online"):
        return _bad("method must be 'cash' or 'online'", reason="bad_method")

    provider = None
    pay_phone = None
    if method == "online":
        provider  = (data.get("provider") or "").upper()
        pay_phone = (data.get("phone") or "").strip()
        if provider not in ("MTN", "AIRTEL"):
            return _bad("provider must be 'MTN' or 'AIRTEL'", reason="bad_provider")
        if not pay_phone:
            return _bad("phone required for online", reason="missing_phone")
        pay_phone = _normalize_phone(pay_phone)

    try:
        sub = (
            _admin().table("yearly_subscriptions")
            .select("id, member_id, subscription_year, amount_due, "
                    "amount_paid, currency, status")
            .eq("org_id", org_id).eq("id", str(sub_uuid)).limit(1).execute()
        ).data
    except Exception:
        log.exception("subscription fetch failed")
        return _bad("Could not load subscription", 500, reason="db_error")
    if not sub:
        return _bad("Subscription not found", 404, reason="not_found")
    sub = sub[0]

    if sub["status"] in ("waived", "cancelled"):
        return _bad(f"Subscription is {sub['status']}", 400, reason="bad_state")

    # ---- CASH ----
    if method == "cash":
        try:
            result = _apply_subscription_payment(org_id, str(sub_uuid), {
                "amount":     str(amount),
                "method":     "cash",
                "narration":  (data.get("narration") or "").strip() or None,
                "created_by": _user_id(),
            })
        except Exception as e:
            log.exception("cash subscription payment failed")
            return _bad(f"Could not apply payment: {e}", 500, reason="apply_failed")
        return jsonify({"ok": True, "payment": result}), 201

    # ---- ONLINE ----
    try:
        if provider == "MTN":
            pay_resp = deposit_mtn_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"sub-{secrets.token_hex(6)}",
                customer_message=PAWAPAY_MESSAGE,
            )
        else:
            pay_resp = deposit_airtel_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"sub-{secrets.token_hex(6)}",
                customer_message=PAWAPAY_MESSAGE,
            )
    except Exception as e:
        log.exception("pawaPay preflight failed")
        return _bad(f"Payment could not be initiated: {e}.", 502, reason="pawapay_raised")

    deposit_id = pay_resp.get("depositId") or pay_resp.get("deposit_id")
    pay_status = (pay_resp.get("status") or "").upper()
    if not deposit_id or pay_status in ("REJECTED", "FAILED"):
        reason = (pay_resp.get("failureReason") or {}).get("failureMessage") \
                 or pay_status or "unknown"
        return _bad(f"Payment was rejected: {reason}", 400, reason="payment_rejected")

    spec = {
        "action":          "subscription_payment",
        "org_id":          org_id,
        "subscription_id": str(sub_uuid),
        "amount":          str(amount),
        "currency":        sub["currency"],
        "provider":        provider,
        "pay_phone":       pay_phone,
        "deposit_id":      deposit_id,
        "narration":       (data.get("narration") or "").strip() or None,
        "created_by":      _user_id(),
        "staged_at":       _now_iso(),
    }
    _save_pending(deposit_id, spec)

    return jsonify({
        "ok": True,
        "pending": True,
        "payment": {
            "method":     "online",
            "status":     "pending",
            "provider":   provider,
            "deposit_id": deposit_id,
            "phone":      pay_phone,
            "amount":     float(amount),
            "currency":   sub["currency"],
        },
    }), 202


# ---------------------------------------------------------------------------
# VERIFY (pawaPay)
# ---------------------------------------------------------------------------
@yearly_subscriptions_bp.route("/api/payment/<deposit_id>/verify", methods=["POST"])
@role_required("admin")
def api_verify(deposit_id):
    org_id = _org_id()
    staged = _load_pending(deposit_id)
    if not staged:
        return _bad("No pending subscription payment for this reference",
                    404, reason="not_found")

    try:
        result = verify_deposit(deposit_id)
    except Exception as e:
        log.exception("verify failed")
        return _bad(f"Verification error: {e}", 502, reason="provider_error")

    status = result.get("status")

    if result.get("completed"):
        try:
            applied = _apply_subscription_payment(
                org_id, staged["subscription_id"], {
                    "amount":     staged["amount"],
                    "method":     "online",
                    "provider":   staged.get("provider"),
                    "pay_phone":  staged.get("pay_phone"),
                    "deposit_id": staged.get("deposit_id"),
                    "narration":  staged.get("narration"),
                    "created_by": staged.get("created_by"),
                })
        except Exception as e:
            log.exception("apply after verify failed")
            return _bad(f"Payment succeeded but apply failed: {e}",
                        500, reason="apply_failed")
        _delete_pending(deposit_id)
        return jsonify({"ok": True, "status": "completed",
                        "provider_status": status, "payment": applied})

    if result.get("failed"):
        _delete_pending(deposit_id)
        return jsonify({"ok": True, "status": "failed",
                        "provider_status": status,
                        "reason": result.get("failure_reason")})

    return jsonify({"ok": True, "status": "pending", "provider_status": status})


# ---------------------------------------------------------------------------
# WAIVE / CANCEL
# ---------------------------------------------------------------------------
@yearly_subscriptions_bp.route("/api/<uuid:sub_uuid>/waive", methods=["POST"])
@role_required("admin")
def api_waive(sub_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip() or None
    if not reason:
        return _bad("reason is required", reason="missing_reason")

    try:
        resp = (
            _admin().table("yearly_subscriptions")
            .update({
                "status":        "waived",
                "waived_by":     _user_id(),
                "waived_at":     _now_iso(),
                "waived_reason": reason,
                "updated_at":    _now_iso(),
            })
            .eq("org_id", org_id).eq("id", str(sub_uuid)).execute()
        )
    except Exception as e:
        log.exception("waive failed")
        return _bad(f"Could not waive: {e}", 500, reason="db_error")
    if not resp.data:
        return _bad("Subscription not found", 404, reason="not_found")
    return jsonify({"ok": True, "subscription": resp.data[0]})


@yearly_subscriptions_bp.route("/api/<uuid:sub_uuid>/cancel", methods=["POST"])
@role_required("admin")
def api_cancel(sub_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip() or None

    try:
        resp = (
            _admin().table("yearly_subscriptions")
            .update({
                "status":     "cancelled",
                "notes":      reason,
                "updated_at": _now_iso(),
            })
            .eq("org_id", org_id).eq("id", str(sub_uuid)).execute()
        )
    except Exception as e:
        log.exception("cancel failed")
        return _bad(f"Could not cancel: {e}", 500, reason="db_error")
    if not resp.data:
        return _bad("Subscription not found", 404, reason="not_found")
    return jsonify({"ok": True, "subscription": resp.data[0]})


# ---------------------------------------------------------------------------
# EXPORT CSV
# ---------------------------------------------------------------------------
@yearly_subscriptions_bp.route("/api/export", methods=["GET"])
@role_required("admin")
def api_export():
    org_id = _org_id()
    try:
        year_arg = request.args.get("year")
        year = int(year_arg) if year_arg else datetime.now(timezone.utc).year
    except ValueError:
        year = datetime.now(timezone.utc).year

    try:
        rows = (
            _admin().table("yearly_subscriptions")
            .select(
                "subscription_year, amount_due, amount_paid, currency, "
                "status, source, due_date, last_payment_at, paid_at, "
                "waived_reason, notes, created_at, "
                "members!inner(member_id, first_name, last_name, phone, email)"
            )
            .eq("org_id", org_id).eq("subscription_year", year)
            .order("created_at", desc=True)
            .execute()
        ).data or []
    except Exception as e:
        log.exception("export fetch failed")
        return _bad("Could not export", 500)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Year", "Member ID", "Member", "Phone",
                "Amount Due", "Amount Paid", "Balance", "Currency",
                "Status", "Source", "Due Date", "Last Payment",
                "Paid At", "Waived Reason", "Notes"])
    for r in rows:
        m = r.get("members") or {}
        due = _to_decimal(r.get("amount_due"))
        paid = _to_decimal(r.get("amount_paid"))
        w.writerow([
            r.get("subscription_year"), m.get("member_id"),
            f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            m.get("phone"),
            float(due), float(paid), float(_q(due - paid)),
            r.get("currency"), r.get("status"), r.get("source"),
            r.get("due_date"), r.get("last_payment_at"),
            r.get("paid_at"), r.get("waived_reason"), r.get("notes"),
        ])

    filename = f"subscriptions_{year}.csv"
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/csv; charset=utf-8",
        },
    )