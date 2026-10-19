"""
Member Detail blueprint.

A single aggregated read-only view of a member across every module.

URL prefix (single source of truth):
    URL_PREFIX = "/member_details_page"
    All routes below are automatically prefixed with it.

Endpoints (all admin-only, read-only):
    GET    {prefix}/<uuid:member_uuid>/                             -> page
    GET    {prefix}/<uuid:member_uuid>/api/profile                  -> identity + accounts
    GET    {prefix}/<uuid:member_uuid>/api/savings                  -> savings account + stats
    GET    {prefix}/<uuid:member_uuid>/api/shares                   -> shareholding + tx
    GET    {prefix}/<uuid:member_uuid>/api/subscriptions            -> yearly subs
    GET    {prefix}/<uuid:member_uuid>/api/loans                    -> applications + loans + repayments
    GET    {prefix}/<uuid:member_uuid>/api/fixed                    -> fixed deposits
    GET    {prefix}/<uuid:member_uuid>/api/dividends                -> dividend payments
    GET    {prefix}/<uuid:member_uuid>/api/transactions             -> unified ledger (paginated)
    GET    {prefix}/<uuid:member_uuid>/api/statement                -> CSV of full member statement

Design:
- Every endpoint scoped by `session["user"]["org_id"]` (admin).
- Read-only — no mutations here. Mutations live in each module.
- Single joined query where possible; no N+1.
- URL prefix is centralised in URL_PREFIX for easy relocation.
"""

import os
import io
import csv
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import (
    Blueprint, render_template, request, jsonify, session, redirect, url_for,
    Response,
)
from dotenv import load_dotenv
from supabase import create_client, Client

from routes.auth.auth import role_required

load_dotenv()

log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] member_detail: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

# ---------------------------------------------------------------------------
# URL PREFIX — change this one line to relocate every route
# ---------------------------------------------------------------------------
URL_PREFIX = "/member_details_page"

