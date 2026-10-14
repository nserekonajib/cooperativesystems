"""
Member Shares blueprint (self-service).

A logged-in member can:
  - view their shareholding
  - buy more shares ONLINE ONLY
  - see their purchase history and print receipts

Endpoints:
    GET    /member/shares                                   -> page
    GET    /member/shares/api/me                            -> shareholding snapshot
    GET    /member/shares/api/transactions                  -> paginated history
    GET    /member/shares/api/transactions/<uuid:tx>/receipt-> printable HTML receipt
    GET    /member/shares/api/settings                      -> share price, min units

    POST   /member/shares/api/buy                           -> online-only purchase
    POST   /member/shares/api/payment/<ref>/verify          -> verify pawaPay deposit

Design:
- Cash is REFUSED at the API boundary. Members must use online.
- All DB writes go through the existing
      routes.sharesManagement.sharesManagement._apply_share_purchase
  so this module and the admin module share one source of truth:
    - share_transactions (state)
    - members.shares_units / shares_value
    - member_transactions (general ledger mirror)
    - chart of accounts (Dr 1010 Bank / Cr 3000 Share capital)
- Online flow uses the same stage-JSON / verify pattern.
"""

import os
import json
import secrets
import logging
from pathlib import Path
from datetime import datetime, timezone
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

# Reuse the exact same core routines the admin module uses.
from routes.sharesManagement.sharesManagement import (
    _apply_share_purchase,
    _render_receipt,
)

load_dotenv()

log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] member_shares: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

member_shares_bp = Blueprint(
    "member_shares", __name__, url_prefix="/member/shares",
)

PAWAPAY_MESSAGE = "SACCO shares"     # ≤ 22 chars
MONEY_Q  = Decimal("0.01")
UNITS_Q  = Decimal("0.0001")
ZERO     = Decimal("0.00")

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


def _to_decimal(v, default="0") -> Decimal:
    if v is None or v == "":
        return Decimal(default)
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid number: {v}")


def _money(v) -> Decimal:
    d = v if isinstance(v, Decimal) else Decimal(str(v if v is not None else 0))
    return d.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _units(v) -> Decimal:
    d = v if isinstance(v, Decimal) else Decimal(str(v if v is not None else 0))
    return d.quantize(UNITS_Q, rounding=ROUND_HALF_UP)


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
# Pending online staging (parallel to the admin module's directory)
# ---------------------------------------------------------------------------
def _pending_path(deposit_id: str) -> Path:
    p = Path(current_app.instance_path) / "pending_member_shares"
    p.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (deposit_id or "") if c.isalnum() or c in "-_")
    return p / f"{safe}.json"


