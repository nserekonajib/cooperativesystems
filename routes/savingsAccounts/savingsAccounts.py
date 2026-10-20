"""
Savings Accounts blueprint.

Endpoints (all admin-only JSON unless noted):
    GET    /savings/                                 -> page
    GET    /savings/api/stats                        -> KPI cards
    GET    /savings/api/list                         -> paginated accounts
    GET    /savings/api/<account_uuid>               -> single account + member
    GET    /savings/api/<account_uuid>/transactions  -> paginated transactions
    GET    /savings/api/<account_uuid>/statement     -> CSV download
    POST   /savings/api/<account_uuid>/deposit       -> cash: apply now; online: pawaPay + stage JSON
    POST   /savings/api/payment/<ref>/verify         -> verify online deposit; apply on success

    GET    /savings/api/withdrawals                  -> list withdrawal requests
                                                     ?status= &source= &account_id=
    POST   /savings/api/<account_uuid>/withdraw      -> admin creates withdrawal request (status=pending)
    PATCH  /savings/api/withdrawals/<req_uuid>       -> { action: approve|reject|mark_paid }

Design:
- Accounts already exist (created at member registration). This module only
  mutates balances + writes transactions.
- Cash deposit: apply balance + write tx immediately.
- Online deposit: call pawaPay FIRST. On accept, stage JSON under
  instance/pending_deposits/<deposit_id>.json and return 202.
  On verify COMPLETED: read JSON, apply balance + write tx, delete JSON.
  On verify FAILED   : delete JSON, no changes.
- Withdrawal requests: admin creates -> status=pending.
  approve -> status=approved (no balance change).
  reject  -> status=rejected  (no balance change).
  mark_paid -> debit balance + write savings_withdrawal tx -> status=paid.
- Balance is never taken from the client; always read from the DB row.
"""

import os
import io
import csv
import json
import secrets
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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
        "%(asctime)s [%(levelname)s] savings: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

savings_bp = Blueprint("savings", __name__, url_prefix="/savings")

PAWAPAY_MESSAGE = "SACCO savings"

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
# Pending deposit staging (online only)
# ---------------------------------------------------------------------------
def _pending_dir() -> Path:
    p = Path(current_app.instance_path) / "pending_deposits"
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
    log.info("pending deposit saved: %s", path)


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
            log.info("pending file deleted: %s", path)
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


def _to_decimal(v, default="0") -> Decimal:
    if v is None or v == "":
        return Decimal(default)
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid number: {v}")


def _normalize_phone(p: str) -> str:
    digits = "".join(c for c in (p or "") if c.isdigit())
    if digits.startswith("0"):
        digits = "256" + digits[1:]
    elif not digits.startswith("256") and len(digits) == 9:
        digits = "256" + digits
    return digits


def _fetch_savings_account(org_id: str, account_id: str) -> dict | None:
    """Single-row helper used by several endpoints."""
    resp = (
        _admin().table("member_accounts")
        .select("id, account_number, balance, currency, member_id, account_type, status")
        .eq("org_id", org_id)
        .eq("id", account_id)
        .eq("account_type", "savings")
        .limit(1)
        .execute()
    )
    return (resp.data or [None])[0]


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
@savings_bp.route("/", methods=["GET"])
def page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    if session["user"].get("role") != "admin":
        return redirect(url_for("auth.dashboard"))
    return render_template("savingsAccounts/savingsAccounts.html")


