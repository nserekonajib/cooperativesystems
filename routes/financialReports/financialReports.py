"""
Financial Reports blueprint with drill-through.

Endpoints (admin-only):
    GET    /reports/                                     -> page
    GET    /reports/api/catalog                          -> list of available reports
    GET    /reports/api/run?report=<key>&params          -> run a report (JSON)
    GET    /reports/api/export?report=<key>&params       -> CSV download
    GET    /reports/api/drill                            -> drill-down for a report row
    GET    /reports/api/accounts                         -> helper: list accounts
    GET    /reports/api/members/search                   -> helper: lookup for member reports

Every report returns the same envelope:
    {
      "ok": true,
      "report": "trial_balance",
      "title":  "Trial Balance",
      "meta":   { ... },
      "columns": [ {"key": "code", "label": "Code", "type": "text"}, ... ],
      "rows":    [ {..., "_drill": {...}}, ... ],           # _drill optional
      "totals":  {...},
      "groups":  [ {"label": "Assets", "rows": [...], "totals": {...}} ]
    }

Drill spec (`_drill`):
    {
      "kind":   "account_transactions" | "loans" | "repayments"
                | "savings_accounts" | "shares" | "subscriptions"
                | "member_transactions",
      "filter": { ... column-value pairs used in a WHERE },
      "title":  "Human-readable label for the modal header"
    }

The frontend, when a row is clicked, GETs /reports/api/drill with the
drill spec as JSON in the `d` query parameter. The endpoint resolves the
kind to a Supabase query and returns columns + rows in the same envelope
shape, so the same renderer draws the modal.
"""

import os
import io
import csv
import json
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
        "%(asctime)s [%(levelname)s] reports: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

financial_reports_bp = Blueprint(
    "financial_reports", __name__, url_prefix="/reports",
)

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


def _parse_date(v, default=None):
    if not v:
        return default
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return default


def _period_from_args(default_from_days: int = 30):
    today = _today()
    to_d  = _parse_date(request.args.get("to"), today)
    from_d = _parse_date(request.args.get("from"),
                         to_d - timedelta(days=default_from_days))
    return from_d, to_d


def _float(v) -> float:
    return float(_q(v))


def _org_info(org_id: str) -> dict:
    try:
        s = (
            _admin().table("institution_settings")
            .select("display_name, address_line, city, country, "
                    "support_phone, support_email, currency")
            .eq("org_id", org_id).limit(1).execute()
        ).data or [{}]
        return s[0]
    except Exception:
        log.exception("org info fetch failed")
        return {}


def _all_accounts(org_id: str) -> list[dict]:
    try:
        return (
            _admin().table("accounts")
            .select("id, code, name, category, subcategory, balance, "
                    "is_cash_account, is_bank_account, currency")
            .eq("org_id", org_id)
            .order("code")
            .execute()
        ).data or []
    except Exception:
        log.exception("accounts fetch failed")
        return []


# ===========================================================================
# DRILL HELPERS
# ===========================================================================
def _drill_account_transactions(org_id: str, flt: dict) -> dict:
    """Filtering against account_transactions."""
    admin = _admin()
    q = (
        admin.table("account_transactions")
        .select("txn_number, txn_date, type, category, amount, currency, "
                "method, payee, reference, narration, status, "
                "debit_account_id, credit_account_id")
        .eq("org_id", org_id)
    )
    date_from = _parse_date(flt.get("from"))
    date_to   = _parse_date(flt.get("to"))
    if date_from: q = q.gte("txn_date", date_from.isoformat())
    if date_to:   q = q.lte("txn_date", date_to.isoformat())
    if flt.get("category"): q = q.eq("category", flt["category"])
    if flt.get("type"):     q = q.eq("type", flt["type"])
    if flt.get("status"):   q = q.eq("status", flt["status"])

    # account_id means "either side"
    account_id = flt.get("account_id")
    if account_id:
        q = q.or_(f"debit_account_id.eq.{account_id},credit_account_id.eq.{account_id}")

    # filter by debit or credit side code
    debit_code  = flt.get("debit_code")
    credit_code = flt.get("credit_code")
    all_accts = {a["code"]: a for a in _all_accounts(org_id)}
    if debit_code and debit_code in all_accts:
        q = q.eq("debit_account_id", all_accts[debit_code]["id"])
    if credit_code and credit_code in all_accts:
        q = q.eq("credit_account_id", all_accts[credit_code]["id"])

    rows = q.order("txn_date", desc=True).limit(1000).execute().data or []

    accts_by_id = {a["id"]: a for a in _all_accounts(org_id)}
    out = []
    total = ZERO
    for r in rows:
        d = accts_by_id.get(r.get("debit_account_id")) or {}
        c = accts_by_id.get(r.get("credit_account_id")) or {}
        amt = _to_decimal(r.get("amount"))
        total += amt
        out.append({
            "txn_date":   r.get("txn_date"),
            "txn_number": r.get("txn_number"),
            "type":       r.get("type"),
            "category":   r.get("category"),
            "debit":      d.get("code") or "",
            "debit_name": d.get("name") or "",
            "credit":     c.get("code") or "",
            "credit_name":c.get("name") or "",
            "amount":     _float(amt),
            "payee":      r.get("payee") or "",
            "narration":  r.get("narration") or "",
            "status":     r.get("status"),
        })

    return {
        "columns": [
            {"key": "txn_date",    "label": "Date",      "type": "date"},
            {"key": "txn_number",  "label": "Voucher",   "type": "text"},
            {"key": "type",        "label": "Type",      "type": "text"},
            {"key": "category",    "label": "Category",  "type": "text"},
            {"key": "debit",       "label": "Dr",        "type": "text"},
            {"key": "credit",      "label": "Cr",        "type": "text"},
            {"key": "amount",      "label": "Amount",    "type": "money", "align": "right"},
            {"key": "payee",       "label": "Payee",     "type": "text"},
            {"key": "narration",   "label": "Narration", "type": "text"},
        ],
        "rows":   out,
        "totals": {"amount": _float(total)},
    }


def _drill_loans(org_id: str, flt: dict) -> dict:
    admin = _admin()
    q = (
        admin.table("loans")
        .select("loan_number, principal_amount, total_payable, "
                "outstanding_principal, outstanding_interest, "
                "outstanding_penalties, collected_principal, "
                "collected_interest, collected_penalties, collected_fees, "
                "currency, disbursed_at, maturity_date, status, "
                "members!inner(member_id, first_name, last_name, phone)")
        .eq("org_id", org_id)
    )
    if flt.get("status"): q = q.eq("status", flt["status"])
    if flt.get("bucket"):
        # bucket = current / 1_30 / 31_60 / 61_90 / 90_plus
        # computed inline; simplest: filter after fetch
        pass
    rows = q.order("disbursed_at", desc=True).limit(2000).execute().data or []

    # bucket filter (post-fetch, uses same rules as aging)
    bucket = flt.get("bucket")
    if bucket:
        reps = (
            admin.table("repayments")
            .select("loan_id, paid_at")
            .eq("org_id", org_id).eq("status", "completed")
            .order("paid_at", desc=True).execute()
        ).data or []
        last_pay = {}
        for r in reps:
            if r["loan_id"] not in last_pay:
                last_pay[r["loan_id"]] = _parse_date(r["paid_at"])

        today = _today()
        def _bucket_of(l):
            d = last_pay.get(l.get("id")) or _parse_date(l.get("disbursed_at"))
            days = (today - d).days if d else 999
            if days <= 30:     return "current"
            if days <= 60:     return "1_30"
            if days <= 90:     return "31_60"
            if days <= 180:    return "61_90"
            return "90_plus"
        rows = [l for l in rows if _bucket_of(l) == bucket]

    out = []
    total = ZERO
    for l in rows:
        m = l.get("members") or {}
        outstanding = (_to_decimal(l.get("outstanding_principal"))
                       + _to_decimal(l.get("outstanding_interest"))
                       + _to_decimal(l.get("outstanding_penalties")))
        total += outstanding
        out.append({
            "loan_number":  l.get("loan_number"),
            "member_id":    m.get("member_id"),
            "member":       f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            "principal":    _float(l.get("principal_amount")),
            "outstanding":  _float(outstanding),
            "disbursed_at": l.get("disbursed_at"),
            "maturity_date": l.get("maturity_date"),
            "status":       l.get("status"),
        })

    return {
        "columns": [
            {"key": "loan_number",   "label": "Loan #",      "type": "text"},
            {"key": "member_id",     "label": "Member ID",   "type": "text"},
            {"key": "member",        "label": "Member",      "type": "text"},
            {"key": "principal",     "label": "Principal",   "type": "money", "align": "right"},
            {"key": "outstanding",   "label": "Outstanding", "type": "money", "align": "right"},
            {"key": "disbursed_at",  "label": "Disbursed",   "type": "date"},
            {"key": "maturity_date", "label": "Maturity",    "type": "date"},
            {"key": "status",        "label": "Status",      "type": "text"},
        ],
        "rows":   out,
        "totals": {"outstanding": _float(total)},
    }


