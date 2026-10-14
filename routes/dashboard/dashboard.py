"""
Dashboard blueprint.

Endpoints (admin-only JSON unless noted):
    GET    /dashboard/                          -> page
    GET    /dashboard/api/overview              -> everything the dashboard needs, one shot
    GET    /dashboard/api/kpis                  -> KPI cards only (for partial refresh)
    GET    /dashboard/api/upcoming-events       -> calendar panel

Design notes:
- The dashboard is a READ MODEL. It never writes.
- One aggregate endpoint (`/api/overview`) fans out internally to every source
  table and returns a stable JSON shape. The frontend renders in one pass.
- MoM deltas are computed by comparing the current period to the previous
  one — no extra tables, no snapshots required.
- Every underlying query is a single round-trip; no N+1.
- All money is Decimal on the server and float in the response.
"""

import os
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
        "%(asctime)s [%(levelname)s] dashboard: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

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
        return Decimal(default)


def _q(v) -> Decimal:
    d = v if isinstance(v, Decimal) else Decimal(str(v if v is not None else 0))
    return d.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(v) -> float:
    return float(_q(v))


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return date(y, m, 28)


def _parse_date(v):
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _delta_pct(current, previous) -> float:
    """
    Returns (current - previous) / previous * 100 rounded to 2 dp.
    Handles previous == 0 as: +100 if current > 0 else 0.
    """
    try:
        c = Decimal(str(current or 0))
        p = Decimal(str(previous or 0))
    except Exception:
        return 0.0
    if p == 0:
        return 100.0 if c > 0 else 0.0
    return float(((c - p) / p * Decimal("100")).quantize(Decimal("0.01")))


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
@dashboard_bp.route("/", methods=["GET"])
def page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    if session["user"].get("role") != "admin":
        return redirect(url_for("auth.dashboard"))
    return render_template("dashboard/dashboard.html")


# ---------------------------------------------------------------------------
# SECTION BUILDERS
# Each returns one slice of the overview payload.
# ---------------------------------------------------------------------------

