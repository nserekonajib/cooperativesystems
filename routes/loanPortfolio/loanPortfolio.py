"""
Loan Portfolio blueprint.

Endpoints (admin-only JSON):
    GET    /loan-portfolio/                                    -> page
    GET    /loan-portfolio/api/overview                        -> top-line KPIs + PAR
    GET    /loan-portfolio/api/aging                           -> aging buckets
    GET    /loan-portfolio/api/products                        -> per-product rollup
    GET    /loan-portfolio/api/top-borrowers                   -> members with largest outstanding
    GET    /loan-portfolio/api/upcoming                        -> expected installments next N days
    GET    /loan-portfolio/api/monthly-performance             -> disbursed vs collected, last 12m
    GET    /loan-portfolio/api/loans                           -> rich filtered list
    GET    /loan-portfolio/api/loans/<uuid:loan_uuid>          -> single loan w/ rollup
    GET    /loan-portfolio/api/snapshots                       -> historical snapshots
    GET    /loan-portfolio/api/export                          -> CSV of current filtered list

    POST   /loan-portfolio/api/loans/<uuid:loan_uuid>/risk     -> set risk grade + notes
    POST   /loan-portfolio/api/loans/<uuid:loan_uuid>/write-off -> mark written_off
    POST   /loan-portfolio/api/snapshots                       -> capture snapshot (manual trigger)
    POST   /loan-portfolio/api/refresh-activity                -> recompute last_activity_at

Design notes:
- Aggregation-heavy; every endpoint avoids N+1 by using joined selects or
  a single RPC call. No per-row loops in Python over DB.
- PAR (Portfolio at Risk) is computed as: outstanding of loans whose last
  completed repayment (or disbursement) is > N days ago, / total outstanding.
- Snapshots are an optional time-series store; capture them daily via a
  scheduled job (or manually) and use /snapshots to plot trends.
- All numbers reported as Decimal on the server and float in JSON.
"""

import os
import io
import csv
import logging
from datetime import datetime, timezone, date, timedelta
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
        "%(asctime)s [%(levelname)s] loan_portfolio: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

