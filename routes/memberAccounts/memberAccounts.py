"""
Member Accounts blueprint (self-service).

A logged-in member can:
  - view their savings account (number, balance, currency)
  - deposit ONLINE ONLY (mobile money via pawaPay)
  - request a withdrawal (creates a withdrawal_requests row, source='member')
  - see their own withdrawal requests
  - see the transaction history of the account
  - download a CSV statement
  - print a receipt for any deposit

Endpoints:
    GET    /member/accounts                                   -> page
    GET    /member/accounts/api/me                            -> savings account snapshot
    GET    /member/accounts/api/transactions                  -> paginated ledger
    GET    /member/accounts/api/withdrawals                   -> paginated own withdrawal requests
    GET    /member/accounts/api/statement                     -> CSV download

    POST   /member/accounts/api/deposit                       -> online-only deposit
    POST   /member/accounts/api/withdrawals                   -> create withdrawal request
    POST   /member/accounts/api/payment/<deposit_id>/verify   -> verify pawaPay deposit

Design:
- Cash deposits are REFUSED at the API boundary.
- Withdrawal requests are ALWAYS source='member', status='pending'.
- The admin's savings module already lists & approves requests; this
  module only creates them and lets the member track them.
- All money math reuses routes.savingsAccounts.savingsAccounts.
"""

import os
import io
import csv
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

# Reuse the admin savings primitives so both modules share one engine.
from routes.savingsAccounts.savingsAccounts import (
    _apply_deposit,
    _fetch_savings_account,
)

load_dotenv()

log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] member_accounts: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

member_accounts_bp = Blueprint(
    "member_accounts", __name__, url_prefix="/member/accounts",
)

PAWAPAY_MESSAGE = "SACCO savings"
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


def _my_savings_account() -> dict | None:
    """Return the logged-in member's savings account row."""
    m = _member()
    if not m:
        return None
    try:
        rows = (
            _admin().table("member_accounts")
            .select("id, account_number, balance, currency, member_id, "
                    "account_type, status, opened_at")
            .eq("org_id", m["org_id"])
            .eq("member_id", m["id"])
            .eq("account_type", "savings")
            .limit(1).execute()
        ).data or []
    except Exception:
        log.exception("savings account lookup failed")
        return None
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Pending online staging (isolated)
# ---------------------------------------------------------------------------
def _pending_path(deposit_id: str) -> Path:
    p = Path(current_app.instance_path) / "pending_member_deposits"
    p.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (deposit_id or "") if c.isalnum() or c in "-_")
    return p / f"{safe}.json"