# ---------- KPIs ----------
def _build_kpis(org_id: str) -> dict:
    """
    Returns the six top-line KPIs with their MoM delta %.
    All values are in their natural unit (money in org currency, counts as int).
    """
    admin = _admin()
    today = _today()
    this_month_start = date(today.year, today.month, 1)
    last_month_start = _add_months(this_month_start, -1)
    last_month_end   = this_month_start - timedelta(days=1)

    # --- org currency + name (used by every tile) ---
    s = (admin.table("institution_settings")
         .select("currency, display_name")
         .eq("org_id", org_id).limit(1).execute()).data or [{}]
    currency = s[0].get("currency") or "UGX"
    org_name = s[0].get("display_name") or "SACCO Pro"

    # ---------- Members ----------
    # total_members / active_members
    members = (admin.table("members")
               .select("id, status, created_at, shares_units, shares_value")
               .eq("org_id", org_id).execute()).data or []

    total_members = len(members)
    active_members = sum(1 for m in members if m.get("status") == "active")

    total_members_prev = sum(
        1 for m in members
        if (_parse_date(m.get("created_at")) or today) <= last_month_end
    )

    # ---------- Shares ----------
    shares_value_total = sum((_to_decimal(m.get("shares_value")) for m in members), ZERO)
    # previous value: we don't have historic snapshots, so we approximate by
    # subtracting the last month's share purchases from the current total.
    last_month_share_purchases = (
        admin.table("share_transactions")
        .select("amount")
        .eq("org_id", org_id).eq("status", "completed").eq("type", "purchase")
        .gte("created_at", last_month_start.isoformat())
        .lte("created_at", last_month_end.isoformat() + "T23:59:59")
        .execute()
    ).data or []
    shares_prev = shares_value_total - sum(
        (_to_decimal(x.get("amount")) for x in last_month_share_purchases), ZERO
    )

    # ---------- Subscriptions (current year) ----------
    this_year = today.year
    subs = (
        admin.table("yearly_subscriptions")
        .select("amount_paid, subscription_year")
        .eq("org_id", org_id)
        .execute()
    ).data or []
    subs_this_year = sum(
        (_to_decimal(x.get("amount_paid")) for x in subs
         if int(x.get("subscription_year") or 0) == this_year),
        ZERO,
    )
    subs_prev_year = sum(
        (_to_decimal(x.get("amount_paid")) for x in subs
         if int(x.get("subscription_year") or 0) == this_year - 1),
        ZERO,
    )

    # ---------- Loans ----------
    loans = (
        admin.table("loans")
        .select("principal_amount, outstanding_principal, "
                "outstanding_interest, outstanding_penalties, status, "
                "disbursed_at")
        .eq("org_id", org_id)
        .execute()
    ).data or []

    loans_outstanding = sum(
        (_to_decimal(l.get("outstanding_principal"))
         + _to_decimal(l.get("outstanding_interest"))
         + _to_decimal(l.get("outstanding_penalties"))
         for l in loans),
        ZERO,
    )
    # previous month's outstanding: current outstanding + repayments during last
    # month - disbursements during last month. Approximation.
    last_month_disb = sum(
        (_to_decimal(l.get("principal_amount")) for l in loans
         if _parse_date(l.get("disbursed_at")) and
            last_month_start <= _parse_date(l.get("disbursed_at")) <= last_month_end),
        ZERO,
    )
    last_month_reps = (
        admin.table("repayments")
        .select("amount")
        .eq("org_id", org_id).eq("status", "completed")
        .gte("paid_at", last_month_start.isoformat())
        .lte("paid_at", last_month_end.isoformat() + "T23:59:59")
        .execute()
    ).data or []
    last_month_collected = sum(
        (_to_decimal(r.get("amount")) for r in last_month_reps), ZERO
    )
    loans_prev = loans_outstanding + last_month_collected - last_month_disb

    # ---------- Dividends ----------
    dividend_runs = (
        admin.table("dividend_runs")
        .select("total_dividend, status, financial_year")
        .eq("org_id", org_id)
        .execute()
    ).data or []

    dividends_total = sum(
        (_to_decimal(r.get("total_dividend")) for r in dividend_runs
         if r.get("status") == "posted"),
        ZERO,
    )
    dividends_prev = sum(
        (_to_decimal(r.get("total_dividend")) for r in dividend_runs
         if r.get("status") == "posted"
         and int(r.get("financial_year") or 0) == this_year - 1),
        ZERO,
    )

    # ---------- Liquidity (cash + bank + mobile) ----------
    cash_accounts = (
        admin.table("accounts")
        .select("code, balance")
        .eq("org_id", org_id)
        .in_("code", ["1000", "1010", "1020"])
        .execute()
    ).data or []
    liquidity = sum((_to_decimal(a.get("balance")) for a in cash_accounts), ZERO)

    # previous liquidity: current - net movement last month from the chart
    last_month_movements = (
        admin.table("account_transactions")
        .select("amount, debit_account_id, credit_account_id")
        .eq("org_id", org_id).eq("status", "completed")
        .gte("txn_date", last_month_start.isoformat())
        .lte("txn_date", last_month_end.isoformat())
        .execute()
    ).data or []
    # resolve cash account IDs for the movement filter
    cash_ids = set()
    for c in (admin.table("accounts").select("id, code")
              .eq("org_id", org_id).in_("code", ["1000","1010","1020"])
              .execute()).data or []:
        cash_ids.add(c["id"])

    net_last_month = ZERO
    for r in last_month_movements:
        amt = _to_decimal(r.get("amount"))
        if r.get("debit_account_id") in cash_ids:
            net_last_month += amt
        if r.get("credit_account_id") in cash_ids:
            net_last_month -= amt
    liquidity_prev = liquidity - net_last_month

    return {
        "currency": org_name and currency,   # keep both available
        "org_name": org_name,
        "items": [
            {
                "key":      "members",
                "label":    "Total Members",
                "value":    total_members,
                "previous": total_members_prev,
                "delta_pct": _delta_pct(total_members, total_members_prev),
                "type":     "count",
                "icon":     "fa-users",
                "color":    "brand",
            },
            {
                "key":      "shares",
                "label":    "Total Shares",
                "value":    _f(shares_value_total),
                "previous": _f(shares_prev),
                "delta_pct": _delta_pct(shares_value_total, shares_prev),
                "type":     "money",
                "icon":     "fa-chart-pie",
                "color":    "accent",
            },
            {
                "key":      "subscriptions",
                "label":    f"Total Subscriptions ({this_year})",
                "value":    _f(subs_this_year),
                "previous": _f(subs_prev_year),
                "delta_pct": _delta_pct(subs_this_year, subs_prev_year),
                "type":     "money",
                "icon":     "fa-coins",
                "color":    "success",
            },
            {
                "key":      "loans",
                "label":    "Total Loans Outstanding",
                "value":    _f(loans_outstanding),
                "previous": _f(loans_prev),
                "delta_pct": _delta_pct(loans_outstanding, loans_prev),
                "type":     "money",
                "icon":     "fa-hand-holding-dollar",
                "color":    "warning",
            },
            {
                "key":      "dividends",
                "label":    "Dividends Distributed",
                "value":    _f(dividends_total),
                "previous": _f(dividends_prev),
                "delta_pct": _delta_pct(dividends_total, dividends_prev),
                "type":     "money",
                "icon":     "fa-hand-holding-heart",
                "color":    "success",
            },
            {
                "key":      "liquidity",
                "label":    "Available Liquidity",
                "value":    _f(liquidity),
                "previous": _f(liquidity_prev),
                "delta_pct": _delta_pct(liquidity, liquidity_prev),
                "type":     "money",
                "icon":     "fa-vault",
                "color":    "success",
            },
        ],
    }