member_detail_bp = Blueprint(
    "member_detail",
    __name__,
    url_prefix=URL_PREFIX,
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


def _guard_member(member_uuid) -> dict | None:
    """Confirm the member exists in this org, return the row."""
    try:
        rows = (
            _admin().table("members")
            .select("id, member_id, org_id")
            .eq("org_id", _org_id()).eq("id", str(member_uuid)).limit(1).execute()
        ).data or []
    except Exception:
        log.exception("member guard failed")
        return None
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# PAGE — GET {prefix}/<uuid>/
# ---------------------------------------------------------------------------
@member_detail_bp.route("/<uuid:member_uuid>/", methods=["GET"])
def page(member_uuid):
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    if session["user"].get("role") != "admin":
        return redirect(url_for("auth.dashboard"))

    member = _guard_member(member_uuid)
    if not member:
        return redirect(url_for("members.page"))

    return render_template(
        "memberDetail/memberDetail.html",
        member_uuid=str(member_uuid),
        member_id=member["member_id"],
        url_prefix=URL_PREFIX,
    )


# ===========================================================================
# 1. Profile
# ===========================================================================
@member_detail_bp.route("/<uuid:member_uuid>/api/profile", methods=["GET"])
@role_required("admin")
def api_profile(member_uuid):
    member = _guard_member(member_uuid)
    if not member:
        return _bad("Member not found", 404, reason="not_found")

    try:
        rows = (
            _admin().table("members")
            .select(
                "*, "
                "member_accounts(account_type, account_number, balance, currency, status)"
            )
            .eq("org_id", _org_id()).eq("id", str(member_uuid)).limit(1).execute()
        ).data
    except Exception as e:
        log.exception("profile fetch failed")
        return _bad(f"Could not load profile: {e}", 500, reason="db_error")

    if not rows:
        return _bad("Member not found", 404, reason="not_found")
    m = rows[0]
    accounts = m.pop("member_accounts", []) or []

    return jsonify({"ok": True, "member": m, "accounts": accounts})


# ===========================================================================
# 2. Savings
# ===========================================================================
@member_detail_bp.route("/<uuid:member_uuid>/api/savings", methods=["GET"])
@role_required("admin")
def api_savings(member_uuid):
    member = _guard_member(member_uuid)
    if not member:
        return _bad("Member not found", 404, reason="not_found")

    try:
        acc_rows = (
            _admin().table("member_accounts")
            .select("id, account_number, balance, currency, status, opened_at")
            .eq("org_id", _org_id()).eq("member_id", str(member_uuid))
            .eq("account_type", "savings").limit(1).execute()
        ).data or [None]
        acc = acc_rows[0]
    except Exception as e:
        log.exception("savings fetch failed")
        return _bad(f"Could not load savings: {e}", 500, reason="db_error")

    if not acc:
        return jsonify({
            "ok": True, "account": None,
            "transactions": [], "withdrawals": [],
            "stats": {"deposits_total": 0, "withdrawals_total": 0, "pending_withdrawals": 0},
        })

    try:
        tx = (
            _admin().table("member_transactions")
            .select("id, type, method, amount, currency, status, narration, balance_after, created_at")
            .eq("org_id", _org_id()).eq("account_id", acc["id"])
            .order("created_at", desc=True).limit(100).execute()
        ).data or []

        withdrawals = (
            _admin().table("withdrawal_requests")
            .select("id, amount, currency, reason, status, source, method, created_at, reviewed_at, paid_at")
            .eq("org_id", _org_id()).eq("member_id", str(member_uuid))
            .order("created_at", desc=True).limit(50).execute()
        ).data or []
    except Exception as e:
        log.exception("savings tx fetch failed")
        return _bad(f"Could not load transactions: {e}", 500, reason="db_error")

    deposits_total = sum(
        (_to_decimal(t.get("amount")) for t in tx
         if t.get("type") == "savings_deposit" and t.get("status") == "completed"),
        ZERO,
    )
    withdrawals_total = sum(
        (_to_decimal(t.get("amount")) for t in tx
         if t.get("type") == "savings_withdrawal" and t.get("status") == "completed"),
        ZERO,
    )
    pending_count = sum(1 for w in withdrawals if w.get("status") in ("pending", "approved"))

    return jsonify({
        "ok": True,
        "account": acc,
        "transactions": tx,
        "withdrawals": withdrawals,
        "stats": {
            "deposits_total":      float(_q(deposits_total)),
            "withdrawals_total":   float(_q(withdrawals_total)),
            "pending_withdrawals": pending_count,
        },
    })


# ===========================================================================
# 3. Shares
# ===========================================================================
@member_detail_bp.route("/<uuid:member_uuid>/api/shares", methods=["GET"])
@role_required("admin")
def api_shares(member_uuid):
    member = _guard_member(member_uuid)
    if not member:
        return _bad("Member not found", 404, reason="not_found")

    try:
        mem = (
            _admin().table("members")
            .select("shares_units, shares_value")
            .eq("org_id", _org_id()).eq("id", str(member_uuid)).limit(1).execute()
        ).data or [{}]
        mem = mem[0]

        settings = (
            _admin().table("institution_settings")
            .select("min_shares, min_share_price, currency")
            .eq("org_id", _org_id()).limit(1).execute()
        ).data or [{}]
        settings = settings[0]

        tx = (
            _admin().table("share_transactions")
            .select(
                "id, txn_number, type, units, unit_price, amount, currency, "
                "method, provider, status, narration, units_before, units_after, "
                "value_after, created_at"
            )
            .eq("org_id", _org_id()).eq("member_id", str(member_uuid))
            .order("created_at", desc=True).limit(100).execute()
        ).data or []
    except Exception as e:
        log.exception("shares fetch failed")
        return _bad(f"Could not load shares: {e}", 500, reason="db_error")

    total_bought = sum(
        (_to_decimal(t.get("units")) for t in tx
         if t.get("type") == "purchase" and t.get("status") == "completed"),
        ZERO,
    )

    return jsonify({
        "ok": True,
        "holding": {
            "units":         float(_to_decimal(mem.get("shares_units"))),
            "value":         float(_q(_to_decimal(mem.get("shares_value")))),
            "min_units":     float(_to_decimal(settings.get("min_shares"))),
            "share_price":   float(_q(_to_decimal(settings.get("min_share_price")))),
            "currency":      settings.get("currency") or "UGX",
            "units_bought":  float(_to_decimal(total_bought)),
        },
        "transactions": tx,
    })


# ===========================================================================
# 4. Subscriptions
# ===========================================================================
@member_detail_bp.route("/<uuid:member_uuid>/api/subscriptions", methods=["GET"])
@role_required("admin")
def api_subscriptions(member_uuid):
    member = _guard_member(member_uuid)
    if not member:
        return _bad("Member not found", 404, reason="not_found")

    try:
        subs = (
            _admin().table("yearly_subscriptions")
            .select(
                "id, subscription_year, amount_due, amount_paid, currency, "
                "status, source, due_date, last_payment_at, paid_at, created_at"
            )
            .eq("org_id", _org_id()).eq("member_id", str(member_uuid))
            .order("subscription_year", desc=True).limit(20).execute()
        ).data or []

        tx = (
            _admin().table("member_transactions")
            .select("id, amount, currency, status, method, provider, narration, created_at, subscription_id")
            .eq("org_id", _org_id()).eq("member_id", str(member_uuid))
            .eq("type", "subscription")
            .order("created_at", desc=True).limit(100).execute()
        ).data or []
    except Exception as e:
        log.exception("subscriptions fetch failed")
        return _bad(f"Could not load subscriptions: {e}", 500, reason="db_error")

    return jsonify({"ok": True, "subscriptions": subs, "payments": tx})


# ===========================================================================
# 5. Loans
# ===========================================================================
@member_detail_bp.route("/<uuid:member_uuid>/api/loans", methods=["GET"])
@role_required("admin")
def api_loans(member_uuid):
    member = _guard_member(member_uuid)
    if not member:
        return _bad("Member not found", 404, reason="not_found")

    try:
        apps = (
            _admin().table("loan_applications")
            .select(
                "id, app_number, requested_amount, requested_term_months, "
                "approved_amount, approved_term_months, status, source, currency, "
                "purpose, submitted_at, approved_at, disbursed_at, created_at, "
                "loan_products!inner(id, name, code, category)"
            )
            .eq("org_id", _org_id()).eq("member_id", str(member_uuid))
            .order("created_at", desc=True).limit(50).execute()
        ).data or []

        loans = (
            _admin().table("loans")
            .select(
                "id, loan_number, principal_amount, total_payable, "
                "outstanding_principal, outstanding_interest, outstanding_penalties, "
                "collected_principal, collected_interest, collected_penalties, "
                "interest_rate_percent, interest_method, repayment_frequency, "
                "term_months, currency, disbursed_at, maturity_date, status"
            )
            .eq("org_id", _org_id()).eq("member_id", str(member_uuid))
            .order("disbursed_at", desc=True).limit(50).execute()
        ).data or []

        reps = (
            _admin().table("repayments")
            .select(
                "id, receipt_number, amount, currency, method, provider, status, "
                "paid_at, created_at, loan_id, narration, "
                "repayment_allocations(to_interest, to_principal, to_penalties, to_fees)"
            )
            .eq("org_id", _org_id()).eq("member_id", str(member_uuid))
            .order("created_at", desc=True).limit(100).execute()
        ).data or []
    except Exception as e:
        log.exception("loans fetch failed")
        return _bad(f"Could not load loans: {e}", 500, reason="db_error")

    total_outstanding = sum(
        (_to_decimal(l.get("outstanding_principal"))
         + _to_decimal(l.get("outstanding_interest"))
         + _to_decimal(l.get("outstanding_penalties"))
         for l in loans if l.get("status") in ("active", "overdue")),
        ZERO,
    )

    return jsonify({
        "ok": True,
        "applications": apps,
        "loans": loans,
        "repayments": reps,
        "stats": {
            "active_loans":      sum(1 for l in loans if l.get("status") in ("active", "overdue")),
            "total_outstanding": float(_q(total_outstanding)),
        },
    })


# ===========================================================================
# 6. Fixed Deposits
# ===========================================================================
@member_detail_bp.route("/<uuid:member_uuid>/api/fixed", methods=["GET"])
@role_required("admin")
def api_fixed(member_uuid):
    member = _guard_member(member_uuid)
    if not member:
        return _bad("Member not found", 404, reason="not_found")

    try:
        fds = (
            _admin().table("fixed_deposits")
            .select(
                "id, fd_number, principal, target_amount, accrued_interest, "
                "paid_interest, currency, start_date, maturity_date, period_months, "
                "status, interest_enabled, interest_rate_percent, interest_type, "
                "created_at"
            )
            .eq("org_id", _org_id()).eq("member_id", str(member_uuid))
            .order("created_at", desc=True).limit(50).execute()
        ).data or []
    except Exception as e:
        log.exception("fixed deposits fetch failed")
        return _bad(f"Could not load fixed deposits: {e}", 500, reason="db_error")

    total_principal = sum((_to_decimal(f.get("principal")) for f in fds), ZERO)
    total_interest  = sum((_to_decimal(f.get("accrued_interest")) for f in fds), ZERO)

    return jsonify({
        "ok": True,
        "deposits": fds,
        "stats": {
            "total_principal": float(_q(total_principal)),
            "total_interest":  float(_q(total_interest)),
            "total_value":     float(_q(total_principal + total_interest)),
        },
    })


# ===========================================================================
# 7. Dividends
# ===========================================================================
@member_detail_bp.route("/<uuid:member_uuid>/api/dividends", methods=["GET"])
@role_required("admin")
def api_dividends(member_uuid):
    member = _guard_member(member_uuid)
    if not member:
        return _bad("Member not found", 404, reason="not_found")

    try:
        payments = (
            _admin().table("dividend_payments")
            .select(
                "id, dividend_amount, currency, status, "
                "shares_units_at_run, share_price_at_run, payout_mode, "
                "posted_at, reversed_at, dividend_run_id, "
                "dividend_runs!inner(run_number, financial_year, payout_mode, status)"
            )
            .eq("org_id", _org_id()).eq("member_id", str(member_uuid))
            .order("posted_at", desc=True).limit(50).execute()
        ).data or []
    except Exception as e:
        log.exception("dividends fetch failed")
        return _bad(f"Could not load dividends: {e}", 500, reason="db_error")

    total = sum(
        (_to_decimal(p.get("dividend_amount")) for p in payments
         if p.get("status") == "posted"),
        ZERO,
    )

    return jsonify({
        "ok": True,
        "payments": payments,
        "stats": {
            "total_earned": float(_q(total)),
            "count":        len(payments),
        },
    })


# ===========================================================================
# 8. Unified transactions
# ===========================================================================
TX_LABELS = {
    "savings_deposit":      ("Savings Deposit",     "in"),
    "savings_withdrawal":   ("Savings Withdrawal",  "out"),
    "share_purchase":       ("Share Purchase",      "in"),
    "share_redemption":     ("Share Redemption",    "out"),
    "loan_disbursement":    ("Loan Disbursement",   "in"),
    "loan_repayment":       ("Loan Repayment",      "out"),
    "subscription":         ("Subscription",        "in"),
    "dividend":             ("Dividend Payment",    "in"),
}


@member_detail_bp.route("/<uuid:member_uuid>/api/transactions", methods=["GET"])
@role_required("admin")
def api_transactions(member_uuid):
    member = _guard_member(member_uuid)
    if not member:
        return _bad("Member not found", 404, reason="not_found")

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
            .eq("org_id", _org_id()).eq("member_id", str(member_uuid))
            .order("created_at", desc=True)
            .range(start, end).execute()
        )
    except Exception as e:
        log.exception("member tx fetch failed")
        return _bad(f"Could not load transactions: {e}", 500, reason="db_error")

    rows = []
    for r in (resp.data or []):
        t = r.get("type") or ""
        label, direction = TX_LABELS.get(t, (t.replace("_", " ").title(), "in"))
        rows.append({**r, "label": label, "direction": direction})

    return jsonify({
        "ok": True,
        "transactions": rows,
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ===========================================================================
# 9. Statement (CSV)
# ===========================================================================
@member_detail_bp.route("/<uuid:member_uuid>/api/statement", methods=["GET"])
@role_required("admin")
def api_statement(member_uuid):
    member = _guard_member(member_uuid)
    if not member:
        return _bad("Member not found", 404, reason="not_found")

    admin = _admin()
    org_id = _org_id()

    try:
        mem_rows = (
            admin.table("members")
            .select("member_id, first_name, last_name, phone, email, status, "
                    "shares_units, shares_value, savings_balance, loan_balance, joined_at")
            .eq("org_id", org_id).eq("id", str(member_uuid)).limit(1).execute()
        ).data or [{}]
        mem = mem_rows[0]

        tx = (
            admin.table("member_transactions")
            .select("created_at, type, method, amount, currency, status, narration, balance_after, provider_ref")
            .eq("org_id", org_id).eq("member_id", str(member_uuid))
            .order("created_at", asc=True).limit(5000).execute()
        ).data or []
    except Exception as e:
        log.exception("statement fetch failed")
        return _bad(f"Could not export statement: {e}", 500, reason="db_error")

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["SACCO Pro — Member Full Statement"])
    w.writerow(["Member", f"{mem.get('first_name','')} {mem.get('last_name','')}".strip()])
    w.writerow(["Member ID", mem.get("member_id") or ""])
    w.writerow(["Phone", mem.get("phone") or ""])
    w.writerow(["Email", mem.get("email") or ""])
    w.writerow(["Status", mem.get("status") or ""])
    w.writerow(["Joined", mem.get("joined_at") or ""])
    w.writerow(["Shares (units)", mem.get("shares_units") or 0])
    w.writerow(["Shares (value)", mem.get("shares_value") or 0])
    w.writerow(["Savings Balance", mem.get("savings_balance") or 0])
    w.writerow(["Loan Balance", mem.get("loan_balance") or 0])
    w.writerow(["Generated", _now_iso()])
    w.writerow([])
    w.writerow(["Date", "Type", "Method", "Amount", "Currency", "Status",
                "Narration", "Balance After", "Reference"])

    for r in tx:
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

    filename = f"member_{mem.get('member_id')}_statement.csv"
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/csv; charset=utf-8",
        },
    )