"""
Member Subscriptions blueprint (self-service).

A logged-in member can:
  - view their subscription for a given year (default: current year)
  - pay ONLINE ONLY (full or partial)
  - see their payment history
  - print a receipt for any payment

Endpoints:
    GET    /member/subscriptions                                -> page
    GET    /member/subscriptions/api/me                         -> subscription snapshot for year
    GET    /member/subscriptions/api/years                      -> years the member has subs for
    GET    /member/subscriptions/api/payments                   -> paginated payment history
    GET    /member/subscriptions/api/payments/<uuid:tx>/receipt -> printable HTML receipt

    POST   /member/subscriptions/api/pay                        -> online-only payment
    POST   /member/subscriptions/api/payment/<ref>/verify       -> verify pawaPay deposit

Design:
- Cash is REFUSED at the API boundary. Members must use online.
- All DB writes go through the existing
      routes.yearlySubscriptions.yearlySubscriptions._apply_subscription_payment
  so this module and the admin module share one source of truth:
    - member_transactions (ledger)
    - yearly_subscriptions (state)
    - chart of accounts (Dr 1000/1010 / Cr 4000)
- pawaPay flow uses the same stage-JSON / verify pattern.
"""

import os
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

# Reuse the exact same core routine the admin module uses.
from routes.yearlySubscriptions.yearlySubscriptions import (
    _apply_subscription_payment,
)

load_dotenv()

log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] member_subs: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

member_subscriptions_bp = Blueprint(
    "member_subscriptions", __name__, url_prefix="/member/subscriptions",
)

PAWAPAY_MESSAGE = "SACCO subs"       # ≤ 22 chars
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


def _member() -> dict | None:
    return session.get("member")


# ---------------------------------------------------------------------------
# Pending online staging (isolated from the admin staging dir)
# ---------------------------------------------------------------------------
def _pending_path(deposit_id: str) -> Path:
    p = Path(current_app.instance_path) / "pending_member_subscriptions"
    p.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (deposit_id or "") if c.isalnum() or c in "-_")
    return p / f"{safe}.json"