def _drill_repayments(org_id: str, flt: dict) -> dict:
    admin = _admin()
    q = (
        admin.table("repayments")
        .select("receipt_number, amount, currency, method, provider, "
                "status, paid_at, "
                "members!inner(member_id, first_name, last_name), "
                "loans!inner(loan_number), "
                "repayment_allocations(to_interest, to_principal, "
                "to_penalties, to_fees)")
        .eq("org_id", org_id)
    )
    date_from = _parse_date(flt.get("from"))
    date_to   = _parse_date(flt.get("to"))
    if date_from: q = q.gte("paid_at", date_from.isoformat())
    if date_to:   q = q.lte("paid_at", date_to.isoformat() + "T23:59:59")
    if flt.get("status"): q = q.eq("status", flt["status"])
    if flt.get("method"): q = q.eq("method", flt["method"])

    rows = q.order("paid_at", desc=True).limit(2000).execute().data or []

    out = []
    tot = ZERO
    for r in rows:
        m = r.get("members") or {}
        l = r.get("loans") or {}
        a = (r.get("repayment_allocations") or [{}])[0]
        amt = _to_decimal(r.get("amount"))
        tot += amt
        out.append({
            "date":        r.get("paid_at"),
            "receipt":     r.get("receipt_number"),
            "member":      f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            "member_id":   m.get("member_id"),
            "loan_number": l.get("loan_number"),
            "amount":      _float(amt),
            "interest":    _float(a.get("to_interest") or 0),
            "principal":   _float(a.get("to_principal") or 0),
            "penalties":   _float(a.get("to_penalties") or 0),
            "fees":        _float(a.get("to_fees") or 0),
            "method":      r.get("method"),
        })

    return {
        "columns": [
            {"key": "date",        "label": "Date",      "type": "date"},
            {"key": "receipt",     "label": "Receipt",   "type": "text"},
            {"key": "member",      "label": "Member",    "type": "text"},
            {"key": "loan_number", "label": "Loan",      "type": "text"},
            {"key": "amount",      "label": "Amount",    "type": "money", "align": "right"},
            {"key": "interest",    "label": "Interest",  "type": "money", "align": "right"},
            {"key": "principal",   "label": "Principal", "type": "money", "align": "right"},
            {"key": "penalties",   "label": "Penalties", "type": "money", "align": "right"},
            {"key": "fees",        "label": "Fees",      "type": "money", "align": "right"},
            {"key": "method",      "label": "Method",    "type": "text"},
        ],
        "rows":   out,
        "totals": {"amount": _float(tot)},
    }


def _drill_savings_accounts(org_id: str, flt: dict) -> dict:
    q = (
        _admin().table("member_accounts")
        .select("account_number, balance, currency, status, opened_at, "
                "members!inner(member_id, first_name, last_name, phone)")
        .eq("org_id", org_id).eq("account_type", "savings")
        .order("balance", desc=True).limit(5000)
    )
    rows = q.execute().data or []

    out = []
    total = ZERO
    for a in rows:
        m = a.get("members") or {}
        bal = _to_decimal(a.get("balance"))
        total += bal
        out.append({
            "account_number": a.get("account_number"),
            "member_id":      m.get("member_id"),
            "member":         f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            "phone":          m.get("phone"),
            "balance":        _float(bal),
            "status":         a.get("status"),
            "opened_at":      a.get("opened_at"),
        })

    return {
        "columns": [
            {"key": "account_number", "label": "Account #", "type": "text"},
            {"key": "member_id",      "label": "Member ID", "type": "text"},
            {"key": "member",         "label": "Member",    "type": "text"},
            {"key": "phone",          "label": "Phone",     "type": "text"},
            {"key": "balance",        "label": "Balance",   "type": "money", "align": "right"},
            {"key": "status",         "label": "Status",    "type": "text"},
            {"key": "opened_at",      "label": "Opened",    "type": "date"},
        ],
        "rows":   out,
        "totals": {"balance": _float(total)},
    }


def _drill_shares(org_id: str, flt: dict) -> dict:
    rows = (
        _admin().table("members")
        .select("member_id, first_name, last_name, phone, status, "
                "shares_units, shares_value, joined_at")
        .eq("org_id", org_id)
        .order("shares_units", desc=True).limit(5000)
        .execute().data or []
    )
    out = []
    units = value = ZERO
    for m in rows:
        u = _to_decimal(m.get("shares_units"))
        v = _to_decimal(m.get("shares_value"))
        units += u
        value += v
        out.append({
            "member_id":    m.get("member_id"),
            "member":       f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            "phone":        m.get("phone"),
            "shares_units": _float(u),
            "shares_value": _float(v),
            "status":       m.get("status"),
            "joined_at":    m.get("joined_at"),
        })
    return {
        "columns": [
            {"key": "member_id",    "label": "Member ID",   "type": "text"},
            {"key": "member",       "label": "Member",      "type": "text"},
            {"key": "phone",        "label": "Phone",       "type": "text"},
            {"key": "shares_units", "label": "Units",       "type": "number", "align": "right"},
            {"key": "shares_value", "label": "Share Value", "type": "money",  "align": "right"},
            {"key": "status",       "label": "Status",      "type": "text"},
            {"key": "joined_at",    "label": "Joined",      "type": "date"},
        ],
        "rows":   out,
        "totals": {"shares_units": _float(units), "shares_value": _float(value)},
    }


def _drill_subscriptions(org_id: str, flt: dict) -> dict:
    year = flt.get("year") or _today().year
    try: year = int(year)
    except: year = _today().year

    rows = (
        _admin().table("yearly_subscriptions")
        .select("subscription_year, amount_due, amount_paid, currency, "
                "status, source, due_date, paid_at, "
                "members!inner(member_id, first_name, last_name, phone)")
        .eq("org_id", org_id).eq("subscription_year", year)
        .order("created_at", desc=True).limit(5000)
        .execute().data or []
    )
    out = []
    due_total = paid_total = ZERO
    for r in rows:
        m = r.get("members") or {}
        due  = _to_decimal(r.get("amount_due") or 0)
        paid = _to_decimal(r.get("amount_paid") or 0)
        out.append({
            "member_id": m.get("member_id"),
            "member":    f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            "phone":     m.get("phone"),
            "year":      r.get("subscription_year"),
            "amount_due":  _float(due),
            "amount_paid": _float(paid),
            "balance":     _float(due - paid),
            "status":      r.get("status"),
            "source":      r.get("source"),
        })
        due_total  += due
        paid_total += paid

    return {
        "columns": [
            {"key": "member_id",   "label": "Member ID", "type": "text"},
            {"key": "member",      "label": "Member",    "type": "text"},
            {"key": "phone",       "label": "Phone",     "type": "text"},
            {"key": "year",        "label": "Year",      "type": "number", "align": "right"},
            {"key": "amount_due",  "label": "Due",       "type": "money", "align": "right"},
            {"key": "amount_paid", "label": "Paid",      "type": "money", "align": "right"},
            {"key": "balance",     "label": "Balance",   "type": "money", "align": "right"},
            {"key": "status",      "label": "Status",    "type": "text"},
        ],
        "rows":   out,
        "totals": {"amount_due": _float(due_total), "amount_paid": _float(paid_total),
                   "balance": _float(due_total - paid_total)},
    }