# ---------- Membership growth (12-month line) ----------
def _build_membership_growth(org_id: str, months: int = 12) -> list[dict]:
    today = _today()
    start = _add_months(date(today.year, today.month, 1), -(months - 1))

    members = (
        _admin().table("members")
        .select("status, created_at")
        .eq("org_id", org_id)
        .execute()
    ).data or []

    buckets = {}
    for i in range(months):
        m = _add_months(start, i)
        buckets[_month_key(m)] = {"total": 0, "active": 0}

    for m in members:
        d = _parse_date(m.get("created_at"))
        if not d:
            continue
        key = _month_key(d)
        if key not in buckets:
            continue
        buckets[key]["total"] += 1
        if m.get("status") == "active":
            buckets[key]["active"] += 1

    # cumulative — since members are permanent, total grows monotonically
    cumulative_total = 0
    cumulative_active = 0
    out = []
    for key in sorted(buckets.keys()):
        cumulative_total += buckets[key]["total"]
        cumulative_active += buckets[key]["active"]
        out.append({
            "month":  key,
            "total":  cumulative_total,
            "active": cumulative_active,
        })
    return out


# ---------- Shares & Subscriptions collection ----------
def _build_collections(org_id: str, months: int = 6) -> list[dict]:
    """
    Monthly bars: shares purchased (amount) and subscription fees collected.
    Last N months.
    """
    today = _today()
    start = _add_months(date(today.year, today.month, 1), -(months - 1))

    share_txns = (
        _admin().table("share_transactions")
        .select("amount, created_at")
        .eq("org_id", org_id).eq("status", "completed").eq("type", "purchase")
        .gte("created_at", start.isoformat())
        .execute()
    ).data or []

    sub_txns = (
        _admin().table("member_transactions")
        .select("amount, created_at")
        .eq("org_id", org_id).eq("type", "subscription").eq("status", "completed")
        .gte("created_at", start.isoformat())
        .execute()
    ).data or []

    buckets = {}
    for i in range(months):
        m = _add_months(start, i)
        buckets[_month_key(m)] = {"shares": ZERO, "subscriptions": ZERO}

    for r in share_txns:
        d = _parse_date(r.get("created_at"))
        if not d: continue
        k = _month_key(d)
        if k in buckets:
            buckets[k]["shares"] += _to_decimal(r.get("amount"))

    for r in sub_txns:
        d = _parse_date(r.get("created_at"))
        if not d: continue
        k = _month_key(d)
        if k in buckets:
            buckets[k]["subscriptions"] += _to_decimal(r.get("amount"))

    return [
        {"month": k, "shares": _f(v["shares"]), "subscriptions": _f(v["subscriptions"])}
        for k, v in sorted(buckets.items())
    ]


# ---------- Dividend distribution donut ----------
def _build_dividend_segments(org_id: str, year: int | None = None) -> dict:
    """
    Donut data for the dividend panel. Segments are computed by member.status
    on the run's payments — productive vs retired vs other — plus optional
    fund splits (education, other).

    If there's no run for the year, returns an empty but valid donut.
    """
    today = _today()
    year = year or today.year

    run = (
        _admin().table("dividend_runs")
        .select("id, total_dividend, currency, status")
        .eq("org_id", org_id).eq("financial_year", year)
        .in_("status", ["posted", "approved"])
        .order("created_at", desc=True).limit(1).execute()
    ).data or []

    if not run:
        return {"total": 0.0, "currency": "UGX", "segments": []}

    run = run[0]

    payments = (
        _admin().table("dividend_payments")
        .select("dividend_amount, member_id, "
                "members!inner(status, category)")
        .eq("org_id", org_id).eq("dividend_run_id", run["id"])
        .execute()
    ).data or []

    by_segment = {}
    total = ZERO
    for p in payments:
        m = p.get("members") or {}
        # segment by member category, defaulting to productive
        cat = (m.get("category") or "individual").lower()
        if m.get("status") not in ("active", "dormant", None):
            key = "Other Funds"
        elif cat in ("group", "corporate"):
            key = "Productive Members"
        elif cat == "individual":
            key = "Productive Members"
        else:
            key = "Other Funds"

        amt = _to_decimal(p.get("dividend_amount"))
        by_segment[key] = by_segment.get(key, ZERO) + amt
        total += amt

    segments = []
    for k, v in sorted(by_segment.items(), key=lambda x: -x[1]):
        segments.append({
            "label":   k,
            "amount":  _f(v),
            "percent": _f((v / total * Decimal("100"))) if total > 0 else 0.0,
        })

    return {
        "total":    _f(total),
        "currency": run.get("currency") or "UGX",
        "year":     year,
        "segments": segments,
    }


