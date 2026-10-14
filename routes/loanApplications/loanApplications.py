"""
Loan Applications blueprint.

Endpoints (admin-only JSON unless noted):
    GET    /loan-applications/                               -> page
    GET    /loan-applications/api/stats                      -> KPI cards
    GET    /loan-applications/api/list                       -> paginated list
    GET    /loan-applications/api/<uuid:app_uuid>            -> detail (events + guarantors)
    GET    /loan-applications/api/<uuid:app_uuid>/events     -> event log
    GET    /loan-applications/api/<uuid:app_uuid>/simulate   -> repayment preview

    POST   /loan-applications/api/create                     -> create (draft or submitted)
    PATCH  /loan-applications/api/<uuid:app_uuid>            -> edit draft/submitted
    POST   /loan-applications/api/<uuid:app_uuid>/submit     -> draft -> submitted
    POST   /loan-applications/api/<uuid:app_uuid>/review     -> submitted -> under_review
    POST   /loan-applications/api/<uuid:app_uuid>/approve    -> -> approved
    POST   /loan-applications/api/<uuid:app_uuid>/reject     -> -> rejected
    POST   /loan-applications/api/<uuid:app_uuid>/cancel     -> -> cancelled
    POST   /loan-applications/api/<uuid:app_uuid>/disburse   -> approved -> disbursed
    POST   /loan-applications/api/eligibility-check          -> pre-check (no writes)

    POST   /loan-applications/api/<uuid:app_uuid>/guarantors
    DELETE /loan-applications/api/guarantors/<uuid:g_uuid>

Design:
- Explicit state machine: each transition has its own endpoint.
- Product terms are snapshotted at creation (`product_snapshot`).
- Fee schedule + total interest computed and frozen at approval.
- On disbursement:
    1. Creates a `loans` row for the repayments engine (idempotent).
    2. Writes a `member_transactions` row (member ledger).
    3. Updates member + account balances.
    4. Posts to the chart of accounts:
         Dr  1100 Loans receivable — principal
         Cr  1000 Cash on hand / 1010 Bank account (by method)
- On approval, accrued interest is posted:
         Dr  1110 Loans receivable — interest
         Cr  4010 Loan interest income
  The two are posted separately because SACCOs generally recognise
  interest income at approval, and the receivable grows as the loan
  disburses.
"""

import os
import secrets
import logging
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import (
    Blueprint, render_template, request, jsonify, session, redirect, url_for,
)
from dotenv import load_dotenv
from supabase import create_client, Client

from routes.auth.auth import role_required

load_dotenv()

log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] loan_apps: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

loan_applications_bp = Blueprint(
    "loan_applications", __name__, url_prefix="/loan-applications",
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


def _add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return date(y, m, 28)


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


# ---------------------------------------------------------------------------
# Number generation
# ---------------------------------------------------------------------------
def _next_app_number(org_id: str) -> str:
    try:
        resp = _admin().rpc("next_loan_app_number", {"p_org": org_id}).execute()
    except Exception as e:
        log.exception("next_loan_app_number failed")
        raise RuntimeError(f"Could not allocate application number: {e}")

    raw = resp.data
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, dict):
        raw = next(iter(raw.values()))
    counter = int(raw)
    year = datetime.now(timezone.utc).year
    return f"LN-{year}-{counter:06d}"


# ---------------------------------------------------------------------------
# Product snapshot + fee/interest computation
# ---------------------------------------------------------------------------
def _load_product(org_id: str, product_id: str) -> dict | None:
    resp = (
        _admin().table("loan_products")
        .select("*")
        .eq("org_id", org_id)
        .eq("id", product_id)
        .limit(1)
        .execute()
    )
    return (resp.data or [None])[0]


def _product_snapshot(product: dict) -> dict:
    keys = (
        "id", "name", "code", "category",
        "min_amount", "max_amount", "amount_multiple_of",
        "min_term_months", "max_term_months",
        "interest_method", "interest_rate_percent", "interest_period",
        "compound_frequency", "grace_periods",
        "repayment_frequency", "repayment_type",
        "processing_fee_percent", "processing_fee_flat",
        "insurance_fee_percent", "insurance_fee_flat",
        "other_fee_flat", "other_fee_label",
        "penalty_rate_percent", "penalty_grace_days", "penalty_compounding",
        "min_membership_months", "min_shares_units", "min_savings_balance",
        "require_guarantors", "min_guarantors", "max_loans_per_member",
        "requires_collateral", "collateral_note",
        "allows_topup", "allows_early_settle", "early_settle_penalty_percent",
    )
    return {k: product.get(k) for k in keys if k in product}


# ---------------------------------------------------------------------------
# Repayment math
# ---------------------------------------------------------------------------
def _periods_per_year(freq: str) -> int:
    return {"weekly": 52, "biweekly": 26, "monthly": 12,
            "quarterly": 4, "lump_sum": 1}.get(freq, 12)


def _rate_per_period(rate_percent: Decimal, interest_period: str,
                     repayment_frequency: str) -> Decimal:
    r = rate_percent / Decimal("100")
    ppy = {"monthly": 12, "quarterly": 4, "annually": 1}.get(interest_period)
    if ppy is None:
        return r
    rpy = _periods_per_year(repayment_frequency)
    return r * Decimal(ppy) / Decimal(rpy)