# ---------------------------------------------------------------------------
# API: stats (KPI cards)
# ---------------------------------------------------------------------------
@savings_bp.route("/api/stats", methods=["GET"])
@role_required("admin")
def api_stats():
    org_id = _org_id()
    try:
        rows = (
            _admin().table("member_accounts")
            .select("balance, status")
            .eq("org_id", org_id)
            .eq("account_type", "savings")
            .execute()
        ).data or []

        total_balance   = sum(float(r.get("balance") or 0) for r in rows)
        total_accounts  = len(rows)
        active_accounts = sum(1 for r in rows if r.get("status") == "active")

        # deposits in the last 30 days
        since_ts = datetime.now(timezone.utc).timestamp() - 30 * 86400
        since = datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()
        dep_rows = (
            _admin().table("member_transactions")
            .select("amount")
            .eq("org_id", org_id)
            .eq("type", "savings_deposit")
            .eq("status", "completed")
            .gte("created_at", since)
            .execute()
        ).data or []
        deposits_30d = sum(float(r.get("amount") or 0) for r in dep_rows)

        # pending withdrawal requests
        pending_wr = (
            _admin().table("withdrawal_requests")
            .select("id", count="exact")
            .eq("org_id", org_id)
            .eq("status", "pending")
            .limit(1)
            .execute()
        ).count or 0

    except Exception as e:
        log.exception("savings stats failed: %s", e)
        return _bad("Could not load stats", 500)

    return jsonify({
        "ok": True,
        "stats": {
            "total_balance":       total_balance,
            "total_accounts":      total_accounts,
            "active_accounts":     active_accounts,
            "deposits_last_30d":   deposits_30d,
            "pending_withdrawals": pending_wr,
        },
    })