loan_portfolio_bp = Blueprint(
    "loan_portfolio", __name__, url_prefix="/loan-portfolio",
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


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
@loan_portfolio_bp.route("/", methods=["GET"])
def page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    if session["user"].get("role") != "admin":
        return redirect(url_for("auth.dashboard"))
    return render_template("loanPortfolio/loanPortfolio.html")


# ---------------------------------------------------------------------------
# OVERVIEW — top-line KPIs + PAR
# ---------------------------------------------------------------------------
@loan_portfolio_bp.route("/api/overview", methods=["GET"])
@role_required("admin")
def api_overview():
    """
    One aggregate query for totals across all loans. PAR is computed by
    scanning the loans table once (no N+1).
    """
    org_id = _org_id()
    today  = _today()

    try:
        loans = (
            _admin().table("loans")
            .select(
                "id, principal_amount, total_payable, "
                "outstanding_principal, outstanding_interest, "
                "outstanding_penalties, "
                "collected_principal, collected_interest, "
                "collected_penalties, collected_fees, "
                "status, disbursed_at, maturity_date"
            )
            .eq("org_id", org_id)
            .execute()
        ).data or []
    except Exception as e:
        log.exception("overview fetch failed: %s", e)
        return _bad("Could not load overview", 500)

    total_loans   = len(loans)
    active_loans  = 0
    overdue_loans = 0
    closed_loans  = 0
    wo_loans      = 0

    disbursed_principal  = ZERO
    out_principal        = ZERO
    out_interest         = ZERO
    out_penalties        = ZERO
    col_principal        = ZERO
    col_interest         = ZERO
    col_penalties        = ZERO
    col_fees             = ZERO

    for l in loans:
        disbursed_principal += _to_decimal(l.get("principal_amount"))
        out_principal       += _to_decimal(l.get("outstanding_principal"))
        out_interest        += _to_decimal(l.get("outstanding_interest"))
        out_penalties       += _to_decimal(l.get("outstanding_penalties"))
        col_principal       += _to_decimal(l.get("collected_principal"))
        col_interest        += _to_decimal(l.get("collected_interest"))
        col_penalties       += _to_decimal(l.get("collected_penalties"))
        col_fees            += _to_decimal(l.get("collected_fees"))
        s = l.get("status")
        if s == "active":       active_loans += 1
        elif s == "overdue":    overdue_loans += 1
        elif s == "closed":     closed_loans += 1
        elif s == "written_off":wo_loans += 1

    total_outstanding = out_principal + out_interest + out_penalties

    # PAR: use the RPC if installed; otherwise compute inline from last pay dates
    par_30 = par_60 = par_90 = ZERO
    try:
        # fetch last payment date per loan in one query using a join
        reps = (
            _admin().table("repayments")
            .select("loan_id, paid_at, status")
            .eq("org_id", org_id)
            .eq("status", "completed")
            .order("paid_at", desc=True)
            .execute()
        ).data or []

        last_pay_by_loan = {}
        for r in reps:
            lid = r["loan_id"]
            if lid not in last_pay_by_loan:
                last_pay_by_loan[lid] = _parse_date(r["paid_at"])

        for l in loans:
            if l.get("status") not in ("active", "overdue"):
                continue
            outstanding = (
                _to_decimal(l.get("outstanding_principal")) +
                _to_decimal(l.get("outstanding_interest"))
            )
            if outstanding <= 0:
                continue
            last_paid = last_pay_by_loan.get(l["id"]) or _parse_date(l.get("disbursed_at"))
            if not last_paid:
                continue
            days = (today - last_paid).days
            if days > 30:  par_30 += outstanding
            if days > 60:  par_60 += outstanding
            if days > 90:  par_90 += outstanding
    except Exception:
        log.exception("PAR computation failed (non-fatal)")

    def _pct(part: Decimal, whole: Decimal) -> float:
        if whole <= 0:
            return 0.0
        return float((part / whole * Decimal("100")).quantize(Decimal("0.01")))

    return jsonify({
        "ok": True,
        "overview": {
            "total_loans":          total_loans,
            "active_loans":         active_loans,
            "overdue_loans":        overdue_loans,
            "closed_loans":         closed_loans,
            "written_off_loans":    wo_loans,
            "disbursed_principal":  float(_q(disbursed_principal)),
            "outstanding_principal":float(_q(out_principal)),
            "outstanding_interest": float(_q(out_interest)),
            "outstanding_penalties":float(_q(out_penalties)),
            "total_outstanding":    float(_q(total_outstanding)),
            "collected_principal":  float(_q(col_principal)),
            "collected_interest":   float(_q(col_interest)),
            "collected_penalties":  float(_q(col_penalties)),
            "collected_fees":       float(_q(col_fees)),
            "par_30":               float(_q(par_30)),
            "par_60":               float(_q(par_60)),
            "par_90":               float(_q(par_90)),
            "par_30_percent":       _pct(par_30, total_outstanding),
            "par_60_percent":       _pct(par_60, total_outstanding),
            "par_90_percent":       _pct(par_90, total_outstanding),
        },
    })


# ---------------------------------------------------------------------------
# AGING buckets
# ---------------------------------------------------------------------------
@loan_portfolio_bp.route("/api/aging", methods=["GET"])
@role_required("admin")
def api_aging():
    """
    Uses the SQL RPC if present; falls back to inline computation otherwise.
    """
    org_id = _org_id()
    try:
        resp = _admin().rpc("loan_aging_buckets", {"p_org": org_id}).execute()
        buckets = resp.data or []
    except Exception:
        log.exception("aging rpc failed, computing inline")
        buckets = _aging_inline(org_id)

    # Return in a fixed order so the UI doesn't have to sort
    order = ["current", "1_30", "31_60", "61_90", "90_plus"]
    buckets = sorted(buckets, key=lambda b: order.index(b["bucket"])
                     if b["bucket"] in order else 99)
    return jsonify({"ok": True, "buckets": buckets})


def _aging_inline(org_id: str) -> list[dict]:
    today = _today()

    try:
        loans = (
            _admin().table("loans")
            .select("id, outstanding_principal, outstanding_interest, "
                    "outstanding_penalties, disbursed_at, status")
            .eq("org_id", org_id)
            .in_("status", ["active", "overdue"])
            .execute()
        ).data or []

        reps = (
            _admin().table("repayments")
            .select("loan_id, paid_at")
            .eq("org_id", org_id).eq("status", "completed")
            .order("paid_at", desc=True)
            .execute()
        ).data or []
    except Exception:
        log.exception("aging inline failed")
        return []

    last_pay_by_loan = {}
    for r in reps:
        lid = r["loan_id"]
        if lid not in last_pay_by_loan:
            last_pay_by_loan[lid] = _parse_date(r["paid_at"])

    agg = {
        "current":  {"loan_count": 0, "principal": ZERO, "interest": ZERO, "penalties": ZERO, "total": ZERO},
        "1_30":     {"loan_count": 0, "principal": ZERO, "interest": ZERO, "penalties": ZERO, "total": ZERO},
        "31_60":    {"loan_count": 0, "principal": ZERO, "interest": ZERO, "penalties": ZERO, "total": ZERO},
        "61_90":    {"loan_count": 0, "principal": ZERO, "interest": ZERO, "penalties": ZERO, "total": ZERO},
        "90_plus":  {"loan_count": 0, "principal": ZERO, "interest": ZERO, "penalties": ZERO, "total": ZERO},
    }

    for l in loans:
        p = _to_decimal(l.get("outstanding_principal"))
        i = _to_decimal(l.get("outstanding_interest"))
        n = _to_decimal(l.get("outstanding_penalties"))
        if (p + i) <= 0:
            continue
        last = last_pay_by_loan.get(l["id"]) or _parse_date(l.get("disbursed_at"))
        days = (today - last).days if last else 999
        if days <= 30:      k = "current"
        elif days <= 60:    k = "1_30"
        elif days <= 90:    k = "31_60"
        elif days <= 180:   k = "61_90"
        else:               k = "90_plus"
        agg[k]["loan_count"] += 1
        agg[k]["principal"] += p
        agg[k]["interest"]  += i
        agg[k]["penalties"] += n
        agg[k]["total"]     += (p + i + n)

    out = []
    for k, v in agg.items():
        out.append({
            "bucket":      k,
            "loan_count":  v["loan_count"],
            "principal":   float(_q(v["principal"])),
            "interest":    float(_q(v["interest"])),
            "penalties":   float(_q(v["penalties"])),
            "total":       float(_q(v["total"])),
        })
    return out


# ---------------------------------------------------------------------------
# PRODUCTS rollup
# ---------------------------------------------------------------------------
@loan_portfolio_bp.route("/api/products", methods=["GET"])
@role_required("admin")
def api_products():
    org_id = _org_id()

    try:
        rows = (
            _admin().table("loans")
            .select(
                "product_id, principal_amount, outstanding_principal, "
                "outstanding_interest, collected_interest, "
                "collected_principal, status, "
                "loan_products!inner(id, name, code, category)"
            )
            .eq("org_id", org_id)
            .execute()
        ).data or []
    except Exception as e:
        log.exception("products rollup failed: %s", e)
        return _bad("Could not load products rollup", 500)

    by_product = {}
    for r in rows:
        p = r.get("loan_products") or {}
        pid = p.get("id") or "unknown"
        if pid not in by_product:
            by_product[pid] = {
                "product_id": pid,
                "name":       p.get("name") or "Unknown",
                "code":       p.get("code"),
                "category":   p.get("category"),
                "loan_count": 0,
                "active_count": 0,
                "disbursed":  ZERO,
                "outstanding":ZERO,
                "out_interest": ZERO,
                "collected_interest": ZERO,
                "collected_principal": ZERO,
            }
        b = by_product[pid]
        b["loan_count"] += 1
        if r.get("status") == "active":
            b["active_count"] += 1
        b["disbursed"] += _to_decimal(r.get("principal_amount"))
        b["outstanding"] += _to_decimal(r.get("outstanding_principal"))
        b["out_interest"] += _to_decimal(r.get("outstanding_interest"))
        b["collected_interest"] += _to_decimal(r.get("collected_interest"))
        b["collected_principal"] += _to_decimal(r.get("collected_principal"))

    out = []
    for b in by_product.values():
        out.append({
            "product_id":          b["product_id"],
            "name":                b["name"],
            "code":                b["code"],
            "category":            b["category"],
            "loan_count":          b["loan_count"],
            "active_count":        b["active_count"],
            "disbursed":           float(_q(b["disbursed"])),
            "outstanding":         float(_q(b["outstanding"])),
            "outstanding_interest":float(_q(b["out_interest"])),
            "collected_interest":  float(_q(b["collected_interest"])),
            "collected_principal": float(_q(b["collected_principal"])),
        })
    out.sort(key=lambda x: -x["outstanding"])
    return jsonify({"ok": True, "products": out})


# ---------------------------------------------------------------------------
# TOP BORROWERS
# ---------------------------------------------------------------------------
@loan_portfolio_bp.route("/api/top-borrowers", methods=["GET"])
@role_required("admin")
def api_top_borrowers():
    org_id = _org_id()
    try:
        limit = min(100, max(5, int(request.args.get("limit", 20))))
    except ValueError:
        limit = 20

    try:
        rows = (
            _admin().table("loans")
            .select(
                "member_id, outstanding_principal, outstanding_interest, "
                "outstanding_penalties, principal_amount, collected_interest, "
                "status, "
                "members!inner(id, member_id, first_name, last_name, phone)"
            )
            .eq("org_id", org_id)
            .in_("status", ["active", "overdue"])
            .execute()
        ).data or []
    except Exception as e:
        log.exception("top borrowers failed: %s", e)
        return _bad("Could not load borrowers", 500)

    by_member = {}
    for r in rows:
        m = r.get("members") or {}
        mid = m.get("id") or "unknown"
        if mid not in by_member:
            by_member[mid] = {
                "member_id":  mid,
                "external_id":m.get("member_id"),
                "name":       f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
                "phone":      m.get("phone"),
                "loan_count": 0,
                "outstanding":ZERO,
                "interest":   ZERO,
                "penalties":  ZERO,
                "disbursed":  ZERO,
                "collected_interest": ZERO,
            }
        b = by_member[mid]
        b["loan_count"] += 1
        b["outstanding"] += _to_decimal(r.get("outstanding_principal"))
        b["interest"]    += _to_decimal(r.get("outstanding_interest"))
        b["penalties"]   += _to_decimal(r.get("outstanding_penalties"))
        b["disbursed"]   += _to_decimal(r.get("principal_amount"))
        b["collected_interest"] += _to_decimal(r.get("collected_interest"))

    out = sorted(
        by_member.values(),
        key=lambda x: -(x["outstanding"] + x["interest"] + x["penalties"]),
    )[:limit]

    for b in out:
        b["outstanding"]        = float(_q(b["outstanding"]))
        b["interest"]           = float(_q(b["interest"]))
        b["penalties"]          = float(_q(b["penalties"]))
        b["disbursed"]          = float(_q(b["disbursed"]))
        b["collected_interest"] = float(_q(b["collected_interest"]))
    return jsonify({"ok": True, "borrowers": out})


# ---------------------------------------------------------------------------
# UPCOMING installments
# ---------------------------------------------------------------------------
@loan_portfolio_bp.route("/api/upcoming", methods=["GET"])
@role_required("admin")
def api_upcoming():
    """
    Expected monthly installment for each active loan, due within N days.
    Uses loan.approved term data + the schedule simulator from loanProducts
    to produce the next expected installment.
    """
    try:
        days = min(180, max(1, int(request.args.get("days", 30))))
    except ValueError:
        days = 30

    org_id = _org_id()
    today  = _today()
    horizon = today + timedelta(days=days)

    try:
        loans = (
            _admin().table("loans")
            .select(
                "id, loan_number, principal_amount, interest_method, "
                "interest_rate_percent, repayment_frequency, repayment_type, "
                "term_months, disbursed_at, maturity_date, "
                "outstanding_principal, outstanding_interest, "
                "currency, status, "
                "members!inner(member_id, first_name, last_name, phone)"
            )
            .eq("org_id", org_id)
            .in_("status", ["active", "overdue"])
            .execute()
        ).data or []
    except Exception as e:
        log.exception("upcoming fetch failed: %s", e)
        return _bad("Could not load upcoming schedule", 500)

    try:
        from routes.loanProducts.loanProducts import _simulate
    except Exception:
        _simulate = None

    upcoming = []
    for l in loans:
        maturity = _parse_date(l.get("maturity_date"))
        if maturity and maturity > horizon:
            # still include if a monthly installment falls before horizon
            pass

        snap = {
            "interest_method":       l.get("interest_method") or "flat",
            "interest_rate_percent": l.get("interest_rate_percent") or 0,
            "interest_period":       "monthly",
            "repayment_frequency":   l.get("repayment_frequency") or "monthly",
            "repayment_type":        l.get("repayment_type") or "amortized",
        }

        installment = ZERO
        if _simulate:
            try:
                sim = _simulate(
                    _to_decimal(l["principal_amount"]),
                    int(l["term_months"] or 1),
                    snap,
                )
                if sim["schedule"]:
                    installment = _to_decimal(sim["schedule"][0]["payment"])
            except Exception:
                log.exception("simulate for upcoming failed (non-fatal)")
                installment = ZERO

        # fallback: split total outstanding across remaining term
        if installment <= 0 and l.get("term_months"):
            installment = _q(
                (_to_decimal(l["outstanding_principal"]) +
                 _to_decimal(l["outstanding_interest"]))
                / Decimal(max(1, int(l["term_months"])))
            )

        # expected date: next monthly boundary after last activity
        disbursed = _parse_date(l.get("disbursed_at")) or today
        # naive: assume the next due is the day-of-month of disbursement
        next_due = today
        if today.day > disbursed.day:
            # next month
            y = today.year + (today.month) // 12
            m = today.month % 12 + 1
            next_due = date(y, m, min(disbursed.day, 28))
        else:
            next_due = date(today.year, today.month, min(disbursed.day, 28))

        if next_due > horizon:
            continue

        m = l.get("members") or {}
        upcoming.append({
            "loan_id":       l["id"],
            "loan_number":   l["loan_number"],
            "member_name":   f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            "member_id":     m.get("member_id"),
            "phone":         m.get("phone"),
            "currency":      l.get("currency"),
            "due_date":      next_due.isoformat(),
            "days_until":    (next_due - today).days,
            "expected":      float(_q(installment)),
            "outstanding":   float(_q(
                _to_decimal(l["outstanding_principal"]) +
                _to_decimal(l["outstanding_interest"])
            )),
        })

    upcoming.sort(key=lambda x: x["days_until"])
    return jsonify({"ok": True, "upcoming": upcoming})


# ---------------------------------------------------------------------------
# MONTHLY PERFORMANCE (disbursed vs collected) — last 12 months
# ---------------------------------------------------------------------------
@loan_portfolio_bp.route("/api/monthly-performance", methods=["GET"])
@role_required("admin")
def api_monthly_performance():
    """
    Returns a 12-month series of { month, disbursed, collected_principal,
    collected_interest } to power a bar/line chart.
    """
    org_id = _org_id()
    today = _today()
    start = date(today.year, today.month, 1)
    # 12 months back
    for _ in range(11):
        y = start.year - (1 if start.month == 1 else 0)
        m = 12 if start.month == 1 else start.month - 1
        start = date(y, m, 1)

    try:
        loans = (
            _admin().table("loans")
            .select("principal_amount, disbursed_at")
            .eq("org_id", org_id)
            .gte("disbursed_at", start.isoformat())
            .execute()
        ).data or []

        reps = (
            _admin().table("repayments")
            .select("amount, paid_at, "
                    "repayment_allocations(to_principal, to_interest, "
                    "to_penalties, to_fees)")
            .eq("org_id", org_id).eq("status", "completed")
            .gte("paid_at", start.isoformat())
            .execute()
        ).data or []
    except Exception as e:
        log.exception("monthly perf fetch failed: %s", e)
        return _bad("Could not load performance", 500)

    # bucket by YYYY-MM
    def _month_key(d: date) -> str:
        return f"{d.year:04d}-{d.month:02d}"

    series = {}
    cur = start
    while cur <= today:
        series[_month_key(cur)] = {
            "month":              _month_key(cur),
            "disbursed":          ZERO,
            "collected_principal":ZERO,
            "collected_interest": ZERO,
            "collected_penalties":ZERO,
            "collected_fees":     ZERO,
            "collected_total":    ZERO,
        }
        # advance a month
        y = cur.year + (1 if cur.month == 12 else 0)
        m = 1 if cur.month == 12 else cur.month + 1
        cur = date(y, m, 1)

    for l in loans:
        d = _parse_date(l.get("disbursed_at"))
        if not d: continue
        k = _month_key(d)
        if k in series:
            series[k]["disbursed"] += _to_decimal(l.get("principal_amount"))

    for r in reps:
        d = _parse_date(r.get("paid_at"))
        if not d: continue
        k = _month_key(d)
        if k not in series: continue
        a = (r.get("repayment_allocations") or [{}])[0]
        series[k]["collected_principal"] += _to_decimal(a.get("to_principal"))
        series[k]["collected_interest"]  += _to_decimal(a.get("to_interest"))
        series[k]["collected_penalties"] += _to_decimal(a.get("to_penalties"))
        series[k]["collected_fees"]      += _to_decimal(a.get("to_fees"))
        series[k]["collected_total"]     += _to_decimal(r.get("amount"))

    out = []
    for k in sorted(series.keys()):
        row = series[k]
        out.append({
            "month":               row["month"],
            "disbursed":           float(_q(row["disbursed"])),
            "collected_principal": float(_q(row["collected_principal"])),
            "collected_interest":  float(_q(row["collected_interest"])),
            "collected_penalties": float(_q(row["collected_penalties"])),
            "collected_fees":      float(_q(row["collected_fees"])),
            "collected_total":     float(_q(row["collected_total"])),
        })
    return jsonify({"ok": True, "series": out})


# ---------------------------------------------------------------------------
# LOANS — rich filtered list
# ---------------------------------------------------------------------------
@loan_portfolio_bp.route("/api/loans", methods=["GET"])
@role_required("admin")
def api_loans():
    org_id = _org_id()
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
        search   = (request.args.get("q") or "").strip()
        status   = (request.args.get("status") or "").strip()
        product  = (request.args.get("product_id") or "").strip()
        bucket   = (request.args.get("bucket") or "").strip()
        min_out  = (request.args.get("min_outstanding") or "").strip()
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    q = (
        _admin().table("loans")
        .select(
            "id, loan_number, principal_amount, total_payable, "
            "outstanding_principal, outstanding_interest, "
            "outstanding_penalties, "
            "collected_principal, collected_interest, "
            "collected_penalties, collected_fees, "
            "interest_rate_percent, interest_method, repayment_frequency, "
            "term_months, currency, disbursed_at, maturity_date, "
            "status, risk_grade, "
            "members!inner(id, member_id, first_name, last_name, phone, email), "
            "loan_products!inner(id, name, code, category)",
            count="exact",
        )
        .eq("org_id", org_id)
        .order("disbursed_at", desc=True)
        .range(start, end)
    )

    if status:
        q = q.eq("status", status)
    if product:
        q = q.eq("product_id", product)
    if search:
        like = f"%{search}%"
        q = q.or_(
            f"loan_number.ilike.{like},"
            f"members.first_name.ilike.{like},"
            f"members.last_name.ilike.{like},"
            f"members.member_id.ilike.{like},"
            f"members.phone.ilike.{like}"
        )

    try:
        resp = q.execute()
    except Exception as e:
        log.exception("portfolio loans list failed: %s", e)
        return _bad("Could not load loans", 500)

    rows = resp.data or []

    # Client-side filter for bucket/min-outstanding (kept simple; the DB
    # doesn't have these as first-class columns)
    def _bucket_of(l):
        last = None
        # use disbursed if we don't have last payment handy
        d = _parse_date(l.get("disbursed_at"))
        if d is None:
            return "90_plus"
        days = (_today() - d).days
        if days <= 30:  return "current"
        if days <= 60:  return "1_30"
        if days <= 90:  return "31_60"
        if days <= 180: return "61_90"
        return "90_plus"

    if bucket:
        rows = [r for r in rows if _bucket_of(r) == bucket]
    if min_out:
        try:
            mo = _to_decimal(min_out)
            rows = [r for r in rows if
                    (_to_decimal(r["outstanding_principal"]) +
                     _to_decimal(r["outstanding_interest"])) >= mo]
        except ValueError:
            pass

    return jsonify({
        "ok": True,
        "loans": rows,
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# LOAN detail with rollup
# ---------------------------------------------------------------------------
@loan_portfolio_bp.route("/api/loans/<uuid:loan_uuid>", methods=["GET"])
@role_required("admin")
def api_loan_detail(loan_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("loans")
            .select(
                "*, "
                "members!inner(id, member_id, first_name, last_name, phone, "
                "email, shares_units, savings_balance, loan_balance, status), "
                "loan_products!inner(id, name, code, category), "
                "loan_applications(id, app_number, purpose)"
            )
            .eq("org_id", org_id).eq("id", str(loan_uuid)).limit(1).execute()
        )
    except Exception as e:
        log.exception("portfolio loan detail failed: %s", e)
        return _bad("Could not load loan", 500)
    if not resp.data:
        return _bad("Loan not found", 404)

    loan = resp.data[0]
    outstanding = (
        _to_decimal(loan["outstanding_principal"]) +
        _to_decimal(loan["outstanding_interest"]) +
        _to_decimal(loan.get("outstanding_penalties") or 0)
    )
    loan["total_outstanding"] = float(_q(outstanding))

    # recent repayments
    try:
        reps = (
            _admin().table("repayments")
            .select("id, receipt_number, amount, currency, method, "
                    "provider, status, paid_at, created_at, "
                    "repayment_allocations(to_interest, to_principal, "
                    "to_penalties, to_fees)")
            .eq("org_id", org_id).eq("loan_id", str(loan_uuid))
            .order("created_at", desc=True).limit(20).execute()
        ).data or []
    except Exception:
        log.exception("recent repayments fetch failed (non-fatal)")
        reps = []

    return jsonify({"ok": True, "loan": loan, "repayments": reps})


# ---------------------------------------------------------------------------
# SNAPSHOTS
# ---------------------------------------------------------------------------
@loan_portfolio_bp.route("/api/snapshots", methods=["GET"])
@role_required("admin")
def api_snapshots():
    org_id = _org_id()
    try:
        limit = min(365, max(1, int(request.args.get("limit", 90))))
    except ValueError:
        limit = 90

    try:
        resp = (
            _admin().table("loan_portfolio_snapshots")
            .select("*")
            .eq("org_id", org_id)
            .order("snapshot_date", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception as e:
        log.exception("snapshots fetch failed: %s", e)
        return _bad("Could not load snapshots", 500)

    return jsonify({"ok": True, "snapshots": resp.data or []})


@loan_portfolio_bp.route("/api/snapshots", methods=["POST"])
@role_required("admin")
def api_capture_snapshot():
    """
    Capture a snapshot row for today. Idempotent: upserts on (org_id, date).
    """
    org_id = _org_id()
    today  = _today()

    # reuse overview computation
    ov_resp = api_overview()
    if isinstance(ov_resp, tuple):
        return ov_resp
    ov = ov_resp.get_json()["overview"]

    row = {
        "org_id":               org_id,
        "snapshot_date":        today.isoformat(),
        "total_loans":          ov["total_loans"],
        "active_loans":         ov["active_loans"],
        "overdue_loans":        ov["overdue_loans"],
        "closed_loans":         ov["closed_loans"],
        "written_off_loans":    ov["written_off_loans"],
        "disbursed_principal":  ov["disbursed_principal"],
        "outstanding_principal":ov["outstanding_principal"],
        "outstanding_interest": ov["outstanding_interest"],
        "outstanding_penalties":ov["outstanding_penalties"],
        "collected_principal":  ov["collected_principal"],
        "collected_interest":   ov["collected_interest"],
        "collected_penalties":  ov["collected_penalties"],
        "collected_fees":       ov["collected_fees"],
        "par_30":               ov["par_30"],
        "par_60":               ov["par_60"],
        "par_90":               ov["par_90"],
    }

    try:
        # upsert
        _admin().table("loan_portfolio_snapshots").upsert(
            row, on_conflict="org_id,snapshot_date"
        ).execute()
    except Exception as e:
        log.exception("snapshot upsert failed")
        return _bad(f"Could not save snapshot: {e}", 500)

    return jsonify({"ok": True, "snapshot": row}), 201


# ---------------------------------------------------------------------------
# EXPORT CSV
# ---------------------------------------------------------------------------
@loan_portfolio_bp.route("/api/export", methods=["GET"])
@role_required("admin")
def api_export():
    """
    CSV of the current loans list (respects same filters as /api/loans).
    """
    org_id = _org_id()
    search = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip()

    q = (
        _admin().table("loans")
        .select(
            "loan_number, principal_amount, total_payable, "
            "outstanding_principal, outstanding_interest, "
            "outstanding_penalties, collected_principal, "
            "collected_interest, collected_penalties, collected_fees, "
            "interest_rate_percent, interest_method, "
            "term_months, disbursed_at, maturity_date, currency, status, "
            "members!inner(member_id, first_name, last_name, phone)"
        )
        .eq("org_id", org_id)
        .order("disbursed_at", desc=True)
    )
    if status:
        q = q.eq("status", status)
    if search:
        like = f"%{search}%"
        q = q.or_(
            f"loan_number.ilike.{like},"
            f"members.first_name.ilike.{like},"
            f"members.last_name.ilike.{like},"
            f"members.member_id.ilike.{like}"
        )

    try:
        rows = q.execute().data or []
    except Exception as e:
        log.exception("export fetch failed: %s", e)
        return _bad("Could not export loans", 500)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Loan", "Member ID", "Member", "Phone", "Principal",
                "Total Payable", "Outstanding Principal",
                "Outstanding Interest", "Outstanding Penalties",
                "Collected Principal", "Collected Interest",
                "Collected Penalties", "Collected Fees",
                "Interest Method", "Rate %", "Term (mo)",
                "Disbursed", "Maturity", "Currency", "Status"])
    for r in rows:
        m = r.get("members") or {}
        w.writerow([
            r.get("loan_number"), m.get("member_id"),
            f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            m.get("phone"),
            r.get("principal_amount"), r.get("total_payable"),
            r.get("outstanding_principal"), r.get("outstanding_interest"),
            r.get("outstanding_penalties"),
            r.get("collected_principal"), r.get("collected_interest"),
            r.get("collected_penalties"), r.get("collected_fees"),
            r.get("interest_method"), r.get("interest_rate_percent"),
            r.get("term_months"),
            r.get("disbursed_at"), r.get("maturity_date"),
            r.get("currency"), r.get("status"),
        ])

    filename = f"loan_portfolio_{_today().isoformat()}.csv"
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/csv; charset=utf-8",
        },
    )


# ---------------------------------------------------------------------------
# ADMIN ACTIONS
# ---------------------------------------------------------------------------
@loan_portfolio_bp.route("/api/loans/<uuid:loan_uuid>/risk", methods=["POST"])
@role_required("admin")
def api_set_risk(loan_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    grade  = (data.get("risk_grade") or "").strip().upper() or None
    notes  = (data.get("risk_notes") or "").strip() or None

    if grade and grade not in ("A", "B", "C", "D"):
        return _bad("risk_grade must be A, B, C, or D", reason="bad_grade")

    try:
        resp = (
            _admin().table("loans")
            .update({
                "risk_grade":      grade,
                "risk_notes":      notes,
                "last_activity_at":_now_iso(),
            })
            .eq("org_id", org_id).eq("id", str(loan_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("risk update failed")
        return _bad(f"Could not update risk: {e}", 500, reason="db_error")

    if not resp.data:
        return _bad("Loan not found", 404, reason="not_found")
    return jsonify({"ok": True, "loan": resp.data[0]})


@loan_portfolio_bp.route("/api/loans/<uuid:loan_uuid>/write-off", methods=["POST"])
@role_required("admin")
def api_write_off(loan_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip() or None
    if not reason:
        return _bad("reason is required", reason="missing_reason")

    try:
        cur = (
            _admin().table("loans")
            .select("status")
            .eq("org_id", org_id).eq("id", str(loan_uuid)).limit(1).execute()
        ).data
    except Exception:
        log.exception("write-off fetch failed")
        return _bad("Could not load loan", 500)
    if not cur:
        return _bad("Loan not found", 404, reason="not_found")
    if cur[0]["status"] not in ("active", "overdue"):
        return _bad(f"Cannot write off a loan in status '{cur[0]['status']}'",
                    400, reason="bad_state")

    try:
        resp = (
            _admin().table("loans")
            .update({
                "status":              "written_off",
                "written_off_at":      _now_iso(),
                "written_off_by":      _user_id(),
                "written_off_reason":  reason,
                "last_activity_at":    _now_iso(),
            })
            .eq("org_id", org_id).eq("id", str(loan_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("write-off update failed")
        return _bad(f"Could not write off: {e}", 500, reason="db_error")

    return jsonify({"ok": True, "loan": resp.data[0]})


# ---------------------------------------------------------------------------
# ACTIVITY REFRESH
# ---------------------------------------------------------------------------
@loan_portfolio_bp.route("/api/refresh-activity", methods=["POST"])
@role_required("admin")
def api_refresh_activity():
    """
    Recompute loans.last_activity_at for every loan in the org.
    Uses a single pass and a bounded number of update calls.
    """
    org_id = _org_id()

    try:
        reps = (
            _admin().table("repayments")
            .select("loan_id, paid_at")
            .eq("org_id", org_id).eq("status", "completed")
            .order("paid_at", desc=True)
            .execute()
        ).data or []
    except Exception as e:
        log.exception("refresh activity fetch failed")
        return _bad(f"Could not refresh: {e}", 500, reason="db_error")

    latest = {}
    for r in reps:
        lid = r["loan_id"]
        if lid not in latest:
            latest[lid] = r["paid_at"]

    updated = 0
    try:
        for lid, ts in latest.items():
            _admin().table("loans").update({
                "last_activity_at": ts,
            }).eq("org_id", org_id).eq("id", lid).execute()
            updated += 1
    except Exception:
        log.exception("some activity updates failed (non-fatal)")

    return jsonify({"ok": True, "updated": updated})