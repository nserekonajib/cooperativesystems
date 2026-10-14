"""
Member Fixed Deposits blueprint (self-service).

A logged-in member can:
  - view every fixed deposit they hold (created by the SACCO)
  - see the interest schedule of each deposit
  - see the transaction history of each deposit
  - TOP UP a deposit ONLINE ONLY (mobile money via pawaPay)
  - download a CSV statement of a deposit

Endpoints:
    GET    /member/fixed                                       -> page
    GET    /member/fixed/api/summary                           -> KPI cards
    GET    /member/fixed/api/deposits                          -> paginated list
    GET    /member/fixed/api/deposits/<uuid:fd_uuid>           -> single FD + accounts
    GET    /member/fixed/api/deposits/<uuid:fd_uuid>/schedule  -> interest schedule
    GET    /member/fixed/api/deposits/<uuid:fd_uuid>/transactions -> ledger
    GET    /member/fixed/api/deposits/<uuid:fd_uuid>/statement -> CSV

    POST   /member/fixed/api/deposits/<uuid:fd_uuid>/topup     -> online-only top-up
    POST   /member/fixed/api/payment/<deposit_id>/verify       -> verify pawaPay deposit

Design:
- Withdrawals are NOT exposed to the member. Only the admin can pay out a
  fixed deposit — the member picks up the money at the counter once the FD
  has matured.
- Cash top-ups are REFUSED at the API boundary; only online (mobile money).
- All money math reuses the admin module's primitives.
"""

import os
import io
import csv
import json
import secrets
import logging
from pathlib import Path
from datetime import datetime, timezone, date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import (
    Blueprint, render_template, request, jsonify, session,
    redirect, url_for, Response, current_app,
)
from dotenv import load_dotenv
from supabase import create_client, Client

from routes.payments.pawapay import (
    deposit_mtn_ug,
    deposit_airtel_ug,
    verify_deposit,
)

# Reuse admin primitives — one math engine for both modules.
from routes.fixedAccounts.fixedAccounts import (
    _interest_to_date,
    _schedule,
    _matured,
    _parse_date,
    _to_decimal as _fd_to_decimal,   # (not always needed, but exported)
)

load_dotenv()

log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] member_fixed: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

member_fixed_bp = Blueprint(
    "member_fixed", __name__, url_prefix="/member/fixed",
)

PAWAPAY_MESSAGE = "SACCO fixed dep"   # ≤ 22 chars
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
    d = v if isinstance(v, Decimal) else Decimal(str(v if v is not None else 0))
    return d.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _normalize_phone(p: str) -> str:
    digits = "".join(c for c in (p or "") if c.isdigit())
    if digits.startswith("0"):
        digits = "256" + digits[1:]
    elif not digits.startswith("256") and len(digits) == 9:
        digits = "256" + digits
    return digits


def _member() -> dict | None:
    return session.get("member")


def _my_fd(org_id: str, member_id: str, fd_id: str) -> dict | None:
    """Fetch a single FD row, but only if it belongs to this member."""
    try:
        rows = (
            _admin().table("fixed_deposits")
            .select("*")
            .eq("org_id", org_id).eq("member_id", member_id)
            .eq("id", fd_id).limit(1).execute()
        ).data or []
    except Exception:
        log.exception("FD fetch failed")
        return None
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Pending online staging (isolated dir)
# ---------------------------------------------------------------------------
def _pending_path(deposit_id: str) -> Path:
    p = Path(current_app.instance_path) / "pending_member_fixed"
    p.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (deposit_id or "") if c.isalnum() or c in "-_")
    return p / f"{safe}.json"