def _drill_member_transactions(org_id: str, flt: dict) -> dict:
    member_id = flt.get("member_id")
    if not member_id:
        return {"columns": [], "rows": [], "totals": {}}

    q = (
        _admin().table("member_transactions")
        .select("created_at, type, method, amount, currency, status, "
                "narration, balance_after, provider_ref, account_id")
        .eq("org_id", org_id).eq("member_id", member_id)
    )
    date_from = _parse_date(flt.get("from"))
    date_to   = _parse_date(flt.get("to"))
    if date_from: q = q.gte("created_at", date_from.isoformat())
    if date_to:   q = q.lte("created_at", date_to.isoformat() + "T23:59:59")
    if flt.get("type"):   q = q.eq("type", flt["type"])
    if flt.get("status"): q = q.eq("status", flt["status"])

    rows = q.order("created_at", desc=True).limit(2000).execute().data or []
    out = []
    total = ZERO
    for r in rows:
        amt = _to_decimal(r.get("amount"))
        total += amt
        out.append({
            "date":       r.get("created_at"),
            "type":       r.get("type"),
            "method":     r.get("method"),
            "amount":     _float(amt),
            "currency":   r.get("currency"),
            "status":     r.get("status"),
            "narration":  r.get("narration"),
        })
    return {
        "columns": [
            {"key": "date",      "label": "Date",      "type": "date"},
            {"key": "type",      "label": "Type",      "type": "text"},
            {"key": "method",    "label": "Method",    "type": "text"},
            {"key": "amount",    "label": "Amount",    "type": "money", "align": "right"},
            {"key": "status",    "label": "Status",    "type": "text"},
            {"key": "narration", "label": "Narration", "type": "text"},
        ],
        "rows":   out,
        "totals": {"amount": _float(total)},
    }


DRILL_HANDLERS = {
    "account_transactions": _drill_account_transactions,
    "loans":                _drill_loans,
    "repayments":           _drill_repayments,
    "savings_accounts":     _drill_savings_accounts,
    "shares":               _drill_shares,
    "subscriptions":        _drill_subscriptions,
    "member_transactions":  _drill_member_transactions,
}


# ===========================================================================
# REPORT IMPLEMENTATIONS
# ===========================================================================

# ---------- Trial Balance ----------
def r_trial_balance(org_id: str, params: dict) -> dict:
    as_of = _parse_date(params.get("as_of"), _today())
    accts = _all_accounts(org_id)

    rows = []
    total_dr = ZERO
    total_cr = ZERO

    for a in accts:
        bal = _to_decimal(a.get("balance"))
        cat = a.get("category")
        is_debit_normal = cat in ("asset", "expense")
        dr = bal if is_debit_normal and bal >= 0 else ZERO
        cr = -bal if is_debit_normal and bal < 0 else ZERO
        if not is_debit_normal:
            cr = bal if bal >= 0 else ZERO
            dr = -bal if bal < 0 else ZERO

        rows.append({
            "code":      a["code"],
            "name":      a["name"],
            "category":  cat,
            "debit":     _float(dr),
            "credit":    _float(cr),
            "_drill": {
                "kind":  "account_transactions",
                "title": f"{a['code']} · {a['name']}",
                "filter": {
                    "account_id": a["id"],
                    "to":         as_of.isoformat(),
                },
            },
        })
        total_dr += dr
        total_cr += cr

    return {
        "title": "Trial Balance",
        "meta":  {"as_of": as_of.isoformat()},
        "columns": [
            {"key": "code",     "label": "Code",     "type": "text"},
            {"key": "name",     "label": "Account",  "type": "text"},
            {"key": "category", "label": "Category", "type": "text"},
            {"key": "debit",    "label": "Debit",    "type": "money", "align": "right"},
            {"key": "credit",   "label": "Credit",   "type": "money", "align": "right"},
        ],
        "rows":   rows,
        "totals": {"debit": _float(total_dr), "credit": _float(total_cr)},
    }


# ---------- Balance Sheet ----------
def r_balance_sheet(org_id: str, params: dict) -> dict:
    as_of = _parse_date(params.get("as_of"), _today())
    accts = _all_accounts(org_id)

    grouped = {"asset": [], "liability": [], "equity": [], "income": [], "expense": []}
    for a in accts:
        cat = a.get("category")
        if cat in grouped:
            grouped[cat].append(a)

    def _total(cat):
        return sum((_to_decimal(a.get("balance")) for a in grouped[cat]), ZERO)

    total_assets      = _total("asset")
    total_liabilities = _total("liability")
    total_equity      = _total("equity")
    total_income      = _total("income")
    total_expense     = _total("expense")
    net_surplus       = total_income - total_expense

    equity_effective  = total_equity + net_surplus
    liab_plus_equity  = total_liabilities + equity_effective

    def _rows(cat):
        rows = []
        for a in grouped[cat]:
            rows.append({
                "code":   a["code"],
                "name":   a["name"],
                "amount": _float(a.get("balance")),
                "_drill": {
                    "kind":  "account_transactions",
                    "title": f"{a['code']} · {a['name']}",
                    "filter": {"account_id": a["id"], "to": as_of.isoformat()},
                },
            })
        return rows

    return {
        "title": "Balance Sheet",
        "meta":  {
            "as_of": as_of.isoformat(),
            "assets": _float(total_assets),
            "liabilities": _float(total_liabilities),
            "equity": _float(equity_effective),
            "balanced": _q(total_assets) == _q(liab_plus_equity),
        },
        "columns": [
            {"key": "code",   "label": "Code",   "type": "text"},
            {"key": "name",   "label": "Account", "type": "text"},
            {"key": "amount", "label": "Amount", "type": "money", "align": "right"},
        ],
        "groups": [
            {"label": "Assets",       "rows": _rows("asset"),
             "totals": {"amount": _float(total_assets)}},
            {"label": "Liabilities",  "rows": _rows("liability"),
             "totals": {"amount": _float(total_liabilities)}},
            {"label": "Equity",       "rows": _rows("equity"),
             "totals": {"amount": _float(total_equity)}},
            {"label": "Current year surplus (income - expense)",
             "rows": [{
                 "code": "", "name": "Retained this period",
                 "amount": _float(net_surplus),
                 "_drill": {
                     "kind":  "account_transactions",
                     "title": "Income and expenses to date",
                     "filter": {"to": as_of.isoformat(), "status": "completed"},
                 },
             }],
             "totals": {"amount": _float(net_surplus)}},
        ],
        "totals": {
            "assets":      _float(total_assets),
            "liabilities": _float(total_liabilities),
            "equity":      _float(equity_effective),
            "liab_equity": _float(liab_plus_equity),
        },
    }


# ---------- P&L helpers ----------
def _income_expense_rows(org_id: str, start: date, end: date) -> dict:
    def _agg(txn_type):
        rows = (
            _admin().table("account_transactions")
            .select("category, amount")
            .eq("org_id", org_id).eq("type", txn_type).eq("status", "completed")
            .gte("txn_date", start.isoformat())
            .lte("txn_date", end.isoformat())
            .execute()
        ).data or []
        agg = {}
        for r in rows:
            c = r.get("category") or "other"
            b = agg.setdefault(c, {"amount": ZERO, "count": 0})
            b["amount"] += _to_decimal(r.get("amount"))
            b["count"]  += 1
        return [
            {"category": k, "amount": _float(v["amount"]), "count": v["count"]}
            for k, v in sorted(agg.items(), key=lambda x: -x[1]["amount"])
        ]
    return {"income": _agg("income"), "expense": _agg("expense")}