# ---------- Loan portfolio donut ----------
def _build_loan_products(org_id: str) -> dict:
    """
    Donut data for the loan portfolio panel: outstanding by product.
    """
    rows = (
        _admin().table("loans")
        .select("outstanding_principal, outstanding_interest, "
                "outstanding_penalties, status, "
                "loan_products!inner(id, name, category)")
        .eq("org_id", org_id)
        .in_("status", ["active", "overdue"])
        .execute()
    ).data or []

    by_prod = {}
    total = ZERO
    for r in rows:
        prod = r.get("loan_products") or {}
        key = prod.get("name") or prod.get("category") or "Other Loans"
        amt = (
            _to_decimal(r.get("outstanding_principal"))
            + _to_decimal(r.get("outstanding_interest"))
            + _to_decimal(r.get("outstanding_penalties"))
        )
        by_prod[key] = by_prod.get(key, ZERO) + amt
        total += amt

    segments = []
    for k, v in sorted(by_prod.items(), key=lambda x: -x[1]):
        segments.append({
            "label":   k,
            "amount":  _f(v),
            "percent": _f((v / total * Decimal("100"))) if total > 0 else 0.0,
        })

    return {"total": _f(total), "segments": segments}


# ---------- Recent activity ----------
def _build_recent_subscriptions(org_id: str, limit: int = 5) -> list[dict]:
    rows = (
        _admin().table("yearly_subscriptions")
        .select("id, subscription_year, amount_paid, currency, status, "
                "created_at, paid_at, "
                "members!inner(member_id, first_name, last_name)")
        .eq("org_id", org_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    ).data or []
    out = []
    for r in rows:
        m = r.get("members") or {}
        out.append({
            "id":        r.get("id"),
            "member":    f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            "member_id": m.get("member_id"),
            "amount":    _f(r.get("amount_paid")),
            "currency":  r.get("currency"),
            "year":      r.get("subscription_year"),
            "status":    r.get("status"),
            "date":      r.get("paid_at") or r.get("created_at"),
        })
    return out


def _build_recent_shares(org_id: str, limit: int = 5) -> list[dict]:
    rows = (
        _admin().table("share_transactions")
        .select("id, units, amount, currency, status, created_at, "
                "members!inner(member_id, first_name, last_name)")
        .eq("org_id", org_id).eq("type", "purchase")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    ).data or []
    out = []
    for r in rows:
        m = r.get("members") or {}
        out.append({
            "id":        r.get("id"),
            "member":    f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            "member_id": m.get("member_id"),
            "units":     _f(r.get("units")),
            "amount":    _f(r.get("amount")),
            "currency":  r.get("currency"),
            "status":    r.get("status"),
            "date":      r.get("created_at"),
        })
    return out


# ---------- Upcoming events ----------
def _build_upcoming_events(org_id: str, days: int = 90) -> list[dict]:
    today = _today()
    horizon = today + timedelta(days=days)
    events = []

    # Yearly subscription deadline: read the due month from settings
    s = (
        _admin().table("institution_settings")
        .select("subscription_due_month")
        .eq("org_id", org_id).limit(1).execute()
    ).data or [{}]
    due_month = int(s[0].get("subscription_due_month") or 1)
    due_month = max(1, min(12, due_month))
    sub_due = date(today.year, due_month, 1)
    if sub_due < today:
        sub_due = date(today.year + 1, due_month, 1)
    if sub_due <= horizon:
        events.append({
            "type":   "subscription",
            "title":  "Yearly Subscription Deadline",
            "date":   sub_due.isoformat(),
            "icon":   "fa-calendar-check",
            "color":  "danger",
        })

    # Next approved-but-not-posted dividend run (if any)
    dividend = (
        _admin().table("dividend_runs")
        .select("id, run_number, financial_year, status, declared_at")
        .eq("org_id", org_id).in_("status", ["draft", "approved"])
        .order("declared_at", desc=True).limit(1).execute()
    ).data or []
    if dividend:
        events.append({
            "type":  "dividend",
            "title": f"Dividend {dividend[0].get('financial_year')} — {dividend[0].get('status')}",
            "date":  dividend[0].get("declared_at"),
            "icon":  "fa-hand-holding-heart",
            "color": "success",
        })

    return events


# ---------- Settings summary ----------
def _build_settings_summary(org_id: str) -> dict:
    s = (
        _admin().table("institution_settings")
        .select("min_shares, min_share_price, annual_subscription_fee, "
                "subscription_due_month, currency")
        .eq("org_id", org_id).limit(1).execute()
    ).data or [{}]
    s = s[0]

    return {
        "min_shares":         _f(s.get("min_shares")),
        "min_share_price":    _f(s.get("min_share_price")),
        "yearly_subscription":_f(s.get("annual_subscription_fee")),
        "due_month":          int(s.get("subscription_due_month") or 1),
        "currency":           s.get("currency") or "UGX",
    }


# ---------- Dividend history (per year table) ----------
def _build_dividend_history(org_id: str, limit: int = 5) -> list[dict]:
    rows = (
        _admin().table("dividend_runs")
        .select("id, financial_year, total_dividend, currency, status, "
                "total_recipients, dividend_per_share")
        .eq("org_id", org_id)
        .order("financial_year", desc=True)
        .limit(limit)
        .execute()
    ).data or []
    return [
        {
            "id":            r.get("id"),
            "year":          r.get("financial_year"),
            "total":         _f(r.get("total_dividend")),
            "currency":      r.get("currency"),
            "status":        r.get("status"),
            "recipients":    r.get("total_recipients"),
            "dps":           _f(r.get("dividend_per_share")),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# OVERVIEW — one aggregate endpoint
# ---------------------------------------------------------------------------
@dashboard_bp.route("/api/overview", methods=["GET"])
@role_required("admin")
def api_overview():
    org_id = _org_id()

    try:
        kpis            = _build_kpis(org_id)
        growth          = _build_membership_growth(org_id, months=12)
        collections     = _build_collections(org_id, months=6)
        dividend_donut  = _build_dividend_segments(org_id)
        loan_donut      = _build_loan_products(org_id)
        recent_subs     = _build_recent_subscriptions(org_id, limit=5)
        recent_shares   = _build_recent_shares(org_id, limit=5)
        upcoming_events = _build_upcoming_events(org_id, days=90)
        settings        = _build_settings_summary(org_id)
        dividend_hist   = _build_dividend_history(org_id, limit=5)
    except Exception as e:
        log.exception("dashboard overview failed")
        return _bad(f"Could not load dashboard: {e}", 500, reason="overview_failed")

    # user info for the header
    user = session.get("user", {})

    return jsonify({
        "ok":        True,
        "generated": _now_iso(),
        "user": {
            "name":   user.get("full_name") or user.get("email"),
            "role":   user.get("role"),
            "org":    user.get("org_name"),
            "email":  user.get("email"),
        },
        "kpis":              kpis,
        "membership_growth": growth,
        "collections":       collections,
        "dividend_donut":    dividend_donut,
        "loan_donut":        loan_donut,
        "recent_subscriptions": recent_subs,
        "recent_shares":        recent_shares,
        "upcoming_events":      upcoming_events,
        "settings":             settings,
        "dividend_history":     dividend_hist,
    })


# ---------------------------------------------------------------------------
# Partial refresh helpers (used by polling widgets)
# ---------------------------------------------------------------------------
@dashboard_bp.route("/api/kpis", methods=["GET"])
@role_required("admin")
def api_kpis():
    org_id = _org_id()
    try:
        return jsonify({"ok": True, "kpis": _build_kpis(org_id)})
    except Exception as e:
        log.exception("kpis failed")
        return _bad(f"Could not load KPIs: {e}", 500, reason="kpi_failed")


@dashboard_bp.route("/api/upcoming-events", methods=["GET"])
@role_required("admin")
def api_upcoming_events():
    org_id = _org_id()
    try:
        days = min(365, max(1, int(request.args.get("days", 90))))
    except ValueError:
        days = 90
    try:
        return jsonify({"ok": True, "events": _build_upcoming_events(org_id, days)})
    except Exception as e:
        log.exception("upcoming events failed")
        return _bad(f"Could not load events: {e}", 500, reason="events_failed")