def _save_pending(deposit_id: str, payload: dict) -> None:
    path = _pending_path(deposit_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    tmp.replace(path)
    log.info("pending member FD top-up saved: %s", path)


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
# Internal: apply a top-up for this member (share engine with admin module)
# ---------------------------------------------------------------------------
def _apply_topup_online_member(org_id: str, dep: dict, spec: dict) -> dict:
    """
    Same shape as the admin module's `_apply_topup_online`, but it also posts
    the chart-of-accounts entry so both paths stay in lockstep.
    """
    admin = _admin()
    amount = _to_decimal(spec["amount"])
    if amount <= 0:
        raise ValueError("amount must be > 0")

    new_principal = _q(_to_decimal(dep["principal"]) + amount)
    accrued = _q(_interest_to_date(dep, _today()))

    admin.table("fixed_deposits").update({
        "principal":        float(new_principal),
        "accrued_interest": float(accrued),
        "updated_at":       _now_iso(),
    }).eq("id", dep["id"]).execute()

    # FD ledger row
    admin.table("fixed_deposit_transactions").insert({
        "org_id":          org_id,
        "deposit_id":      dep["id"],
        "member_id":       dep["member_id"],
        "type":            "topup",
        "method":          "online",
        "amount":          float(amount),
        "currency":        dep["currency"],
        "status":          "completed",
        "principal_after": float(new_principal),
        "interest_after":  float(accrued),
        "total_after":     float(_q(new_principal + accrued)),
        "provider":        spec.get("provider"),
        "provider_ref":    spec.get("deposit_id"),
        "phone":           spec.get("pay_phone"),
        "narration":       "Fixed deposit top-up (online)",
        "created_by":      spec.get("created_by"),
    }).execute()

    # Chart of accounts: Dr 1010 Bank (online) / Cr 2010 FD payable
    try:
        from routes.accounts.accounts import record_liability_movement
        record_liability_movement(
            org_id=org_id,
            direction="in",
            amount=amount,
            method="online",
            counterpart_account_code="2010",       # Fixed deposits payable
            category="other_adjustment",
            narration=f"FD top-up (online) · {dep['fd_number']}",
            reference=spec.get("deposit_id") or None,
            created_by=spec.get("created_by"),
            meta={
                "source":     "fixed_deposit_topup",
                "deposit_id": dep["id"],
                "member_id":  dep["member_id"],
                "method":     "online",
            },
        )
    except Exception:
        log.exception("chart post for FD top-up failed (non-fatal)")

    return {
        "deposit_id":       dep["id"],
        "fd_number":        dep["fd_number"],
        "principal":        float(new_principal),
        "accrued_interest": float(accrued),
        "total_value":      float(_q(new_principal + accrued)),
    }


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
@member_fixed_bp.route("/", methods=["GET"])
def page():
    if not session.get("member"):
        return redirect(url_for("member_auth.login_page"))
    return render_template("memberFixed/memberFixed.html")


# ---------------------------------------------------------------------------
# API: summary (KPI cards)
# ---------------------------------------------------------------------------
@member_fixed_bp.route("/api/summary", methods=["GET"])
def api_summary():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        rows = (
            _admin().table("fixed_deposits")
            .select("principal, accrued_interest, status, maturity_date")
            .eq("org_id", m["org_id"]).eq("member_id", m["id"])
            .execute()
        ).data or []
    except Exception as e:
        log.exception("member FD summary failed")
        return _bad(f"Could not load summary: {e}", 500, reason="db_error")

    today = _today()
    total_principal = ZERO
    total_interest  = ZERO
    active = matured = 0
    maturing_30d = 0
    for r in rows:
        total_principal += _to_decimal(r.get("principal"))
        total_interest  += _to_decimal(r.get("accrued_interest"))
        md = _parse_date(r.get("maturity_date"))
        if r.get("status") == "active":
            if md and md <= today:
                matured += 1
            else:
                active += 1
                if md and (md - today).days <= 30:
                    maturing_30d += 1
        elif r.get("status") == "matured":
            matured += 1

    return jsonify({
        "ok": True,
        "summary": {
            "total_principal": float(_q(total_principal)),
            "total_interest":  float(_q(total_interest)),
            "total_value":     float(_q(total_principal + total_interest)),
            "active_count":    active,
            "matured_count":   matured,
            "maturing_30d":    maturing_30d,
            "total_count":     len(rows),
        },
    })


# ---------------------------------------------------------------------------
# API: list my fixed deposits
# ---------------------------------------------------------------------------
@member_fixed_bp.route("/api/deposits", methods=["GET"])
def api_deposits():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
        status   = (request.args.get("status") or "").strip()
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    q = (
        _admin().table("fixed_deposits")
        .select(
            "id, fd_number, principal, target_amount, accrued_interest, "
            "currency, start_date, maturity_date, period_months, status, "
            "interest_enabled, interest_rate_percent, interest_type",
            count="exact",
        )
        .eq("org_id", m["org_id"]).eq("member_id", m["id"])
        .order("created_at", desc=True)
        .range(start, end)
    )
    if status:
        q = q.eq("status", status)

    try:
        resp = q.execute()
    except Exception as e:
        log.exception("member FD list failed")
        return _bad(f"Could not load deposits: {e}", 500, reason="db_error")

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
        # Members cannot withdraw — always false here so the UI stays honest.
        d["can_withdraw"] = False

    return jsonify({
        "ok": True,
        "deposits": deposits,
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# API: single FD
# ---------------------------------------------------------------------------
@member_fixed_bp.route("/api/deposits/<uuid:fd_uuid>", methods=["GET"])
def api_detail(fd_uuid):
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    dep = _my_fd(m["org_id"], m["id"], str(fd_uuid))
    if not dep:
        return _bad("Deposit not found", 404, reason="not_found")

    principal = _to_decimal(dep.get("principal"))
    target = _to_decimal(dep.get("target_amount"))
    interest = _to_decimal(dep.get("accrued_interest"))
    total = principal + interest
    progress = 0
    if target > 0:
        progress = float(min(Decimal("100"), (total / target) * Decimal("100")))

    dep["progress_percent"] = round(progress, 2)
    dep["total_value"]      = float(_q(total))
    dep["matured"]          = _matured(dep)
    dep["interest_to_date"] = float(_interest_to_date(dep, _today()))
    dep["can_topup"]        = dep.get("status") == "active"
    dep["can_withdraw"]     = False   # never on the member portal

    return jsonify({"ok": True, "deposit": dep})


# ---------------------------------------------------------------------------
# API: schedule
# ---------------------------------------------------------------------------
@member_fixed_bp.route("/api/deposits/<uuid:fd_uuid>/schedule", methods=["GET"])
def api_schedule(fd_uuid):
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    dep = _my_fd(m["org_id"], m["id"], str(fd_uuid))
    if not dep:
        return _bad("Deposit not found", 404, reason="not_found")

    try:
        schedule = _schedule(dep)
    except Exception as e:
        log.exception("FD schedule failed")
        return _bad(f"Could not build schedule: {e}", 500, reason="db_error")

    return jsonify({"ok": True, "schedule": schedule})


# ---------------------------------------------------------------------------
# API: transactions
# ---------------------------------------------------------------------------
@member_fixed_bp.route("/api/deposits/<uuid:fd_uuid>/transactions", methods=["GET"])
def api_transactions(fd_uuid):
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    dep = _my_fd(m["org_id"], m["id"], str(fd_uuid))
    if not dep:
        return _bad("Deposit not found", 404, reason="not_found")

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
            .eq("org_id", m["org_id"]).eq("deposit_id", dep["id"])
            .order("created_at", desc=True)
            .range(start, end).execute()
        )
    except Exception as e:
        log.exception("member FD tx list failed")
        return _bad(f"Could not load transactions: {e}", 500, reason="db_error")

    return jsonify({
        "ok": True,
        "transactions": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# API: CSV statement
# ---------------------------------------------------------------------------
@member_fixed_bp.route("/api/deposits/<uuid:fd_uuid>/statement", methods=["GET"])
def api_statement(fd_uuid):
    m = _member()
    if not m:
        return redirect(url_for("member_auth.login_page"))

    dep = _my_fd(m["org_id"], m["id"], str(fd_uuid))
    if not dep:
        return _bad("Deposit not found", 404, reason="not_found")

    try:
        rows = (
            _admin().table("fixed_deposit_transactions")
            .select("created_at, type, method, amount, currency, status, "
                    "narration, principal_after, interest_after, total_after, "
                    "provider_ref")
            .eq("org_id", m["org_id"]).eq("deposit_id", dep["id"])
            .order("created_at", desc=False)
            .execute()
        ).data or []
    except Exception as e:
        log.exception("FD statement fetch failed")
        return _bad(f"Could not load transactions: {e}", 500, reason="db_error")

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["SACCO Pro — Fixed Deposit Statement"])
    w.writerow(["Member", m["full_name"]])
    w.writerow(["Member ID", m["member_id"]])
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


# ---------------------------------------------------------------------------
# API: TOP-UP (online only)
# ---------------------------------------------------------------------------
@member_fixed_bp.route("/api/deposits/<uuid:fd_uuid>/topup", methods=["POST"])
def api_topup(fd_uuid):
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    data = request.get_json(silent=True) or {}

    # ── METHOD: online only ────────────────────────────────────────────────
    method = (data.get("method") or "online").lower()
    if method == "cash":
        return _bad(
            "Cash top-ups are only accepted at the SACCO counter. "
            "Please use online (mobile money) to top up from your portal.",
            400, reason="cash_not_allowed",
        )
    if method != "online":
        return _bad("method must be 'online'", 400, reason="bad_method")

    try:
        amount = _to_decimal(data.get("amount"))
        if amount <= 0:
            return _bad("Amount must be > 0", 400, reason="bad_amount")
    except ValueError:
        return _bad("Invalid amount", 400, reason="bad_amount")

    provider  = (data.get("provider") or "").upper()
    pay_phone = (data.get("phone") or "").strip()
    if provider not in ("MTN", "AIRTEL"):
        return _bad("provider must be 'MTN' or 'AIRTEL'", 400,
                    reason="bad_provider")
    if not pay_phone:
        return _bad("phone is required", 400, reason="missing_phone")
    pay_phone = _normalize_phone(pay_phone)

    dep = _my_fd(m["org_id"], m["id"], str(fd_uuid))
    if not dep:
        return _bad("Deposit not found", 404, reason="not_found")
    if dep.get("status") != "active":
        return _bad(f"Cannot top up a deposit in status '{dep.get('status')}'",
                    400, reason="bad_state")

    # ── pawaPay preflight ──────────────────────────────────────────────────
    try:
        if provider == "MTN":
            pay_resp = deposit_mtn_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"mfd-{secrets.token_hex(6)}",
                customer_message=PAWAPAY_MESSAGE,
            )
        else:
            pay_resp = deposit_airtel_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"mfd-{secrets.token_hex(6)}",
                customer_message=PAWAPAY_MESSAGE,
            )
    except Exception as e:
        log.exception("pawaPay preflight failed")
        return _bad(f"Payment could not be initiated: {e}", 502,
                    reason="pawapay_raised")

    deposit_id = pay_resp.get("depositId") or pay_resp.get("deposit_id")
    pay_status = (pay_resp.get("status") or "").upper()
    if not deposit_id or pay_status in ("REJECTED", "FAILED"):
        reason = (pay_resp.get("failureReason") or {}).get("failureMessage") \
                 or pay_status or "unknown"
        return _bad(f"Payment was rejected: {reason}", 400,
                    reason="payment_rejected")

    # ── Stage ──────────────────────────────────────────────────────────────
    spec = {
        "action":         "member_fixed_topup",
        "org_id":         m["org_id"],
        "deposit_id_fk":  dep["id"],
        "member_id":      m["id"],
        "amount":         str(amount),
        "currency":       dep["currency"],
        "method":         "online",
        "provider":       provider,
        "pay_phone":      pay_phone,
        "deposit_id":     deposit_id,
        "created_by":     m["user_id"],
        "staged_at":      _now_iso(),
    }
    try:
        _save_pending(deposit_id, spec)
    except Exception as e:
        log.exception("staging failed")
        return _bad(f"Payment accepted but staging failed: {e}", 500,
                    reason="staging_failed")

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
            "currency":   dep["currency"],
        },
    }), 202


