"""
Accounts blueprint — Expenses & Other Incomes.

Endpoints (admin-only JSON):
    GET    /accounts/                                        -> page
    GET    /accounts/api/overview                            -> P&L snapshot + cash position
    GET    /accounts/api/chart                               -> chart of accounts tree
    GET    /accounts/api/accounts                            -> flat list of accounts
    GET    /accounts/api/accounts/<uuid:acc_uuid>            -> single account + recent txns
    GET    /accounts/api/transactions                        -> paginated feed
    GET    /accounts/api/transactions/<uuid:txn_uuid>        -> single transaction
    GET    /accounts/api/transactions/<uuid:txn_uuid>/receipt -> printable HTML voucher

    POST   /accounts/api/accounts                            -> create account
    PATCH  /accounts/api/accounts/<uuid:acc_uuid>            -> update account
    POST   /accounts/api/accounts/<uuid:acc_uuid>/toggle     -> activate/deactivate
    POST   /accounts/api/seed                                -> seed default chart (idempotent)

    POST   /accounts/api/expenses                            -> record an expense
    POST   /accounts/api/incomes                             -> record other income
    POST   /accounts/api/transfers                           -> cash <-> bank <-> mobile transfer
    POST   /accounts/api/transactions/<uuid:txn_uuid>/reverse -> reverse a transaction

    GET    /accounts/api/summary/expenses                    -> expense breakdown for a period
    GET    /accounts/api/summary/incomes                     -> other-income breakdown for a period
    GET    /accounts/api/export                              -> CSV

Design notes:
- Every transaction records BOTH the debit and credit account so a
  balance sheet view is derivable. Account.balance is denormalised for
  speed and updated inside the same handler.
- Loans/savings/subscriptions do NOT post here; their ledgers live in
  their own modules. This module is for the SACCO's operating P&L:
  expenses, other income, transfers, and adjustments.
- All money is Decimal, quantised to 0.01.
"""

import os
import io
import csv
import logging
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import (
    Blueprint, render_template, request, jsonify, session,
    redirect, url_for, Response,
)
from dotenv import load_dotenv
from supabase import create_client, Client

from routes.auth.auth import role_required

load_dotenv()

log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] accounts: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

accounts_bp = Blueprint("accounts", __name__, url_prefix="/accounts")

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


def _parse_date(v):
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _next_txn_number(org_id: str) -> str:
    try:
        resp = _admin().rpc("next_account_txn_number", {"p_org": org_id}).execute()
    except Exception as e:
        log.exception("next_account_txn_number failed")
        raise RuntimeError(f"Could not allocate txn number: {e}")
    raw = resp.data
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, dict):
        raw = next(iter(raw.values()))
    return f"TX-{int(raw):06d}"


# ---------------------------------------------------------------------------
# Expense & income categories (mirror the SQL check constraint)
# ---------------------------------------------------------------------------
EXPENSE_CATEGORIES = {
    "salaries","rent","utilities","internet","transport","office_supplies",
    "marketing","training","legal","audit","insurance","repairs",
    "bank_charges","taxes","loan_loss_provision","dividend_payout",
    "other_operating","capital_expenditure","other_expense",
}
INCOME_CATEGORIES = {
    "grant","donation","bank_interest","rental_income","dividend_income",
    "asset_sale","misc_income","membership_penalty","other_income",
}
TRANSFER_CATEGORIES = {
    "cash_transfer","bank_deposit","bank_withdrawal","opening_balance",
    "reconciliation","other_adjustment",
}


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
@accounts_bp.route("/", methods=["GET"])
def page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    if session["user"].get("role") != "admin":
        return redirect(url_for("auth.dashboard"))
    return render_template("accounts/accounts.html")


# ---------------------------------------------------------------------------
# SEED (idempotent)
# ---------------------------------------------------------------------------
@accounts_bp.route("/api/seed", methods=["POST"])
@role_required("admin")
def api_seed():
    org_id = _org_id()
    try:
        s = (
            _admin().table("institution_settings")
            .select("currency")
            .eq("org_id", org_id).limit(1).execute()
        ).data or [{}]
        currency = s[0].get("currency") or "UGX"
        _admin().rpc("seed_default_accounts", {
            "p_org": org_id, "p_currency": currency,
        }).execute()
    except Exception as e:
        log.exception("seed failed")
        return _bad(f"Could not seed accounts: {e}", 500, reason="db_error")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# OVERVIEW — P&L + cash position