def _save_pending(deposit_id: str, payload: dict) -> None:
    path = _pending_path(deposit_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    tmp.replace(path)
    log.info("pending member deposit saved: %s", path)


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
@member_accounts_bp.route("/", methods=["GET"])
def page():
    if not session.get("member"):
        return redirect(url_for("member_auth.login_page"))
    return render_template("memberAccounts/memberAccounts.html")


# ---------------------------------------------------------------------------
# API: snapshot of my savings account
# ---------------------------------------------------------------------------
@member_accounts_bp.route("/api/me", methods=["GET"])
def api_me():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    acc = _my_savings_account()
    if not acc:
        return jsonify({
            "ok": True,
            "member": {"member_id": m["member_id"], "full_name": m["full_name"]},
            "account": None,
            "stats":   {"deposits_total": 0.0, "withdrawals_total": 0.0,
                        "deposits_30d": 0.0, "pending_withdrawals": 0},
        })

    admin = _admin()
    try:
        # totals from member_transactions
        tx = (
            admin.table("member_transactions")
            .select("type, amount, created_at")
            .eq("org_id", m["org_id"]).eq("account_id", acc["id"])
            .eq("status", "completed")
            .execute()
        ).data or []

        deposits_total = ZERO
        withdrawals_total = ZERO
        deposits_30d = ZERO
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

        for t in tx:
            amt = _to_decimal(t.get("amount"))
            if t.get("type") == "savings_deposit":
                deposits_total += amt
                if (t.get("created_at") or "") >= cutoff:
                    deposits_30d += amt
            elif t.get("type") == "savings_withdrawal":
                withdrawals_total += amt

        # pending withdrawals
        pending = (
            admin.table("withdrawal_requests")
            .select("id", count="exact")
            .eq("org_id", m["org_id"]).eq("account_id", acc["id"])
            .in_("status", ["pending", "approved"])
            .limit(1).execute()
        ).count or 0

    except Exception as e:
        log.exception("member account snapshot failed")
        return _bad(f"Could not load account: {e}", 500, reason="db_error")

    return jsonify({
        "ok": True,
        "member": {"member_id": m["member_id"], "full_name": m["full_name"]},
        "account": {
            "id":             acc["id"],
            "account_number": acc["account_number"],
            "balance":        float(_to_decimal(acc.get("balance"))),
            "currency":       acc.get("currency") or "UGX",
            "status":         acc.get("status"),
            "opened_at":      acc.get("opened_at"),
        },
        "stats": {
            "deposits_total":      float(_q(deposits_total)),
            "withdrawals_total":   float(_q(withdrawals_total)),
            "deposits_30d":        float(_q(deposits_30d)),
            "pending_withdrawals": pending,
        },
    })


# ---------------------------------------------------------------------------
# API: transaction history
# ---------------------------------------------------------------------------
@member_accounts_bp.route("/api/transactions", methods=["GET"])
def api_transactions():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    acc = _my_savings_account()
    if not acc:
        return jsonify({"ok": True, "transactions": [], "total": 0,
                        "page": 1, "per_page": 20})

    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(200, max(10, int(request.args.get("per_page", 50))))
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    try:
        resp = (
            _admin().table("member_transactions")
            .select(
                "id, type, method, amount, currency, status, provider, "
                "provider_ref, phone, narration, balance_after, created_at",
                count="exact",
            )
            .eq("org_id", m["org_id"]).eq("account_id", acc["id"])
            .order("created_at", desc=True)
            .range(start, end).execute()
        )
    except Exception as e:
        log.exception("member tx list failed")
        return _bad(f"Could not load transactions: {e}", 500, reason="db_error")

    return jsonify({
        "ok": True,
        "transactions": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# API: my withdrawal requests
# ---------------------------------------------------------------------------
@member_accounts_bp.route("/api/withdrawals", methods=["GET"])
def api_withdrawals():
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
            _admin().table("withdrawal_requests")
            .select(
                "id, amount, currency, reason, status, source, method, "
                "provider, created_at, reviewed_at, paid_at, review_notes",
                count="exact",
            )
            .eq("org_id", m["org_id"]).eq("member_id", m["id"])
            .order("created_at", desc=True)
            .range(start, end).execute()
        )
    except Exception as e:
        log.exception("member withdrawals list failed")
        return _bad(f"Could not load requests: {e}", 500, reason="db_error")

    return jsonify({
        "ok": True,
        "requests": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# API: create a withdrawal request (member-initiated)
# ---------------------------------------------------------------------------
@member_accounts_bp.route("/api/withdrawals", methods=["POST"])
def api_create_withdrawal():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    data = request.get_json(silent=True) or {}

    acc = _my_savings_account()
    if not acc:
        return _bad("You don't have a savings account yet", 404,
                    reason="no_account")

    try:
        amount = _to_decimal(data.get("amount"))
        if amount <= 0:
            return _bad("Amount must be greater than zero", reason="bad_amount")
    except ValueError:
        return _bad("Invalid amount", reason="bad_amount")

    reason_text = (data.get("reason") or "").strip() or None

    current_balance = _to_decimal(acc.get("balance"))
    if amount > current_balance:
        return _bad(
            f"Insufficient balance. Available: {current_balance}",
            reason="insufficient_balance",
        )

    # Don't allow stacking more than the balance in pending requests
    try:
        pending_rows = (
            _admin().table("withdrawal_requests")
            .select("amount")
            .eq("org_id", m["org_id"]).eq("account_id", acc["id"])
            .in_("status", ["pending", "approved"])
            .execute()
        ).data or []
        pending_total = sum((_to_decimal(r.get("amount")) for r in pending_rows), ZERO)
        if (pending_total + amount) > current_balance:
            return _bad(
                f"Existing pending withdrawals ({pending_total}) plus this "
                f"request ({amount}) exceed available balance "
                f"({current_balance}).",
                reason="insufficient_balance_pending",
            )
    except Exception:
        log.exception("pending check failed (non-fatal)")

    row = {
        "org_id":     m["org_id"],
        "member_id":  m["id"],
        "account_id": acc["id"],
        "amount":     float(_q(amount)),
        "currency":   acc.get("currency") or "UGX",
        "reason":     reason_text,
        "status":     "pending",
        "source":     "member",           # ← distinguishes from admin-created
        "method":     "cash",             # members always pick up cash at the counter
        "provider":   None,
        "created_by": m["user_id"],
    }

    try:
        resp = (
            _admin().table("withdrawal_requests").insert(row).execute()
        )
    except Exception as e:
        log.exception("member withdrawal create failed")
        return _bad(f"Could not create request: {e}", 500, reason="db_error")

    log.info("member %s requested withdrawal %s", m["member_id"], amount)
    return jsonify({
        "ok": True,
        "message": "Withdrawal request submitted. Awaiting approval.",
        "request": resp.data[0],
    }), 201


# ---------------------------------------------------------------------------
# API: deposit (online only)
# ---------------------------------------------------------------------------
@member_accounts_bp.route("/api/deposit", methods=["POST"])
def api_deposit():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    acc = _my_savings_account()
    if not acc:
        return _bad("You don't have a savings account yet", 404,
                    reason="no_account")

    data = request.get_json(silent=True) or {}

    # ── METHOD: online only ────────────────────────────────────────────────
    method = (data.get("method") or "online").lower()
    if method == "cash":
        return _bad(
            "Cash deposits are only accepted at the SACCO counter. "
            "Please use online (mobile money) to deposit from your portal.",
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

    # ── pawaPay preflight ──────────────────────────────────────────────────
    try:
        if provider == "MTN":
            pay_resp = deposit_mtn_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"mdep-{secrets.token_hex(6)}",
                customer_message=PAWAPAY_MESSAGE,
            )
        else:
            pay_resp = deposit_airtel_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"mdep-{secrets.token_hex(6)}",
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

    # Stage
    spec = {
        "action":     "member_savings_deposit",
        "org_id":     m["org_id"],
        "account_id": acc["id"],
        "member_id":  m["id"],
        "amount":     str(amount),
        "currency":   acc.get("currency") or "UGX",
        "method":     "online",
        "provider":   provider,
        "pay_phone":  pay_phone,
        "deposit_id": deposit_id,
        "narration":  (data.get("narration") or "").strip() or "Online savings deposit",
        "created_by": m["user_id"],
        "staged_at":  _now_iso(),
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
            "currency":   spec["currency"],
        },
    }), 202


# ---------------------------------------------------------------------------
# API: verify pawaPay deposit
# ---------------------------------------------------------------------------
@member_accounts_bp.route("/api/payment/<deposit_id>/verify", methods=["POST"])
def api_verify(deposit_id):
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    staged = _load_pending(deposit_id)
    if not staged:
        return _bad("No pending deposit for this reference", 404,
                    reason="not_found")

    # Ownership check
    if staged.get("member_id") != m["id"]:
        return _bad("Not your deposit", 403, reason="forbidden")

    try:
        result = verify_deposit(deposit_id)
    except Exception as e:
        log.exception("verify failed")
        return _bad(f"Verification error: {e}", 502, reason="provider_error")

    status = result.get("status")

    if result.get("completed"):
        try:
            applied = _apply_deposit(staged["org_id"], {
                "account_id": staged["account_id"],
                "amount":     staged["amount"],
                "currency":   staged.get("currency"),
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
                        "provider_status": status, "deposit": applied})

    if result.get("failed"):
        _delete_pending(deposit_id)
        return jsonify({"ok": True, "status": "failed",
                        "provider_status": status,
                        "reason": result.get("failure_reason")})

    return jsonify({"ok": True, "status": "pending", "provider_status": status})


# ---------------------------------------------------------------------------
# API: CSV statement
# ---------------------------------------------------------------------------
@member_accounts_bp.route("/api/statement", methods=["GET"])
def api_statement():
    m = _member()
    if not m:
        return redirect(url_for("member_auth.login_page"))

    acc = _my_savings_account()
    if not acc:
        return _bad("No savings account", 404, reason="not_found")

    date_from = request.args.get("from")
    date_to   = request.args.get("to")

    try:
        q = (
            _admin().table("member_transactions")
            .select("created_at, type, method, amount, currency, status, "
                    "narration, balance_after, provider_ref")
            .eq("org_id", m["org_id"]).eq("account_id", acc["id"])
            .order("created_at", desc=False)
        )
        if date_from: q = q.gte("created_at", date_from)
        if date_to:   q = q.lte("created_at", date_to + "T23:59:59")
        rows = q.execute().data or []
    except Exception as e:
        log.exception("member statement fetch failed")
        return _bad(f"Could not load transactions: {e}", 500, reason="db_error")

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["SACCO Pro — Savings Statement"])
    w.writerow(["Member", m["full_name"]])
    w.writerow(["Member ID", m["member_id"]])
    w.writerow(["Account Number", acc["account_number"]])
    w.writerow(["Currency", acc.get("currency") or "UGX"])
    w.writerow(["Current Balance", acc["balance"]])
    w.writerow(["Generated", _now_iso()])
    w.writerow([])
    w.writerow(["Date", "Type", "Method", "Amount", "Currency",
                "Status", "Narration", "Balance After", "Reference"])

    for r in rows:
        w.writerow([
            r.get("created_at") or "",
            r.get("type") or "",
            r.get("method") or "",
            r.get("amount") or 0,
            r.get("currency") or "",
            r.get("status") or "",
            (r.get("narration") or "").replace("\n", " "),
            r.get("balance_after") if r.get("balance_after") is not None else "",
            r.get("provider_ref") or "",
        ])

    filename = f"statement_{m['member_id']}_{acc['account_number']}.csv"
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/csv; charset=utf-8",
        },
    )