# ---------------------------------------------------------------------------
# API: VERIFY (pawaPay)
# ---------------------------------------------------------------------------
@member_fixed_bp.route("/api/payment/<deposit_id>/verify", methods=["POST"])
def api_verify(deposit_id):
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    staged = _load_pending(deposit_id)
    if not staged:
        return _bad("No pending top-up for this reference", 404,
                    reason="not_found")

    # Ownership check
    if staged.get("member_id") != m["id"]:
        return _bad("Not your payment", 403, reason="forbidden")

    try:
        result = verify_deposit(deposit_id)
    except Exception as e:
        log.exception("verify failed")
        return _bad(f"Verification error: {e}", 502, reason="provider_error")

    status = result.get("status")

    if result.get("completed"):
        # Re-fetch the deposit fresh (it may have changed since staging)
        dep = _my_fd(m["org_id"], m["id"], staged["deposit_id_fk"])
        if not dep:
            return _bad("Deposit disappeared", 500, reason="db_error")

        try:
            applied = _apply_topup_online_member(m["org_id"], dep, {
                "amount":     staged["amount"],
                "provider":   staged.get("provider"),
                "pay_phone":  staged.get("pay_phone"),
                "deposit_id": staged.get("deposit_id"),
                "created_by": staged.get("created_by"),
            })
        except Exception as e:
            log.exception("apply after verify failed")
            return _bad(f"Payment succeeded but apply failed: {e}",
                        500, reason="apply_failed")
        _delete_pending(deposit_id)
        return jsonify({"ok": True, "status": "completed",
                        "provider_status": status, "topup": applied})

    if result.get("failed"):
        _delete_pending(deposit_id)
        return jsonify({"ok": True, "status": "failed",
                        "provider_status": status,
                        "reason": result.get("failure_reason")})

    return jsonify({"ok": True, "status": "pending", "provider_status": status})