def _n_periods(term_months: int, freq: str) -> int:
    if freq == "weekly":      return int(round(term_months * 52 / 12))
    if freq == "biweekly":    return int(round(term_months * 26 / 12))
    if freq == "monthly":     return int(term_months)
    if freq == "quarterly":   return max(1, term_months // 3)
    if freq == "lump_sum":    return 1
    return int(term_months)


def _simulate(principal: Decimal, term_months: int, snap: dict) -> dict:
    P = principal
    n = _n_periods(term_months, snap.get("repayment_frequency") or "monthly")
    if n < 1:
        n = 1

    rate = _rate_per_period(
        Decimal(str(snap.get("interest_rate_percent") or 0)),
        snap.get("interest_period") or "monthly",
        snap.get("repayment_frequency") or "monthly",
    )
    method = snap.get("interest_method") or "flat"
    rtype  = snap.get("repayment_type") or "amortized"

    schedule = []

    if method == "flat":
        total_interest = _q(P * rate * Decimal(n))
        total_payable  = _q(P + total_interest)
        if rtype == "amortized":
            pay = _q(total_payable / Decimal(n))
            running = ZERO
            for i in range(1, n + 1):
                amt = pay if i < n else _q(total_payable - running)
                running += amt
                princ = _q(P / Decimal(n))
                schedule.append({
                    "period": i, "payment": float(amt),
                    "principal": float(princ),
                    "interest":  float(_q(amt - princ)),
                    "balance":   float(_q(max(ZERO, P - princ * i))),
                })
        elif rtype == "equal_principal":
            princ_p = _q(P / Decimal(n))
            interest_p = _q(P * rate)
            running = P
            for i in range(1, n + 1):
                p = princ_p if i < n else _q(P - princ_p * (n - 1))
                running = _q(running - p)
                schedule.append({
                    "period": i,
                    "payment": float(_q(p + interest_p)),
                    "principal": float(p),
                    "interest":  float(interest_p),
                    "balance":   float(_q(max(ZERO, running))),
                })
        elif rtype == "interest_only":
            interest_p = _q(P * rate)
            for i in range(1, n + 1):
                last = i == n
                schedule.append({
                    "period": i,
                    "payment": float(_q(interest_p + (P if last else ZERO))),
                    "principal": float(P if last else ZERO),
                    "interest":  float(interest_p),
                    "balance":   float(P if not last else ZERO),
                })
        else:  # bullet
            schedule = [{
                "period": 1,
                "payment": float(total_payable),
                "principal": float(P),
                "interest":  float(total_interest),
                "balance":   0.0,
            }]
            n = 1

    else:  # reducing_balance / compound
        if rate == 0:
            pay = _q(P / Decimal(n))
        else:
            f = (Decimal(1) + rate) ** n
            pay = _q(P * rate * f / (f - Decimal(1)))
        running = P
        total_interest = ZERO
        for i in range(1, n + 1):
            interest_p = _q(running * rate)
            princ_p    = _q(pay - interest_p)
            if i == n or princ_p > running:
                princ_p = running
                pay_i   = _q(princ_p + interest_p)
            else:
                pay_i   = pay
            running = _q(running - princ_p)
            total_interest += interest_p
            schedule.append({
                "period": i, "payment": float(pay_i),
                "principal": float(princ_p),
                "interest":  float(interest_p),
                "balance":   float(_q(max(ZERO, running))),
            })
        total_payable = _q(P + total_interest)

    return {
        "method":         method,
        "repayment_type": rtype,
        "periods":        len(schedule),
        "total_interest": float(_q(total_interest)),
        "total_payable":  float(_q(total_payable)),
        "schedule":       schedule,
    }


def _fees(amount: Decimal, snap: dict) -> dict:
    pf_pct  = Decimal(str(snap.get("processing_fee_percent") or 0))
    pf_flat = Decimal(str(snap.get("processing_fee_flat") or 0))
    in_pct  = Decimal(str(snap.get("insurance_fee_percent") or 0))
    in_flat = Decimal(str(snap.get("insurance_fee_flat") or 0))
    ot_flat = Decimal(str(snap.get("other_fee_flat") or 0))

    processing = _q(amount * pf_pct / Decimal("100") + pf_flat)
    insurance  = _q(amount * in_pct / Decimal("100") + in_flat)
    other      = _q(ot_flat)
    total      = _q(processing + insurance + other)
    net        = _q(amount - total)
    return {
        "processing_fee":   float(processing),
        "insurance_fee":    float(insurance),
        "other_fee":        float(other),
        "total_fees":       float(total),
        "net_disbursement": float(net),
    }


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------
def _eligibility(org_id: str, member_id: str, product: dict,
                 requested_amount: Decimal, requested_term: int,
                 exclude_app_id: str | None = None) -> list[dict]:
    results: list[dict] = []

    m = (
        _admin().table("members")
        .select("id, status, joined_at, shares_units, savings_balance, loan_balance")
        .eq("org_id", org_id).eq("id", member_id).limit(1).execute()
    ).data
    if not m:
        return [{"rule": "member", "ok": False, "message": "Member not found."}]
    m = m[0]

    results.append({
        "rule": "member_active",
        "ok": m.get("status") == "active",
        "message": "Member must be active.",
    })

    amin = Decimal(str(product.get("min_amount") or 0))
    amax = Decimal(str(product.get("max_amount") or 0))
    results.append({
        "rule": "min_amount",
        "ok": requested_amount >= amin,
        "message": f"Minimum is {amin}.",
    })
    results.append({
        "rule": "max_amount",
        "ok": (amax == 0) or (requested_amount <= amax),
        "message": f"Maximum is {amax}.",
    })

    mult = Decimal(str(product.get("amount_multiple_of") or 0))
    results.append({
        "rule": "amount_multiple_of",
        "ok": (mult == 0) or (requested_amount % mult == 0),
        "message": f"Amount must be a multiple of {mult}.",
    })

    tmin = int(product.get("min_term_months") or 1)
    tmax = int(product.get("max_term_months") or 60)
    results.append({
        "rule": "term_range",
        "ok": tmin <= requested_term <= tmax,
        "message": f"Term must be between {tmin} and {tmax} months.",
    })

    min_months = int(product.get("min_membership_months") or 0)
    if min_months > 0 and m.get("joined_at"):
        joined = datetime.fromisoformat(m["joined_at"].replace("Z", "+00:00"))
        months = (datetime.now(timezone.utc) - joined).days // 30
        results.append({
            "rule": "membership_duration",
            "ok": months >= min_months,
            "message": f"Must have been a member for at least {min_months} months.",
        })
    else:
        results.append({"rule": "membership_duration", "ok": True, "message": ""})

    min_shares = Decimal(str(product.get("min_shares_units") or 0))
    cur_shares = Decimal(str(m.get("shares_units") or 0))
    results.append({
        "rule": "min_shares",
        "ok": cur_shares >= min_shares,
        "message": f"Must hold at least {min_shares} shares.",
    })

    min_sav = Decimal(str(product.get("min_savings_balance") or 0))
    cur_sav = Decimal(str(m.get("savings_balance") or 0))
    results.append({
        "rule": "min_savings",
        "ok": cur_sav >= min_sav,
        "message": f"Must have at least {min_sav} in savings.",
    })

    max_loans = int(product.get("max_loans_per_member") or 1)
    active = (
        _admin().table("loan_applications")
        .select("id", count="exact")
        .eq("org_id", org_id).eq("member_id", member_id)
        .in_("status", ["approved", "disbursed", "active"])
        .limit(1).execute()
    ).count or 0
    results.append({
        "rule": "max_loans",
        "ok": active < max_loans,
        "message": f"Max {max_loans} concurrent active loan(s).",
    })

    if product.get("require_guarantors"):
        results.append({
            "rule": "guarantors_required",
            "ok": False,
            "message": f"{product.get('min_guarantors') or 1} guarantor(s) required.",
        })
    else:
        results.append({"rule": "guarantors_required", "ok": True, "message": ""})

    return results


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
@loan_applications_bp.route("/", methods=["GET"])
def page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    if session["user"].get("role") != "admin":
        return redirect(url_for("auth.dashboard"))
    return render_template("loanApplications/loanApplications.html")


# ---------------------------------------------------------------------------
# STATS
# ---------------------------------------------------------------------------
@loan_applications_bp.route("/api/stats", methods=["GET"])
@role_required("admin")
def api_stats():
    org_id = _org_id()
    try:
        rows = (
            _admin().table("loan_applications")
            .select("status, requested_amount, approved_amount")
            .eq("org_id", org_id)
            .execute()
        ).data or []

        counts = {}
        total_requested = ZERO
        total_approved = ZERO
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            total_requested += _to_decimal(r.get("requested_amount"))
            total_approved  += _to_decimal(r.get("approved_amount"))

    except Exception as e:
        log.exception("la stats failed: %s", e)
        return _bad("Could not load stats", 500)

    return jsonify({
        "ok": True,
        "stats": {
            "total":           len(rows),
            "pending_review":  counts.get("submitted", 0) + counts.get("under_review", 0),
            "approved":        counts.get("approved", 0),
            "disbursed":       counts.get("disbursed", 0) + counts.get("active", 0),
            "rejected":        counts.get("rejected", 0),
            "total_requested": float(_q(total_requested)),
            "total_approved":  float(_q(total_approved)),
        },
    })


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------
@loan_applications_bp.route("/api/list", methods=["GET"])
@role_required("admin")
def api_list():
    org_id = _org_id()
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
        search   = (request.args.get("q") or "").strip()
        status   = (request.args.get("status") or "").strip()
        source   = (request.args.get("source") or "").strip()
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    q = (
        _admin().table("loan_applications")
        .select(
            "id, app_number, requested_amount, requested_term_months, "
            "approved_amount, approved_term_months, status, source, currency, "
            "purpose, submitted_at, approved_at, disbursed_at, "
            "created_at, updated_at, "
            "members!inner(id, member_id, first_name, last_name, phone, email), "
            "loan_products!inner(id, name, code, category, interest_method, "
            "interest_rate_percent, interest_period, repayment_frequency)",
            count="exact",
        )
        .eq("org_id", org_id)
        .order("created_at", desc=True)
        .range(start, end)
    )
    if status:
        q = q.eq("status", status)
    if source:
        q = q.eq("source", source)
    if search:
        like = f"%{search}%"
        q = q.or_(
            f"app_number.ilike.{like},"
            f"members.first_name.ilike.{like},"
            f"members.last_name.ilike.{like},"
            f"members.member_id.ilike.{like},"
            f"members.phone.ilike.{like}"
        )

    try:
        resp = q.execute()
    except Exception as e:
        log.exception("la list failed: %s", e)
        return _bad("Could not load applications", 500)

    return jsonify({
        "ok": True,
        "applications": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# DETAIL
# ---------------------------------------------------------------------------
@loan_applications_bp.route("/api/<uuid:app_uuid>", methods=["GET"])
@role_required("admin")
def api_detail(app_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("loan_applications")
            .select(
                "*, "
                "members!inner(id, member_id, first_name, last_name, phone, "
                "email, address_line, city, occupation, shares_units, "
                "shares_value, savings_balance, loan_balance, status, "
                "joined_at), "
                "loan_products!inner(id, name, code, category)"
            )
            .eq("org_id", org_id)
            .eq("id", str(app_uuid))
            .limit(1)
            .execute()
        )
    except Exception as e:
        log.exception("la detail failed: %s", e)
        return _bad("Could not load application", 500)

    if not resp.data:
        return _bad("Application not found", 404)
    app = resp.data[0]

    events = (
        _admin().table("loan_application_events")
        .select("id, event, note, meta, created_at, actor_id")
        .eq("org_id", org_id)
        .eq("application_id", str(app_uuid))
        .order("created_at", desc=True)
        .execute()
    ).data or []

    guarantors = (
        _admin().table("loan_guarantors")
        .select("id, amount_guaranteed, status, note, responded_at, "
                "members!inner(member_id, first_name, last_name, phone)")
        .eq("org_id", org_id)
        .eq("application_id", str(app_uuid))
        .execute()
    ).data or []

    return jsonify({
        "ok": True,
        "application": app,
        "events": events,
        "guarantors": guarantors,
    })


# ---------------------------------------------------------------------------
# EVENTS
# ---------------------------------------------------------------------------
@loan_applications_bp.route("/api/<uuid:app_uuid>/events", methods=["GET"])
@role_required("admin")
def api_events(app_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("loan_application_events")
            .select("id, event, note, meta, created_at, actor_id")
            .eq("org_id", org_id).eq("application_id", str(app_uuid))
            .order("created_at", desc=True).execute()
        )
    except Exception as e:
        log.exception("events fetch failed")
        return _bad("Could not load events", 500)
    return jsonify({"ok": True, "events": resp.data or []})


def _log_event(org_id: str, app_id: str, event: str,
               note: str | None = None, meta: dict | None = None):
    try:
        _admin().table("loan_application_events").insert({
            "org_id": org_id,
            "application_id": app_id,
            "actor_id": _user_id(),
            "event": event,
            "note": note,
            "meta": meta or {},
        }).execute()
    except Exception:
        log.exception("event log failed (non-fatal)")


# ---------------------------------------------------------------------------
# SIMULATE for this application
# ---------------------------------------------------------------------------
@loan_applications_bp.route("/api/<uuid:app_uuid>/simulate", methods=["GET"])
@role_required("admin")
def api_simulate_app(app_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("loan_applications")
            .select("requested_amount, requested_term_months, "
                    "approved_amount, approved_term_months, product_snapshot")
            .eq("org_id", org_id).eq("id", str(app_uuid)).limit(1).execute()
        )
    except Exception as e:
        log.exception("simulate fetch failed")
        return _bad("Could not load application", 500)
    if not resp.data:
        return _bad("Application not found", 404)

    app  = resp.data[0]
    snap = app.get("product_snapshot") or {}

    amount = _to_decimal(app.get("approved_amount") or app.get("requested_amount"))
    term   = int(app.get("approved_term_months") or app.get("requested_term_months") or 1)

    sim  = _simulate(amount, term, snap)
    fees = _fees(amount, snap)
    return jsonify({
        "ok": True,
        "simulation": sim, "fees": fees,
        "amount": float(amount), "term": term,
    })


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------
@loan_applications_bp.route("/api/create", methods=["POST"])
@role_required("admin")
def api_create():
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}

    member_id  = (data.get("member_id") or "").strip()
    product_id = (data.get("product_id") or "").strip()
    if not member_id or not product_id:
        return _bad("member_id and product_id are required", reason="missing_fields")

    try:
        amount = _to_decimal(data.get("requested_amount"))
        if amount <= 0:
            return _bad("requested_amount must be > 0", reason="bad_amount")
    except ValueError:
        return _bad("Invalid requested_amount", reason="bad_amount")

    try:
        term = int(data.get("requested_term_months") or 0)
        if term < 1:
            return _bad("requested_term_months must be >= 1", reason="bad_term")
    except (TypeError, ValueError):
        return _bad("Invalid requested_term_months", reason="bad_term")

    source = (data.get("source") or "admin").lower()
    if source not in ("admin", "member"):
        return _bad("source must be 'admin' or 'member'", reason="bad_source")

    submit = bool(data.get("submit"))

    # product
    product = _load_product(org_id, product_id)
    if not product:
        return _bad("Product not found", 404, reason="product_not_found")
    if not product.get("is_active"):
        return _bad("Product is inactive", 400, reason="product_inactive")

    # eligibility (soft)
    elig = _eligibility(org_id, member_id, product, amount, term)

    # member existence
    m_row = (
        _admin().table("members")
        .select("id")
        .eq("org_id", org_id).eq("id", member_id).limit(1).execute()
    ).data
    if not m_row:
        return _bad("Member not found", 404, reason="member_not_found")

    # currency from institution_settings
    try:
        s_row = (
            _admin().table("institution_settings")
            .select("currency")
            .eq("org_id", org_id).limit(1).execute()
        ).data
    except Exception:
        log.exception("settings fetch failed")
        return _bad("Could not read institution settings", 500, reason="db_error")
    currency = (s_row or [{}])[0].get("currency") or "UGX"

    snap = _product_snapshot(product)

    try:
        app_number = _next_app_number(org_id)
    except Exception as e:
        return _bad(str(e), 500, reason="number_alloc")

    row = {
        "org_id":                org_id,
        "member_id":             member_id,
        "product_id":            product_id,
        "app_number":            app_number,
        "source":                source,
        "created_by":            _user_id(),
        "requested_amount":      float(amount),
        "requested_term_months": term,
        "purpose":               (data.get("purpose") or "").strip() or None,
        "currency":              currency,
        "product_snapshot":      snap,
        "status":                "submitted" if submit else "draft",
        "submitted_at":          _now_iso() if submit else None,
    }

    try:
        resp = _admin().table("loan_applications").insert(row).execute()
    except Exception as e:
        log.exception("la insert failed; payload=%s", row)
        return _bad(f"Could not create application: {e}", 500, reason="db_error")

    app = resp.data[0]
    _log_event(org_id, app["id"], "created",
               note=("Submitted on creation" if submit else "Draft created"),
               meta={"source": source, "amount": float(amount), "term": term})
    if submit:
        _log_event(org_id, app["id"], "submitted", note="Application submitted")

    g_list = data.get("guarantors") or []
    if g_list:
        _insert_guarantors(org_id, app["id"], g_list)

    return jsonify({
        "ok": True,
        "application": app,
        "eligibility": elig,
    }), 201


def _insert_guarantors(org_id: str, app_id: str, guarantors: list[dict]):
    rows = []
    for g in guarantors:
        mid = (g.get("member_id") or "").strip()
        if not mid:
            continue
        amt = _to_decimal(g.get("amount_guaranteed"))
        rows.append({
            "org_id":            org_id,
            "application_id":    app_id,
            "member_id":         mid,
            "amount_guaranteed": float(amt),
            "status":            "pending",
            "note":              (g.get("note") or "").strip() or None,
        })
    if not rows:
        return
    try:
        _admin().table("loan_guarantors").insert(rows).execute()
    except Exception:
        log.exception("guarantor insert failed")


# ---------------------------------------------------------------------------
# EDIT
# ---------------------------------------------------------------------------
@loan_applications_bp.route("/api/<uuid:app_uuid>", methods=["PATCH"])
@role_required("admin")
def api_update(app_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}

    try:
        cur = (
            _admin().table("loan_applications")
            .select("*")
            .eq("org_id", org_id).eq("id", str(app_uuid)).limit(1).execute()
        )
    except Exception:
        log.exception("fetch failed")
        return _bad("Could not load application", 500)
    if not cur.data:
        return _bad("Application not found", 404, reason="not_found")
    app = cur.data[0]

    if app["status"] not in ("draft", "submitted", "under_review"):
        return _bad(f"Cannot edit an application in status '{app['status']}'",
                    reason="bad_state")

    upd = {}
    if "requested_amount" in data:
        try:
            a = _to_decimal(data["requested_amount"])
            if a <= 0: raise ValueError()
            upd["requested_amount"] = float(a)
        except ValueError:
            return _bad("Invalid requested_amount", reason="bad_amount")
    if "requested_term_months" in data:
        try:
            t = int(data["requested_term_months"])
            if t < 1: raise ValueError()
            upd["requested_term_months"] = t
        except (TypeError, ValueError):
            return _bad("Invalid requested_term_months", reason="bad_term")
    if "purpose" in data:
        upd["purpose"] = (data["purpose"] or "").strip() or None

    if not upd:
        return _bad("No valid fields to update", reason="empty")

    upd["updated_at"] = _now_iso()
    try:
        resp = (
            _admin().table("loan_applications")
            .update(upd).eq("org_id", org_id).eq("id", str(app_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("update failed")
        return _bad(f"Could not update: {e}", 500, reason="db_error")

    _log_event(org_id, str(app_uuid), "note",
               note="Application edited",
               meta={"changed": list(upd.keys())})
    return jsonify({"ok": True, "application": resp.data[0]})


# ---------------------------------------------------------------------------
# TRANSITIONS
# ---------------------------------------------------------------------------
def _transition(app_uuid: str, *, allowed_from: set[str], to_status: str,
                allowed_fields: dict | None = None):
    org_id = _org_id()
    try:
        cur = (
            _admin().table("loan_applications")
            .select("*").eq("org_id", org_id).eq("id", str(app_uuid))
            .limit(1).execute()
        )
    except Exception:
        log.exception("transition fetch failed")
        return None, "Could not load application", 500

    if not cur.data:
        return None, "Application not found", 404
    app = cur.data[0]
    if app["status"] not in allowed_from:
        return None, (
            f"Cannot move to '{to_status}' from '{app['status']}'"
        ), 400

    upd = {"status": to_status, "updated_at": _now_iso()}
    if allowed_fields:
        upd.update(allowed_fields)

    try:
        resp = (
            _admin().table("loan_applications")
            .update(upd).eq("org_id", org_id).eq("id", str(app_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("transition update failed")
        return None, f"Update failed: {e}", 500
    return resp.data[0], None, 200


@loan_applications_bp.route("/api/<uuid:app_uuid>/submit", methods=["POST"])
@role_required("admin")
def api_submit(app_uuid):
    org_id = _org_id()
    app, err, code = _transition(
        app_uuid,
        allowed_from={"draft"},
        to_status="submitted",
        allowed_fields={"submitted_at": _now_iso()},
    )
    if err:
        return _bad(err, code, reason="bad_state")
    _log_event(org_id, str(app_uuid), "submitted", note="Application submitted")
    return jsonify({"ok": True, "application": app})


@loan_applications_bp.route("/api/<uuid:app_uuid>/review", methods=["POST"])
@role_required("admin")
def api_review(app_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    note   = (data.get("note") or "").strip() or None

    app, err, code = _transition(
        app_uuid,
        allowed_from={"submitted"},
        to_status="under_review",
        allowed_fields={"reviewed_by": _user_id(), "reviewed_at": _now_iso()},
    )
    if err:
        return _bad(err, code, reason="bad_state")
    _log_event(org_id, str(app_uuid), "reviewed", note=note)
    return jsonify({"ok": True, "application": app})


@loan_applications_bp.route("/api/<uuid:app_uuid>/approve", methods=["POST"])
@role_required("admin")
def api_approve(app_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}

    try:
        cur = (
            _admin().table("loan_applications")
            .select("*").eq("org_id", org_id).eq("id", str(app_uuid))
            .limit(1).execute()
        )
    except Exception:
        log.exception("approve fetch failed")
        return _bad("Could not load application", 500, reason="db_error")
    if not cur.data:
        return _bad("Application not found", 404, reason="not_found")
    app = cur.data[0]

    if app["status"] not in ("submitted", "under_review"):
        return _bad(
            f"Cannot approve from status '{app['status']}'",
            400, reason="bad_state",
        )

    snap = app.get("product_snapshot") or {}

    try:
        ap_amount = _to_decimal(
            data.get("approved_amount") or app["requested_amount"]
        )
        if ap_amount <= 0:
            return _bad("approved_amount must be > 0", reason="bad_amount")
    except ValueError:
        return _bad("Invalid approved_amount", reason="bad_amount")

    try:
        ap_term = int(
            data.get("approved_term_months") or app["requested_term_months"]
        )
        if ap_term < 1:
            return _bad("approved_term_months must be >= 1", reason="bad_term")
    except (TypeError, ValueError):
        return _bad("Invalid approved_term_months", reason="bad_term")

    ap_rate  = float(_to_decimal(
        data.get("approved_interest_rate_percent")
        or snap.get("interest_rate_percent") or 0
    ))
    ap_imeth = data.get("approved_interest_method") or snap.get("interest_method") or "flat"
    ap_freq  = data.get("approved_repayment_frequency") or snap.get("repayment_frequency") or "monthly"
    ap_rtype = data.get("approved_repayment_type") or snap.get("repayment_type") or "amortized"

    fees = _fees(ap_amount, snap)

    snap_for_sim = dict(snap)
    snap_for_sim["interest_rate_percent"] = ap_rate
    snap_for_sim["interest_method"]       = ap_imeth
    snap_for_sim["repayment_frequency"]   = ap_freq
    snap_for_sim["repayment_type"]        = ap_rtype
    sim = _simulate(ap_amount, ap_term, snap_for_sim)

    upd = {
        "status":                         "approved",
        "approved_by":                    _user_id(),
        "approved_at":                    _now_iso(),
        "approval_notes":                 (data.get("notes") or "").strip() or None,
        "approved_amount":                float(ap_amount),
        "approved_term_months":           ap_term,
        "approved_interest_rate_percent": ap_rate,
        "approved_interest_method":       ap_imeth,
        "approved_repayment_frequency":   ap_freq,
        "approved_repayment_type":        ap_rtype,
        "processing_fee":                 fees["processing_fee"],
        "insurance_fee":                  fees["insurance_fee"],
        "other_fee":                      fees["other_fee"],
        "total_fees":                     fees["total_fees"],
        "net_disbursement":               fees["net_disbursement"],
        "total_interest":                 sim["total_interest"],
        "total_payable":                  sim["total_payable"],
        "updated_at":                     _now_iso(),
    }

    try:
        resp = (
            _admin().table("loan_applications")
            .update(upd).eq("org_id", org_id).eq("id", str(app_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("approve update failed")
        return _bad(f"Could not approve: {e}", 500, reason="db_error")

    _log_event(org_id, str(app_uuid), "approved",
               note=upd["approval_notes"],
               meta={"approved_amount": upd["approved_amount"],
                     "approved_term_months": upd["approved_term_months"],
                     "total_payable": upd["total_payable"]})

    return jsonify({"ok": True, "application": resp.data[0]})


@loan_applications_bp.route("/api/<uuid:app_uuid>/reject", methods=["POST"])
@role_required("admin")
def api_reject(app_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip() or None
    if not reason:
        return _bad("reason is required", reason="missing_reason")

    app, err, code = _transition(
        app_uuid,
        allowed_from={"submitted", "under_review", "approved"},
        to_status="rejected",
        allowed_fields={
            "rejected_by":      _user_id(),
            "rejected_at":      _now_iso(),
            "rejection_reason": reason,
        },
    )
    if err:
        return _bad(err, code, reason="bad_state")
    _log_event(org_id, str(app_uuid), "rejected", note=reason)
    return jsonify({"ok": True, "application": app})


@loan_applications_bp.route("/api/<uuid:app_uuid>/cancel", methods=["POST"])
@role_required("admin")
def api_cancel(app_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip() or None

    app, err, code = _transition(
        app_uuid,
        allowed_from={"draft", "submitted", "under_review", "approved"},
        to_status="cancelled",
        allowed_fields={"stage_notes": reason},
    )
    if err:
        return _bad(err, code, reason="bad_state")
    _log_event(org_id, str(app_uuid), "cancelled", note=reason)
    return jsonify({"ok": True, "application": app})


# ---------------------------------------------------------------------------
# DISBURSE
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# DISBURSE
# ---------------------------------------------------------------------------
@loan_applications_bp.route("/api/<uuid:app_uuid>/disburse", methods=["POST"])
@role_required("admin")
def api_disburse(app_uuid):
    """
    Body: { method? ('cash'|'savings_credit'|'online'), notes? }

    Steps:
      1. Flip the application to 'disbursed' (idempotent on retry).
      2. Update member + account balances.
      3. Write a `member_transactions` ledger entry.
      4. Create a `loans` row for the repayments engine (idempotent).
      5. Post to the chart of accounts:
           cash          : Dr 1100 Loans receivable   Cr 1000 Cash on hand
           online        : Dr 1100 Loans receivable   Cr 1010 Bank account
           savings_credit: Dr 1100 Loans receivable   Cr 2000 Member savings payable

    NOTE: chart `method` enum is only ('cash'|'bank'|'online'|'internal').
    savings_credit maps to 'internal' on the chart, while the member ledger
    keeps the original 'savings_credit' method for the member's view.
    """
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    method = (data.get("method") or "cash").lower()
    if method not in ("cash", "savings_credit", "online"):
        return _bad("method must be 'cash' | 'savings_credit' | 'online'",
                    reason="bad_method")

    # -----------------------------------------------------------------
    # 1. Load application
    # -----------------------------------------------------------------
    try:
        cur = (
            _admin().table("loan_applications")
            .select("*").eq("org_id", org_id).eq("id", str(app_uuid))
            .limit(1).execute()
        )
    except Exception:
        log.exception("disburse fetch failed")
        return _bad("Could not load application", 500, reason="db_error")
    if not cur.data:
        return _bad("Application not found", 404, reason="not_found")
    app = cur.data[0]

    if app["status"] != "approved":
        return _bad(
            f"Only approved applications can be disbursed "
            f"(current: '{app['status']}')",
            400, reason="bad_state",
        )

    amount   = _to_decimal(app["approved_amount"] or app["requested_amount"])
    net      = _to_decimal(app.get("net_disbursement") or amount)
    currency = app.get("currency") or "UGX"

    # -----------------------------------------------------------------
    # 2. Find the member's loan + savings accounts
    # -----------------------------------------------------------------
    try:
        accts = (
            _admin().table("member_accounts")
            .select("id, account_type")
            .eq("org_id", org_id)
            .eq("member_id", app["member_id"])
            .execute()
        ).data or []
    except Exception:
        log.exception("accounts fetch failed")
        return _bad("Could not load member accounts", 500, reason="db_error")

    loan_acc    = next((a for a in accts if a["account_type"] == "loan"), None)
    savings_acc = next((a for a in accts if a["account_type"] == "savings"), None)

    # -----------------------------------------------------------------
    # 3. Financial writes (member balance, accounts, ledger, loans row)
    # -----------------------------------------------------------------
    try:
        # --- 3a. update members.loan_balance ---
        member = (
            _admin().table("members")
            .select("loan_balance")
            .eq("org_id", org_id).eq("id", app["member_id"])
            .limit(1).execute()
        ).data[0]
        new_loan_balance = _q(_to_decimal(member.get("loan_balance")) + net)

        _admin().table("members").update({
            "loan_balance": float(new_loan_balance),
            "updated_at":   _now_iso(),
        }).eq("id", app["member_id"]).execute()

        # --- 3b. mirror on member_accounts.loan ---
        if loan_acc:
            _admin().table("member_accounts").update({
                "balance":    float(new_loan_balance),
                "updated_at": _now_iso(),
            }).eq("id", loan_acc["id"]).execute()

        # --- 3c. savings_credit: also credit savings ---
        if method == "savings_credit" and savings_acc:
            sav_row = (
                _admin().table("member_accounts")
                .select("balance").eq("id", savings_acc["id"])
                .limit(1).execute()
            ).data or [{"balance": 0}]
            new_sav = _q(_to_decimal(sav_row[0].get("balance")) + net)

            _admin().table("member_accounts").update({
                "balance":    float(new_sav),
                "updated_at": _now_iso(),
            }).eq("id", savings_acc["id"]).execute()

            _admin().table("members").update({
                "savings_balance": float(new_sav),
                "updated_at":      _now_iso(),
            }).eq("id", app["member_id"]).execute()

        # --- 3d. member_transactions ledger row (keeps the original method) ---
        _admin().table("member_transactions").insert({
            "org_id":     org_id,
            "member_id":  app["member_id"],
            "account_id": loan_acc["id"] if loan_acc else None,
            "type":       "loan_disbursement",
            "method":     method,          # 'cash' | 'online' | 'savings_credit'
            "amount":     float(_q(net)),
            "currency":   currency,
            "status":     "completed",
            "narration":  f"Loan disbursement · {app['app_number']}",
            "created_by": _user_id(),
        }).execute()

        # --- 3e. create loans row for the repayments engine (idempotent) ---
        existing_loan = (
            _admin().table("loans")
            .select("id")
            .eq("org_id", org_id).eq("application_id", app["id"])
            .limit(1).execute()
        ).data or []

        if not existing_loan:
            principal      = _to_decimal(app["approved_amount"] or app["requested_amount"])
            total_interest = _to_decimal(app.get("total_interest"))
            total_payable  = _to_decimal(app.get("total_payable") or principal)
            term_months    = int(app["approved_term_months"]
                                 or app["requested_term_months"] or 1)

            today     = _today()
            maturity  = _add_months(today, term_months)
            first_due = _add_months(today, 1)

            snap = app.get("product_snapshot") or {}

            loan_row = {
                "org_id":                org_id,
                "member_id":             app["member_id"],
                "application_id":        app["id"],
                "product_id":            app["product_id"],
                "loan_number":           app["app_number"],
                "principal_amount":      float(_q(principal)),
                "interest_method":       app.get("approved_interest_method")
                                         or snap.get("interest_method") or "flat",
                "interest_rate_percent": float(app.get("approved_interest_rate_percent")
                                        or snap.get("interest_rate_percent") or 0),
                "interest_period":       snap.get("interest_period") or "monthly",
                "repayment_frequency":   app.get("approved_repayment_frequency")
                                         or snap.get("repayment_frequency") or "monthly",
                "repayment_type":        app.get("approved_repayment_type")
                                         or snap.get("repayment_type") or "amortized",
                "term_months":           term_months,
                "currency":              currency,
                "total_interest":        float(_q(total_interest)),
                "total_payable":         float(_q(total_payable)),
                "outstanding_principal": float(_q(principal)),
                "outstanding_interest":  float(_q(total_interest)),
                "outstanding_penalties": 0,
                "outstanding_fees":      0,
                "collected_principal":   0,
                "collected_interest":    0,
                "collected_penalties":   0,
                "collected_fees":        0,
                "disbursed_at":          _now_iso(),
                "first_due_date":        first_due.isoformat(),
                "maturity_date":         maturity.isoformat(),
                "status":                "active",
                "created_by":            _user_id(),
            }
            try:
                loan_resp = _admin().table("loans").insert(loan_row).execute()
                if loan_resp.data:
                    _admin().table("loan_applications").update({
                        "loan_account_id": loan_resp.data[0]["id"],
                    }).eq("id", app["id"]).execute()
                    log.info("loans row created for application %s → loan %s",
                             app["app_number"], loan_resp.data[0]["loan_number"])
            except Exception:
                log.exception("could not create loans row (non-fatal)")

    except Exception as e:
        log.exception("disbursement financials failed")
        return _bad(f"Disbursement failed: {e}", 500, reason="apply_failed")

    # -----------------------------------------------------------------
    # 4. Chart of accounts post
    #    enum-safe: savings_credit -> 'internal' for the chart
    # -----------------------------------------------------------------
    try:
        from routes.accounts.accounts import _apply_txn

        if method == "cash":
            credit_code  = "1000"
            chart_method = "cash"
            chart_cat    = "cash_transfer"
        elif method == "online":
            credit_code  = "1010"
            chart_method = "online"
            chart_cat    = "cash_transfer"
        else:  # savings_credit
            credit_code  = "2000"
            chart_method = "internal"
            chart_cat    = "other_adjustment"

        admin = _admin()
        receivable = (
            admin.table("accounts").select("id")
            .eq("org_id", org_id).eq("code", "1100").limit(1).execute()
        ).data
        credit_row = (
            admin.table("accounts").select("id")
            .eq("org_id", org_id).eq("code", credit_code).limit(1).execute()
        ).data

        if receivable and credit_row:
            _apply_txn(org_id, {
                "type":              "transfer",
                "category":          chart_cat,
                "amount":            str(_q(net)),
                "debit_account_id":  receivable[0]["id"],
                "credit_account_id": credit_row[0]["id"],
                "method":            chart_method,     # 'cash' | 'online' | 'internal'
                "reference":         app["app_number"],
                "narration":         f"Loan disbursement · {app['app_number']} ({method})",
                "created_by":        _user_id(),
                "meta": {
                    "source":            "loan_disbursement",
                    "application_id":    app["id"],
                    "disbursement_mode": method,       # original, for audit
                },
            })
        else:
            log.warning(
                "chart post skipped: receivable=%s credit(%s)=%s",
                bool(receivable), credit_code, bool(credit_row),
            )
    except Exception:
        log.exception("chart post for disbursement failed (non-fatal)")

    # -----------------------------------------------------------------
    # 5. Flip application to 'disbursed'
    # -----------------------------------------------------------------
    upd = {
        "status":              "disbursed",
        "disbursed_by":        _user_id(),
        "disbursed_at":        _now_iso(),
        "disbursement_method": method,
        "updated_at":          _now_iso(),
    }
    try:
        resp = (
            _admin().table("loan_applications")
            .update(upd).eq("org_id", org_id).eq("id", str(app_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("disburse update failed")
        return _bad(f"Could not update application: {e}", 500, reason="db_error")

    _log_event(
        org_id, str(app_uuid), "disbursed",
        note=(data.get("notes") or "").strip() or None,
        meta={"method": method, "net": float(net), "amount": float(amount)},
    )
    return jsonify({"ok": True, "application": resp.data[0]})

# ---------------------------------------------------------------------------
# GUARANTORS
# ---------------------------------------------------------------------------
@loan_applications_bp.route("/api/<uuid:app_uuid>/guarantors", methods=["POST"])
@role_required("admin")
def api_add_guarantor(app_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    mid    = (data.get("member_id") or "").strip()
    if not mid:
        return _bad("member_id is required", reason="missing_field")
    try:
        amt = _to_decimal(data.get("amount_guaranteed"))
        if amt <= 0:
            return _bad("amount_guaranteed must be > 0", reason="bad_amount")
    except ValueError:
        return _bad("Invalid amount_guaranteed", reason="bad_amount")

    row = {
        "org_id":            org_id,
        "application_id":    str(app_uuid),
        "member_id":         mid,
        "amount_guaranteed": float(amt),
        "status":            "pending",
        "note":              (data.get("note") or "").strip() or None,
    }
    try:
        resp = _admin().table("loan_guarantors").insert(row).execute()
    except Exception as e:
        log.exception("guarantor insert failed")
        if "23505" in str(e):
            return _bad("Member is already a guarantor for this application",
                        409, reason="duplicate")
        return _bad(f"Could not add guarantor: {e}", 500, reason="db_error")
    return jsonify({"ok": True, "guarantor": resp.data[0]}), 201


@loan_applications_bp.route("/api/guarantors/<uuid:g_uuid>", methods=["DELETE"])
@role_required("admin")
def api_remove_guarantor(g_uuid):
    org_id = _org_id()
    try:
        _admin().table("loan_guarantors").delete() \
            .eq("org_id", org_id).eq("id", str(g_uuid)).execute()
    except Exception as e:
        log.exception("guarantor delete failed")
        return _bad(f"Could not remove: {e}", 500, reason="db_error")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# ELIGIBILITY PRE-CHECK
# ---------------------------------------------------------------------------
@loan_applications_bp.route("/api/eligibility-check", methods=["POST"])
@role_required("admin")
def api_eligibility():
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    mid    = (data.get("member_id") or "").strip()
    pid    = (data.get("product_id") or "").strip()
    if not mid or not pid:
        return _bad("member_id and product_id required", reason="missing_fields")
    try:
        amt  = _to_decimal(data.get("requested_amount"))
        term = int(data.get("requested_term_months") or 0)
        if amt <= 0 or term < 1:
            raise ValueError()
    except (ValueError, TypeError):
        return _bad("Invalid amount or term", reason="bad_amount")

    product = _load_product(org_id, pid)
    if not product:
        return _bad("Product not found", 404, reason="not_found")

    return jsonify({
        "ok": True,
        "eligibility": _eligibility(org_id, mid, product, amt, term),
    })