def r_profit_loss(org_id: str, params: dict) -> dict:
    start, end = _period_from_args(30)
    data = _income_expense_rows(org_id, start, end)

    def _with_drill(rows, txn_type):
        out = []
        for r in rows:
            out.append({
                "category": r["category"],
                "amount":   r["amount"],
                "count":    r["count"],
                "_drill": {
                    "kind":  "account_transactions",
                    "title": f"{r['category'].replace('_',' ').title()} ({txn_type})",
                    "filter": {
                        "category": r["category"],
                        "type":     txn_type,
                        "from":     start.isoformat(),
                        "to":       end.isoformat(),
                        "status":   "completed",
                    },
                },
            })
        return out

    income_rows  = _with_drill(data["income"], "income")
    expense_rows = _with_drill(data["expense"], "expense")

    total_income  = sum((Decimal(str(r["amount"])) for r in income_rows),  ZERO)
    total_expense = sum((Decimal(str(r["amount"])) for r in expense_rows), ZERO)
    net = total_income - total_expense

    return {
        "title": "Profit & Loss",
        "meta":  {"from": start.isoformat(), "to": end.isoformat(),
                  "net_surplus": _float(net)},
        "columns": [
            {"key": "category", "label": "Category", "type": "text"},
            {"key": "count",    "label": "Txns",     "type": "number", "align": "right"},
            {"key": "amount",   "label": "Amount",   "type": "money",  "align": "right"},
        ],
        "groups": [
            {"label": "Income",   "rows": income_rows,  "totals": {"amount": _float(total_income)}},
            {"label": "Expenses", "rows": expense_rows, "totals": {"amount": _float(total_expense)}},
            {"label": "Net Surplus / (Deficit)",
             "rows": [{"category": "", "count": "", "amount": _float(net),
                       "_drill": {
                           "kind": "account_transactions",
                           "title": "All income and expense in period",
                           "filter": {"from": start.isoformat(),
                                      "to": end.isoformat(),
                                      "status": "completed"},
                       }}],
             "totals": {"amount": _float(net)}},
        ],
        "totals": {"income": _float(total_income), "expense": _float(total_expense),
                   "net": _float(net)},
    }


def r_income_statement_ytd(org_id: str, params: dict) -> dict:
    today = _today()
    start = date(today.year, 1, 1)
    data  = _income_expense_rows(org_id, start, today)

    def _with_drill(rows, txn_type):
        out = []
        for r in rows:
            out.append({
                "category": r["category"],
                "amount":   r["amount"],
                "count":    r["count"],
                "_drill": {
                    "kind":  "account_transactions",
                    "title": f"{r['category'].replace('_',' ').title()} ({txn_type})",
                    "filter": {
                        "category": r["category"],
                        "type":     txn_type,
                        "from":     start.isoformat(),
                        "to":       today.isoformat(),
                        "status":   "completed",
                    },
                },
            })
        return out

    income_rows  = _with_drill(data["income"], "income")
    expense_rows = _with_drill(data["expense"], "expense")
    total_income  = sum((Decimal(str(r["amount"])) for r in income_rows),  ZERO)
    total_expense = sum((Decimal(str(r["amount"])) for r in expense_rows), ZERO)

    return {
        "title": "Income Statement (YTD)",
        "meta":  {"from": start.isoformat(), "to": today.isoformat(),
                  "year": today.year},
        "columns": [
            {"key": "category", "label": "Category", "type": "text"},
            {"key": "count",    "label": "Txns",     "type": "number", "align": "right"},
            {"key": "amount",   "label": "Amount",   "type": "money",  "align": "right"},
        ],
        "groups": [
            {"label": "Income",   "rows": income_rows,  "totals": {"amount": _float(total_income)}},
            {"label": "Expenses", "rows": expense_rows, "totals": {"amount": _float(total_expense)}},
            {"label": "Net Surplus",
             "rows": [{"category": "", "count": "", "amount": _float(total_income - total_expense)}],
             "totals": {"amount": _float(total_income - total_expense)}},
        ],
        "totals": {"income": _float(total_income), "expense": _float(total_expense),
                   "net": _float(total_income - total_expense)},
    }


# ---------- Cash Flow ----------
def r_cash_flow(org_id: str, params: dict) -> dict:
    start, end = _period_from_args(30)
    admin = _admin()

    cash_accts = (
        admin.table("accounts")
        .select("id, code, name")
        .eq("org_id", org_id)
        .in_("code", ["1000", "1010", "1020"])
        .execute()
    ).data or []
    cash_ids = {a["id"]: a for a in cash_accts}
    if not cash_ids:
        return {"title": "Cash Flow", "meta": {}, "columns": [], "rows": [], "totals": {}}

    rows = (
        admin.table("account_transactions")
        .select("txn_number, txn_date, category, amount, type, "
                "debit_account_id, credit_account_id, narration")
        .eq("org_id", org_id).eq("status", "completed")
        .gte("txn_date", start.isoformat())
        .lte("txn_date", end.isoformat())
        .execute()
    ).data or []

    buckets = {}
    for r in rows:
        dr = r.get("debit_account_id")
        cr = r.get("credit_account_id")
        if dr not in cash_ids and cr not in cash_ids:
            continue
        cat = r.get("category") or "other"
        b = buckets.setdefault(cat, {"in": ZERO, "out": ZERO, "count": 0})
        amt = _to_decimal(r.get("amount"))
        if dr in cash_ids: b["in"]  += amt
        else:              b["out"] += amt
        b["count"] += 1

    out_rows = []
    total_in = total_out = ZERO
    for cat, b in sorted(buckets.items()):
        out_rows.append({
            "category": cat,
            "count":    b["count"],
            "in":       _float(b["in"]),
            "out":      _float(b["out"]),
            "net":      _float(b["in"] - b["out"]),
            "_drill": {
                "kind":  "account_transactions",
                "title": f"{cat.replace('_',' ').title()} (cash movement)",
                "filter": {
                    "category": cat,
                    "from":     start.isoformat(),
                    "to":       end.isoformat(),
                    "status":   "completed",
                },
            },
        })
        total_in  += b["in"]
        total_out += b["out"]

    return {
        "title": "Cash Flow",
        "meta":  {"from": start.isoformat(), "to": end.isoformat()},
        "columns": [
            {"key": "category", "label": "Category",  "type": "text"},
            {"key": "count",    "label": "Txns",      "type": "number", "align": "right"},
            {"key": "in",       "label": "Money in",  "type": "money",  "align": "right"},
            {"key": "out",      "label": "Money out", "type": "money",  "align": "right"},
            {"key": "net",      "label": "Net",       "type": "money",  "align": "right"},
        ],
        "rows": out_rows,
        "totals": {"in": _float(total_in), "out": _float(total_out),
                   "net": _float(total_in - total_out)},
    }


# ---------- General Ledger ----------
def r_ledger(org_id: str, params: dict) -> dict:
    start, end = _period_from_args(30)
    admin = _admin()

    rows = (
        admin.table("account_transactions")
        .select("txn_number, txn_date, type, category, amount, currency, "
                "method, payee, reference, narration, status, "
                "debit_account_id, credit_account_id")
        .eq("org_id", org_id)
        .gte("txn_date", start.isoformat())
        .lte("txn_date", end.isoformat())
        .order("txn_date", desc=True)
        .limit(5000).execute()
    ).data or []

    accounts = {a["id"]: a for a in _all_accounts(org_id)}
    out = []
    for r in rows:
        d = accounts.get(r.get("debit_account_id")) or {}
        c = accounts.get(r.get("credit_account_id")) or {}
        out.append({
            "txn_number":  r.get("txn_number"),
            "txn_date":    r.get("txn_date"),
            "type":        r.get("type"),
            "category":    r.get("category"),
            "debit":       d.get("code") or "",
            "debit_name":  d.get("name") or "",
            "credit":      c.get("code") or "",
            "credit_name": c.get("name") or "",
            "amount":      _float(r.get("amount")),
            "payee":       r.get("payee") or "",
            "narration":   r.get("narration") or "",
            "status":      r.get("status"),
        })
    total = sum((Decimal(str(r["amount"])) for r in out), ZERO)
    return {
        "title": "General Ledger",
        "meta":  {"from": start.isoformat(), "to": end.isoformat(),
                  "entries": len(out)},
        "columns": [
            {"key": "txn_date",   "label": "Date",      "type": "date"},
            {"key": "txn_number", "label": "Voucher",   "type": "text"},
            {"key": "type",       "label": "Type",      "type": "text"},
            {"key": "category",   "label": "Category",  "type": "text"},
            {"key": "debit",      "label": "Dr",        "type": "text"},
            {"key": "credit",     "label": "Cr",        "type": "text"},
            {"key": "amount",     "label": "Amount",    "type": "money", "align": "right"},
            {"key": "payee",      "label": "Payee",     "type": "text"},
            {"key": "narration",  "label": "Narration", "type": "text"},
        ],
        "rows":   out,
        "totals": {"amount": _float(total)},
    }