# ---------------------------------------------------------------------------
@accounts_bp.route("/api/overview", methods=["GET"])
@role_required("admin")
def api_overview():
    org_id = _org_id()
    try:
        today = _today()
        first_of_month = date(today.year, today.month, 1)
        year_start = date(today.year, 1, 1)

        # Cash positions
        cash_accts = (
            _admin().table("accounts")
            .select("id, code, name, balance, currency, is_cash_account, is_bank_account")
            .eq("org_id", org_id)
            .eq("category", "asset")
            .execute()
        ).data or []

        cash_total = ZERO
        bank_total = ZERO
        mobile_total = ZERO
        for a in cash_accts:
            bal = _to_decimal(a.get("balance"))
            if a.get("is_bank_account"):
                bank_total += bal
            elif a.get("is_cash_account"):
                if "mobile" in (a.get("name") or "").lower():
                    mobile_total += bal
                else:
                    cash_total += bal

        # Income & expense totals for period
        def _period_totals(start: date) -> dict:
            rows = (
                _admin().table("account_transactions")
                .select("type, amount")
                .eq("org_id", org_id)
                .eq("status", "completed")
                .gte("txn_date", start.isoformat())
                .execute()
            ).data or []
            inc = ZERO
            exp = ZERO
            for r in rows:
                amt = _to_decimal(r.get("amount"))
                if r.get("type") == "income":
                    inc += amt
                elif r.get("type") == "expense":
                    exp += amt
            return {"income": inc, "expense": exp}

        month = _period_totals(first_of_month)
        year  = _period_totals(year_start)

        # All-time expense/income
        all_rows = (
            _admin().table("account_transactions")
            .select("type, amount")
            .eq("org_id", org_id).eq("status", "completed")
            .execute()
        ).data or []
        all_inc = sum((_to_decimal(r["amount"]) for r in all_rows if r["type"] == "income"), ZERO)
        all_exp = sum((_to_decimal(r["amount"]) for r in all_rows if r["type"] == "expense"), ZERO)

    except Exception as e:
        log.exception("overview failed: %s", e)
        return _bad("Could not load overview", 500)

    return jsonify({
        "ok": True,
        "overview": {
            "cash_on_hand":     float(_q(cash_total)),
            "bank_balance":     float(_q(bank_total)),
            "mobile_float":     float(_q(mobile_total)),
            "cash_total":       float(_q(cash_total + bank_total + mobile_total)),
            "month_income":     float(_q(month["income"])),
            "month_expense":    float(_q(month["expense"])),
            "month_net":        float(_q(month["income"] - month["expense"])),
            "year_income":      float(_q(year["income"])),
            "year_expense":     float(_q(year["expense"])),
            "year_net":         float(_q(year["income"] - year["expense"])),
            "lifetime_income":  float(_q(all_inc)),
            "lifetime_expense": float(_q(all_exp)),
            "lifetime_net":     float(_q(all_inc - all_exp)),
        },
    })


# ---------------------------------------------------------------------------
# CHART OF ACCOUNTS
# ---------------------------------------------------------------------------
@accounts_bp.route("/api/chart", methods=["GET"])
@role_required("admin")
def api_chart():
    org_id = _org_id()
    try:
        rows = (
            _admin().table("accounts")
            .select("*")
            .eq("org_id", org_id)
            .order("code")  
            .execute()
        ).data or []
    except Exception as e:
        log.exception("chart failed: %s", e)
        return _bad("Could not load chart of accounts", 500)

    grouped = {"asset": [], "liability": [], "equity": [],
               "income": [], "expense": []}
    for r in rows:
        cat = r.get("category")
        if cat in grouped:
            grouped[cat].append(r)

    return jsonify({"ok": True, "accounts": rows, "grouped": grouped})


@accounts_bp.route("/api/accounts", methods=["GET"])
@role_required("admin")
def api_accounts_list():
    org_id = _org_id()
    category = (request.args.get("category") or "").strip()
    active_only = request.args.get("active_only", "true").lower() == "true"

    q = (
        _admin().table("accounts")
        .select("*")
        .eq("org_id", org_id)
        .order("code")  
    )
    if category:
        q = q.eq("category", category)
    if active_only:
        q = q.eq("is_active", True)

    try:
        rows = q.execute().data or []
    except Exception as e:
        log.exception("accounts list failed: %s", e)
        return _bad("Could not load accounts", 500)
    return jsonify({"ok": True, "accounts": rows})


@accounts_bp.route("/api/accounts/<uuid:acc_uuid>", methods=["GET"])
@role_required("admin")
def api_account_detail(acc_uuid):
    org_id = _org_id()
    try:
        acc = (
            _admin().table("accounts")
            .select("*")
            .eq("org_id", org_id).eq("id", str(acc_uuid)).limit(1).execute()
        ).data
    except Exception as e:
        log.exception("account detail failed")
        return _bad("Could not load account", 500)
    if not acc:
        return _bad("Account not found", 404)
    acc = acc[0]

    try:
        txns = (
            _admin().table("account_transactions")
            .select("id, txn_number, type, category, amount, currency, "
                    "method, payee, narration, txn_date, status, created_at, "
                    "debit_account_id, credit_account_id")
            .eq("org_id", org_id)
            .or_(f"debit_account_id.eq.{acc['id']},credit_account_id.eq.{acc['id']}")
            .order("txn_date", desc=True)
            .limit(50)
            .execute()
        ).data or []
    except Exception:
        log.exception("account txns fetch failed (non-fatal)")
        txns = []

    return jsonify({"ok": True, "account": acc, "transactions": txns})