def _save_pending(deposit_id: str, payload: dict) -> None:
    path = _pending_path(deposit_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    tmp.replace(path)
    log.info("pending member share purchase saved: %s", path)


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
@member_shares_bp.route("/", methods=["GET"])
def page():
    if not session.get("member"):
        return redirect(url_for("member_auth.login_page"))
    return render_template("memberShares/myShares.html")


# ---------------------------------------------------------------------------
# API: settings
# ---------------------------------------------------------------------------
@member_shares_bp.route("/api/settings", methods=["GET"])
def api_settings():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        s = (
            _admin().table("institution_settings")
            .select("min_shares, min_share_price, currency")
            .eq("org_id", m["org_id"]).limit(1).execute()
        ).data or [{}]
    except Exception as e:
        log.exception("settings fetch failed")
        return _bad(f"Could not load settings: {e}", 500, reason="db_error")

    s = s[0]
    return jsonify({
        "ok": True,
        "settings": {
            "min_shares":  float(_units(s.get("min_shares") or 0)),
            "share_price": float(_money(s.get("min_share_price") or 0)),
            "currency":    s.get("currency") or "UGX",
        },
    })


# ---------------------------------------------------------------------------
# API: my shareholding
# ---------------------------------------------------------------------------
@member_shares_bp.route("/api/me", methods=["GET"])
def api_me():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        rows = (
            _admin().table("members")
            .select("member_id, first_name, last_name, shares_units, "
                    "shares_value, status")
            .eq("org_id", m["org_id"]).eq("id", m["id"]).limit(1).execute()
        ).data
        if not rows:
            return _bad("Member not found", 404, reason="not_found")
        member = rows[0]

        settings_rows = (
            _admin().table("institution_settings")
            .select("min_shares, min_share_price, currency")
            .eq("org_id", m["org_id"]).limit(1).execute()
        ).data or [{}]
        settings = settings_rows[0]

        # total purchases (units) — from share_transactions
        tx_resp = (
            _admin().table("share_transactions")
            .select("units, amount")
            .eq("org_id", m["org_id"]).eq("member_id", m["id"])
            .eq("status", "completed").eq("type", "purchase")
            .execute()
        ).data or []
        total_units_bought = sum((_to_decimal(t.get("units")) for t in tx_resp), ZERO)
        total_amount_paid  = sum((_to_decimal(t.get("amount")) for t in tx_resp), ZERO)

    except Exception as e:
        log.exception("member shares snapshot failed")
        return _bad(f"Could not load shareholding: {e}", 500, reason="db_error")

    price    = _money(settings.get("min_share_price") or 0)
    min_units= _units(settings.get("min_shares") or 0)
    units    = _units(member.get("shares_units") or 0)

    return jsonify({
        "ok": True,
        "member": {
            "member_id":  member.get("member_id"),
            "full_name":  f"{member.get('first_name','')} {member.get('last_name','')}".strip(),
            "status":     member.get("status"),
            "shares_units": float(units),
            "shares_value": float(_money(member.get("shares_value") or 0)),
        },
        "settings": {
            "share_price":  float(price),
            "min_shares":   float(min_units),
            "currency":     settings.get("currency") or "UGX",
        },
        "stats": {
            "units_bought":     float(_units(total_units_bought)),
            "amount_paid":      float(_money(total_amount_paid)),
            "meets_minimum":    bool(min_units == 0 or units >= min_units),
        },
    })


# ---------------------------------------------------------------------------
# API: transaction history
# ---------------------------------------------------------------------------
@member_shares_bp.route("/api/transactions", methods=["GET"])
def api_transactions():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    try:
        resp = (
            _admin().table("share_transactions")
            .select(
                "id, txn_number, type, units, unit_price, amount, currency, "
                "method, provider, status, paid_at, created_at, narration, "
                "units_before, units_after, value_after",
                count="exact",
            )
            .eq("org_id", m["org_id"]).eq("member_id", m["id"])
            .order("created_at", desc=True)
            .range(start, end)
            .execute()
        )
    except Exception as e:
        log.exception("member shares txns failed")
        return _bad(f"Could not load transactions: {e}", 500, reason="db_error")

    return jsonify({
        "ok": True,
        "transactions": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


@member_shares_bp.route("/api/transactions/<uuid:tx_uuid>/receipt", methods=["GET"])
def api_receipt(tx_uuid):
    m = _member()
    if not m:
        return redirect(url_for("member_auth.login_page"))

    try:
        tx_rows = (
            _admin().table("share_transactions")
            .select("*, members!inner(member_id, first_name, last_name, phone, email)")
            .eq("org_id", m["org_id"]).eq("id", str(tx_uuid)).limit(1).execute()
        ).data
    except Exception:
        log.exception("receipt fetch failed")
        return _bad("Could not load transaction", 500)
    if not tx_rows:
        return _bad("Transaction not found", 404)
    tx = tx_rows[0]

    # Security: the member must own this transaction.
    if tx.get("member_id") != m["id"]:
        return _bad("Not your transaction", 403, reason="forbidden")

    try:
        s = (
            _admin().table("institution_settings")
            .select("display_name, address_line, city, country, "
                    "support_phone, support_email")
            .eq("org_id", m["org_id"]).limit(1).execute()
        ).data
    except Exception:
        s = []

    return Response(_render_receipt(tx, (s or [{}])[0]), mimetype="text/html")


# ---------------------------------------------------------------------------
# API: BUY (online only)
# ---------------------------------------------------------------------------
@member_shares_bp.route("/api/buy", methods=["POST"])
def api_buy():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    data = request.get_json(silent=True) or {}

    # ── METHOD: online only ────────────────────────────────────────────────
    method = (data.get("method") or "online").lower()
    if method == "cash":
        return _bad(
            "Cash purchases are only accepted at the SACCO counter. "
            "Please use online (mobile money) to buy shares from your portal.",
            400, reason="cash_not_allowed",
        )
    if method != "online":
        return _bad("method must be 'online'", 400, reason="bad_method")

    # ── UNITS ──────────────────────────────────────────────────────────────
    try:
        units = _units(data.get("units"))
        if units <= 0:
            return _bad("units must be > 0", 400, reason="bad_units")
    except ValueError:
        return _bad("Invalid units", 400, reason="bad_units")

    # ── PROVIDER + PHONE ───────────────────────────────────────────────────
    provider  = (data.get("provider") or "").upper()
    pay_phone = (data.get("phone") or "").strip()
    if provider not in ("MTN", "AIRTEL"):
        return _bad("provider must be 'MTN' or 'AIRTEL'", 400,
                    reason="bad_provider")
    if not pay_phone:
        return _bad("phone is required", 400, reason="missing_phone")
    pay_phone = _normalize_phone(pay_phone)

    # ── PRICE from settings (member cannot override) ───────────────────────
    try:
        s = (
            _admin().table("institution_settings")
            .select("min_share_price, currency")
            .eq("org_id", m["org_id"]).limit(1).execute()
        ).data or [{}]
    except Exception:
        log.exception("settings fetch failed")
        return _bad("Could not read share price", 500, reason="db_error")

    unit_price = _money(s[0].get("min_share_price") or 0)
    currency   = s[0].get("currency") or "UGX"
    if unit_price <= 0:
        return _bad("Share price is not configured. Contact the SACCO.", 500,
                    reason="no_price")

    amount = _money(units * unit_price)

    # ── Confirm member is active ───────────────────────────────────────────
    try:
        mem_rows = (
            _admin().table("members")
            .select("id, status")
            .eq("org_id", m["org_id"]).eq("id", m["id"]).limit(1).execute()
        ).data
    except Exception:
        log.exception("member fetch failed")
        return _bad("Could not load member", 500, reason="db_error")
    if not mem_rows:
        return _bad("Member not found", 404, reason="not_found")
    if mem_rows[0]["status"] != "active":
        return _bad(f"Your membership is {mem_rows[0]['status']}", 400,
                    reason="bad_state")

    # ── pawaPay preflight ──────────────────────────────────────────────────
    try:
        if provider == "MTN":
            pay_resp = deposit_mtn_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"mshr-{secrets.token_hex(6)}",
                customer_message=PAWAPAY_MESSAGE,
            )
        else:
            pay_resp = deposit_airtel_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"mshr-{secrets.token_hex(6)}",
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
        "action":      "member_share_purchase",
        "org_id":      m["org_id"],
        "member_id":   m["id"],
        "units":       str(units),
        "unit_price":  str(unit_price),
        "amount":      str(amount),
        "currency":    currency,
        "method":      "online",
        "provider":    provider,
        "pay_phone":   pay_phone,
        "deposit_id":  deposit_id,
        "created_by":  m["user_id"],   # attribute the txn to the member's auth user
        "staged_at":   _now_iso(),
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
            "units":      float(units),
            "currency":   currency,
        },
    }), 202


# ---------------------------------------------------------------------------
# API: VERIFY (pawaPay)
# ---------------------------------------------------------------------------
@member_shares_bp.route("/api/payment/<deposit_id>/verify", methods=["POST"])
def api_verify(deposit_id):
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    staged = _load_pending(deposit_id)
    if not staged:
        return _bad("No pending purchase for this reference", 404,
                    reason="not_found")

    # Security: the staged purchase must belong to this member.
    if staged.get("member_id") != m["id"]:
        return _bad("Not your transaction", 403, reason="forbidden")

    try:
        result = verify_deposit(deposit_id)
    except Exception as e:
        log.exception("verify failed")
        return _bad(f"Verification error: {e}", 502, reason="provider_error")

    status = result.get("status")

    if result.get("completed"):
        try:
            applied = _apply_share_purchase(staged["org_id"], staged["member_id"], {
                "units":      staged["units"],
                "unit_price": staged["unit_price"],
                "method":     "online",
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
                        "provider_status": status, "purchase": applied})

    if result.get("failed"):
        _delete_pending(deposit_id)
        return jsonify({"ok": True, "status": "failed",
                        "provider_status": status,
                        "reason": result.get("failure_reason")})

    return jsonify({"ok": True, "status": "pending", "provider_status": status})