# ---------- Account Ledger ----------
def r_account_ledger(org_id: str, params: dict) -> dict:
    account_id = (params.get("account_id") or "").strip()
    if not account_id:
        return {"title": "Account Ledger",
                "meta": {"error": "account_id required"},
                "columns": [], "rows": [], "totals": {}}

    start, end = _period_from_args(90)
    admin = _admin()

    rows = (
        admin.table("account_transactions")
        .select("txn_number, txn_date, type, category, amount, currency, "
                "method, payee, reference, narration, status, "
                "debit_account_id, credit_account_id")
        .eq("org_id", org_id)
        .or_(f"debit_account_id.eq.{account_id},credit_account_id.eq.{account_id}")
        .gte("txn_date", start.isoformat())
        .lte("txn_date", end.isoformat())
        .order("txn_date", desc=True)
        .limit(5000).execute()
    ).data or []

    acct_meta = next((a for a in _all_accounts(org_id) if a["id"] == account_id), {})

    out = []
    net = ZERO
    for r in rows:
        is_debit = r.get("debit_account_id") == account_id
        amt = _to_decimal(r.get("amount"))
        signed = amt if is_debit else -amt
        net += signed
        out.append({
            "txn_number": r.get("txn_number"),
            "txn_date":   r.get("txn_date"),
            "type":       r.get("type"),
            "category":   r.get("category"),
            "side":       "Dr" if is_debit else "Cr",
            "amount":     _float(amt),
            "signed":     _float(signed),
            "payee":      r.get("payee") or "",
            "narration":  r.get("narration") or "",
            "status":     r.get("status"),
        })

    return {
        "title": f"Account Ledger — {acct_meta.get('code','')} {acct_meta.get('name','')}",
        "meta":  {"from": start.isoformat(), "to": end.isoformat(),
                  "account_id": account_id,
                  "account_code": acct_meta.get("code"),
                  "account_name": acct_meta.get("name"),
                  "closing_balance": _float(acct_meta.get("balance") or 0)},
        "columns": [
            {"key": "txn_date",   "label": "Date",       "type": "date"},
            {"key": "txn_number", "label": "Voucher",    "type": "text"},
            {"key": "type",       "label": "Type",       "type": "text"},
            {"key": "category",   "label": "Category",   "type": "text"},
            {"key": "side",       "label": "Dr/Cr",      "type": "text"},
            {"key": "amount",     "label": "Amount",     "type": "money", "align": "right"},
            {"key": "payee",      "label": "Payee",      "type": "text"},
            {"key": "narration",  "label": "Narration",  "type": "text"},
        ],
        "rows":   out,
        "totals": {"net": _float(net)},
    }


# ---------- Expenses / Income by category ----------
def r_expenses_by_category(org_id: str, params: dict) -> dict:
    start, end = _period_from_args(30)
    data = _income_expense_rows(org_id, start, end)
    rows = []
    for r in data["expense"]:
        rows.append({
            **r,
            "_drill": {
                "kind":  "account_transactions",
                "title": f"{r['category'].replace('_',' ').title()} (expenses)",
                "filter": {
                    "category": r["category"], "type": "expense",
                    "from": start.isoformat(), "to": end.isoformat(),
                    "status": "completed",
                },
            },
        })
    total = sum((Decimal(str(r["amount"])) for r in rows), ZERO)
    return {
        "title": "Expenses by Category",
        "meta":  {"from": start.isoformat(), "to": end.isoformat()},
        "columns": [
            {"key": "category", "label": "Category", "type": "text"},
            {"key": "count",    "label": "Txns",     "type": "number", "align": "right"},
            {"key": "amount",   "label": "Amount",   "type": "money",  "align": "right"},
        ],
        "rows":   rows,
        "totals": {"amount": _float(total)},
    }


def r_income_by_category(org_id: str, params: dict) -> dict:
    start, end = _period_from_args(30)
    data = _income_expense_rows(org_id, start, end)
    rows = []
    for r in data["income"]:
        rows.append({
            **r,
            "_drill": {
                "kind":  "account_transactions",
                "title": f"{r['category'].replace('_',' ').title()} (income)",
                "filter": {
                    "category": r["category"], "type": "income",
                    "from": start.isoformat(), "to": end.isoformat(),
                    "status": "completed",
                },
            },
        })
    total = sum((Decimal(str(r["amount"])) for r in rows), ZERO)
    return {
        "title": "Income by Category",
        "meta":  {"from": start.isoformat(), "to": end.isoformat()},
        "columns": [
            {"key": "category", "label": "Category", "type": "text"},
            {"key": "count",    "label": "Txns",     "type": "number", "align": "right"},
            {"key": "amount",   "label": "Amount",   "type": "money",  "align": "right"},
        ],
        "rows":   rows,
        "totals": {"amount": _float(total)},
    }


# ---------- Loan Portfolio ----------
def r_loan_portfolio(org_id: str, params: dict) -> dict:
    admin = _admin()
    loans = (
        admin.table("loans")
        .select("loan_number, principal_amount, total_payable, "
                "outstanding_principal, outstanding_interest, "
                "outstanding_penalties, collected_principal, "
                "collected_interest, collected_penalties, collected_fees, "
                "currency, disbursed_at, maturity_date, status, "
                "members!inner(member_id, first_name, last_name, phone)")
        .eq("org_id", org_id)
        .order("disbursed_at", desc=True)
        .limit(5000).execute()
    ).data or []

    out = []
    tot_disp = tot_out = tot_col = ZERO
    for l in loans:
        m = l.get("members") or {}
        outstanding = (_to_decimal(l.get("outstanding_principal"))
                       + _to_decimal(l.get("outstanding_interest"))
                       + _to_decimal(l.get("outstanding_penalties")))
        collected = (_to_decimal(l.get("collected_principal"))
                     + _to_decimal(l.get("collected_interest"))
                     + _to_decimal(l.get("collected_penalties"))
                     + _to_decimal(l.get("collected_fees")))
        out.append({
            "loan_number":  l.get("loan_number"),
            "member":       f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            "member_id":    m.get("member_id"),
            "principal":    _float(l.get("principal_amount")),
            "outstanding":  _float(outstanding),
            "collected":    _float(collected),
            "disbursed_at": l.get("disbursed_at"),
            "status":       l.get("status"),
            "_drill": {
                "kind":  "repayments",
                "title": f"Repayments for {l.get('loan_number')}",
                "filter": {"loan_number": l.get("loan_number")},
            },
        })
        tot_disp += _to_decimal(l.get("principal_amount"))
        tot_out  += outstanding
        tot_col  += collected

    return {
        "title": "Loan Portfolio",
        "meta":  {"loans": len(out)},
        "columns": [
            {"key": "loan_number",  "label": "Loan #",      "type": "text"},
            {"key": "member",       "label": "Member",      "type": "text"},
            {"key": "member_id",    "label": "Member ID",   "type": "text"},
            {"key": "principal",    "label": "Principal",   "type": "money", "align": "right"},
            {"key": "outstanding",  "label": "Outstanding", "type": "money", "align": "right"},
            {"key": "collected",    "label": "Collected",   "type": "money", "align": "right"},
            {"key": "disbursed_at", "label": "Disbursed",   "type": "date"},
            {"key": "status",       "label": "Status",      "type": "text"},
        ],
        "rows":   out,
        "totals": {"principal": _float(tot_disp), "outstanding": _float(tot_out),
                   "collected": _float(tot_col)},
    }