# ---------------------------------------------------------------------------
# CREATE / UPDATE ACCOUNT
# ---------------------------------------------------------------------------
@accounts_bp.route("/api/accounts", methods=["POST"])
@role_required("admin")
def api_account_create():
    org_id = _org_id()
    data = request.get_json(silent=True) or {}

    code = (data.get("code") or "").strip()
    name = (data.get("name") or "").strip()
    category = (data.get("category") or "").strip()
    if not code or not name or not category:
        return _bad("code, name and category are required", reason="missing_fields")
    if category not in ("asset","liability","equity","income","expense"):
        return _bad("Invalid category", reason="bad_category")

    row = {
        "org_id":          org_id,
        "code":            code,
        "name":            name,
        "category":        category,
        "subcategory":     (data.get("subcategory") or "").strip() or None,
        "parent_id":       data.get("parent_id") or None,
        "description":     (data.get("description") or "").strip() or None,
        "is_cash_account": bool(data.get("is_cash_account", False)),
        "is_bank_account": bool(data.get("is_bank_account", False)),
        "is_active":       True,
        "currency":        (data.get("currency") or "UGX"),
        "created_by":      _user_id(),
    }

    try:
        resp = _admin().table("accounts").insert(row).execute()
    except Exception as e:
        log.exception("account insert failed")
        if "23505" in str(e):
            return _bad("Account code or name already exists", 409, reason="duplicate")
        return _bad(f"Could not create account: {e}", 500, reason="db_error")

    return jsonify({"ok": True, "account": resp.data[0]}), 201