def _save_pending(deposit_id: str, payload: dict) -> None:
    path = _pending_path(deposit_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    tmp.replace(path)
    log.info("pending member subscription payment saved: %s", path)


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
@member_subscriptions_bp.route("/", methods=["GET"])
def page():
    if not session.get("member"):
        return redirect(url_for("member_auth.login_page"))
    return render_template("memberSubscriptions/memberSubscriptions.html")


# ---------------------------------------------------------------------------
# API: my subscription for a year (default current)
# ---------------------------------------------------------------------------
@member_subscriptions_bp.route("/api/me", methods=["GET"])
def api_me():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    year_arg = request.args.get("year")
    try:
        year = int(year_arg) if year_arg else datetime.now(timezone.utc).year
    except ValueError:
        year = datetime.now(timezone.utc).year

    admin = _admin()
    try:
        # subscription row for this member/year (may not exist)
        rows = (
            admin.table("yearly_subscriptions")
            .select(
                "id, subscription_year, amount_due, amount_paid, currency, "
                "status, source, due_date, last_payment_at, paid_at, notes"
            )
            .eq("org_id", m["org_id"]).eq("member_id", m["id"])
            .eq("subscription_year", year)
            .limit(1).execute()
        ).data or []

        sub = rows[0] if rows else None

        # settings for the year (default fee for display when there's no row)
        settings_rows = (
            admin.table("institution_settings")
            .select("annual_subscription_fee, subscription_due_month, currency")
            .eq("org_id", m["org_id"]).limit(1).execute()
        ).data or [{}]
        settings = settings_rows[0]

    except Exception as e:
        log.exception("member subscription fetch failed")
        return _bad(f"Could not load subscription: {e}", 500, reason="db_error")

    # build the response — if there's no row yet, show the org default as "not yet created"
    if sub:
        due = _to_decimal(sub.get("amount_due"))
        paid = _to_decimal(sub.get("amount_paid"))
        balance = _q(due - paid)
        payload = {
            "exists":        True,
            "id":            sub["id"],
            "year":          sub["subscription_year"],
            "amount_due":    float(_q(due)),
            "amount_paid":   float(_q(paid)),
            "balance":       float(balance),
            "currency":      sub.get("currency") or settings.get("currency") or "UGX",
            "status":        sub.get("status"),
            "source":        sub.get("source"),
            "due_date":      sub.get("due_date"),
            "last_payment_at": sub.get("last_payment_at"),
            "paid_at":       sub.get("paid_at"),
            "notes":         sub.get("notes"),
            "can_pay":       sub.get("status") not in ("paid", "waived", "cancelled") and balance > 0,
        }
    else:
        # no row yet — show the org default as guidance
        default_fee = _to_decimal(settings.get("annual_subscription_fee"))
        payload = {
            "exists":       False,
            "year":         year,
            "amount_due":   float(_q(default_fee)),
            "amount_paid":  0.0,
            "balance":      float(_q(default_fee)),
            "currency":     settings.get("currency") or "UGX",
            "status":       "not_created",
            "source":       None,
            "due_date":     None,
            "last_payment_at": None,
            "paid_at":      None,
            "notes":        "Your SACCO has not created this year's subscription yet.",
            "can_pay":      False,
        }

    return jsonify({
        "ok": True,
        "member": {
            "member_id": m["member_id"],
            "full_name": m["full_name"],
        },
        "subscription": payload,
    })


# ---------------------------------------------------------------------------
# API: years the member has subscriptions for
# ---------------------------------------------------------------------------
@member_subscriptions_bp.route("/api/years", methods=["GET"])
def api_years():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        rows = (
            _admin().table("yearly_subscriptions")
            .select("subscription_year")
            .eq("org_id", m["org_id"]).eq("member_id", m["id"])
            .execute()
        ).data or []
    except Exception as e:
        log.exception("member subscription years failed")
        return _bad(f"Could not load years: {e}", 500, reason="db_error")

    years = sorted({int(r["subscription_year"]) for r in rows}, reverse=True)
    current = datetime.now(timezone.utc).year
    if current not in years:
        years.insert(0, current)

    return jsonify({"ok": True, "years": years, "current": current})


# ---------------------------------------------------------------------------
# API: my payment history
# ---------------------------------------------------------------------------
@member_subscriptions_bp.route("/api/payments", methods=["GET"])
def api_payments():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
    except ValueError:
        return _bad("Invalid pagination")

    year_arg = request.args.get("year")
    try:
        year = int(year_arg) if year_arg else datetime.now(timezone.utc).year
    except ValueError:
        year = datetime.now(timezone.utc).year

    start = (page - 1) * per_page
    end   = start + per_page - 1

    # Payment ledger rows are member_transactions with type='subscription'
    # and the year filter uses created_at.
    try:
        q = (
            _admin().table("member_transactions")
            .select(
                "id, type, method, amount, currency, status, provider, "
                "provider_ref, phone, narration, created_at, "
                "subscription_id",
                count="exact",
            )
            .eq("org_id", m["org_id"]).eq("member_id", m["id"])
            .eq("type", "subscription")
            .gte("created_at", f"{year}-01-01")
            .lte("created_at", f"{year}-12-31T23:59:59")
            .order("created_at", desc=True)
            .range(start, end)
        )
        resp = q.execute()
    except Exception as e:
        log.exception("member subscription payments failed")
        return _bad(f"Could not load payments: {e}", 500, reason="db_error")

    return jsonify({
        "ok": True,
        "payments": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
        "year": year,
    })


# ---------------------------------------------------------------------------
# API: receipt for a payment (HTML, printable)
# ---------------------------------------------------------------------------
@member_subscriptions_bp.route("/api/payments/<uuid:tx_uuid>/receipt", methods=["GET"])
def api_receipt(tx_uuid):
    m = _member()
    if not m:
        return redirect(url_for("member_auth.login_page"))

    admin = _admin()
    try:
        rows = (
            admin.table("member_transactions")
            .select(
                "id, member_id, type, method, amount, currency, status, "
                "provider, provider_ref, phone, narration, created_at"
            )
            .eq("org_id", m["org_id"]).eq("id", str(tx_uuid)).limit(1).execute()
        ).data
    except Exception:
        log.exception("receipt fetch failed")
        return _bad("Could not load payment", 500)
    if not rows:
        return _bad("Payment not found", 404)
    tx = rows[0]

    # Ownership check
    if tx.get("member_id") != m["id"]:
        return _bad("Not your payment", 403, reason="forbidden")

    try:
        s = (
            admin.table("institution_settings")
            .select("display_name, address_line, city, country, "
                    "support_phone, support_email, currency")
            .eq("org_id", m["org_id"]).limit(1).execute()
        ).data or [{}]
    except Exception:
        s = [{}]
    settings = s[0]

    html = _render_subscription_receipt(tx, settings, m)
    return Response(html, mimetype="text/html")


def _render_subscription_receipt(tx: dict, settings: dict, member: dict) -> str:
    display = settings.get("display_name") or "SACCO Pro"
    addr = " ".join(filter(None, [
        settings.get("address_line") or "",
        settings.get("city") or "",
        settings.get("country") or "",
    ]))
    contact = " · ".join(filter(None, [
        settings.get("support_phone") or "",
        settings.get("support_email") or "",
    ]))

    amount  = tx.get("amount") or 0
    cur     = tx.get("currency") or settings.get("currency") or "UGX"
    method  = tx.get("method") or ""
    provider= tx.get("provider")
    method_line = method + (f" · {provider}" if provider else "")
    when    = tx.get("created_at") or ""
    receipt = tx.get("id") or ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Subscription Receipt {receipt[:8]}</title>
<style>
 body{{font-family:Inter,system-ui,sans-serif;padding:40px;color:#1E293B;background:#F4F6F9;}}
 .card{{max-width:640px;margin:auto;background:#fff;border:1px solid #E2E8F0;
        border-radius:16px;padding:32px;}}
 h1{{font-size:20px;margin:0 0 4px;}}
 h2{{font-size:15px;margin:0;}}
 h3{{font-size:13px;color:#64748B;text-transform:uppercase;letter-spacing:.06em;margin:24px 0 6px;}}
 .sub{{color:#64748B;font-size:12px;margin:0 0 24px;}}
 .row{{display:flex;justify-content:space-between;padding:8px 0;
       border-bottom:1px dashed #E2E8F0;font-size:14px;}}
 .row:last-child{{border-bottom:none;}}
 .label{{color:#64748B;}}
 .total{{font-weight:800;font-size:18px;color:#1A62FF;}}
 .tag{{display:inline-block;background:#E6F4EA;color:#065F46;padding:4px 10px;
       border-radius:999px;font-size:11px;font-weight:600;}}
 .foot{{text-align:center;color:#94A3B8;font-size:11px;margin-top:24px;}}
 @media print{{body{{padding:0;background:#fff;}} .card{{border:none;}}}}
</style>
</head>
<body>
<div class="card">
  <h1>{display}</h1>
  <p class="sub">{addr}<br>{contact}</p>

  <span class="tag">{tx.get('status','completed').upper()}</span>
  <h2 style="margin-top:16px;">Subscription Payment Receipt</h2>
  <p class="sub">Reference <strong>{receipt[:8].upper()}</strong> · {when}</p>

  <div class="row"><span class="label">Member</span><span>{member.get('full_name')}</span></div>
  <div class="row"><span class="label">Member ID</span><span>{member.get('member_id')}</span></div>
  <div class="row"><span class="label">Method</span><span>{method_line}</span></div>
  <div class="row"><span class="label">Narration</span><span>{tx.get('narration') or 'Subscription payment'}</span></div>
  <div class="row"><span class="label">Amount</span>
       <span class="total">{amount} {cur}</span></div>

  <p class="foot">Generated by {display} · Thank you for your payment.</p>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# API: PAY (online only)
# ---------------------------------------------------------------------------
@member_subscriptions_bp.route("/api/pay", methods=["POST"])
def api_pay():
    """
    Body: { amount, provider, phone, year?, narration? }
    Method is forced to 'online'. Cash is refused.
    """
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    data = request.get_json(silent=True) or {}

    # ── METHOD: online only ────────────────────────────────────────────────
    method = (data.get("method") or "online").lower()
    if method == "cash":
        return _bad(
            "Cash subscriptions are only accepted at the SACCO counter. "
            "Please use online (mobile money) to pay from your portal.",
            400, reason="cash_not_allowed",
        )
    if method != "online":
        return _bad("method must be 'online'", 400, reason="bad_method")

    # ── AMOUNT ─────────────────────────────────────────────────────────────
    try:
        amount = _to_decimal(data.get("amount"))
        if amount <= 0:
            return _bad("Amount must be > 0", 400, reason="bad_amount")
    except ValueError:
        return _bad("Invalid amount", 400, reason="bad_amount")

    # ── PROVIDER + PHONE ───────────────────────────────────────────────────
    provider  = (data.get("provider") or "").upper()
    pay_phone = (data.get("phone") or "").strip()
    if provider not in ("MTN", "AIRTEL"):
        return _bad("provider must be 'MTN' or 'AIRTEL'", 400,
                    reason="bad_provider")
    if not pay_phone:
        return _bad("phone is required", 400, reason="missing_phone")
    pay_phone = _normalize_phone(pay_phone)

    # ── YEAR (default current) ─────────────────────────────────────────────
    year_arg = data.get("year")
    try:
        year = int(year_arg) if year_arg else datetime.now(timezone.utc).year
    except (ValueError, TypeError):
        year = datetime.now(timezone.utc).year

    # ── Find the member's subscription for that year ───────────────────────
    admin = _admin()
    try:
        rows = (
            admin.table("yearly_subscriptions")
            .select(
                "id, subscription_year, amount_due, amount_paid, currency, "
                "status"
            )
            .eq("org_id", m["org_id"]).eq("member_id", m["id"])
            .eq("subscription_year", year)
            .limit(1).execute()
        ).data or []
    except Exception as e:
        log.exception("member subscription lookup failed")
        return _bad(f"Could not load subscription: {e}", 500, reason="db_error")

    if not rows:
        return _bad(
            f"No subscription found for {year}. "
            f"Please contact your SACCO to create it.",
            404, reason="not_created",
        )

    sub = rows[0]
    if sub["status"] in ("waived", "cancelled"):
        return _bad(f"Subscription is {sub['status']}", 400, reason="bad_state")
    if sub["status"] == "paid":
        return _bad("Subscription already fully paid", 400, reason="already_paid")

    due   = _to_decimal(sub["amount_due"])
    paid  = _to_decimal(sub["amount_paid"])
    balance = _q(due - paid)
    if balance <= 0:
        return _bad("Nothing outstanding", 400, reason="no_balance")

    if amount > balance:
        return _bad(
            f"Amount exceeds the outstanding balance ({balance} {sub['currency']}).",
            400, reason="overpay",
        )

    # ── pawaPay preflight ──────────────────────────────────────────────────
    try:
        if provider == "MTN":
            pay_resp = deposit_mtn_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"msub-{secrets.token_hex(6)}",
                customer_message=PAWAPAY_MESSAGE,
            )
        else:
            pay_resp = deposit_airtel_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"msub-{secrets.token_hex(6)}",
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
        "action":          "member_subscription_payment",
        "org_id":          m["org_id"],
        "subscription_id": sub["id"],
        "member_id":       m["id"],
        "year":            year,
        "amount":          str(amount),
        "currency":        sub["currency"],
        "method":          "online",
        "provider":        provider,
        "pay_phone":       pay_phone,
        "deposit_id":      deposit_id,
        "narration":       (data.get("narration") or "").strip() or None,
        "created_by":      m["user_id"],
        "staged_at":       _now_iso(),
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
            "currency":   sub["currency"],
            "year":       year,
        },
    }), 202


# ---------------------------------------------------------------------------
# API: VERIFY (pawaPay)
# ---------------------------------------------------------------------------
@member_subscriptions_bp.route("/api/payment/<deposit_id>/verify", methods=["POST"])
def api_verify(deposit_id):
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    staged = _load_pending(deposit_id)
    if not staged:
        return _bad("No pending payment for this reference", 404,
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
        try:
            applied = _apply_subscription_payment(
                staged["org_id"], staged["subscription_id"], {
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