def r_loan_aging(org_id: str, params: dict) -> dict:
    try:
        resp = _admin().rpc("loan_aging_buckets", {"p_org": org_id}).execute()
        buckets = resp.data or []
    except Exception:
        log.exception("loan_aging_buckets RPC failed; computing inline")
        buckets = _aging_inline(org_id)

    out = []
    tot = ZERO
    for b in buckets:
        out.append({
            "bucket":    b.get("bucket"),
            "count":     b.get("loan_count", 0),
            "principal": _float(b.get("principal") or 0),
            "interest":  _float(b.get("interest") or 0),
            "penalties": _float(b.get("penalties") or 0),
            "total":     _float(b.get("total") or 0),
            "_drill": {
                "kind":  "loans",
                "title": f"Loans · bucket {b.get('bucket')}",
                "filter": {"bucket": b.get("bucket")},
            },
        })
        tot += _to_decimal(b.get("total") or 0)

    return {
        "title": "Loan Aging",
        "meta":  {"buckets": len(out)},
        "columns": [
            {"key": "bucket",    "label": "Bucket",    "type": "text"},
            {"key": "count",     "label": "Loans",     "type": "number", "align": "right"},
            {"key": "principal", "label": "Principal", "type": "money",  "align": "right"},
            {"key": "interest",  "label": "Interest",  "type": "money",  "align": "right"},
            {"key": "penalties", "label": "Penalties", "type": "money",  "align": "right"},
            {"key": "total",     "label": "Total",     "type": "money",  "align": "right"},
        ],
        "rows":   out,
        "totals": {"total": _float(tot)},
    }


def _aging_inline(org_id: str) -> list[dict]:
    today = _today()
    admin = _admin()
    loans = (
        admin.table("loans")
        .select("id, outstanding_principal, outstanding_interest, "
                "outstanding_penalties, disbursed_at")
        .eq("org_id", org_id).in_("status", ["active", "overdue"])
        .execute().data or []
    )
    reps = (
        admin.table("repayments")
        .select("loan_id, paid_at")
        .eq("org_id", org_id).eq("status", "completed")
        .order("paid_at", desc=True).execute()
    ).data or []

    last_pay = {}
    for r in reps:
        if r["loan_id"] not in last_pay:
            last_pay[r["loan_id"]] = _parse_date(r["paid_at"])

    buckets = {k: {"loan_count": 0, "principal": ZERO, "interest": ZERO,
                   "penalties": ZERO, "total": ZERO}
               for k in ("current","1_30","31_60","61_90","90_plus")}
    for l in loans:
        p = _to_decimal(l.get("outstanding_principal"))
        i = _to_decimal(l.get("outstanding_interest"))
        n = _to_decimal(l.get("outstanding_penalties"))
        if p + i <= 0: continue
        last = last_pay.get(l["id"]) or _parse_date(l.get("disbursed_at"))
        days = (today - last).days if last else 999
        if days <= 30:     k = "current"
        elif days <= 60:   k = "1_30"
        elif days <= 90:   k = "31_60"
        elif days <= 180:  k = "61_90"
        else:              k = "90_plus"
        b = buckets[k]
        b["loan_count"] += 1
        b["principal"] += p
        b["interest"]  += i
        b["penalties"] += n
        b["total"]     += (p + i + n)

    return [
        {"bucket": k, "loan_count": v["loan_count"],
         "principal": float(_q(v["principal"])),
         "interest":  float(_q(v["interest"])),
         "penalties": float(_q(v["penalties"])),
         "total":     float(_q(v["total"]))}
        for k, v in buckets.items()
    ]


# ---------- Collections ----------
def r_collections(org_id: str, params: dict) -> dict:
    start, end = _period_from_args(30)
    reps = (
        _admin().table("repayments")
        .select("receipt_number, amount, currency, method, provider, "
                "status, paid_at, "
                "members!inner(member_id, first_name, last_name), "
                "loans!inner(loan_number), "
                "repayment_allocations(to_interest, to_principal, "
                "to_penalties, to_fees)")
        .eq("org_id", org_id).eq("status", "completed")
        .gte("paid_at", start.isoformat())
        .lte("paid_at", end.isoformat() + "T23:59:59")
        .order("paid_at", desc=True).limit(5000)
        .execute()
    ).data or []

    out = []
    tot = ZERO
    tot_int = tot_princ = tot_pen = tot_fee = ZERO
    for r in reps:
        m = r.get("members") or {}
        l = r.get("loans") or {}
        a = (r.get("repayment_allocations") or [{}])[0]
        amt = _to_decimal(r.get("amount"))
        out.append({
            "receipt":     r.get("receipt_number"),
            "date":        r.get("paid_at"),
            "member":      f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            "member_id":   m.get("member_id"),
            "loan_number": l.get("loan_number"),
            "amount":      _float(amt),
            "interest":    _float(a.get("to_interest") or 0),
            "principal":   _float(a.get("to_principal") or 0),
            "penalties":   _float(a.get("to_penalties") or 0),
            "fees":        _float(a.get("to_fees") or 0),
            "method":      r.get("method"),
            "_drill": {
                "kind":  "member_transactions",
                "title": f"Ledger for {m.get('first_name','')} {m.get('last_name','')}",
                "filter": {"member_id": r.get("member_id"),
                           "type": "loan_repayment"},
            },
        })
        tot      += amt
        tot_int  += _to_decimal(a.get("to_interest") or 0)
        tot_princ+= _to_decimal(a.get("to_principal") or 0)
        tot_pen  += _to_decimal(a.get("to_penalties") or 0)
        tot_fee  += _to_decimal(a.get("to_fees") or 0)

    return {
        "title": "Repayment Collections",
        "meta":  {"from": start.isoformat(), "to": end.isoformat(),
                  "entries": len(out)},
        "columns": [
            {"key": "date",        "label": "Date",      "type": "date"},
            {"key": "receipt",     "label": "Receipt",   "type": "text"},
            {"key": "member",      "label": "Member",    "type": "text"},
            {"key": "loan_number", "label": "Loan",      "type": "text"},
            {"key": "amount",      "label": "Amount",    "type": "money", "align": "right"},
            {"key": "interest",    "label": "Interest",  "type": "money", "align": "right"},
            {"key": "principal",   "label": "Principal", "type": "money", "align": "right"},
            {"key": "penalties",   "label": "Penalties", "type": "money", "align": "right"},
            {"key": "fees",        "label": "Fees",      "type": "money", "align": "right"},
            {"key": "method",      "label": "Method",    "type": "text"},
        ],
        "rows":   out,
        "totals": {"amount": _float(tot), "interest": _float(tot_int),
                   "principal": _float(tot_princ), "penalties": _float(tot_pen),
                   "fees": _float(tot_fee)},
    }


# ---------- Savings summary ----------
def r_savings_summary(org_id: str, params: dict) -> dict:
    accts = (
        _admin().table("member_accounts")
        .select("account_number, balance, currency, status, opened_at, "
                "members!inner(member_id, first_name, last_name, phone)")
        .eq("org_id", org_id).eq("account_type", "savings")
        .order("balance", desc=True).limit(5000)
        .execute()
    ).data or []

    out = []
    total = ZERO
    for a in accts:
        m = a.get("members") or {}
        bal = _to_decimal(a.get("balance") or 0)
        out.append({
            "account_number": a.get("account_number"),
            "member":         f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            "member_id":      m.get("member_id"),
            "phone":          m.get("phone"),
            "balance":        _float(bal),
            "currency":       a.get("currency"),
            "status":         a.get("status"),
            "opened_at":      a.get("opened_at"),
            "_drill": {
                "kind":  "member_transactions",
                "title": f"Transactions · {a.get('account_number')}",
                "filter": {"member_id": a.get("member_id")},
            },
        })
        total += bal

    return {
        "title": "Savings Summary",
        "meta":  {"accounts": len(out)},
        "columns": [
            {"key": "account_number", "label": "Account #", "type": "text"},
            {"key": "member",         "label": "Member",    "type": "text"},
            {"key": "member_id",      "label": "Member ID", "type": "text"},
            {"key": "phone",          "label": "Phone",     "type": "text"},
            {"key": "balance",        "label": "Balance",   "type": "money", "align": "right"},
            {"key": "status",         "label": "Status",    "type": "text"},
            {"key": "opened_at",      "label": "Opened",    "type": "date"},
        ],
        "rows":   out,
        "totals": {"balance": _float(total)},
    }