@accounts_bp.route("/api/accounts/<uuid:acc_uuid>", methods=["PATCH"])
@role_required("admin")
def api_account_update(acc_uuid):
    org_id = _org_id()
    data = request.get_json(silent=True) or {}

    upd = {}
    for k in ("name","description","subcategory","parent_id",
              "is_cash_account","is_bank_account","is_active","currency"):
        if k in data:
            upd[k] = data[k]
    if not upd:
        return _bad("No fields to update", reason="empty")
    upd["updated_at"] = _now_iso()

    try:
        resp = (
            _admin().table("accounts")
            .update(upd).eq("org_id", org_id).eq("id", str(acc_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("account update failed")
        return _bad(f"Could not update: {e}", 500, reason="db_error")
    if not resp.data:
        return _bad("Account not found", 404, reason="not_found")
    return jsonify({"ok": True, "account": resp.data[0]})


@accounts_bp.route("/api/accounts/<uuid:acc_uuid>/toggle", methods=["PATCH"])
@role_required("admin")
def api_account_toggle(acc_uuid):
    org_id = _org_id()
    data = request.get_json(silent=True) or {}
    if "is_active" not in data:
        return _bad("is_active required", reason="missing_field")

    try:
        resp = (
            _admin().table("accounts")
            .update({"is_active": bool(data["is_active"]),
                     "updated_at": _now_iso()})
            .eq("org_id", org_id).eq("id", str(acc_uuid))
            .execute()
        )
    except Exception as e:
        return _bad(f"Could not toggle: {e}", 500, reason="db_error")
    if not resp.data:
        return _bad("Account not found", 404, reason="not_found")
    return jsonify({"ok": True, "account": resp.data[0]})


# ---------------------------------------------------------------------------
# TRANSACTIONS FEED
# ---------------------------------------------------------------------------
@accounts_bp.route("/api/transactions", methods=["GET"])
@role_required("admin")
def api_transactions():
    org_id = _org_id()
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
        txn_type = (request.args.get("type") or "").strip()
        category = (request.args.get("category") or "").strip()
        status   = (request.args.get("status") or "").strip()
        date_from= request.args.get("from")
        date_to  = request.args.get("to")
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    q = (
        _admin().table("account_transactions")
        .select(
            "id, txn_number, type, category, amount, currency, method, "
            "payee, reference, narration, txn_date, status, created_at, "
            "meta",
            count="exact",
        )
        .eq("org_id", org_id)
        .order("txn_date", desc=True)
        .order("created_at", desc=True)
        .range(start, end)
    )
    if txn_type: q = q.eq("type", txn_type)
    if category: q = q.eq("category", category)
    if status:   q = q.eq("status", status)
    if date_from:q = q.gte("txn_date", date_from)
    if date_to:  q = q.lte("txn_date", date_to)

    try:
        resp = q.execute()
    except Exception as e:
        log.exception("txn feed failed: %s", e)
        return _bad("Could not load transactions", 500)

    return jsonify({
        "ok": True,
        "transactions": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


@accounts_bp.route("/api/transactions/<uuid:txn_uuid>", methods=["GET"])
@role_required("admin")
def api_transaction_detail(txn_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("account_transactions")
            .select("*")
            .eq("org_id", org_id).eq("id", str(txn_uuid)).limit(1).execute()
        )
    except Exception:
        log.exception("txn detail failed")
        return _bad("Could not load transaction", 500)
    if not resp.data:
        return _bad("Transaction not found", 404)
    return jsonify({"ok": True, "transaction": resp.data[0]})


# ---------------------------------------------------------------------------
# RECEIPT / VOUCHER
# ---------------------------------------------------------------------------
def _render_voucher(txn: dict, settings: dict,
                    debit_acc: dict | None, credit_acc: dict | None) -> str:
    display = settings.get("display_name") or "SACCO Pro"
    addr = " ".join(filter(None, [settings.get("address_line") or "",
                                   settings.get("city") or "",
                                   settings.get("country") or ""]))

    def acc_label(a):
        if not a: return "—"
        return f"{a.get('code')} · {a.get('name')}"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Voucher {txn['txn_number']}</title>
<style>
 body{{font-family:Inter,system-ui,sans-serif;padding:40px;color:#1E293B;background:#F4F6F9;}}
 .card{{max-width:680px;margin:auto;background:#fff;border:1px solid #E2E8F0;border-radius:16px;padding:32px;}}
 h1{{font-size:20px;margin:0 0 4px;}} h2{{font-size:15px;margin:0;}}
 h3{{font-size:13px;color:#64748B;text-transform:uppercase;letter-spacing:.06em;margin:24px 0 6px;}}
 .sub{{color:#64748B;font-size:12px;margin:0 0 24px;}}
 .row{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px dashed #E2E8F0;font-size:14px;}}
 .row:last-child{{border-bottom:none;}}
 .label{{color:#64748B;}} .total{{font-weight:800;font-size:18px;color:#1A62FF;}}
 .tag{{display:inline-block;background:#E6F4EA;color:#065F46;padding:4px 10px;border-radius:999px;font-size:11px;font-weight:600;}}
 .exp{{background:#FEE2E2;color:#991B1B;}}
 @media print{{body{{padding:0;background:#fff;}} .card{{border:none;}}}}
</style></head><body>
<div class="card">
  <h1>{display}</h1>
  <p class="sub">{addr}</p>
  <span class="tag {'exp' if txn['type'] == 'expense' else ''}">{txn['type'].upper()}</span>
  <h2 style="margin-top:16px;">Transaction Voucher</h2>
  <p class="sub">Voucher <strong>{txn['txn_number']}</strong> · {txn.get('txn_date')}</p>

  <div class="row"><span class="label">Category</span><span>{txn.get('category')}</span></div>
  <div class="row"><span class="label">Method</span><span>{txn.get('method') or '—'}</span></div>
  <div class="row"><span class="label">Payee</span><span>{txn.get('payee') or '—'}</span></div>
  <div class="row"><span class="label">Reference</span><span>{txn.get('reference') or '—'}</span></div>
  <div class="row"><span class="label">Amount</span><span class="total">{txn['amount']} {txn['currency']}</span></div>

  <h3>Postings</h3>
  <div class="row"><span class="label">Debit</span><span>{acc_label(debit_acc)}</span></div>
  <div class="row"><span class="label">Credit</span><span>{acc_label(credit_acc)}</span></div>

  <h3>Narration</h3>
  <p style="font-size:14px;">{txn.get('narration') or '—'}</p>
</div>
</body></html>"""


@accounts_bp.route("/api/transactions/<uuid:txn_uuid>/receipt", methods=["GET"])
@role_required("admin")
def api_receipt(txn_uuid):
    org_id = _org_id()
    try:
        txn = (
            _admin().table("account_transactions")
            .select("*")
            .eq("org_id", org_id).eq("id", str(txn_uuid)).limit(1).execute()
        ).data
    except Exception:
        log.exception("voucher fetch failed")
        return _bad("Could not load transaction", 500)
    if not txn:
        return _bad("Transaction not found", 404)
    txn = txn[0]

    try:
        s = (
            _admin().table("institution_settings")
            .select("display_name, address_line, city, country")
            .eq("org_id", org_id).limit(1).execute()
        ).data
    except Exception:
        s = []

    debit_acc = credit_acc = None
    for aid, key in ((txn.get("debit_account_id"), "debit"),
                     (txn.get("credit_account_id"), "credit")):
        if not aid: continue
        try:
            r = (
                _admin().table("accounts")
                .select("code, name")
                .eq("org_id", org_id).eq("id", aid).limit(1).execute()
            ).data
            if r:
                if key == "debit": debit_acc = r[0]
                else:              credit_acc = r[0]
        except Exception:
            pass

    return Response(
        _render_voucher(txn, (s or [{}])[0], debit_acc, credit_acc),
        mimetype="text/html",
    )


# ---------------------------------------------------------------------------
# CORE: apply a transaction (updates account balances + inserts row)
# ---------------------------------------------------------------------------
def _apply_txn(org_id: str, spec: dict) -> dict:
    """
    spec: type, category, amount, currency?, method?, reference?, payee?,
          narration?, txn_date?, debit_account_id?, credit_account_id?,
          meta?, created_by?
    """
    admin = _admin()

    amount = _to_decimal(spec["amount"])
    if amount <= 0:
        raise ValueError("amount must be > 0")

    txn_type  = spec["type"]
    category  = spec["category"]
    debit_id  = spec.get("debit_account_id")
    credit_id = spec.get("credit_account_id")

    # fetch the two accounts we're about to move
    def _fetch_acc(aid):
        if not aid: return None
        r = (
            admin.table("accounts")
            .select("id, code, name, category, balance, is_active, currency")
            .eq("org_id", org_id).eq("id", aid).limit(1).execute()
        ).data
        return r[0] if r else None

    debit_acc  = _fetch_acc(debit_id)
    credit_acc = _fetch_acc(credit_id)

    if debit_id and not debit_acc:
        raise RuntimeError("Debit account not found")
    if credit_id and not credit_acc:
        raise RuntimeError("Credit account not found")

    currency = spec.get("currency") or (
        (debit_acc or credit_acc or {}).get("currency") or "UGX"
    )

    txn_number = _next_txn_number(org_id)

    row = {
        "org_id":            org_id,
        "txn_number":        txn_number,
        "type":              txn_type,
        "category":          category,
        "amount":            float(_q(amount)),
        "currency":          currency,
        "debit_account_id":  debit_id,
        "credit_account_id": credit_id,
        "method":            spec.get("method"),
        "reference":         spec.get("reference"),
        "payee":             spec.get("payee"),
        "narration":         spec.get("narration"),
        "txn_date":          (spec.get("txn_date") or _today()).isoformat()
                             if isinstance(spec.get("txn_date") or _today(), date)
                             else str(spec.get("txn_date")),
        "status":            "completed",
        "meta":              spec.get("meta") or {},
        "created_by":        spec.get("created_by") or _user_id(),
    }

    try:
        resp = admin.table("account_transactions").insert(row).execute()
    except Exception as e:
        log.exception("txn insert failed")
        raise RuntimeError(f"ledger insert failed: {e}")

    txn = resp.data[0]

    # update balances
    # For income: debit asset (cash/bank) increases, credit income increases
    # For expense: debit expense increases, credit asset decreases
    # Convention: DEBIT increases assets & expenses; CREDIT increases income & liabilities
    # So the balance update rule depends on the account's own category:
    #   asset / expense  -> debit increases balance
    #   income / liability / equity -> credit increases balance
    def _new_balance(acc, is_debit):
        if not acc:
            return None
        bal = _to_decimal(acc.get("balance"))
        cat = acc.get("category")
        delta = amount if is_debit else -amount
        if cat in ("income", "liability", "equity"):
            # credit-positive: credit increases, debit decreases
            delta = -delta
        return _q(bal + delta)

    try:
        if debit_acc:
            nb = _new_balance(debit_acc, True)
            admin.table("accounts").update({
                "balance": float(nb), "updated_at": _now_iso(),
            }).eq("id", debit_acc["id"]).execute()
        if credit_acc:
            nb = _new_balance(credit_acc, False)
            admin.table("accounts").update({
                "balance": float(nb), "updated_at": _now_iso(),
            }).eq("id", credit_acc["id"]).execute()
    except Exception:
        log.exception("balance update failed (non-fatal)")

    return {
        "id":           txn["id"],
        "txn_number":   txn_number,
        "type":         txn_type,
        "category":     category,
        "amount":       float(_q(amount)),
        "currency":     currency,
        "txn_date":     row["txn_date"],
    }


# ---------------------------------------------------------------------------
# POST: expense
# ---------------------------------------------------------------------------
@accounts_bp.route("/api/expenses", methods=["POST"])
@role_required("admin")
def api_expense():
    """
    Body:
    {
      category,            -- see EXPENSE_CATEGORIES
      amount,
      payee,
      method: 'cash'|'bank'|'online'|'internal',
      expense_account_id,  -- which expense account to debit
      cash_account_id?,    -- which asset to credit; default: cash on hand (code 1000)
      reference?, narration?, txn_date?
    }
    """
    org_id = _org_id()
    data = request.get_json(silent=True) or {}

    category = (data.get("category") or "").strip()
    if category not in EXPENSE_CATEGORIES:
        return _bad("Invalid expense category", reason="bad_category")

    try:
        amount = _to_decimal(data.get("amount"))
        if amount <= 0:
            return _bad("amount must be > 0", reason="bad_amount")
    except ValueError:
        return _bad("Invalid amount", reason="bad_amount")

    expense_account_id = (data.get("expense_account_id") or "").strip()
    if not expense_account_id:
        return _bad("expense_account_id required", reason="missing_account")

    cash_account_id = (data.get("cash_account_id") or "").strip()
    if not cash_account_id:
        # default: code 1000
        try:
            r = (
                _admin().table("accounts")
                .select("id")
                .eq("org_id", org_id).eq("code", "1000").limit(1).execute()
            ).data
            if r: cash_account_id = r[0]["id"]
        except Exception:
            pass
    if not cash_account_id:
        return _bad("cash_account_id required", reason="missing_cash_account")

    method = (data.get("method") or "cash").lower()
    if method not in ("cash","bank","online","internal"):
        return _bad("Invalid method", reason="bad_method")

    try:
        result = _apply_txn(org_id, {
            "type":              "expense",
            "category":          category,
            "amount":            str(amount),
            "debit_account_id":  expense_account_id,
            "credit_account_id": cash_account_id,
            "method":            method,
            "payee":             (data.get("payee") or "").strip() or None,
            "reference":         (data.get("reference") or "").strip() or None,
            "narration":         (data.get("narration") or "").strip() or None,
            "txn_date":          data.get("txn_date") or _today(),
            "created_by":        _user_id(),
        })
    except Exception as e:
        log.exception("expense failed")
        return _bad(f"Could not record expense: {e}", 500, reason="apply_failed")

    return jsonify({"ok": True, "transaction": result}), 201


# ---------------------------------------------------------------------------
# POST: other income
# ---------------------------------------------------------------------------
@accounts_bp.route("/api/incomes", methods=["POST"])
@role_required("admin")
def api_income():
    """
    Body:
    {
      category,           -- see INCOME_CATEGORIES
      amount,
      payer,
      method: 'cash'|'bank'|'online'|'internal',
      income_account_id,  -- which income account to credit
      cash_account_id?,   -- which asset to debit; default: cash on hand (code 1000)
      reference?, narration?, txn_date?
    }
    """
    org_id = _org_id()
    data = request.get_json(silent=True) or {}

    category = (data.get("category") or "").strip()
    if category not in INCOME_CATEGORIES:
        return _bad("Invalid income category", reason="bad_category")

    try:
        amount = _to_decimal(data.get("amount"))
        if amount <= 0:
            return _bad("amount must be > 0", reason="bad_amount")
    except ValueError:
        return _bad("Invalid amount", reason="bad_amount")

    income_account_id = (data.get("income_account_id") or "").strip()
    if not income_account_id:
        return _bad("income_account_id required", reason="missing_account")

    cash_account_id = (data.get("cash_account_id") or "").strip()
    if not cash_account_id:
        try:
            r = (
                _admin().table("accounts")
                .select("id")
                .eq("org_id", org_id).eq("code", "1000").limit(1).execute()
            ).data
            if r: cash_account_id = r[0]["id"]
        except Exception:
            pass
    if not cash_account_id:
        return _bad("cash_account_id required", reason="missing_cash_account")

    method = (data.get("method") or "cash").lower()
    if method not in ("cash","bank","online","internal"):
        return _bad("Invalid method", reason="bad_method")

    try:
        result = _apply_txn(org_id, {
            "type":              "income",
            "category":          category,
            "amount":            str(amount),
            # income: debit asset, credit income
            "debit_account_id":  cash_account_id,
            "credit_account_id": income_account_id,
            "method":            method,
            "payee":             (data.get("payer") or "").strip() or None,
            "reference":         (data.get("reference") or "").strip() or None,
            "narration":         (data.get("narration") or "").strip() or None,
            "txn_date":          data.get("txn_date") or _today(),
            "created_by":        _user_id(),
        })
    except Exception as e:
        log.exception("income failed")
        return _bad(f"Could not record income: {e}", 500, reason="apply_failed")

    return jsonify({"ok": True, "transaction": result}), 201


# ---------------------------------------------------------------------------
# POST: transfer (cash <-> bank <-> mobile)
# ---------------------------------------------------------------------------
@accounts_bp.route("/api/transfers", methods=["POST"])
@role_required("admin")
def api_transfer():
    """
    Body:
    {
      from_account_id, to_account_id, amount,
      reference?, narration?, txn_date?
    }
    """
    org_id = _org_id()
    data = request.get_json(silent=True) or {}

    from_id = (data.get("from_account_id") or "").strip()
    to_id   = (data.get("to_account_id") or "").strip()
    if not from_id or not to_id:
        return _bad("from_account_id and to_account_id are required",
                    reason="missing_accounts")
    if from_id == to_id:
        return _bad("from and to must differ", reason="same_account")

    try:
        amount = _to_decimal(data.get("amount"))
        if amount <= 0:
            return _bad("amount must be > 0", reason="bad_amount")
    except ValueError:
        return _bad("Invalid amount", reason="bad_amount")

    try:
        result = _apply_txn(org_id, {
            "type":              "transfer",
            "category":          "cash_transfer",
            "amount":            str(amount),
            "debit_account_id":  to_id,      # money in
            "credit_account_id": from_id,    # money out
            "method":            "internal",
            "reference":         (data.get("reference") or "").strip() or None,
            "narration":         (data.get("narration") or "").strip() or None,
            "txn_date":          data.get("txn_date") or _today(),
            "created_by":        _user_id(),
        })
    except Exception as e:
        log.exception("transfer failed")
        return _bad(f"Could not transfer: {e}", 500, reason="apply_failed")

    return jsonify({"ok": True, "transaction": result}), 201


# ---------------------------------------------------------------------------
# REVERSE
# ---------------------------------------------------------------------------
@accounts_bp.route("/api/transactions/<uuid:txn_uuid>/reverse", methods=["POST"])
@role_required("admin")
def api_reverse(txn_uuid):
    org_id = _org_id()
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip() or None
    if not reason:
        return _bad("reason is required", reason="missing_reason")

    try:
        txn = (
            _admin().table("account_transactions")
            .select("*")
            .eq("org_id", org_id).eq("id", str(txn_uuid)).limit(1).execute()
        ).data
    except Exception:
        return _bad("Could not load transaction", 500)
    if not txn:
        return _bad("Transaction not found", 404)
    txn = txn[0]

    if txn["status"] != "completed":
        return _bad(f"Cannot reverse a transaction in status '{txn['status']}'",
                    400, reason="bad_state")

    amount = _to_decimal(txn["amount"])

    # Apply the opposite postings
    def _fetch_acc(aid):
        if not aid: return None
        r = (
            _admin().table("accounts").select("*")
            .eq("org_id", org_id).eq("id", aid).limit(1).execute()
        ).data
        return r[0] if r else None

    debit_acc  = _fetch_acc(txn.get("debit_account_id"))
    credit_acc = _fetch_acc(txn.get("credit_account_id"))

    def _new_balance(acc, is_debit, amt):
        if not acc: return None
        bal = _to_decimal(acc.get("balance"))
        cat = acc.get("category")
        delta = amt if is_debit else -amt
        if cat in ("income", "liability", "equity"):
            delta = -delta
        return _q(bal + delta)

    try:
        # undo debit (was +, now -)
        if debit_acc:
            nb = _new_balance(debit_acc, False, amount)
            _admin().table("accounts").update({
                "balance": float(nb), "updated_at": _now_iso(),
            }).eq("id", debit_acc["id"]).execute()
        # undo credit (was -, now +)
        if credit_acc:
            nb = _new_balance(credit_acc, True, amount)
            _admin().table("accounts").update({
                "balance": float(nb), "updated_at": _now_iso(),
            }).eq("id", credit_acc["id"]).execute()

        _admin().table("account_transactions").update({
            "status":          "reversed",
            "reversed_at":     _now_iso(),
            "reversed_by":     _user_id(),
            "reversal_reason": reason,
            "updated_at":      _now_iso(),
        }).eq("id", txn["id"]).execute()
    except Exception as e:
        log.exception("reverse failed")
        return _bad(f"Could not reverse: {e}", 500, reason="db_error")

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# SUMMARIES
# ---------------------------------------------------------------------------
def _period_summary(org_id: str, txn_type: str, start: date, end: date) -> list[dict]:
    rows = (
        _admin().table("account_transactions")
        .select("category, amount, currency")
        .eq("org_id", org_id).eq("type", txn_type).eq("status", "completed")
        .gte("txn_date", start.isoformat())
        .lte("txn_date", end.isoformat())
        .execute()
    ).data or []

    agg: dict = {}
    for r in rows:
        cat = r.get("category") or "other"
        b = agg.setdefault(cat, {"amount": ZERO, "count": 0, "currency": r.get("currency") or "UGX"})
        b["amount"] += _to_decimal(r.get("amount"))
        b["count"]  += 1

    out = []
    for cat, b in agg.items():
        out.append({
            "category": cat,
            "amount":   float(_q(b["amount"])),
            "count":    b["count"],
            "currency": b["currency"],
        })
    out.sort(key=lambda x: -x["amount"])
    return out


@accounts_bp.route("/api/summary/expenses", methods=["GET"])
@role_required("admin")
def api_summary_expenses():
    org_id = _org_id()
    today = _today()
    try:
        start = _parse_date(request.args.get("from")) or date(today.year, today.month, 1)
        end   = _parse_date(request.args.get("to"))   or today
    except Exception:
        start = date(today.year, today.month, 1)
        end = today

    items = _period_summary(org_id, "expense", start, end)
    total = sum((Decimal(str(i["amount"])) for i in items), ZERO)
    return jsonify({
        "ok": True,
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "items": items,
        "total": float(_q(total)),
    })


@accounts_bp.route("/api/summary/incomes", methods=["GET"])
@role_required("admin")
def api_summary_incomes():
    org_id = _org_id()
    today = _today()
    try:
        start = _parse_date(request.args.get("from")) or date(today.year, today.month, 1)
        end   = _parse_date(request.args.get("to"))   or today
    except Exception:
        start = date(today.year, today.month, 1)
        end = today

    items = _period_summary(org_id, "income", start, end)
    total = sum((Decimal(str(i["amount"])) for i in items), ZERO)
    return jsonify({
        "ok": True,
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "items": items,
        "total": float(_q(total)),
    })


# ---------------------------------------------------------------------------
# EXPORT
# ---------------------------------------------------------------------------
@accounts_bp.route("/api/export", methods=["GET"])
@role_required("admin")
def api_export():
    org_id = _org_id()
    txn_type = (request.args.get("type") or "").strip()
    date_from = request.args.get("from")
    date_to   = request.args.get("to")

    q = (
        _admin().table("account_transactions")
        .select("txn_number, type, category, amount, currency, method, "
                "payee, reference, narration, txn_date, status, created_at")
        .eq("org_id", org_id)
        .order("txn_date", desc=True)
    )
    if txn_type: q = q.eq("type", txn_type)
    if date_from:q = q.gte("txn_date", date_from)
    if date_to:  q = q.lte("txn_date", date_to)

    try:
        rows = q.execute().data or []
    except Exception as e:
        log.exception("export failed")
        return _bad("Could not export", 500)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Voucher #","Date","Type","Category","Amount","Currency",
                "Method","Payee","Reference","Narration","Status"])
    for r in rows:
        w.writerow([
            r.get("txn_number"), r.get("txn_date"), r.get("type"),
            r.get("category"), r.get("amount"), r.get("currency"),
            r.get("method"), r.get("payee"), r.get("reference"),
            (r.get("narration") or "").replace("\n", " "),
            r.get("status"),
        ])

    filename = f"accounts_transactions_{_today().isoformat()}.csv"
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/csv; charset=utf-8",
        },
    )
    
    
# ---------------------------------------------------------------------------
# PUBLIC HELPER — used by other modules to post to the chart of accounts
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# PUBLIC HELPER — other modules call this to post into the chart of accounts
# ---------------------------------------------------------------------------
def record_asset_movement(
    *,
    org_id: str,
    direction: str,                 # 'in' | 'out'
    amount,
    method: str,                    # 'cash' | 'bank' | 'online' | 'mobile'
    counterpart_account_id: str | None = None,
    counterpart_account_code: str | None = None,
    category: str = "other_adjustment",
    narration: str | None = None,
    reference: str | None = None,
    txn_date=None,
    created_by: str | None = None,
    meta: dict | None = None,
):
    """
    Post a simple asset movement to the chart of accounts.
    direction='in'  -> debit asset, credit counterpart
    direction='out' -> credit asset, debit counterpart
    Failures are logged, not raised (caller's ledger is still source of truth).
    """
    try:
        admin = _admin()

        code_map = {
            "cash":     "1000",
            "bank":     "1010",
            "online":   "1010",     # online money lands in the bank
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
            log.warning("asset account %s not found for org %s; skipping post",
                        asset_code, org_id)
            return None
        asset_id = asset_row[0]["id"]

        counter_id = counterpart_account_id
        if not counter_id and counterpart_account_code:
            cr = (
                admin.table("accounts")
                .select("id")
                .eq("org_id", org_id)
                .eq("code", counterpart_account_code).limit(1).execute()
            ).data
            if cr:
                counter_id = cr[0]["id"]

        if direction == "in":
            debit_id, credit_id = asset_id, counter_id
        elif direction == "out":
            debit_id, credit_id = counter_id, asset_id
        else:
            log.warning("record_asset_movement: invalid direction %s", direction)
            return None

        return _apply_txn(org_id, {
            "type":              "income" if direction == "in" else "expense",
            "category":          category,
            "amount":            str(amount),
            "debit_account_id":  debit_id,
            "credit_account_id": credit_id,
            "method":            method,
            "reference":         reference,
            "narration":         narration,
            "txn_date":          txn_date or _today(),
            "meta":              meta or {},
            "created_by":        created_by,
        })
    except Exception:
        log.exception("record_asset_movement failed (non-fatal for caller)")
        return None
    

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
    counterpart_account_code: str,   # e.g. '2000' savings payable, '2010' FD payable, '3000' share capital
    category: str = "other_adjustment",
    narration: str | None = None,
    reference: str | None = None,
    txn_date=None,
    created_by: str | None = None,
    meta: dict | None = None,
):
    """
    Post a savings/FD/share movement where the counterpart is a
    liability or equity account.

    direction='in'  (deposit / share purchase / FD top-up):
        DEBIT  asset  (cash or bank)         [asset increases]
        CREDIT liability/equity              [liability increases]

    direction='out' (withdrawal / share redemption / FD withdrawal):
        CREDIT asset  (cash or bank)         [asset decreases]
        DEBIT  liability/equity              [liability decreases]

    Returns the created transaction row, or None on soft failure.
    Failures are logged and swallowed so the caller's own ledger stays
    authoritative.
    """
    try:
        admin = _admin()

        # Resolve the asset account by method
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

        # Debit / credit assignment
        #   in  (deposit): Dr asset, Cr liability
        #   out (withdraw): Dr liability, Cr asset
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
            "txn_date":          txn_date or _today(),
            "meta":              meta or {},
            "created_by":        created_by,
        })
        return result
    except Exception:
        log.exception("record_liability_movement failed (non-fatal)")
        return None