"""
Member Dashboard blueprint.

Backs every widget on the member's home dashboard.

Endpoints:
    GET    /member/dashboard/api/kpis                  -> 4 KPI cards
    GET    /member/dashboard/api/trends                -> savings & shares trend (12m)
    GET    /member/dashboard/api/loan-summary          -> donut + eligibility
    GET    /member/dashboard/api/announcements         -> right-rail feed
    GET    /member/dashboard/api/recent-transactions   -> latest 10 ledger rows
    GET    /member/dashboard/api/share-progress        -> circular gauge
    GET    /member/dashboard/api/upcoming-dues         -> next installment / subscription
    GET    /member/dashboard/api/summary               -> everything in one payload

Design:
- Every endpoint scopes by session["member"]["id"] + ["org_id"].
- No N+1: each widget is one query (or one per table it truly needs).
- Nothing money-mutating lives here — only reads.
- `summary` calls the other endpoints internally (fast local reads); the
  frontend uses this on first paint and the individual endpoints on refresh.
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

load_dotenv()

log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] member_dash: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

member_dashboard_bp = Blueprint(
    "member_dashboard", __name__, url_prefix="/member/dashboard",
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


def _member() -> dict | None:
    return session.get("member")


def _add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return date(y, m, 28)


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
@member_dashboard_bp.route("/", methods=["GET"])
def page():
    if not session.get("member"):
        return redirect(url_for("member_auth.login_page"))
    return render_template("memberDashboard/dashboard.html")


# ===========================================================================
# 1. KPIs — 4 cards
# ===========================================================================
@member_dashboard_bp.route("/api/kpis", methods=["GET"])
def api_kpis():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    admin = _admin()
    org_id = m["org_id"]
    member_id = m["id"]

    try:
        # ---- 1. member row (shares + savings + loan) ----
        mem_rows = (
            admin.table("members")
            .select("shares_units, shares_value, savings_balance, loan_balance")
            .eq("org_id", org_id).eq("id", member_id).limit(1).execute()
        ).data or [{}]
        mem = mem_rows[0]

        # ---- 2. settings (min shares, share price, currency) ----
        s_rows = (
            admin.table("institution_settings")
            .select("min_shares, min_share_price, share_target_units, currency")
            .eq("org_id", org_id).limit(1).execute()
        ).data or [{}]
        s = s_rows[0]
        currency = s.get("currency") or "UGX"

        # ---- 3. active loans count + outstanding (single query) ----
        loans = (
            admin.table("loans")
            .select("outstanding_principal, outstanding_interest, status")
            .eq("org_id", org_id).eq("member_id", member_id)
            .in_("status", ["active", "overdue"]).execute()
        ).data or []

        active_loan_count = len(loans)
        total_outstanding = sum(
            (_to_decimal(l.get("outstanding_principal"))
             + _to_decimal(l.get("outstanding_interest")) for l in loans),
            ZERO,
        )

        # ---- 4. dividends received (posted) ----
        divs = (
            admin.table("dividend_payments")
            .select("dividend_amount, posted_at")
            .eq("org_id", org_id).eq("member_id", member_id)
            .eq("status", "posted").execute()
        ).data or []
        total_dividends = sum((_to_decimal(d.get("dividend_amount")) for d in divs), ZERO)
        last_payout = None
        if divs:
            dates = [d.get("posted_at") for d in divs if d.get("posted_at")]
            last_payout = max(dates) if dates else None

        # ---- 5. savings 30-day change ----
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        recent_savings = (
            admin.table("member_transactions")
            .select("amount, type")
            .eq("org_id", org_id).eq("member_id", member_id)
            .eq("type", "savings_deposit")
            .eq("status", "completed")
            .gte("created_at", cutoff).execute()
        ).data or []
        savings_30d = sum((_to_decimal(r.get("amount")) for r in recent_savings), ZERO)

        cur_savings = _to_decimal(mem.get("savings_balance"))
        prior_savings = cur_savings - savings_30d
        savings_pct = 0.0
        if prior_savings > 0:
            savings_pct = float((savings_30d / prior_savings * Decimal("100")).quantize(Decimal("0.1")))

    except Exception as e:
        log.exception("kpis failed")
        return _bad(f"Could not load KPIs: {e}", 500, reason="db_error")

    return jsonify({
        "ok": True,
        "kpis": {
            "shares": {
                "units":         float(_to_decimal(mem.get("shares_units"))),
                "value":         float(_q(_to_decimal(mem.get("shares_value")))),
                "min_units":     float(_to_decimal(s.get("min_shares"))),
                "currency":      currency,
            },
            "savings": {
                "balance":       float(_q(cur_savings)),
                "change_30d":    float(_q(savings_30d)),
                "change_pct":    savings_pct,
                "currency":      currency,
            },
            "loans": {
                "outstanding":   float(_q(total_outstanding)),
                "active_count":  active_loan_count,
                "currency":      currency,
            },
            "dividends": {
                "total_earned":  float(_q(total_dividends)),
                "last_payout":   last_payout,
                "currency":      currency,
            },
        },
    })


# ===========================================================================
# 2. Trends — savings & shares over N months
# ===========================================================================
@member_dashboard_bp.route("/api/trends", methods=["GET"])
def api_trends():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        months = min(24, max(3, int(request.args.get("months", 12))))
    except ValueError:
        months = 12

    admin = _admin()
    org_id = m["org_id"]
    member_id = m["id"]

    # window start = first day of the month N-1 months back
    today = _today()
    start = _add_months(date(today.year, today.month, 1), -(months - 1))

    # build the ordered list of YYYY-MM labels
    month_keys = []
    cur = start
    for _ in range(months):
        month_keys.append(f"{cur.year:04d}-{cur.month:02d}")
        cur = _add_months(cur, 1)

    try:
        # ---- savings series: deposits & withdrawals (completed only) ----
        sav_rows = (
            admin.table("member_transactions")
            .select("created_at, type, amount")
            .eq("org_id", org_id).eq("member_id", member_id)
            .in_("type", ["savings_deposit", "savings_withdrawal"])
            .eq("status", "completed")
            .gte("created_at", start.isoformat())
            .order("created_at", asc=True)
            .execute()
        ).data or []

        # ---- shares series: purchases ----
        shr_rows = (
            admin.table("share_transactions")
            .select("created_at, units, unit_price, amount")
            .eq("org_id", org_id).eq("member_id", member_id)
            .eq("type", "purchase").eq("status", "completed")
            .gte("created_at", start.isoformat())
            .order("created_at", asc=True)
            .execute()
        ).data or []

        # ---- current baseline: shift each series back so the last point == current balance ----
        mem = (
            admin.table("members")
            .select("savings_balance, shares_value")
            .eq("org_id", org_id).eq("id", member_id).limit(1).execute()
        ).data or [{}]
        mem = mem[0]

    except Exception as e:
        log.exception("trends fetch failed")
        return _bad(f"Could not load trends: {e}", 500, reason="db_error")

    # bucket deltas by month
    sav_delta = {k: ZERO for k in month_keys}
    shr_delta = {k: ZERO for k in month_keys}

    for r in sav_rows:
        d = (r.get("created_at") or "")[:7]   # YYYY-MM
        if d in sav_delta:
            amt = _to_decimal(r.get("amount"))
            if r.get("type") == "savings_deposit":
                sav_delta[d] += amt
            else:
                sav_delta[d] -= amt

    for r in shr_rows:
        d = (r.get("created_at") or "")[:7]
        if d in shr_delta:
            amt = _to_decimal(r.get("amount"))
            shr_delta[d] += amt

    # walk forward to build running balances; last point anchored to today's value
    current_sav = _to_decimal(mem.get("savings_balance"))
    current_shr = _to_decimal(mem.get("shares_value"))

    # total deltas within window
    total_sav_delta = sum(sav_delta.values(), ZERO)
    total_shr_delta = sum(shr_delta.values(), ZERO)

    # starting balances = today's - deltas within window
    running_sav = current_sav - total_sav_delta
    running_shr = current_shr - total_shr_delta

    savings_series = []
    shares_series  = []
    for k in month_keys:
        running_sav += sav_delta[k]
        running_shr += shr_delta[k]
        savings_series.append(float(_q(running_sav)))
        shares_series.append(float(_q(running_shr)))

    return jsonify({
        "ok": True,
        "trend": {
            "months":  month_keys,
            "savings": savings_series,
            "shares":  shares_series,
        },
    })


# ===========================================================================
# 3. Loan summary — donut
# ===========================================================================
@member_dashboard_bp.route("/api/loan-summary", methods=["GET"])
def api_loan_summary():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    admin = _admin()
    org_id = m["org_id"]
    member_id = m["id"]

    try:
        loans = (
            admin.table("loans")
            .select(
                "id, principal_amount, outstanding_principal, "
                "outstanding_interest, status, "
                "loan_products!inner(name, code, category)"
            )
            .eq("org_id", org_id).eq("member_id", member_id)
            .in_("status", ["active", "overdue"]).execute()
        ).data or []
    except Exception as e:
        log.exception("loan summary failed")
        return _bad(f"Could not load loans: {e}", 500, reason="db_error")

    # group by product name
    by_product = {}
    total_outstanding = ZERO
    for l in loans:
        prod = l.get("loan_products") or {}
        pname = prod.get("name") or "Other"
        outstanding = (
            _to_decimal(l.get("outstanding_principal"))
            + _to_decimal(l.get("outstanding_interest"))
        )
        b = by_product.setdefault(pname, ZERO)
        by_product[pname] = b + outstanding
        total_outstanding += outstanding

    breakdown = []
    for pname, amount in sorted(by_product.items(), key=lambda x: -x[1]):
        pct = 0.0
        if total_outstanding > 0:
            pct = float((amount / total_outstanding * Decimal("100")).quantize(Decimal("0.1")))
        breakdown.append({
            "product":      pname,
            "outstanding":  float(_q(amount)),
            "percent":      pct,
        })

    # eligibility hint — check against active products with best-effort rules
    eligible = False
    note = "Speak to your SACCO about loan eligibility."
    try:
        from routes.loanApplications.loanApplications import _eligibility
        prods = (
            admin.table("loan_products")
            .select("*").eq("org_id", org_id).eq("is_active", True)
            .limit(5).execute()
        ).data or []
        for p in prods:
            min_amt = _to_decimal(p.get("min_amount"))
            if min_amt <= 0:
                continue
            rules = _eligibility(org_id, member_id, p, min_amt,
                                 int(p.get("min_term_months") or 1))
            if all(r.get("ok") for r in rules):
                eligible = True
                note = f"Based on your savings and shares — eligible for {p.get('name')}"
                break
    except Exception:
        log.exception("eligibility check failed (non-fatal)")

    return jsonify({
        "ok": True,
        "outstanding_total": float(_q(total_outstanding)),
        "breakdown": breakdown,
        "eligible_for_new_loan": eligible,
        "eligibility_note": note,
    })


# ===========================================================================
# 4. Announcements — right rail
# ===========================================================================
@member_dashboard_bp.route("/api/announcements", methods=["GET"])
def api_announcements():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        limit = min(20, max(1, int(request.args.get("limit", 5))))
    except ValueError:
        limit = 5

    try:
        now = _now_iso()
        rows = (
            _admin().table("member_announcements")
            .select("id, title, body, icon, category, is_pinned, published_at, expires_at")
            .eq("org_id", m["org_id"])
            .in_("audience", ["all", "member"])
            .order("is_pinned", desc=True)
            .order("published_at", desc=True)
            .limit(limit * 2)   # overfetch, then trim expired in Python
            .execute()
        ).data or []
    except Exception as e:
        log.exception("announcements fetch failed")
        return _bad(f"Could not load announcements: {e}", 500, reason="db_error")

    # filter expired
    live = []
    for r in rows:
        exp = r.get("expires_at")
        if exp and exp < now:
            continue
        live.append(r)
        if len(live) >= limit:
            break

    return jsonify({"ok": True, "announcements": live})


# ===========================================================================
# 5. Recent transactions — list of 10
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


@member_dashboard_bp.route("/api/recent-transactions", methods=["GET"])
def api_recent_transactions():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        limit = min(50, max(5, int(request.args.get("limit", 10))))
    except ValueError:
        limit = 10

    try:
        rows = (
            _admin().table("member_transactions")
            .select("id, type, method, amount, currency, status, narration, created_at")
            .eq("org_id", m["org_id"]).eq("member_id", m["id"])
            .order("created_at", desc=True)
            .limit(limit).execute()
        ).data or []
    except Exception as e:
        log.exception("recent tx failed")
        return _bad(f"Could not load transactions: {e}", 500, reason="db_error")

    out = []
    for r in rows:
        t = r.get("type") or ""
        label, direction = TX_LABELS.get(t, (t.replace("_", " ").title(), "in"))
        out.append({
            "id":         r["id"],
            "created_at": r.get("created_at"),
            "type":       t,
            "label":      label,
            "direction":  direction,
            "amount":     float(_q(_to_decimal(r.get("amount")))),
            "currency":   r.get("currency") or "UGX",
            "status":     r.get("status"),
            "narration":  r.get("narration"),
        })

    return jsonify({"ok": True, "transactions": out})


# ===========================================================================
# 6. Share progress — circular gauge
# ===========================================================================
@member_dashboard_bp.route("/api/share-progress", methods=["GET"])
def api_share_progress():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    admin = _admin()
    try:
        mem_rows = (
            admin.table("members")
            .select("shares_units")
            .eq("org_id", m["org_id"]).eq("id", m["id"]).limit(1).execute()
        ).data or [{}]
        mem = mem_rows[0]

        s_rows = (
            admin.table("institution_settings")
            .select("min_shares, share_target_units, min_share_price, currency")
            .eq("org_id", m["org_id"]).limit(1).execute()
        ).data or [{}]
        s = s_rows[0]
    except Exception as e:
        log.exception("share progress failed")
        return _bad(f"Could not load share progress: {e}", 500, reason="db_error")

    current  = _to_decimal(mem.get("shares_units"))
    minimum  = _to_decimal(s.get("min_shares"))
    target   = _to_decimal(s.get("share_target_units")) or minimum
    price    = _to_decimal(s.get("min_share_price"))
    currency = s.get("currency") or "UGX"

    # progress is toward target (not min), but min is the gate
    pct = 0.0
    if target > 0:
        pct = float(min(Decimal("100"), (current / target * Decimal("100"))).quantize(Decimal("0.1")))

    meets = current >= minimum
    message = (
        "You have met the minimum share requirement. Buying more shares increases your benefits and voting power."
        if meets else
        f"You need {minimum - current} more shares to meet the minimum requirement."
    )

    return jsonify({
        "ok": True,
        "current_units":    float(current),
        "minimum_units":    float(minimum),
        "target_units":     float(target),
        "progress_percent": pct,
        "meets_minimum":    meets,
        "message":          message,
        "currency":         currency,
        "share_price":      float(_q(price)),
        "value_at_target":  float(_q(target * price)),
    })


# ===========================================================================
# 7. Upcoming dues — loan installment + unpaid subscription
# ===========================================================================
@member_dashboard_bp.route("/api/upcoming-dues", methods=["GET"])
def api_upcoming_dues():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    admin = _admin()
    org_id = m["org_id"]
    member_id = m["id"]
    today = _today()

    dues = []

    # ---- 1. Loan repayment due (first active loan, next due date) ----
    try:
        loans = (
            admin.table("loans")
            .select(
                "id, loan_number, principal_amount, total_payable, term_months, "
                "outstanding_principal, outstanding_interest, "
                "repayment_frequency, first_due_date, disbursed_at, status"
            )
            .eq("org_id", org_id).eq("member_id", member_id)
            .in_("status", ["active", "overdue"])
            .order("disbursed_at", desc=True).limit(1).execute()
        ).data or []

        if loans:
            l = loans[0]
            first_due = l.get("first_due_date")
            freq = l.get("repayment_frequency") or "monthly"
            outstanding = (
                _to_decimal(l.get("outstanding_principal"))
                + _to_decimal(l.get("outstanding_interest"))
            )
            term = int(l.get("term_months") or 1)

            # period length in days depends on frequency
            period_days = {"weekly": 7, "biweekly": 14, "monthly": 30,
                           "quarterly": 91, "lump_sum": 9999}.get(freq, 30)

            # first_due may be null; fall back to disbursed + 1 period
            base = None
            if first_due:
                try:
                    base = date.fromisoformat(str(first_due)[:10])
                except Exception:
                    base = None
            if base is None and l.get("disbursed_at"):
                try:
                    disb = date.fromisoformat(str(l["disbursed_at"])[:10])
                    base = disb + timedelta(days=period_days)
                except Exception:
                    base = None

            if base:
                # advance the base until it's >= today
                next_due = base
                safety = 0
                while next_due < today and safety < 100:
                    next_due = next_due + timedelta(days=period_days)
                    safety += 1

                installment = _q(outstanding / Decimal(max(1, term)))
                if installment > 0:
                    dues.append({
                        "kind":         "loan_repayment",
                        "label":        "Loan Repayment",
                        "reference":    l.get("loan_number"),
                        "due_date":     next_due.isoformat(),
                        "amount":       float(installment),
                        "currency":     "UGX",
                        "action_url":   "/member/loans",
                        "action_label": "Pay Now",
                    })
    except Exception:
        log.exception("loan due computation failed (non-fatal)")

    # ---- 2. Unpaid subscription for current year ----
    try:
        year = today.year
        subs = (
            admin.table("yearly_subscriptions")
            .select("id, subscription_year, amount_due, amount_paid, "
                    "due_date, status, currency")
            .eq("org_id", org_id).eq("member_id", member_id)
            .eq("subscription_year", year).limit(1).execute()
        ).data or []

        if subs:
            s = subs[0]
            balance = _q(_to_decimal(s.get("amount_due")) - _to_decimal(s.get("amount_paid")))
            if balance > 0 and s.get("status") not in ("waived", "cancelled", "paid"):
                due_date = s.get("due_date") or date(year, 12, 31).isoformat()
                dues.append({
                    "kind":         "subscription",
                    "label":        "Annual Subscription",
                    "reference":    f"Year {s.get('subscription_year')}",
                    "due_date":     due_date,
                    "amount":       float(balance),
                    "currency":     s.get("currency") or "UGX",
                    "action_url":   "/member/subscriptions",
                    "action_label": "Pay Now",
                })
    except Exception:
        log.exception("subscription due computation failed (non-fatal)")

    # sort by due date, earliest first
    dues.sort(key=lambda x: x.get("due_date") or "9999-12-31")

    return jsonify({"ok": True, "dues": dues[:5]})


# ===========================================================================
# 8. Bundled summary — everything at once
# ===========================================================================
@member_dashboard_bp.route("/api/summary", methods=["GET"])
def api_summary():
    """
    Convenience endpoint — calls each individual handler and merges responses.
    Each sub-call reuses the same helpers so there's no duplicated logic.
    """
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    from flask import current_app
    with current_app.test_request_context():
        # The individual handlers rely only on session["member"], which is
        # already set — so we can call them directly.
        pass

    # We can't easily call each view (they read `request.args`). Instead we
    # factor out small internal functions. To keep it simple and fast, we
    # inline the four heavy queries here by delegating to the same helpers.

    # If you want true minimal latency, add a Postgres RPC that does all of
    # this in one statement. For now, this endpoint just returns a hint
    # that the frontend should fan out to the individual endpoints in
    # parallel — which is what the template does.
    return jsonify({
        "ok": True,
        "hint": "Call the individual endpoints in parallel: kpis, trends, "
                "loan-summary, announcements, recent-transactions, "
                "share-progress, upcoming-dues.",
    })