# ---------------------------------------------------------------------------
# API: list savings accounts
# ---------------------------------------------------------------------------
@savings_bp.route("/api/list", methods=["GET"])
@role_required("admin")
def api_list():
    org_id = _org_id()
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
        search   = (request.args.get("q") or "").strip()
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    # Root at member_accounts. Search is applied on primary-table columns
    # only (account_number). Member-field search is done via a preceding
    # lookup against members -> account IDs.
    account_ids_from_members = None

    if search:
        like = f"%{search}%"
        try:
            member_rows = (
                _admin().table("members")
                .select("id")
                .eq("org_id", org_id)
                .or_(
                    f"first_name.ilike.{like},"
                    f"last_name.ilike.{like},"
                    f"member_id.ilike.{like},"
                    f"phone.ilike.{like}"
                )
                .execute()
            ).data or []
        except Exception as e:
            log.exception("member search failed: %s", e)
            return _bad("Could not search members", 500)

        account_ids_from_members = [m["id"] for m in member_rows]

    q = (
        _admin().table("member_accounts")
        .select(
            "id, account_number, balance, currency, status, opened_at, "
            "members!inner(id, member_id, first_name, last_name, phone, email, category, status)",
            count="exact",
        )
        .eq("org_id", org_id)
        .eq("account_type", "savings")
        .order("opened_at", desc=True)
        .range(start, end)
    )

    if search:
        like = f"%{search}%"
        if account_ids_from_members:
            # account_number ILIKE OR member_id IN (...)
            or_expr = (
                f"account_number.ilike.{like},"
                f"member_id.in.({','.join(account_ids_from_members)})"
            )
        else:
            or_expr = f"account_number.ilike.{like}"
        q = q.or_(or_expr)

    try:
        resp = q.execute()
    except Exception as e:
        log.exception("savings list failed: %s", e)
        return _bad("Could not load accounts", 500)

    return jsonify({
        "ok": True,
        "accounts": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# API: single account
# ---------------------------------------------------------------------------
@savings_bp.route("/api/<uuid:account_uuid>", methods=["GET"])
@role_required("admin")
def api_detail(account_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("member_accounts")
            .select(
                "*, members!inner(id, member_id, first_name, last_name, "
                "phone, email, category, status, shares_units, shares_value)"
            )
            .eq("org_id", org_id)
            .eq("id", str(account_uuid))
            .eq("account_type", "savings")
            .limit(1)
            .execute()
        )
    except Exception as e:
        log.exception("savings detail failed: %s", e)
        return _bad("Could not load account", 500)

    if not resp.data:
        return _bad("Account not found", 404)
    return jsonify({"ok": True, "account": resp.data[0]})


# ---------------------------------------------------------------------------
# API: transactions for one account
# ---------------------------------------------------------------------------
@savings_bp.route("/api/<uuid:account_uuid>/transactions", methods=["GET"])
@role_required("admin")
def api_transactions(account_uuid):
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
            _admin().table("member_transactions")
            .select(
                "id, type, method, amount, currency, status, provider, "
                "provider_ref, phone, narration, balance_after, "
                "created_at, updated_at",
                count="exact",
            )
            .eq("org_id", org_id)
            .eq("account_id", str(account_uuid))
            .order("created_at", desc=True)
            .range(start, end)
            .execute()
        )
    except Exception as e:
        log.exception("tx list failed: %s", e)
        return _bad("Could not load transactions", 500)

    return jsonify({
        "ok": True,
        "transactions": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# Internal: apply a completed deposit to the account
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Internal: apply a completed deposit to the account
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Internal: apply a completed deposit to the account
# ---------------------------------------------------------------------------
def _apply_deposit(org_id: str, spec: dict) -> dict:
    """
    Records a completed savings deposit:
      1. writes a member_transactions row (the member ledger)
      2. updates member_accounts.balance
      3. mirrors members.savings_balance
      4. posts to the chart of accounts:
           Dr  1000 Cash on hand  (or 1010 Bank for online)
           Cr  2000 Member savings payable
      5. sends a short SMS to the member's phone (if enabled)

    The member's phone is read from members.phone and normalized to
    256XXXXXXXXX before being handed to send_sms().
    """
    admin = _admin()
    amount = _to_decimal(spec["amount"])
    if amount <= 0:
        raise ValueError("amount must be > 0")

    acc_row = _fetch_savings_account(org_id, spec["account_id"])
    if not acc_row:
        raise RuntimeError("Account not found")

    currency = spec.get("currency") or acc_row["currency"]

    # Fetch phone + first name in one shot (used later for SMS)
    member_row = (
        admin.table("members")
        .select("phone, first_name")
        .eq("id", acc_row["member_id"]).limit(1).execute()
    ).data or [{}]
    member_row = member_row[0]
    member_phone = (member_row.get("phone") or "").strip()
    member_first = (member_row.get("first_name") or "").strip() or "Member"

    current_balance = _to_decimal(acc_row["balance"])
    new_balance = (current_balance + amount).quantize(Decimal("0.01"))

    # --- 1. member ledger row ---
    tx = {
        "org_id":        org_id,
        "member_id":     acc_row["member_id"],
        "account_id":    acc_row["id"],
        "type":          "savings_deposit",
        "method":        spec["method"],
        "amount":        float(amount),
        "currency":      currency,
        "status":        "completed",
        "narration":     spec.get("narration") or "Savings deposit",
        "balance_after": float(new_balance),
        "created_by":    spec.get("created_by") or _user_id(),
    }
    if spec.get("provider"):
        tx["provider"] = spec["provider"]
    if spec.get("deposit_id"):
        tx["provider_ref"] = spec["deposit_id"]
    if spec.get("pay_phone"):
        tx["phone"] = spec["pay_phone"]

    admin.table("member_transactions").insert(tx).execute()

    # --- 2. update member_accounts ---
    admin.table("member_accounts").update({
        "balance":    float(new_balance),
        "updated_at": _now_iso(),
    }).eq("id", acc_row["id"]).execute()

    # --- 3. mirror members.savings_balance ---
    admin.table("members").update({
        "savings_balance": float(new_balance),
        "updated_at":      _now_iso(),
    }).eq("id", acc_row["member_id"]).execute()

    # --- 4. post to chart of accounts ---
    # Savings is a LIABILITY.  Money in:  Dr asset, Cr liability.
    try:
        from routes.accounts.accounts import record_liability_movement
        record_liability_movement(
            org_id=org_id,
            direction="in",
            amount=amount,
            method=spec["method"],
            counterpart_account_code="2000",
            category="other_adjustment",
            narration=f"Savings deposit · {acc_row['account_number']}",
            reference=spec.get("deposit_id") or None,
            created_by=spec.get("created_by"),
            meta={
                "source":     "savings_deposit",
                "account_id": acc_row["id"],
                "member_id":  acc_row["member_id"],
            },
        )
    except Exception:
        log.exception("chart post for deposit failed (non-fatal)")

    # --- 5. SMS notification (fire-and-forget) ---
    if member_phone:
        try:
            from routes.sms.sms import send_sms, clamp_sms
            msg = (
                f"Hi {member_first}, deposit of "
                f"{amount:.0f} {currency} received. "
                f"Balance: {new_balance:.0f} {currency}."
            )
            send_sms(
                org_id=org_id,
                phone=member_phone,
                message=clamp_sms(msg),
                member_id=acc_row["member_id"],
                triggered_by=spec.get("created_by") or _user_id(),
                meta={
                    "kind":       "savings_deposit",
                    "account_id": acc_row["id"],
                    "amount":     float(amount),
                },
            )
        except Exception:
            log.exception("SMS for deposit failed (non-fatal)")

    log.info("deposit applied: account=%s amount=%s new_balance=%s",
             acc_row["account_number"], amount, new_balance)

    return {
        "account_id":     acc_row["id"],
        "account_number": acc_row["account_number"],
        "new_balance":    float(new_balance),
        "amount":         float(amount),
    }

# ---------------------------------------------------------------------------
# Internal: apply a completed withdrawal to the account
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Internal: apply a completed withdrawal to the account
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Internal: apply a completed withdrawal to the account
# ---------------------------------------------------------------------------
def _apply_withdrawal(org_id: str, req_row: dict) -> dict:
    """
    Records a completed savings withdrawal (called only when a withdrawal
    request transitions to status='paid'):
      1. writes a member_transactions row (the member ledger)
      2. updates member_accounts.balance
      3. mirrors members.savings_balance
      4. posts to the chart of accounts:
           Dr  2000 Member savings payable
           Cr  1000 Cash on hand  (or 1010 Bank for online)
      5. sends a short SMS to the member's phone (if enabled)

    Phone is normalized to 256XXXXXXXXX before being handed to send_sms().
    """
    admin = _admin()
    amount = _to_decimal(req_row["amount"])
    if amount <= 0:
        raise ValueError("amount must be > 0")

    acc_row = _fetch_savings_account(org_id, req_row["account_id"])
    if not acc_row:
        raise RuntimeError("Account not found")

    currency = req_row.get("currency") or acc_row["currency"]

    member_row = (
        admin.table("members")
        .select("phone, first_name")
        .eq("id", acc_row["member_id"]).limit(1).execute()
    ).data or [{}]
    member_row = member_row[0]
    member_phone = (member_row.get("phone") or "").strip()
    member_first = (member_row.get("first_name") or "").strip() or "Member"

    current_balance = _to_decimal(acc_row["balance"])
    if amount > current_balance:
        raise RuntimeError(f"Insufficient balance. Available: {current_balance}")
    new_balance = (current_balance - amount).quantize(Decimal("0.01"))

    # --- 1. member ledger row ---
    tx = {
        "org_id":        org_id,
        "member_id":     acc_row["member_id"],
        "account_id":    acc_row["id"],
        "type":          "savings_withdrawal",
        "method":        req_row.get("method") or "cash",
        "amount":        float(amount),
        "currency":      currency,
        "status":        "completed",
        "narration":     req_row.get("reason") or "Savings withdrawal",
        "balance_after": float(new_balance),
        "created_by":    _user_id(),
    }
    if req_row.get("provider"):
        tx["provider"] = req_row["provider"]

    admin.table("member_transactions").insert(tx).execute()

    # --- 2. update member_accounts ---
    admin.table("member_accounts").update({
        "balance":    float(new_balance),
        "updated_at": _now_iso(),
    }).eq("id", acc_row["id"]).execute()

    # --- 3. mirror members.savings_balance ---
    admin.table("members").update({
        "savings_balance": float(new_balance),
        "updated_at":      _now_iso(),
    }).eq("id", acc_row["member_id"]).execute()

    # --- 4. post to chart of accounts ---
    try:
        from routes.accounts.accounts import record_liability_movement
        record_liability_movement(
            org_id=org_id,
            direction="out",
            amount=amount,
            method=req_row.get("method") or "cash",
            counterpart_account_code="2000",
            category="other_adjustment",
            narration=f"Savings withdrawal · {acc_row['account_number']}",
            reference=None,
            created_by=_user_id(),
            meta={
                "source":     "savings_withdrawal",
                "account_id": acc_row["id"],
                "member_id":  acc_row["member_id"],
                "request_id": req_row.get("id"),
            },
        )
    except Exception:
        log.exception("chart post for withdrawal failed (non-fatal)")

    # --- 5. SMS notification ---
    if member_phone:
        try:
            from routes.sms.sms import send_sms, clamp_sms
            msg = (
                f"Hi {member_first}, withdrawal of "
                f"{amount:.0f} {currency} processed. "
                f"Balance: {new_balance:.0f} {currency}."
            )
            send_sms(
                org_id=org_id,
                phone=member_phone,
                message=clamp_sms(msg),
                member_id=acc_row["member_id"],
                triggered_by=_user_id(),
                meta={
                    "kind":       "savings_withdrawal",
                    "account_id": acc_row["id"],
                    "request_id": req_row.get("id"),
                    "amount":     float(amount),
                },
            )
        except Exception:
            log.exception("SMS for withdrawal failed (non-fatal)")

    log.info("withdrawal applied: account=%s amount=%s new_balance=%s",
             acc_row["account_number"], amount, new_balance)

    return {
        "account_id":     acc_row["id"],
        "account_number": acc_row["account_number"],
        "new_balance":    float(new_balance),
        "amount":         float(amount),
    }
    
# ---------------------------------------------------------------------------
# PUBLIC HELPER — deposit / withdrawal against a liability or equity account
# ---------------------------------------------------------------------------
def record_liability_movement(
    *,
    org_id: str,
    direction: str,           # 'in' = money received from member (deposit)
                              # 'out' = money paid to member (withdrawal)
    amount,
    method: str,              # 'cash' | 'bank' | 'online' | 'mobile'
    counterpart_account_code: str,   # '2000' savings, '2010' FD, '3000' share capital
    category: str = "other_adjustment",
    narration: str | None = None,
    reference: str | None = None,
    txn_date=None,
    created_by: str | None = None,
    meta: dict | None = None,
):
    """
    Post a movement where the counterpart is a liability or equity account.

    direction='in'  (deposit / share purchase / FD top-up):
        DEBIT  asset  (cash or bank)         [asset increases]
        CREDIT liability/equity              [liability increases]

    direction='out' (withdrawal / redemption):
        CREDIT asset  (cash or bank)         [asset decreases]
        DEBIT  liability/equity              [liability decreases]

    Returns the created transaction row, or None on soft failure.
    """
    try:
        admin = _admin()

        code_map = {
            "cash":     "1000",
            "bank":     "1010",
            "online":   "1010",
            "mobile":   "1020",
            "internal": "1000",
        }
        asset_code = code_map.get((method or "").lower(), "1000")

        asset_row = (
            admin.table("accounts")
            .select("id, code, name")
            .eq("org_id", org_id).eq("code", asset_code).limit(1).execute()
        ).data
        if not asset_row:
            log.warning("asset account %s missing; skipping post", asset_code)
            return None
        asset_id = asset_row[0]["id"]

        counter_row = (
            admin.table("accounts")
            .select("id, code, name")
            .eq("org_id", org_id)
            .eq("code", counterpart_account_code).limit(1).execute()
        ).data
        if not counter_row:
            log.warning("counterpart account %s missing; skipping post",
                        counterpart_account_code)
            return None
        counter_id = counter_row[0]["id"]

        if direction == "in":
            debit_id, credit_id = asset_id, counter_id
        elif direction == "out":
            debit_id, credit_id = counter_id, asset_id
        else:
            log.warning("record_liability_movement: invalid direction %s", direction)
            return None

        result = _apply_txn(org_id, {
            "type":              "transfer",
            "category":          category,
            "amount":            str(amount),
            "debit_account_id":  debit_id,
            "credit_account_id": credit_id,
            "method":            method,
            "reference":         reference,
            "narration":         narration,
            "txn_date":          txn_date or datetime.now(timezone.utc).date().isoformat(),
            "meta":              meta or {},
            "created_by":        created_by,
        })
        return result
    except Exception:
        log.exception("record_liability_movement failed (non-fatal)")
        return None

# ---------------------------------------------------------------------------
# API: deposit (cash or online)
# ---------------------------------------------------------------------------
@savings_bp.route("/api/<uuid:account_uuid>/deposit", methods=["POST"])
@role_required("admin")
def api_deposit(account_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}

    log.info("=== DEPOSIT === account=%s payload=%s",
             account_uuid, {k: v for k, v in data.items() if k != "notes"})

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
            return _bad("Phone is required for online deposit", reason="missing_phone")
        pay_phone = _normalize_phone(pay_phone)

    acc = _fetch_savings_account(org_id, str(account_uuid))
    if not acc:
        return _bad("Savings account not found", 404, reason="not_found")
    currency = acc["currency"] or "UGX"

    narration = (data.get("narration") or "").strip() or None
    if not narration:
        narration = "Cash savings deposit" if method == "cash" else "Online savings deposit"

    # ---------------- CASH ----------------
    if method == "cash":
        try:
            result = _apply_deposit(org_id, {
                "account_id": str(account_uuid),
                "amount":     str(amount),
                "currency":   currency,
                "method":     "cash",
                "narration":  narration,
                "created_by": _user_id(),
            })
        except Exception as e:
            log.exception("cash deposit failed")
            return _bad(f"Could not complete deposit: {e}", 500, reason="apply_failed")

        return jsonify({
            "ok": True,
            "message": "Deposit recorded",
            "deposit": result,
            "payment": {
                "method":   "cash",
                "status":   "completed",
                "amount":   float(amount),
                "currency": currency,
            },
        }), 201

    # ---------------- ONLINE ----------------
    log.info("initiating pawaPay deposit: provider=%s phone=%s amount=%s",
             provider, pay_phone, int(amount))
    try:
        if provider == "MTN":
            pay_resp = deposit_mtn_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"dep-{secrets.token_hex(6)}",
                customer_message=PAWAPAY_MESSAGE,
            )
        else:
            pay_resp = deposit_airtel_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"dep-{secrets.token_hex(6)}",
                customer_message=PAWAPAY_MESSAGE,
            )
    except Exception as e:
        log.exception("pawaPay deposit failed")
        return _bad(f"Payment could not be initiated: {e}. No deposit applied.",
                    502, reason="pawapay_raised")

    log.info("pawaPay response: %s", pay_resp)

    deposit_id = pay_resp.get("depositId") or pay_resp.get("deposit_id")
    pay_status = (pay_resp.get("status") or "").upper()

    if not deposit_id or pay_status in ("REJECTED", "FAILED"):
        reason = (pay_resp.get("failureReason") or {}).get("failureMessage") \
                 or pay_status or "unknown"
        return _bad(f"Payment was rejected: {reason}. No deposit applied.",
                    400, reason="payment_rejected")

    spec = {
        "org_id":     org_id,
        "account_id": str(account_uuid),
        "member_id":  acc["member_id"],
        "amount":     str(amount),
        "currency":   currency,
        "method":     "online",
        "provider":   provider,
        "pay_phone":  pay_phone,
        "deposit_id": deposit_id,
        "narration":  narration,
        "created_by": _user_id(),
        "staged_at":  _now_iso(),
    }
    try:
        _save_pending(deposit_id, spec)
    except Exception as e:
        log.exception("failed to stage deposit")
        return _bad(f"Payment accepted but staging failed: {e}",
                    500, reason="staging_failed")

    return jsonify({
        "ok": True,
        "pending": True,
        "payment": {
            "method":       "online",
            "status":       "pending",
            "provider":     provider,
            "deposit_id":   deposit_id,
            "phone":        pay_phone,
            "amount":       float(amount),
            "currency":     currency,
            "instructions": "Approve the prompt on the customer's phone.",
        },
    }), 202


# ---------------------------------------------------------------------------
# API: verify online deposit
# ---------------------------------------------------------------------------
@savings_bp.route("/api/payment/<deposit_id>/verify", methods=["POST"])
@role_required("admin")
def api_verify(deposit_id):
    org_id = _org_id()
    log.info("VERIFY deposit_id=%s", deposit_id)

    staged = _load_pending(deposit_id)
    if staged is None:
        return _bad("No pending deposit for this reference", 404,
                    reason="not_found")

    try:
        result = verify_deposit(deposit_id)
    except Exception as e:
        log.exception("verify_deposit failed")
        return _bad(f"Verification error: {e}", 502, reason="provider_error")

    status = result.get("status")
    log.info("verify: provider_status=%s", status)

    if result.get("completed"):
        try:
            applied = _apply_deposit(org_id, staged)
        except Exception as e:
            log.exception("apply after payment failed")
            return _bad(f"Payment succeeded but balance update failed: {e}",
                        500, reason="apply_failed")
        _delete_pending(deposit_id)
        return jsonify({
            "ok": True,
            "status": "completed",
            "provider_status": status,
            "deposit": applied,
        })

    if result.get("failed"):
        _delete_pending(deposit_id)
        return jsonify({
            "ok": True,
            "status": "failed",
            "provider_status": status,
            "reason": result.get("failure_reason"),
        })

    return jsonify({"ok": True, "status": "pending", "provider_status": status})


# ---------------------------------------------------------------------------
# API: list withdrawal requests
# ---------------------------------------------------------------------------
@savings_bp.route("/api/withdrawals", methods=["GET"])
@role_required("admin")
def api_withdrawals():
    org_id = _org_id()
    status_filter  = (request.args.get("status") or "").strip()
    source_filter  = (request.args.get("source") or "").strip()
    account_filter = (request.args.get("account_id") or "").strip()

    try:
        q = (
            _admin().table("withdrawal_requests")
            .select(
                "id, amount, currency, reason, status, source, method, provider, "
                "created_at, reviewed_at, paid_at, review_notes, "
                "members!inner(member_id, first_name, last_name, phone), "
                "member_accounts!inner(account_number)"
            )
            .eq("org_id", org_id)
            .order("created_at", desc=True)
            .limit(300)
        )
        if status_filter:
            q = q.eq("status", status_filter)
        if source_filter:
            q = q.eq("source", source_filter)
        if account_filter:
            q = q.eq("account_id", account_filter)
        resp = q.execute()
    except Exception as e:
        log.exception("withdrawals list failed: %s", e)
        return _bad("Could not load withdrawal requests", 500)

    return jsonify({"ok": True, "requests": resp.data or []})


# ---------------------------------------------------------------------------
# API: create withdrawal request (admin-initiated)
# ---------------------------------------------------------------------------
@savings_bp.route("/api/<uuid:account_uuid>/withdraw", methods=["POST"])
@role_required("admin")
def api_withdraw_create(account_uuid):
    """
    Body: { amount, reason?, method? ('cash'|'online'), provider?, phone? }
    Creates a withdrawal_requests row with status='pending'.
    Balance is NOT changed yet — mark_paid does that.
    """
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
            return _bad("phone is required for online payout", reason="missing_phone")
        pay_phone = _normalize_phone(pay_phone)

    acc = _fetch_savings_account(org_id, str(account_uuid))
    if not acc:
        return _bad("Savings account not found", 404, reason="not_found")

    current_balance = _to_decimal(acc["balance"])
    if amount > current_balance:
        return _bad(
            f"Insufficient balance. Available: {current_balance}",
            reason="insufficient_balance",
        )

    pending_rows = (
        _admin().table("withdrawal_requests")
        .select("amount")
        .eq("org_id", org_id)
        .eq("account_id", str(account_uuid))
        .in_("status", ["pending", "approved"])
        .execute()
    ).data or []
    pending_total = sum(_to_decimal(r.get("amount")) for r in pending_rows)
    if (pending_total + amount) > current_balance:
        return _bad(
            f"Existing pending withdrawals ({pending_total}) plus this "
            f"request ({amount}) exceed available balance ({current_balance}).",
            reason="insufficient_balance_pending",
        )

    row = {
        "org_id":     org_id,
        "member_id":  acc["member_id"],
        "account_id": acc["id"],
        "amount":     float(amount),
        "currency":   acc["currency"] or "UGX",
        "reason":     (data.get("reason") or "").strip() or None,
        "status":     "pending",
        "source":     "admin",
        "method":     method,
        "provider":   provider,
        "created_by": _user_id(),
    }
    try:
        resp = _admin().table("withdrawal_requests").insert(row).execute()
    except Exception as e:
        log.exception("withdrawal create failed")
        return _bad(f"Could not create withdrawal: {e}", 500, reason="db_error")

    return jsonify({"ok": True, "request": resp.data[0]}), 201


# ---------------------------------------------------------------------------
# API: approve / reject / mark paid a withdrawal request
# ---------------------------------------------------------------------------
@savings_bp.route("/api/withdrawals/<uuid:req_uuid>", methods=["PATCH"])
@role_required("admin")
def api_withdraw_update(req_uuid):
    """
    Body: { action: 'approve'|'reject'|'mark_paid', notes? }
    """
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    action = (data.get("action") or "").lower()
    notes  = (data.get("notes") or "").strip() or None

    if action not in ("approve", "reject", "mark_paid"):
        return _bad("action must be 'approve', 'reject', or 'mark_paid'",
                    reason="bad_action")

    try:
        resp = (
            _admin().table("withdrawal_requests")
            .select("*")
            .eq("org_id", org_id)
            .eq("id", str(req_uuid))
            .limit(1)
            .execute()
        )
    except Exception as e:
        log.exception("withdrawal fetch failed")
        return _bad("Could not load request", 500, reason="db_error")

    req = (resp.data or [None])[0]
    if not req:
        return _bad("Withdrawal request not found", 404, reason="not_found")

    now = _now_iso()

    if action == "approve":
        if req["status"] != "pending":
            return _bad(f"Cannot approve a request in status '{req['status']}'",
                        reason="bad_state")
        upd = {
            "status":       "approved",
            "reviewed_by":  _user_id(),
            "reviewed_at":  now,
            "review_notes": notes,
            "updated_at":   now,
        }
        r = _admin().table("withdrawal_requests").update(upd).eq("id", req["id"]).execute()
        return jsonify({"ok": True, "request": r.data[0]})

    if action == "reject":
        if req["status"] not in ("pending", "approved"):
            return _bad(f"Cannot reject a request in status '{req['status']}'",
                        reason="bad_state")
        upd = {
            "status":       "rejected",
            "reviewed_by":  _user_id(),
            "reviewed_at":  now,
            "review_notes": notes,
            "updated_at":   now,
        }
        r = _admin().table("withdrawal_requests").update(upd).eq("id", req["id"]).execute()
        return jsonify({"ok": True, "request": r.data[0]})

    # mark_paid
    if req["status"] != "approved":
        return _bad(
            f"Request must be 'approved' before marking paid "
            f"(currently '{req['status']}')",
            reason="bad_state",
        )
    try:
        applied = _apply_withdrawal(org_id, req)
    except Exception as e:
        log.exception("withdrawal apply failed")
        return _bad(f"Could not apply withdrawal: {e}", 500, reason="apply_failed")

    upd = {
        "status":       "paid",
        "paid_at":      now,
        "updated_at":   now,
        "review_notes": notes or req.get("review_notes"),
    }
    r = _admin().table("withdrawal_requests").update(upd).eq("id", req["id"]).execute()
    return jsonify({"ok": True, "request": r.data[0], "applied": applied})


# ---------------------------------------------------------------------------
# API: CSV statement download
# ---------------------------------------------------------------------------
@savings_bp.route("/api/<uuid:account_uuid>/statement", methods=["GET"])
@role_required("admin")
def api_statement(account_uuid):
    """
    CSV export of all transactions for one savings account.
    Optional ?from=YYYY-MM-DD&to=YYYY-MM-DD
    """
    org_id = _org_id()

    try:
        acc_resp = (
            _admin().table("member_accounts")
            .select("*, members!inner(member_id, first_name, last_name, phone, email)")
            .eq("org_id", org_id)
            .eq("id", str(account_uuid))
            .eq("account_type", "savings")
            .limit(1)
            .execute()
        )
    except Exception as e:
        log.exception("statement account fetch failed")
        return _bad("Could not load account", 500)

    if not acc_resp.data:
        return _bad("Account not found", 404)
    acc = acc_resp.data[0]
    member = acc["members"]

    try:
        q = (
            _admin().table("member_transactions")
            .select("created_at, type, method, amount, currency, status, "
                    "narration, balance_after, provider_ref")
            .eq("org_id", org_id)
            .eq("account_id", str(account_uuid))
            .order("created_at", desc=False)
        )
        date_from = request.args.get("from")
        date_to   = request.args.get("to")
        if date_from:
            q = q.gte("created_at", date_from)
        if date_to:
            q = q.lte("created_at", date_to + "T23:59:59")

        tx_resp = q.execute()
    except Exception as e:
        log.exception("statement tx fetch failed")
        return _bad("Could not load transactions", 500)

    rows = tx_resp.data or []

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["SACCO Pro — Savings Statement"])
    w.writerow(["Member", f"{member['first_name']} {member['last_name']}"])
    w.writerow(["Member ID", member["member_id"]])
    w.writerow(["Phone", member.get("phone") or ""])
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

    csv_bytes = buf.getvalue().encode("utf-8-sig")

    filename = f"statement_{member['member_id']}_{acc['account_number']}.csv"
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/csv; charset=utf-8",
        },
    )
    