# ---------- Shares summary ----------
def r_shares_summary(org_id: str, params: dict) -> dict:
    members = (
        _admin().table("members")
        .select("id, member_id, first_name, last_name, phone, status, "
                "shares_units, shares_value, joined_at")
        .eq("org_id", org_id)
        .order("shares_units", desc=True).limit(5000)
        .execute().data or []
    )
    out = []
    units = value = ZERO
    for m in members:
        u = _to_decimal(m.get("shares_units") or 0)
        v = _to_decimal(m.get("shares_value") or 0)
        units += u
        value += v
        out.append({
            "member_id":     m.get("member_id"),
            "member":        f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            "phone":         m.get("phone"),
            "shares_units":  _float(u),
            "shares_value":  _float(v),
            "status":        m.get("status"),
            "joined_at":     m.get("joined_at"),
            "_drill": {
                "kind":  "member_transactions",
                "title": f"Share movements · {m.get('first_name','')} {m.get('last_name','')}",
                "filter": {"member_id": m.get("id"), "type": "share_purchase"},
            },
        })

    return {
        "title": "Shares Summary",
        "meta":  {"members": len(out)},
        "columns": [
            {"key": "member_id",    "label": "Member ID",   "type": "text"},
            {"key": "member",       "label": "Member",      "type": "text"},
            {"key": "phone",        "label": "Phone",       "type": "text"},
            {"key": "shares_units", "label": "Units",       "type": "number", "align": "right"},
            {"key": "shares_value", "label": "Share Value", "type": "money",  "align": "right"},
            {"key": "status",       "label": "Status",      "type": "text"},
            {"key": "joined_at",    "label": "Joined",      "type": "date"},
        ],
        "rows":   out,
        "totals": {"shares_units": _float(units), "shares_value": _float(value)},
    }


# ---------- Member statement ----------
def r_member_statement(org_id: str, params: dict) -> dict:
    member_id = (params.get("member_id") or "").strip()
    if not member_id:
        return {"title": "Member Statement", "meta": {"error": "member_id required"},
                "columns": [], "rows": [], "totals": {}}

    m = (
        _admin().table("members")
        .select("member_id, first_name, last_name, phone, email, "
                "shares_units, shares_value, savings_balance, loan_balance")
        .eq("org_id", org_id).eq("id", member_id).limit(1).execute()
    ).data or []
    if not m:
        return {"title": "Member Statement", "meta": {"error": "member not found"},
                "columns": [], "rows": [], "totals": {}}
    m = m[0]

    start, end = _period_from_args(365)

    txns = (
        _admin().table("member_transactions")
        .select("created_at, type, method, amount, currency, status, "
                "narration, balance_after, provider_ref")
        .eq("org_id", org_id).eq("member_id", member_id)
        .gte("created_at", start.isoformat())
        .lte("created_at", end.isoformat() + "T23:59:59")
        .order("created_at", desc=True).limit(5000)
        .execute()
    ).data or []

    out = []
    total_in = total_out = ZERO
    for t in txns:
        kind = (t.get("type") or "").lower()
        amt = _to_decimal(t.get("amount"))
        is_credit = kind in ("savings_deposit","subscription","share_purchase")
        is_debit  = kind in ("savings_withdrawal","loan_repayment","loan_disbursement")
        signed = ZERO
        if is_credit: signed = amt;  total_in += amt
        if is_debit:  signed = -amt; total_out += amt
        out.append({
            "date":       t.get("created_at"),
            "type":       kind,
            "method":     t.get("method"),
            "amount":     _float(amt),
            "signed":     _float(signed),
            "status":     t.get("status"),
            "narration":  t.get("narration"),
        })

    return {
        "title": f"Member Statement — {m.get('first_name','')} {m.get('last_name','')}",
        "meta":  {"from": start.isoformat(), "to": end.isoformat(),
                  "member_id": m.get("member_id"),
                  "phone": m.get("phone"),
                  "shares_units": _float(m.get("shares_units") or 0),
                  "shares_value": _float(m.get("shares_value") or 0),
                  "savings_balance": _float(m.get("savings_balance") or 0),
                  "loan_balance": _float(m.get("loan_balance") or 0)},
        "columns": [
            {"key": "date",      "label": "Date",      "type": "date"},
            {"key": "type",      "label": "Type",      "type": "text"},
            {"key": "method",    "label": "Method",    "type": "text"},
            {"key": "amount",    "label": "Amount",    "type": "money", "align": "right"},
            {"key": "signed",    "label": "Signed",    "type": "money", "align": "right"},
            {"key": "status",    "label": "Status",    "type": "text"},
            {"key": "narration", "label": "Narration", "type": "text"},
        ],
        "rows":   out,
        "totals": {"in": _float(total_in), "out": _float(total_out),
                   "net": _float(total_in - total_out)},
    }


# ---------- Subscriptions ----------
def r_subscriptions(org_id: str, params: dict) -> dict:
    try:
        year = int(params.get("year") or _today().year)
    except ValueError:
        year = _today().year

    rows = (
        _admin().table("yearly_subscriptions")
        .select("subscription_year, amount_due, amount_paid, currency, "
                "status, source, due_date, paid_at, member_id, "
                "members!inner(member_id, first_name, last_name, phone)")
        .eq("org_id", org_id).eq("subscription_year", year)
        .order("created_at", desc=True).limit(5000)
        .execute()
    ).data or []

    out = []
    due_total = paid_total = ZERO
    for r in rows:
        m = r.get("members") or {}
        due  = _to_decimal(r.get("amount_due") or 0)
        paid = _to_decimal(r.get("amount_paid") or 0)
        out.append({
            "member_id": m.get("member_id"),
            "member":    f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            "phone":     m.get("phone"),
            "year":      r.get("subscription_year"),
            "amount_due":  _float(due),
            "amount_paid": _float(paid),
            "balance":     _float(due - paid),
            "status":      r.get("status"),
            "source":      r.get("source"),
            "_drill": {
                "kind":  "member_transactions",
                "title": f"Subscription movements · {m.get('first_name','')} {m.get('last_name','')}",
                "filter": {"member_id": r.get("member_id"), "type": "subscription"},
            },
        })
        due_total  += due
        paid_total += paid

    return {
        "title": f"Yearly Subscriptions — {year}",
        "meta":  {"year": year, "entries": len(out)},
        "columns": [
            {"key": "member_id",   "label": "Member ID", "type": "text"},
            {"key": "member",      "label": "Member",    "type": "text"},
            {"key": "phone",       "label": "Phone",     "type": "text"},
            {"key": "year",        "label": "Year",      "type": "number", "align": "right"},
            {"key": "amount_due",  "label": "Due",       "type": "money", "align": "right"},
            {"key": "amount_paid", "label": "Paid",      "type": "money", "align": "right"},
            {"key": "balance",     "label": "Balance",   "type": "money", "align": "right"},
            {"key": "status",      "label": "Status",    "type": "text"},
        ],
        "rows":   out,
        "totals": {"amount_due": _float(due_total), "amount_paid": _float(paid_total),
                   "balance": _float(due_total - paid_total)},
    }


# ---------- Monthly Trial Balance ----------
def r_trial_balance_by_month(org_id: str, params: dict) -> dict:
    today = _today()
    start = date(today.year - 1, today.month, 1)
    rows = (
        _admin().table("account_transactions")
        .select("txn_date, type, amount")
        .eq("org_id", org_id).eq("status", "completed")
        .gte("txn_date", start.isoformat())
        .execute()
    ).data or []

    buckets = {}
    for r in rows:
        d = _parse_date(r.get("txn_date"))
        if not d: continue
        key = f"{d.year:04d}-{d.month:02d}"
        b = buckets.setdefault(key, {"income": ZERO, "expense": ZERO})
        if r.get("type") == "income":  b["income"]  += _to_decimal(r.get("amount"))
        if r.get("type") == "expense": b["expense"] += _to_decimal(r.get("amount"))

    out = []
    for k in sorted(buckets.keys()):
        b = buckets[k]
        net = b["income"] - b["expense"]
        y, mo = k.split("-")
        first_of_month = date(int(y), int(mo), 1)
        last_of_month = _add_months(first_of_month, 1) - timedelta(days=1)
        out.append({
            "month":   k,
            "income":  _float(b["income"]),
            "expense": _float(b["expense"]),
            "net":     _float(net),
            "_drill": {
                "kind":  "account_transactions",
                "title": f"Transactions · {k}",
                "filter": {"from": first_of_month.isoformat(),
                           "to":   last_of_month.isoformat(),
                           "status": "completed"},
            },
        })

    return {
        "title": "Monthly Trial Balance (last 12 months)",
        "meta":  {"months": len(out)},
        "columns": [
            {"key": "month",   "label": "Month",   "type": "text"},
            {"key": "income",  "label": "Income",  "type": "money", "align": "right"},
            {"key": "expense", "label": "Expense", "type": "money", "align": "right"},
            {"key": "net",     "label": "Net",     "type": "money", "align": "right"},
        ],
        "rows":   out,
        "totals": {
            "income":  _float(sum((Decimal(str(r["income"]))  for r in out), ZERO)),
            "expense": _float(sum((Decimal(str(r["expense"])) for r in out), ZERO)),
            "net":     _float(sum((Decimal(str(r["net"]))     for r in out), ZERO)),
        },
    }


def _add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return date(y, m, 28)


# ===========================================================================
# CATALOG
# ===========================================================================
REPORTS = {
    "trial_balance":          {"fn": r_trial_balance,           "params": ["as_of"]},
    "balance_sheet":          {"fn": r_balance_sheet,           "params": ["as_of"]},
    "profit_loss":            {"fn": r_profit_loss,             "params": ["from", "to"]},
    "income_statement_ytd":   {"fn": r_income_statement_ytd,    "params": []},
    "cash_flow":              {"fn": r_cash_flow,               "params": ["from", "to"]},
    "ledger":                 {"fn": r_ledger,                  "params": ["from", "to"]},
    "account_ledger":         {"fn": r_account_ledger,          "params": ["account_id", "from", "to"]},
    "expenses_by_category":   {"fn": r_expenses_by_category,    "params": ["from", "to"]},
    "income_by_category":     {"fn": r_income_by_category,      "params": ["from", "to"]},
    "loan_portfolio":         {"fn": r_loan_portfolio,          "params": []},
    "loan_aging":             {"fn": r_loan_aging,              "params": []},
    "collections":            {"fn": r_collections,             "params": ["from", "to"]},
    "savings_summary":        {"fn": r_savings_summary,         "params": []},
    "shares_summary":         {"fn": r_shares_summary,          "params": []},
    "member_statement":       {"fn": r_member_statement,        "params": ["member_id", "from", "to"]},
    "subscriptions":          {"fn": r_subscriptions,           "params": ["year"]},
    "trial_balance_by_month": {"fn": r_trial_balance_by_month,  "params": []},
}


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
@financial_reports_bp.route("/", methods=["GET"])
def page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    if session["user"].get("role") != "admin":
        return redirect(url_for("auth.dashboard"))
    return render_template("financialReports/financialReports.html")


# ---------------------------------------------------------------------------
# CATALOG
# ---------------------------------------------------------------------------
@financial_reports_bp.route("/api/catalog", methods=["GET"])
@role_required("admin")
def api_catalog():
    cat = []
    for key, meta in REPORTS.items():
        try:
            dummy = meta["fn"](_org_id(), {})
        except Exception:
            dummy = {}
        cat.append({
            "key":    key,
            "title":  dummy.get("title") or key.replace("_", " ").title(),
            "params": meta.get("params", []),
        })
    return jsonify({"ok": True, "catalog": cat})


# ---------------------------------------------------------------------------
# RUN
# ---------------------------------------------------------------------------
@financial_reports_bp.route("/api/run", methods=["GET"])
@role_required("admin")
def api_run():
    key = (request.args.get("report") or "").strip()
    if not key or key not in REPORTS:
        return _bad("Unknown report", 404, reason="unknown_report")

    meta = REPORTS[key]
    params = {p: request.args.get(p) for p in meta.get("params", [])}

    try:
        result = meta["fn"](_org_id(), params)
    except Exception as e:
        log.exception("report %s failed", key)
        return _bad(f"Could not run report: {e}", 500, reason="run_failed")

    result["report"] = key
    result["ok"]     = True
    result["org"]    = _org_info(_org_id())
    return jsonify(result)


# ---------------------------------------------------------------------------
# DRILL
# ---------------------------------------------------------------------------
@financial_reports_bp.route("/api/drill", methods=["GET"])
@role_required("admin")
def api_drill():
    raw = request.args.get("d") or ""
    try:
        spec = json.loads(raw)
    except Exception:
        return _bad("Invalid drill spec", 400, reason="bad_spec")

    kind   = (spec.get("kind") or "").strip()
    flt    = spec.get("filter") or {}
    title  = spec.get("title") or "Details"

    if kind not in DRILL_HANDLERS:
        return _bad("Unknown drill kind", 400, reason="bad_kind")

    try:
        result = DRILL_HANDLERS[kind](_org_id(), flt)
    except Exception as e:
        log.exception("drill %s failed", kind)
        return _bad(f"Could not load details: {e}", 500, reason="drill_failed")

    result["ok"]    = True
    result["title"] = title
    return jsonify(result)


# ---------------------------------------------------------------------------
# CSV EXPORT
# ---------------------------------------------------------------------------
@financial_reports_bp.route("/api/export", methods=["GET"])
@role_required("admin")
def api_export():
    key = (request.args.get("report") or "").strip()
    if not key or key not in REPORTS:
        return _bad("Unknown report", 404, reason="unknown_report")

    meta = REPORTS[key]
    params = {p: request.args.get(p) for p in meta.get("params", [])}

    try:
        result = meta["fn"](_org_id(), params)
    except Exception as e:
        log.exception("export failed")
        return _bad(f"Could not run report: {e}", 500, reason="run_failed")

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([result.get("title") or key])
    for k, v in (result.get("meta") or {}).items():
        w.writerow([k, v])
    w.writerow([])

    columns = result.get("columns") or []
    w.writerow([c["label"] for c in columns])

    groups = result.get("groups")
    if groups:
        for g in groups:
            w.writerow([f"— {g.get('label','')} —"] + [""] * (len(columns) - 1))
            for row in g.get("rows", []):
                w.writerow([row.get(c["key"], "") for c in columns])
            totals = g.get("totals") or {}
            if totals:
                w.writerow(["TOTAL"] + [totals.get(c["key"], "")
                                         for c in columns[1:]])
            w.writerow([])
    else:
        for row in (result.get("rows") or []):
            w.writerow([row.get(c["key"], "") for c in columns])
        totals = result.get("totals") or {}
        if totals:
            w.writerow(["TOTAL"] + [totals.get(c["key"], "")
                                     for c in columns[1:]])

    filename = f"{key}_{_today().isoformat()}.csv"
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/csv; charset=utf-8",
        },
    )


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------
@financial_reports_bp.route("/api/accounts", methods=["GET"])
@role_required("admin")
def api_accounts():
    return jsonify({"ok": True, "accounts": _all_accounts(_org_id())})


@financial_reports_bp.route("/api/members/search", methods=["GET"])
@role_required("admin")
def api_members_search():
    q = (request.args.get("q") or "").strip()
    query = (
        _admin().table("members")
        .select("id, member_id, first_name, last_name, phone")
        .eq("org_id", _org_id()).limit(50)
    )
    if q:
        like = f"%{q}%"
        query = query.or_(
            f"first_name.ilike.{like},last_name.ilike.{like},"
            f"member_id.ilike.{like},phone.ilike.{like}"
        )
    rows = query.execute().data or []
    return jsonify({"ok": True, "members": rows})