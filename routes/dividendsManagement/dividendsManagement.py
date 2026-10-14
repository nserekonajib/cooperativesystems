"""
Dividends Management blueprint.

Endpoints (admin-only JSON unless noted):
    GET    /dividends/                                       -> page
    GET    /dividends/api/stats                              -> KPI cards
    GET    /dividends/api/runs                               -> list of dividend runs
    GET    /dividends/api/runs/<uuid:run_uuid>               -> single run + payments
    GET    /dividends/api/runs/<uuid:run_uuid>/payments      -> paginated payments
    GET    /dividends/api/runs/<uuid:run_uuid>/receipt       -> CSV of payments
    GET    /dividends/api/members/search                     -> lookup helper

    POST   /dividends/api/preview                            -> compute distribution (no DB write)
    POST   /dividends/api/runs                               -> declare a run (status=draft or approved)
    POST   /dividends/api/runs/<uuid:run_uuid>/approve       -> draft -> approved
    POST   /dividends/api/runs/<uuid:run_uuid>/post          -> approved -> posted (creates payments)
    POST   /dividends/api/runs/<uuid:run_uuid>/reverse       -> posted -> reversed
    POST   /dividends/api/runs/<uuid:run_uuid>/cancel        -> draft/approved -> cancelled

Design:
- Two-phase. `preview` gives you the exact total without touching the DB.
  `declare` freezes the terms and writes the run in `draft` (or `approved`).
- `post` is the money moment: it writes one `dividend_payments` row per eligible
  member, credits each member's balance / shares / cash, and posts a matching
  entry into the chart of accounts (chart posts are batched into one journal
  per run — see the note at the bottom of the file).
- Reversal is straightforward: every payment stores its ledger links, so
  we can walk them and undo.
- Dividends are paid from RETAINED EARNINGS (equity), not from income —
  that's the correct accounting.
"""

import os
import io
import csv
import logging
from datetime import datetime, timezone, date
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
        "%(asctime)s [%(levelname)s] dividends: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

dividends_bp = Blueprint("dividends", __name__, url_prefix="/dividends")

MONEY_Q  = Decimal("0.01")
SHARE_Q  = Decimal("0.0001")
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


def _money(v) -> Decimal:
    d = v if isinstance(v, Decimal) else Decimal(str(v if v is not None else 0))
    return d.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _shares(v) -> Decimal:
    d = v if isinstance(v, Decimal) else Decimal(str(v if v is not None else 0))
    return d.quantize(SHARE_Q, rounding=ROUND_HALF_UP)


def _next_run_number(org_id: str) -> str:
    try:
        resp = _admin().rpc("next_dividend_run_number", {"p_org": org_id}).execute()
    except Exception as e:
        log.exception("next_dividend_run_number failed")
        raise RuntimeError(f"Could not allocate run number: {e}")
    raw = resp.data
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, dict):
        raw = next(iter(raw.values()))
    year = datetime.now(timezone.utc).year
    return f"DIV-{year}-{int(raw):06d}"


# ---------------------------------------------------------------------------
# Distribution math
# ---------------------------------------------------------------------------
def _compute_distribution(org_id: str, *, financial_year: int,
                          basis: str, rate_per_share,
                          percent_of_value, min_shares_units,
                          only_active: bool = True) -> dict:
    """
    Reads eligible members and computes each one's dividend.
    Does NOT write anything. Returns:
      {
        members: [{member_id, member_code, member_name, shares_units,
                   share_price, dividend_amount, currency, status}],
        total: Decimal, count: int, currency: str
      }
    """
    admin = _admin()

    # share price (from settings)
    s = (
        admin.table("institution_settings")
        .select("min_share_price, currency")
        .eq("org_id", org_id).limit(1).execute()
    ).data or [{}]
    share_price = _to_decimal(s[0].get("min_share_price"))
    currency    = s[0].get("currency") or "UGX"

    # fetch eligible members
    q = (
        admin.table("members")
        .select("id, member_id, first_name, last_name, status, "
                "shares_units, shares_value")
        .eq("org_id", org_id)
    )
    if only_active:
        q = q.eq("status", "active")

    members = q.execute().data or []

    rows = []
    total = ZERO
    for m in members:
        units = _to_decimal(m.get("shares_units"))
        if units < _to_decimal(min_shares_units):
            continue

        if basis == "per_share":
            amount = _money(units * _to_decimal(rate_per_share))
        else:  # percent_of_value
            # share_value = units * share_price (recomputed, so price changes
            # don't skew the base)
            value = _money(units * share_price)
            amount = _money(value * _to_decimal(percent_of_value) / Decimal("100"))

        if amount <= 0:
            continue

        total += amount
        rows.append({
            "member_id":    m["id"],
            "member_code":  m.get("member_id"),
            "member_name":  f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            "shares_units": float(_shares(units)),
            "share_price":  float(_money(share_price)),
            "dividend_amount": float(amount),
            "currency":     currency,
            "status":       m.get("status"),
        })

    return {
        "members": rows,
        "total":   _money(total),
        "count":   len(rows),
        "currency": currency,
        "share_price": float(_money(share_price)),
    }


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
@dividends_bp.route("/", methods=["GET"])
def page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    if session["user"].get("role") != "admin":
        return redirect(url_for("auth.dashboard"))
    return render_template("dividendsManagement/dividendsManagement.html")


# ---------------------------------------------------------------------------
# STATS
# ---------------------------------------------------------------------------
@dividends_bp.route("/api/stats", methods=["GET"])
@role_required("admin")
def api_stats():
    org_id = _org_id()
    try:
        runs = (
            _admin().table("dividend_runs")
            .select("id, financial_year, status, total_dividend, "
                    "total_recipients, payout_mode, currency")
            .eq("org_id", org_id)
            .execute()
        ).data or []

        lifetime_total = ZERO
        posted_runs = 0
        draft_runs  = 0
        approved_runs = 0
        years = set()

        for r in runs:
            years.add(r.get("financial_year"))
            if r.get("status") == "posted":
                posted_runs += 1
                lifetime_total += _to_decimal(r.get("total_dividend"))
            elif r.get("status") == "draft":
                draft_runs += 1
            elif r.get("status") == "approved":
                approved_runs += 1

        # latest run
        latest = None
        if runs:
            latest = max(runs, key=lambda x: (x.get("financial_year") or 0))

    except Exception as e:
        log.exception("dividend stats failed: %s", e)
        return _bad("Could not load stats", 500)

    return jsonify({
        "ok": True,
        "stats": {
            "total_runs":     len(runs),
            "posted_runs":    posted_runs,
            "approved_runs":  approved_runs,
            "draft_runs":     draft_runs,
            "lifetime_total": float(_money(lifetime_total)),
            "years_covered":  len(years),
            "latest_year":    latest.get("financial_year") if latest else None,
            "latest_total":   float(_to_decimal(latest.get("total_dividend"))) if latest else 0.0,
        },
    })


# ---------------------------------------------------------------------------
# LIST runs
# ---------------------------------------------------------------------------
@dividends_bp.route("/api/runs", methods=["GET"])
@role_required("admin")
def api_runs():
    org_id = _org_id()
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
        status   = (request.args.get("status") or "").strip()
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    q = (
        _admin().table("dividend_runs")
        .select("*", count="exact")
        .eq("org_id", org_id)
        .order("created_at", desc=True)
        .range(start, end)
    )
    if status:
        q = q.eq("status", status)

    try:
        resp = q.execute()
    except Exception as e:
        log.exception("dividend runs list failed: %s", e)
        return _bad("Could not load runs", 500)

    return jsonify({
        "ok": True,
        "runs": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# RUN detail
# ---------------------------------------------------------------------------
@dividends_bp.route("/api/runs/<uuid:run_uuid>", methods=["GET"])
@role_required("admin")
def api_run_detail(run_uuid):
    org_id = _org_id()
    try:
        run = (
            _admin().table("dividend_runs")
            .select("*")
            .eq("org_id", org_id).eq("id", str(run_uuid)).limit(1).execute()
        ).data
    except Exception as e:
        log.exception("dividend run detail failed: %s", e)
        return _bad("Could not load run", 500)
    if not run:
        return _bad("Run not found", 404)
    run = run[0]

    # aggregate by payout_mode for a quick summary
    try:
        payments = (
            _admin().table("dividend_payments")
            .select("id, member_id, dividend_amount, currency, status, "
                    "shares_units_at_run, share_price_at_run, payout_mode, "
                    "posted_at, reversed_at")
            .eq("org_id", org_id).eq("dividend_run_id", str(run_uuid))
            .order("dividend_amount", desc=True)
            .execute()
        ).data or []
    except Exception:
        log.exception("payments fetch failed (non-fatal)")
        payments = []

    return jsonify({
        "ok": True,
        "run": run,
        "payments": payments,
        "payments_count": len(payments),
    })


@dividends_bp.route("/api/runs/<uuid:run_uuid>/payments", methods=["GET"])
@role_required("admin")
def api_run_payments(run_uuid):
    org_id = _org_id()
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(500, max(10, int(request.args.get("per_page", 100))))
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    try:
        resp = (
            _admin().table("dividend_payments")
            .select(
                "id, dividend_amount, currency, status, shares_units_at_run, "
                "share_price_at_run, payout_mode, posted_at, reversed_at, "
                "members!inner(member_id, first_name, last_name, phone)",
                count="exact",
            )
            .eq("org_id", org_id).eq("dividend_run_id", str(run_uuid))
            .order("dividend_amount", desc=True)
            .range(start, end)
            .execute()
        )
    except Exception as e:
        log.exception("dividend payments fetch failed: %s", e)
        return _bad("Could not load payments", 500)

    return jsonify({
        "ok": True,
        "payments": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# PREVIEW (no DB write)
# ---------------------------------------------------------------------------
@dividends_bp.route("/api/preview", methods=["POST"])
@role_required("admin")
def api_preview():
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}

    try:
        financial_year = int(data.get("financial_year") or _today().year)
    except (ValueError, TypeError):
        return _bad("Invalid financial_year", reason="bad_year")

    basis = (data.get("basis") or "per_share").lower()
    if basis not in ("per_share", "percent_of_value", "percent_of_profit"):
        return _bad("Invalid basis", reason="bad_basis")

    try:
        rate  = _to_decimal(data.get("rate_per_share"))
        pct   = _to_decimal(data.get("percent_of_value"))
        dpct  = _to_decimal(data.get("declare_percent"))
        minsh = _to_decimal(data.get("min_shares_units"))
    except ValueError as e:
        return _bad(str(e), reason="bad_amount")

    only_active = bool(data.get("only_active", True))

    try:
        if basis == "percent_of_profit":
            result = _compute_from_profit(
                org_id,
                financial_year=financial_year,
                declare_percent=dpct,
                min_shares_units=minsh,
                only_active=only_active,
            )
        else:
            result = _compute_distribution(
                org_id,
                financial_year=financial_year,
                basis=basis,
                rate_per_share=rate,
                percent_of_value=pct,
                min_shares_units=minsh,
                only_active=only_active,
            )
    except ValueError as e:
        return _bad(str(e), reason="compute_failed")
    except Exception as e:
        log.exception("preview failed")
        return _bad(f"Could not preview: {e}", 500, reason="preview_failed")

    preview = {
        "financial_year":   financial_year,
        "basis":            basis,
        "currency":         result["currency"],
        "total_dividend":   float(result["total"]),
        "total_recipients": result["count"],
        "members":          result["members"][:500],
    }
    # Include the extra fields for the profit basis
    if basis == "percent_of_profit":
        preview.update({
            "net_surplus":        float(result["net_surplus"]),
            "declare_percent":    float(result["declare_percent"]),
            "declare_amount":     float(result["declare_amount"]),
            "retained_amount":    float(result["retained_amount"]),
            "eligible_shares":    float(result["eligible_shares"]),
            "dividend_per_share": float(result["dividend_per_share"]),
            "period_from":        result["period_from"],
            "period_to":          result["period_to"],
            "income":             float(result["income"]),
            "expense":            float(result["expense"]),
        })
    else:
        preview.update({
            "rate_per_share":   float(rate),
            "percent_of_value": float(pct),
            "share_price":      result.get("share_price"),
        })

    return jsonify({"ok": True, "preview": preview})


@dividends_bp.route("/api/runs", methods=["POST"])
@role_required("admin")
def api_declare():
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}

    try:
        financial_year = int(data.get("financial_year") or _today().year)
    except (ValueError, TypeError):
        return _bad("Invalid financial_year", reason="bad_year")

    basis = (data.get("basis") or "per_share").lower()
    if basis not in ("per_share", "percent_of_value", "percent_of_profit"):
        return _bad("Invalid basis", reason="bad_basis")

    try:
        rate  = _to_decimal(data.get("rate_per_share"))
        pct   = _to_decimal(data.get("percent_of_value"))
        dpct  = _to_decimal(data.get("declare_percent"))
        minsh = _to_decimal(data.get("min_shares_units"))
    except ValueError as e:
        return _bad(str(e), reason="bad_amount")

    payout_mode = (data.get("payout_mode") or "to_savings").lower()
    if payout_mode not in ("to_savings", "reinvest_shares", "cash_payout"):
        return _bad("Invalid payout_mode", reason="bad_mode")

    approve_now = bool(data.get("approve_now"))

    # Compute distribution — this is the source of truth for totals
    try:
        if basis == "percent_of_profit":
            result = _compute_from_profit(
                org_id,
                financial_year=financial_year,
                declare_percent=dpct,
                min_shares_units=minsh,
                only_active=True,
            )
        else:
            result = _compute_distribution(
                org_id,
                financial_year=financial_year,
                basis=basis,
                rate_per_share=rate,
                percent_of_value=pct,
                min_shares_units=minsh,
                only_active=True,
            )
    except ValueError as e:
        return _bad(str(e), reason="compute_failed")
    except Exception as e:
        log.exception("declare compute failed")
        return _bad(f"Could not compute distribution: {e}", 500,
                    reason="compute_failed")

    if result["count"] == 0:
        return _bad("No eligible members for these terms", reason="no_recipients")

    try:
        run_number = _next_run_number(org_id)
    except Exception as e:
        return _bad(str(e), 500, reason="number_alloc")

    row = {
        "org_id":              org_id,
        "run_number":          run_number,
        "financial_year":      financial_year,
        "basis":               basis,
        "rate_per_share":      float(rate),
        "percent_of_value":    float(pct),
        "declare_percent":     float(dpct),
        "min_shares_units":    float(minsh),
        "currency":            result["currency"],
        "total_dividend":      float(result["total"]),
        "total_recipients":    result["count"],
        "payout_mode":         payout_mode,
        "status":              "approved" if approve_now else "draft",
        "declared_by":         _user_id(),
        "declared_at":         _now_iso(),
        "approved_by":         _user_id() if approve_now else None,
        "approved_at":         _now_iso() if approve_now else None,
        "notes":               (data.get("notes") or "").strip() or None,
    }
    if basis == "percent_of_profit":
        row.update({
            "net_surplus_source": float(result["net_surplus"]),
            "declare_percent":    float(result["declare_percent"]),
            "retained_amount":    float(result["retained_amount"]),
            "eligible_shares":    float(result["eligible_shares"]),
            "dividend_per_share": float(result["dividend_per_share"]),
            "period_from":        result["period_from"],
            "period_to":          result["period_to"],
        })

    try:
        run_resp = _admin().table("dividend_runs").insert(row).execute().data
    except Exception as e:
        log.exception("dividend run insert failed")
        return _bad(f"Could not create run: {e}", 500, reason="db_error")

    return jsonify({"ok": True, "run": run_resp[0]}), 201
# ---------------------------------------------------------------------------
# APPROVE
# ---------------------------------------------------------------------------
@dividends_bp.route("/api/runs/<uuid:run_uuid>/approve", methods=["POST"])
@role_required("admin")
def api_approve(run_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("dividend_runs")
            .select("*").eq("org_id", org_id).eq("id", str(run_uuid))
            .limit(1).execute()
        )
    except Exception:
        return _bad("Could not load run", 500)
    if not resp.data:
        return _bad("Run not found", 404, reason="not_found")
    run = resp.data[0]

    if run["status"] != "draft":
        return _bad(f"Cannot approve a run in status '{run['status']}'",
                    reason="bad_state")

    try:
        upd = {
            "status":      "approved",
            "approved_by": _user_id(),
            "approved_at": _now_iso(),
            "updated_at":  _now_iso(),
        }
        r = (
            _admin().table("dividend_runs")
            .update(upd).eq("org_id", org_id).eq("id", str(run_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("approve failed")
        return _bad(f"Could not approve: {e}", 500, reason="db_error")

    return jsonify({"ok": True, "run": r.data[0]})


# ---------------------------------------------------------------------------
# CANCEL
# ---------------------------------------------------------------------------
@dividends_bp.route("/api/runs/<uuid:run_uuid>/cancel", methods=["POST"])
@role_required("admin")
def api_cancel(run_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip() or None

    try:
        resp = (
            _admin().table("dividend_runs")
            .select("status").eq("org_id", org_id).eq("id", str(run_uuid))
            .limit(1).execute()
        )
    except Exception:
        return _bad("Could not load run", 500)
    if not resp.data:
        return _bad("Run not found", 404, reason="not_found")
    status = resp.data[0]["status"]
    if status not in ("draft", "approved"):
        return _bad(f"Cannot cancel a run in status '{status}'",
                    reason="bad_state")

    try:
        upd = {
            "status":     "cancelled",
            "updated_at": _now_iso(),
            "meta":       {"cancel_reason": reason} if reason else {},
        }
        r = (
            _admin().table("dividend_runs")
            .update(upd).eq("org_id", org_id).eq("id", str(run_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("cancel failed")
        return _bad(f"Could not cancel: {e}", 500, reason="db_error")

    return jsonify({"ok": True, "run": r.data[0]})


# ---------------------------------------------------------------------------
# POST (the money moment)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# POST (the money moment)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# POST (the money moment)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# POST (the money moment)
# ---------------------------------------------------------------------------
@dividends_bp.route("/api/runs/<uuid:run_uuid>/post", methods=["POST"])
@role_required("admin")
def api_post(run_uuid):
    """
    Posts the dividend:

      1. Loads the run (must be 'approved').
      2. Recomputes distribution from the frozen run terms (source of truth).
      3. Writes one `dividend_payments` row per eligible member.
      4. Credits each member's savings / shares (or cash) and mirrors a
         `member_transactions` ledger row (with account_id so it appears in
         the savings account drawer), linking back to the payment.
      5. Posts to the chart of accounts:
             For percent_of_profit runs:
                 Dr  3090 Current year surplus    (sweep full net surplus)
                 Cr  3020 Retained earnings
             Then, for all runs:
                 Dr  3020 Retained earnings       (declare amount)
                 Cr  2000/3000/1000              (per payout mode)
      6. Flips the run to 'posted' and stores the chart txn id in meta.

    JSON-safety note:
        postgrest encodes payloads with json.dumps, so every numeric must be
        a native JSON type (int/float/str). This function coerces every Decimal
        to float at the point of building the payload.
    """
    org_id = _org_id()

    # --------------------------------------------------------------------
    # 1. Load the run
    # --------------------------------------------------------------------
    try:
        resp = (
            _admin().table("dividend_runs")
            .select("*").eq("org_id", org_id).eq("id", str(run_uuid))
            .limit(1).execute()
        )
    except Exception:
        log.exception("post: run fetch failed")
        return _bad("Could not load run", 500, reason="db_error")

    if not resp.data:
        return _bad("Run not found", 404, reason="not_found")
    run = resp.data[0]

    if run["status"] != "approved":
        return _bad(
            f"Run must be approved before posting (current: '{run['status']}')",
            reason="bad_state",
        )

    payout_mode    = run["payout_mode"]
    financial_year = run["financial_year"]
    basis          = run["basis"]

    log.info("post: starting run=%s basis=%s payout=%s",
             run.get("run_number"), basis, payout_mode)

    # --------------------------------------------------------------------
    # 2. Recompute distribution from the frozen run terms
    # --------------------------------------------------------------------
    try:
        if basis == "percent_of_profit":
            result = _compute_from_profit(
                org_id,
                financial_year=financial_year,
                declare_percent=run["declare_percent"],
                min_shares_units=run["min_shares_units"],
                only_active=True,
            )
        else:
            result = _compute_distribution(
                org_id,
                financial_year=financial_year,
                basis=basis,
                rate_per_share=run["rate_per_share"],
                percent_of_value=run["percent_of_value"],
                min_shares_units=run["min_shares_units"],
                only_active=True,
            )
    except ValueError as e:
        return _bad(f"Could not recompute distribution: {e}", 400,
                    reason="compute_failed")
    except Exception as e:
        log.exception("post: recompute failed")
        return _bad(f"Could not recompute distribution: {e}", 500,
                    reason="compute_failed")

    members = result["members"]
    if not members:
        return _bad("No eligible members to post", reason="no_recipients")

    log.info("post: %d recipients · total=%s %s",
             len(members), result["total"], result["currency"])

    admin = _admin()
    now   = _now_iso()

    # --------------------------------------------------------------------
    # 3. Load the chart-of-accounts rows we need (single query)
    # --------------------------------------------------------------------
    try:
        acct_rows = (
            admin.table("accounts")
            .select("id, code, name, category, balance")
            .eq("org_id", org_id)
            .in_("code", ["1000", "1010", "2000", "2020", "3000", "3020", "3090"])
            .execute()
        ).data or []
        accounts = {a["code"]: a for a in acct_rows}
    except Exception:
        log.exception("post: accounts fetch failed")
        return _bad("Could not load chart of accounts", 500, reason="db_error")

    credit_code = {
        "to_savings":      "2000",   # Member savings payable
        "reinvest_shares": "3000",   # Share capital
        "cash_payout":     "1000",   # Cash on hand
    }[payout_mode]
    debit_code = "3020"              # Retained earnings

    # --------------------------------------------------------------------
    # 4. Build the payment_rows payload (JSON-safe)
    # --------------------------------------------------------------------
    payment_rows = []
    for m in members:
        units       = _shares(m.get("shares_units"))
        share_price = _money(m.get("share_price") or 0)
        share_value = _money(units * share_price)
        amount      = _money(m.get("dividend_amount"))

        payment_rows.append({
            "org_id":              org_id,
            "dividend_run_id":     run["id"],
            "member_id":           m["member_id"],
            "shares_units_at_run": float(units),
            "share_value_at_run":  float(share_value),
            "share_price_at_run":  float(share_price),
            "dividend_amount":     float(amount),
            "currency":            m["currency"],
            "payout_mode":         payout_mode,
            "status":              "posted",
            "posted_at":           now,
        })

    # --------------------------------------------------------------------
    # 5. Insert payment rows in chunks
    #    Capture DB-assigned id per member_id so the mirror step can link.
    # --------------------------------------------------------------------
    payment_id_by_member = {}
    try:
        chunk = 500
        for i in range(0, len(payment_rows), chunk):
            part = payment_rows[i:i + chunk]
            r = admin.table("dividend_payments").insert(part).execute()
            for row in (r.data or []):
                payment_id_by_member[row["member_id"]] = row["id"]
    except Exception as e:
        log.exception("post: payment insert failed")
        return _bad(f"Could not insert payments: {e}", 500, reason="db_error")

    log.info("post: inserted %d payment rows", len(payment_id_by_member))

    # --------------------------------------------------------------------
    # 6. Update members + mirror to member_transactions
    # --------------------------------------------------------------------
    total_amount   = ZERO
    failed_members = []

    for p in payment_rows:
        try:
            amount = _to_decimal(p["dividend_amount"])
            if amount <= 0:
                continue
            total_amount += amount

            # --- 6a. Credit member balance or shares ---
            savings_account_id = None
            if payout_mode in ("to_savings", "cash_payout"):
                # Fetch current balance AND the account id in one query
                mem = (
                    admin.table("members")
                    .select("savings_balance")
                    .eq("org_id", org_id).eq("id", p["member_id"])
                    .limit(1).execute()
                ).data[0]
                new_sav = _money(_to_decimal(mem.get("savings_balance")) + amount)

                admin.table("members").update({
                    "savings_balance": float(new_sav),
                    "updated_at":      now,
                }).eq("id", p["member_id"]).execute()

                sav_acct = (
                    admin.table("member_accounts")
                    .select("id")
                    .eq("org_id", org_id)
                    .eq("member_id", p["member_id"])
                    .eq("account_type", "savings")
                    .limit(1).execute()
                ).data
                if sav_acct:
                    savings_account_id = sav_acct[0]["id"]
                    admin.table("member_accounts").update({
                        "balance":    float(new_sav),
                        "updated_at": now,
                    }).eq("id", savings_account_id).execute()

            elif payout_mode == "reinvest_shares":
                mem = (
                    admin.table("members")
                    .select("shares_units, shares_value")
                    .eq("org_id", org_id).eq("id", p["member_id"])
                    .limit(1).execute()
                ).data[0]
                price     = _to_decimal(p["share_price_at_run"])
                add_units = _shares(amount / price) if price > 0 else ZERO
                new_units = _shares(_to_decimal(mem.get("shares_units")) + add_units)
                new_value = _money(new_units * price)

                admin.table("members").update({
                    "shares_units": float(new_units),
                    "shares_value": float(new_value),
                    "updated_at":   now,
                }).eq("id", p["member_id"]).execute()

            # --- 6b. Mirror into member_transactions + link back ---
            # account_id is set for savings/cash payouts so the row shows
            # up in the savings account drawer's transaction history.
            try:
                tx = admin.table("member_transactions").insert({
                    "org_id":     org_id,
                    "member_id":  p["member_id"],
                    "account_id": savings_account_id,   # may be None for reinvest
                    "type":       "dividend",
                    "method":     "internal",
                    "amount":     float(amount),
                    "currency":   p["currency"],
                    "status":     "completed",
                    "narration":  f"Dividend {financial_year} · {run['run_number']}",
                    "created_by": _user_id(),
                }).execute().data

                if tx:
                    payment_id = payment_id_by_member.get(p["member_id"])
                    if payment_id:
                        admin.table("dividend_payments").update({
                            "member_transaction_id": tx[0]["id"],
                        }).eq("id", payment_id).execute()
                    else:
                        log.warning(
                            "post: no payment_id for member %s — "
                            "skipping member_transaction_id link",
                            p["member_id"],
                        )
            except Exception:
                log.exception("post: member_transaction mirror failed for %s",
                              p["member_id"])

        except Exception:
            log.exception("post: member posting failed for %s", p["member_id"])
            failed_members.append(p["member_id"])

    log.info("post: credited %d members · total=%s",
             len(payment_rows) - len(failed_members), total_amount)

    # --------------------------------------------------------------------
    # 7. Chart of accounts postings
    # --------------------------------------------------------------------
    chart_txn_id = None
    try:
        from routes.accounts.accounts import _apply_txn

        # --- 7a. For percent_of_profit: sweep full net surplus into 3020 ---
        if basis == "percent_of_profit":
            surplus = _to_decimal(run.get("net_surplus_source"))
            if surplus > 0:
                try:
                    _apply_txn(org_id, {
                        "type":              "transfer",
                        "category":          "other_adjustment",
                        "amount":            str(_money(surplus)),
                        "debit_account_id":  (accounts.get("3090") or {}).get("id"),
                        "credit_account_id": (accounts.get("3020") or {}).get("id"),
                        "method":            "internal",
                        "reference":         run["run_number"],
                        "narration":         f"Close year {financial_year} surplus into retained earnings",
                        "created_by":        _user_id(),
                        "meta": {
                            "source":          "dividend_year_close",
                            "dividend_run_id": run["id"],
                            "run_number":      run["run_number"],
                        },
                    })
                except Exception:
                    log.exception("post: year-close sweep failed (non-fatal)")

        # --- 7b. Distribute the declared amount out of 3020 ---
        chart_txn_id = _apply_txn(org_id, {
            "type":              "transfer",
            "category":          "other_adjustment",
            "amount":            str(_money(total_amount)),
            "debit_account_id":  (accounts.get(debit_code) or {}).get("id"),
            "credit_account_id": (accounts.get(credit_code) or {}).get("id"),
            "method":            "internal",
            "reference":         run["run_number"],
            "narration":         f"Dividend distribution {financial_year} ({payout_mode})",
            "created_by":        _user_id(),
            "meta": {
                "source":          "dividend",
                "dividend_run_id": run["id"],
                "run_number":      run["run_number"],
                "recipients":      len(payment_rows),
            },
        })
    except Exception:
        log.exception("post: chart journal failed (non-fatal)")

    # --------------------------------------------------------------------
    # 8. Flip the run to 'posted'
    # --------------------------------------------------------------------
    try:
        r = (
            admin.table("dividend_runs")
            .update({
                "status":     "posted",
                "posted_by":  _user_id(),
                "posted_at":  now,
                "updated_at": now,
                "meta": {
                    "chart_txn_id": (chart_txn_id or {}).get("id") if chart_txn_id else None,
                    "chart_ref":    (chart_txn_id or {}).get("txn_number") if chart_txn_id else None,
                },
            })
            .eq("org_id", org_id).eq("id", str(run_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("post: status update failed")
        return _bad(f"Dividend computed but status update failed: {e}",
                    500, reason="db_error")

    log.info("post: run %s marked posted", run.get("run_number"))

    return jsonify({
        "ok":           True,
        "run":          r.data[0],
        "posted_count": len(payment_rows) - len(failed_members),
        "failed":       failed_members,
        "total_amount": float(_money(total_amount)),
        "chart_txn":    chart_txn_id,
    })
    # --------------------------------------------------------------------
    # 2. Recompute distribution from the frozen run terms
    # --------------------------------------------------------------------
    try:
        if basis == "percent_of_profit":
            result = _compute_from_profit(
                org_id,
                financial_year=financial_year,
                declare_percent=run["declare_percent"],
                min_shares_units=run["min_shares_units"],
                only_active=True,
            )
        else:
            result = _compute_distribution(
                org_id,
                financial_year=financial_year,
                basis=basis,
                rate_per_share=run["rate_per_share"],
                percent_of_value=run["percent_of_value"],
                min_shares_units=run["min_shares_units"],
                only_active=True,
            )
    except ValueError as e:
        return _bad(f"Could not recompute distribution: {e}", 400,
                    reason="compute_failed")
    except Exception as e:
        log.exception("post: recompute failed")
        return _bad(f"Could not recompute distribution: {e}", 500,
                    reason="compute_failed")

    members = result["members"]
    if not members:
        return _bad("No eligible members to post", reason="no_recipients")

    log.info("post: %d recipients · total=%s %s",
             len(members), result["total"], result["currency"])

    admin = _admin()
    now   = _now_iso()

    # --------------------------------------------------------------------
    # 3. Load the chart-of-accounts rows we need (single query)
    # --------------------------------------------------------------------
    try:
        acct_rows = (
            admin.table("accounts")
            .select("id, code, name, category, balance")
            .eq("org_id", org_id)
            .in_("code", ["1000", "1010", "2000", "2020", "3000", "3020", "3090"])
            .execute()
        ).data or []
        accounts = {a["code"]: a for a in acct_rows}
    except Exception:
        log.exception("post: accounts fetch failed")
        return _bad("Could not load chart of accounts", 500, reason="db_error")

    credit_code = {
        "to_savings":      "2000",   # Member savings payable
        "reinvest_shares": "3000",   # Share capital
        "cash_payout":     "1000",   # Cash on hand
    }[payout_mode]
    debit_code = "3020"              # Retained earnings

    # --------------------------------------------------------------------
    # 4. Build the payment_rows payload (JSON-safe)
    # --------------------------------------------------------------------
    payment_rows = []
    for m in members:
        units       = _shares(m.get("shares_units"))
        share_price = _money(m.get("share_price") or 0)
        share_value = _money(units * share_price)
        amount      = _money(m.get("dividend_amount"))

        payment_rows.append({
            "org_id":              org_id,
            "dividend_run_id":     run["id"],
            "member_id":           m["member_id"],
            "shares_units_at_run": float(units),
            "share_value_at_run":  float(share_value),
            "share_price_at_run":  float(share_price),
            "dividend_amount":     float(amount),
            "currency":            m["currency"],
            "payout_mode":         payout_mode,
            "status":              "posted",
            "posted_at":           now,
        })

    # --------------------------------------------------------------------
    # 5. Insert payment rows in chunks
    #    Capture DB-assigned id per member_id so the mirror step can link.
    # --------------------------------------------------------------------
    payment_id_by_member = {}
    try:
        chunk = 500
        for i in range(0, len(payment_rows), chunk):
            part = payment_rows[i:i + chunk]
            r = admin.table("dividend_payments").insert(part).execute()
            for row in (r.data or []):
                payment_id_by_member[row["member_id"]] = row["id"]
    except Exception as e:
        log.exception("post: payment insert failed")
        return _bad(f"Could not insert payments: {e}", 500, reason="db_error")

    log.info("post: inserted %d payment rows", len(payment_id_by_member))

    # --------------------------------------------------------------------
    # 6. Update members + mirror to member_transactions
    # --------------------------------------------------------------------
    total_amount   = ZERO
    failed_members = []

    for p in payment_rows:
        try:
            amount = _to_decimal(p["dividend_amount"])
            if amount <= 0:
                continue
            total_amount += amount

            # --- 6a. Credit member balance or shares ---
            if payout_mode in ("to_savings", "cash_payout"):
                mem = (
                    admin.table("members")
                    .select("savings_balance")
                    .eq("org_id", org_id).eq("id", p["member_id"])
                    .limit(1).execute()
                ).data[0]
                new_sav = _money(_to_decimal(mem.get("savings_balance")) + amount)

                admin.table("members").update({
                    "savings_balance": float(new_sav),
                    "updated_at":      now,
                }).eq("id", p["member_id"]).execute()

                sav_acct = (
                    admin.table("member_accounts")
                    .select("id")
                    .eq("org_id", org_id)
                    .eq("member_id", p["member_id"])
                    .eq("account_type", "savings")
                    .limit(1).execute()
                ).data
                if sav_acct:
                    admin.table("member_accounts").update({
                        "balance":    float(new_sav),
                        "updated_at": now,
                    }).eq("id", sav_acct[0]["id"]).execute()

            elif payout_mode == "reinvest_shares":
                mem = (
                    admin.table("members")
                    .select("shares_units, shares_value")
                    .eq("org_id", org_id).eq("id", p["member_id"])
                    .limit(1).execute()
                ).data[0]
                price     = _to_decimal(p["share_price_at_run"])
                add_units = _shares(amount / price) if price > 0 else ZERO
                new_units = _shares(_to_decimal(mem.get("shares_units")) + add_units)
                new_value = _money(new_units * price)

                admin.table("members").update({
                    "shares_units": float(new_units),
                    "shares_value": float(new_value),
                    "updated_at":   now,
                }).eq("id", p["member_id"]).execute()

            # --- 6b. Mirror into member_transactions + link back ---
            try:
                tx = admin.table("member_transactions").insert({
                    "org_id":     org_id,
                    "member_id":  p["member_id"],
                    "type":       "dividend",
                    "method":     "internal",
                    "amount":     float(amount),
                    "currency":   p["currency"],
                    "status":     "completed",
                    "narration":  f"Dividend {financial_year} · {run['run_number']}",
                    "created_by": _user_id(),
                }).execute().data

                if tx:
                    payment_id = payment_id_by_member.get(p["member_id"])
                    if payment_id:
                        admin.table("dividend_payments").update({
                            "member_transaction_id": tx[0]["id"],
                        }).eq("id", payment_id).execute()
                    else:
                        log.warning(
                            "post: no payment_id for member %s — "
                            "skipping member_transaction_id link",
                            p["member_id"],
                        )
            except Exception:
                log.exception("post: member_transaction mirror failed for %s",
                              p["member_id"])

        except Exception:
            log.exception("post: member posting failed for %s", p["member_id"])
            failed_members.append(p["member_id"])

    log.info("post: credited %d members · total=%s",
             len(payment_rows) - len(failed_members), total_amount)

    # --------------------------------------------------------------------
    # 7. Chart of accounts postings
    # --------------------------------------------------------------------
    chart_txn_id = None
    try:
        from routes.accounts.accounts import _apply_txn

        # --- 7a. For percent_of_profit: sweep full net surplus into 3020 ---
        if basis == "percent_of_profit":
            surplus = _to_decimal(run.get("net_surplus_source"))
            if surplus > 0:
                try:
                    _apply_txn(org_id, {
                        "type":              "transfer",
                        "category":          "other_adjustment",
                        "amount":            str(_money(surplus)),
                        "debit_account_id":  (accounts.get("3090") or {}).get("id"),
                        "credit_account_id": (accounts.get("3020") or {}).get("id"),
                        "method":            "internal",
                        "reference":         run["run_number"],
                        "narration":         f"Close year {financial_year} surplus into retained earnings",
                        "created_by":        _user_id(),
                        "meta": {
                            "source":          "dividend_year_close",
                            "dividend_run_id": run["id"],
                            "run_number":      run["run_number"],
                        },
                    })
                except Exception:
                    log.exception("post: year-close sweep failed (non-fatal)")

        # --- 7b. Distribute the declared amount out of 3020 ---
        chart_txn_id = _apply_txn(org_id, {
            "type":              "transfer",
            "category":          "other_adjustment",
            "amount":            str(_money(total_amount)),
            "debit_account_id":  (accounts.get(debit_code) or {}).get("id"),
            "credit_account_id": (accounts.get(credit_code) or {}).get("id"),
            "method":            "internal",
            "reference":         run["run_number"],
            "narration":         f"Dividend distribution {financial_year} ({payout_mode})",
            "created_by":        _user_id(),
            "meta": {
                "source":          "dividend",
                "dividend_run_id": run["id"],
                "run_number":      run["run_number"],
                "recipients":      len(payment_rows),
            },
        })
    except Exception:
        log.exception("post: chart journal failed (non-fatal)")

    # --------------------------------------------------------------------
    # 8. Flip the run to 'posted'
    # --------------------------------------------------------------------
    try:
        r = (
            admin.table("dividend_runs")
            .update({
                "status":     "posted",
                "posted_by":  _user_id(),
                "posted_at":  now,
                "updated_at": now,
                "meta": {
                    "chart_txn_id": (chart_txn_id or {}).get("id") if chart_txn_id else None,
                    "chart_ref":    (chart_txn_id or {}).get("txn_number") if chart_txn_id else None,
                },
            })
            .eq("org_id", org_id).eq("id", str(run_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("post: status update failed")
        return _bad(f"Dividend computed but status update failed: {e}",
                    500, reason="db_error")

    log.info("post: run %s marked posted", run.get("run_number"))

    return jsonify({
        "ok":           True,
        "run":          r.data[0],
        "posted_count": len(payment_rows) - len(failed_members),
        "failed":       failed_members,
        "total_amount": float(_money(total_amount)),
        "chart_txn":    chart_txn_id,
    })

    # --------------------------------------------------------------------
    # 3. Load the chart-of-accounts rows we need (single query)
    # --------------------------------------------------------------------
    try:
        acct_rows = (
            admin.table("accounts")
            .select("id, code, name, category, balance")
            .eq("org_id", org_id)
            .in_("code", ["1000", "1010", "2000", "2020", "3000", "3020", "3090"])
            .execute()
        ).data or []
        accounts = {a["code"]: a for a in acct_rows}
    except Exception:
        log.exception("post: accounts fetch failed")
        return _bad("Could not load chart of accounts", 500, reason="db_error")

    # Resolve credit account by payout mode
    credit_code = {
        "to_savings":      "2000",   # Member savings payable
        "reinvest_shares": "3000",   # Share capital
        "cash_payout":     "1000",   # Cash on hand
    }[payout_mode]

    debit_code = "3020"              # Retained earnings

    # --------------------------------------------------------------------
    # 4. Build the payment_rows payload (JSON-safe)
    # --------------------------------------------------------------------
    payment_rows = []
    for m in members:
        units       = _shares(m.get("shares_units"))
        share_price = _money(m.get("share_price") or 0)
        share_value = _money(units * share_price)
        amount      = _money(m.get("dividend_amount"))

        payment_rows.append({
            "org_id":              org_id,
            "dividend_run_id":     run["id"],
            "member_id":           m["member_id"],
            "shares_units_at_run": float(units),
            "share_value_at_run":  float(share_value),
            "share_price_at_run":  float(share_price),
            "dividend_amount":     float(amount),
            "currency":            m["currency"],
            "payout_mode":         payout_mode,
            "status":              "posted",
            "posted_at":           now,
        })

    # --------------------------------------------------------------------
    # 5. Insert payment rows in chunks
    # --------------------------------------------------------------------
    created_ids = []
    try:
        chunk = 500
        for i in range(0, len(payment_rows), chunk):
            part = payment_rows[i:i + chunk]
            r = admin.table("dividend_payments").insert(part).execute()
            created_ids.extend([x["id"] for x in (r.data or [])])
    except Exception as e:
        log.exception("post: payment insert failed")
        return _bad(f"Could not insert payments: {e}", 500, reason="db_error")

    log.info("post: inserted %d payment rows", len(created_ids))

    # --------------------------------------------------------------------
    # 6. Update members + mirror to member_transactions
    # --------------------------------------------------------------------
    total_amount   = ZERO
    failed_members = []

    for p in payment_rows:
        try:
            amount = _to_decimal(p["dividend_amount"])
            if amount <= 0:
                continue
            total_amount += amount

            # --- 6a. Credit member balance or shares ---
            if payout_mode in ("to_savings", "cash_payout"):
                mem = (
                    admin.table("members")
                    .select("savings_balance")
                    .eq("org_id", org_id).eq("id", p["member_id"])
                    .limit(1).execute()
                ).data[0]
                new_sav = _money(_to_decimal(mem.get("savings_balance")) + amount)

                admin.table("members").update({
                    "savings_balance": float(new_sav),
                    "updated_at":      now,
                }).eq("id", p["member_id"]).execute()

                sav_acct = (
                    admin.table("member_accounts")
                    .select("id")
                    .eq("org_id", org_id)
                    .eq("member_id", p["member_id"])
                    .eq("account_type", "savings")
                    .limit(1).execute()
                ).data
                if sav_acct:
                    admin.table("member_accounts").update({
                        "balance":    float(new_sav),
                        "updated_at": now,
                    }).eq("id", sav_acct[0]["id"]).execute()

            elif payout_mode == "reinvest_shares":
                mem = (
                    admin.table("members")
                    .select("shares_units, shares_value")
                    .eq("org_id", org_id).eq("id", p["member_id"])
                    .limit(1).execute()
                ).data[0]
                price     = _to_decimal(p["share_price_at_run"])
                add_units = _shares(amount / price) if price > 0 else ZERO
                new_units = _shares(_to_decimal(mem.get("shares_units")) + add_units)
                new_value = _money(new_units * price)

                admin.table("members").update({
                    "shares_units": float(new_units),
                    "shares_value": float(new_value),
                    "updated_at":   now,
                }).eq("id", p["member_id"]).execute()

            # --- 6b. Mirror into member_transactions ---
            try:
                tx = admin.table("member_transactions").insert({
                    "org_id":     org_id,
                    "member_id":  p["member_id"],
                    "type":       "dividend",
                    "method":     "internal",
                    "amount":     float(amount),
                    "currency":   p["currency"],
                    "status":     "completed",
                    "narration":  f"Dividend {financial_year} · {run['run_number']}",
                    "created_by": _user_id(),
                }).execute().data

                if tx:
                    admin.table("dividend_payments").update({
                        "member_transaction_id": tx[0]["id"],
                    }).eq("id", p["id"]).execute()
            except Exception:
                log.exception("post: member_transaction mirror failed for %s",
                              p["member_id"])

        except Exception:
            log.exception("post: member posting failed for %s", p["member_id"])
            failed_members.append(p["member_id"])

    log.info("post: credited %d members · total=%s",
             len(payment_rows) - len(failed_members), total_amount)

    # --------------------------------------------------------------------
    # 7. Chart of accounts postings
    # --------------------------------------------------------------------
    chart_txn_id = None
    try:
        from routes.accounts.accounts import _apply_txn

        # --- 7a. For percent_of_profit: sweep full net surplus into 3020 ---
        if basis == "percent_of_profit":
            surplus = _to_decimal(run.get("net_surplus_source"))
            if surplus > 0:
                try:
                    _apply_txn(org_id, {
                        "type":              "transfer",
                        "category":          "other_adjustment",
                        "amount":            str(_money(surplus)),
                        "debit_account_id":  (accounts.get("3090") or {}).get("id"),
                        "credit_account_id": (accounts.get("3020") or {}).get("id"),
                        "method":            "internal",
                        "reference":         run["run_number"],
                        "narration":         f"Close year {financial_year} surplus into retained earnings",
                        "created_by":        _user_id(),
                        "meta": {
                            "source":          "dividend_year_close",
                            "dividend_run_id": run["id"],
                            "run_number":      run["run_number"],
                        },
                    })
                except Exception:
                    log.exception("post: year-close sweep failed (non-fatal)")

        # --- 7b. Distribute the declared amount out of 3020 ---
        chart_txn_id = _apply_txn(org_id, {
            "type":              "transfer",
            "category":          "other_adjustment",
            "amount":            str(_money(total_amount)),
            "debit_account_id":  (accounts.get(debit_code) or {}).get("id"),
            "credit_account_id": (accounts.get(credit_code) or {}).get("id"),
            "method":            "internal",
            "reference":         run["run_number"],
            "narration":         f"Dividend distribution {financial_year} ({payout_mode})",
            "created_by":        _user_id(),
            "meta": {
                "source":          "dividend",
                "dividend_run_id": run["id"],
                "run_number":      run["run_number"],
                "recipients":      len(payment_rows),
            },
        })
    except Exception:
        log.exception("post: chart journal failed (non-fatal)")

    # --------------------------------------------------------------------
    # 8. Flip the run to 'posted'
    # --------------------------------------------------------------------
    try:
        r = (
            admin.table("dividend_runs")
            .update({
                "status":     "posted",
                "posted_by":  _user_id(),
                "posted_at":  now,
                "updated_at": now,
                "meta": {
                    "chart_txn_id": (chart_txn_id or {}).get("id") if chart_txn_id else None,
                    "chart_ref":    (chart_txn_id or {}).get("txn_number") if chart_txn_id else None,
                },
            })
            .eq("org_id", org_id).eq("id", str(run_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("post: status update failed")
        return _bad(f"Dividend computed but status update failed: {e}",
                    500, reason="db_error")

    log.info("post: run %s marked posted", run.get("run_number"))

    return jsonify({
        "ok":           True,
        "run":          r.data[0],
        "posted_count": len(payment_rows) - len(failed_members),
        "failed":       failed_members,
        "total_amount": float(_money(total_amount)),
        "chart_txn":    chart_txn_id,
    })

    # --------------------------------------------------------------------
    # 1. Fetch the chart of accounts we'll need once
    # --------------------------------------------------------------------
    try:
                accounts = {
            a["code"]: a
            for a in (
                admin.table("accounts")
                .select("id, code, name, category, balance")
                .eq("org_id", org_id)
                .in_("code", ["1000","1010","2000","2020","3000","3020","3090"])
                .execute()
            ).data or []
        }
    except Exception:
        log.exception("accounts fetch failed")
        return _bad("Could not load chart of accounts", 500, reason="db_error")

    # Which credit account does the payout hit?
    if payout_mode == "to_savings":
        credit_code = "2000"      # Member savings payable
    elif payout_mode == "reinvest_shares":
        credit_code = "3000"      # Share capital
    else:  # cash_payout
        credit_code = "1000"      # Cash on hand

    # Debit: Retained earnings (equity) or Dividends paid (expense)
    # Accounting convention: SACCO dividends come out of retained earnings.
    debit_code = "3020"

    # --------------------------------------------------------------------
    # 2. Prepare the batch of payment rows
    # --------------------------------------------------------------------
    payment_rows = []
    for m in members:
        payment_rows.append({
            "org_id":              org_id,
            "dividend_run_id":     run["id"],
            "member_id":           m["member_id"],
            "shares_units_at_run": m["shares_units"],
            "share_value_at_run":  _money(Decimal(str(m["shares_units"])) *
                                           Decimal(str(m["share_price"]))),
            "share_price_at_run":  m["share_price"],
            "dividend_amount":     m["dividend_amount"],
            "currency":            m["currency"],
            "payout_mode":         payout_mode,
            "status":              "posted",
            "posted_at":           now,
        })

    # --------------------------------------------------------------------
    # 3. Insert payment rows in chunks
    # --------------------------------------------------------------------
    created_ids = []
    try:
        chunk = 500
        for i in range(0, len(payment_rows), chunk):
            part = payment_rows[i:i+chunk]
            r = admin.table("dividend_payments").insert(part).execute()
            created_ids.extend([x["id"] for x in (r.data or [])])
    except Exception as e:
        log.exception("payment insert failed")
        return _bad(f"Could not insert payments: {e}", 500, reason="db_error")

    # --------------------------------------------------------------------
    # 4. Update members (balance or shares) + member_transactions + chart
    # --------------------------------------------------------------------
    total_amount = ZERO
    failed_members = []

    for p in payment_rows:
        try:
            amount = _to_decimal(p["dividend_amount"])
            if amount <= 0:
                continue
            total_amount += amount

            # 4a. Update member
            if payout_mode == "to_savings" or payout_mode == "cash_payout":
                # credit savings
                mem = (
                    admin.table("members")
                    .select("savings_balance")
                    .eq("org_id", org_id).eq("id", p["member_id"])
                    .limit(1).execute()
                ).data[0]
                new_sav = _money(_to_decimal(mem.get("savings_balance")) + amount)
                admin.table("members").update({
                    "savings_balance": float(new_sav),
                    "updated_at":      now,
                }).eq("id", p["member_id"]).execute()

                # also update the member_accounts savings row
                sav_acct = (
                    admin.table("member_accounts")
                    .select("id, balance")
                    .eq("org_id", org_id)
                    .eq("member_id", p["member_id"])
                    .eq("account_type", "savings")
                    .limit(1).execute()
                ).data
                if sav_acct:
                    admin.table("member_accounts").update({
                        "balance":    float(new_sav),
                        "updated_at": now,
                    }).eq("id", sav_acct[0]["id"]).execute()

            elif payout_mode == "reinvest_shares":
                # credit shares
                mem = (
                    admin.table("members")
                    .select("shares_units, shares_value")
                    .eq("org_id", org_id).eq("id", p["member_id"])
                    .limit(1).execute()
                ).data[0]
                price = _to_decimal(p["share_price_at_run"])
                add_units = _shares(amount / price) if price > 0 else ZERO
                new_units = _shares(_to_decimal(mem.get("shares_units")) + add_units)
                new_value = _money(new_units * price)
                admin.table("members").update({
                    "shares_units": float(new_units),
                    "shares_value": float(new_value),
                    "updated_at":   now,
                }).eq("id", p["member_id"]).execute()

            # 4b. member_transactions ledger entry
            try:
                tx = admin.table("member_transactions").insert({
                    "org_id":     org_id,
                    "member_id":  p["member_id"],
                    "type":       "dividend",
                    "method":     "internal",
                    "amount":     float(amount),
                    "currency":   p["currency"],
                    "status":     "completed",
                    "narration":  f"Dividend {financial_year} · {run['run_number']}",
                    "created_by": _user_id(),
                }).execute().data
                if tx:
                    admin.table("dividend_payments").update({
                        "member_transaction_id": tx[0]["id"],
                    }).eq("id", p["id"]).execute()
            except Exception:
                log.exception("member_transaction insert failed for member %s",
                              p["member_id"])

        except Exception as e:
            log.exception("posting failed for member %s", p["member_id"])
            failed_members.append(p["member_id"])

    # --------------------------------------------------------------------
    # 5. Single chart-of-accounts journal for the whole run
    # --------------------------------------------------------------------
        # --------------------------------------------------------------------
    # 5. Chart of accounts postings
    # --------------------------------------------------------------------
    chart_txn_id = None
    try:
        from routes.accounts.accounts import _apply_txn

        debit_code = "3020"    # Retained earnings (destination of the surplus)
        credit_code = {
            "to_savings":      "2000",
            "reinvest_shares": "3000",
            "cash_payout":     "1000",
        }[payout_mode]

        # If this run is declared as a percent of the year's profit,
        # first sweep the whole YTD surplus into retained earnings.
        # That way the remaining (100 - declare_percent) shows up
        # correctly as "left in retained earnings".
        if run["basis"] == "percent_of_profit":
            try:
                from routes.accounts.accounts import _apply_txn as _apply
                surplus = _to_decimal(run["net_surplus_source"])
                if surplus > 0:
                    _apply(org_id, {
                        "type":              "transfer",
                        "category":          "other_adjustment",
                        "amount":            str(_money(surplus)),
                        "debit_account_id":  (accounts.get("3090") or {}).get("id"),
                        "credit_account_id": (accounts.get("3020") or {}).get("id"),
                        "method":            "internal",
                        "reference":         run["run_number"],
                        "narration":         f"Close year {financial_year} surplus into retained earnings",
                        "created_by":        _user_id(),
                        "meta": {
                            "source":          "dividend_year_close",
                            "dividend_run_id": run["id"],
                            "run_number":      run["run_number"],
                        },
                    })
            except Exception:
                log.exception("year-close sweep failed (non-fatal)")

        # Now distribute the declared amount out of retained earnings
        chart_txn_id = _apply_txn(org_id, {
            "type":              "transfer",
            "category":          "other_adjustment",
            "amount":            str(_money(total_amount)),
            "debit_account_id":  (accounts.get(debit_code) or {}).get("id"),
            "credit_account_id": (accounts.get(credit_code) or {}).get("id"),
            "method":            "internal",
            "reference":         run["run_number"],
            "narration":         f"Dividend distribution {financial_year} ({payout_mode})",
            "created_by":        _user_id(),
            "meta": {
                "source":          "dividend",
                "dividend_run_id": run["id"],
                "run_number":      run["run_number"],
                "recipients":      len(payment_rows),
            },
        })
    except Exception:
        log.exception("chart journal failed (non-fatal)")
    # --------------------------------------------------------------------
    # 6. Flip the run to posted
    # --------------------------------------------------------------------
    try:
        r = (
            admin.table("dividend_runs")
            .update({
                "status":     "posted",
                "posted_by":  _user_id(),
                "posted_at":  now,
                "updated_at": now,
                "meta": {
                    "chart_txn_id":  (chart_txn_id or {}).get("id") if chart_txn_id else None,
                    "chart_ref":     (chart_txn_id or {}).get("txn_number") if chart_txn_id else None,
                },
            })
            .eq("org_id", org_id).eq("id", str(run_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("post status update failed")
        return _bad(f"Dividend computed but status update failed: {e}",
                    500, reason="db_error")

    return jsonify({
        "ok":            True,
        "run":           r.data[0],
        "posted_count":  len(payment_rows) - len(failed_members),
        "failed":        failed_members,
        "total_amount":  float(_money(total_amount)),
        "chart_txn":     chart_txn_id,
    })


# ---------------------------------------------------------------------------
# REVERSE
# ---------------------------------------------------------------------------
@dividends_bp.route("/api/runs/<uuid:run_uuid>/reverse", methods=["POST"])
@role_required("admin")
def api_reverse(run_uuid):
    """
    Reverses a posted dividend run:
      - walks every dividend_payment, subtracts the amount from the member's
        savings or shares (whichever was credited)
      - marks payments as 'reversed'
      - posts a compensating entry to the chart of accounts
      - flips the run to 'reversed'
    """
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip() or None
    if not reason:
        return _bad("reason is required", reason="missing_reason")

    try:
        resp = (
            _admin().table("dividend_runs")
            .select("*").eq("org_id", org_id).eq("id", str(run_uuid))
            .limit(1).execute()
        )
    except Exception:
        return _bad("Could not load run", 500)
    if not resp.data:
        return _bad("Run not found", 404, reason="not_found")
    run = resp.data[0]

    if run["status"] != "posted":
        return _bad(f"Cannot reverse a run in status '{run['status']}'",
                    reason="bad_state")

    # fetch all payments
    try:
        payments = (
            _admin().table("dividend_payments")
            .select("*").eq("org_id", org_id)
            .eq("dividend_run_id", str(run_uuid))
            .eq("status", "posted")
            .execute()
        ).data or []
    except Exception:
        return _bad("Could not load payments", 500)

    admin = _admin()
    now   = _now_iso()
    total = ZERO

    for p in payments:
        try:
            amount = _to_decimal(p["dividend_amount"])
            if amount <= 0:
                continue
            total += amount

            if run["payout_mode"] in ("to_savings", "cash_payout"):
                mem = (
                    admin.table("members")
                    .select("savings_balance")
                    .eq("org_id", org_id).eq("id", p["member_id"])
                    .limit(1).execute()
                ).data[0]
                new_sav = _money(_to_decimal(mem.get("savings_balance")) - amount)
                admin.table("members").update({
                    "savings_balance": float(new_sav),
                    "updated_at":      now,
                }).eq("id", p["member_id"]).execute()

                sav_acct = (
                    admin.table("member_accounts")
                    .select("id").eq("org_id", org_id)
                    .eq("member_id", p["member_id"])
                    .eq("account_type", "savings").limit(1).execute()
                ).data
                if sav_acct:
                    admin.table("member_accounts").update({
                        "balance": float(new_sav), "updated_at": now,
                    }).eq("id", sav_acct[0]["id"]).execute()

            elif run["payout_mode"] == "reinvest_shares":
                mem = (
                    admin.table("members")
                    .select("shares_units, shares_value")
                    .eq("org_id", org_id).eq("id", p["member_id"])
                    .limit(1).execute()
                ).data[0]
                price = _to_decimal(p["share_price_at_run"])
                sub_units = _shares(amount / price) if price > 0 else ZERO
                new_units = _shares(_to_decimal(mem.get("shares_units")) - sub_units)
                if new_units < 0: new_units = ZERO
                new_value = _money(new_units * price)
                admin.table("members").update({
                    "shares_units": float(new_units),
                    "shares_value": float(new_value),
                    "updated_at":   now,
                }).eq("id", p["member_id"]).execute()

            # mark payment reversed
            admin.table("dividend_payments").update({
                "status":          "reversed",
                "reversed_at":     now,
                "reversal_reason": reason,
                "updated_at":      now,
            }).eq("id", p["id"]).execute()

        except Exception:
            log.exception("reverse failed for payment %s", p["id"])

    # reverse the chart entry
    try:
        from routes.accounts.accounts import _apply_txn
        # opposite of the original: Dr credit_account, Cr debit_account
        # we re-resolve by code to keep this simple
        accounts = {
            a["code"]: a
            for a in (
                admin.table("accounts")
                .select("id, code")
                .eq("org_id", org_id)
                .in_("code", ["1000","2000","3000","3020"])
                .execute()
            ).data or []
        }
        credit_code = {
            "to_savings":      "2000",
            "reinvest_shares": "3000",
            "cash_payout":     "1000",
        }[run["payout_mode"]]

        _apply_txn(org_id, {
            "type":              "transfer",
            "category":          "other_adjustment",
            "amount":            str(_money(total)),
            "debit_account_id":  (accounts.get(credit_code) or {}).get("id"),
            "credit_account_id": (accounts.get("3020") or {}).get("id"),
            "method":            "internal",
            "reference":         run["run_number"],
            "narration":         f"REVERSAL · Dividend {run['financial_year']}",
            "created_by":        _user_id(),
            "meta": {
                "source":          "dividend_reversal",
                "dividend_run_id": run["id"],
                "reason":          reason,
            },
        })
    except Exception:
        log.exception("chart reversal failed (non-fatal)")

    # mark run reversed
    try:
        r = (
            admin.table("dividend_runs")
            .update({
                "status":          "reversed",
                "reversed_by":     _user_id(),
                "reversed_at":     now,
                "reversal_reason": reason,
                "updated_at":      now,
            })
            .eq("org_id", org_id).eq("id", str(run_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("reverse status update failed")
        return _bad(f"Could not finalize reversal: {e}", 500, reason="db_error")

    return jsonify({
        "ok":           True,
        "run":          r.data[0],
        "reversed_count": len(payments),
        "total_reversed": float(_money(total)),
    })


# ---------------------------------------------------------------------------
# CSV export of payments
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# CSV export of payments
# ---------------------------------------------------------------------------
@dividends_bp.route("/api/runs/<uuid:run_uuid>/receipt", methods=["GET"])
@role_required("admin")
def api_run_receipt(run_uuid):
    """
    CSV export of a dividend run.

    Header rows adapt to the run's basis:
      per_share         -> rate per share
      percent_of_value  -> percent of share value
      percent_of_profit -> net surplus, declare %, retained, eligible shares, DPS

    Body columns: member_id, member, phone, shares, share_price,
                  dividend, currency, payout_mode, status
    """
    org_id = _org_id()

    # ---- load run ---------------------------------------------------------
    try:
        run_rows = (
            _admin().table("dividend_runs")
            .select("*").eq("org_id", org_id).eq("id", str(run_uuid))
            .limit(1).execute()
        ).data
    except Exception:
        log.exception("receipt: run fetch failed")
        return _bad("Could not load run", 500)
    if not run_rows:
        return _bad("Run not found", 404)
    run = run_rows[0]

    # ---- load payments (with member join) ---------------------------------
    try:
        payments = (
            _admin().table("dividend_payments")
            .select(
                "dividend_amount, currency, status, shares_units_at_run, "
                "share_price_at_run, payout_mode, posted_at, reversed_at, "
                "members!inner(member_id, first_name, last_name, phone)"
            )
            .eq("org_id", org_id).eq("dividend_run_id", str(run_uuid))
            .order("dividend_amount", desc=True)
            .limit(5000).execute()
        ).data or []
    except Exception:
        log.exception("receipt: payments fetch failed")
        return _bad("Could not load payments", 500)

    # ---- build CSV --------------------------------------------------------
    buf = io.StringIO()
    w = csv.writer(buf)

    # ---- header block -----------------------------------------------------
    w.writerow([f"Dividend Distribution — {run.get('run_number')}"])
    w.writerow(["Financial year", run.get("financial_year")])
    w.writerow(["Basis", run.get("basis")])

    basis = run.get("basis")
    if basis == "per_share":
        w.writerow(["Rate per share", run.get("rate_per_share")])
    elif basis == "percent_of_value":
        w.writerow(["Percent of share value", run.get("percent_of_value")])
    elif basis == "percent_of_profit":
        w.writerow(["Net surplus (source)",   run.get("net_surplus_source")])
        w.writerow(["Declare % of profit",    run.get("declare_percent")])
        w.writerow(["Declared amount",        run.get("total_dividend")])
        w.writerow(["Retained",               run.get("retained_amount")])
        w.writerow(["Eligible shares",        run.get("eligible_shares")])
        w.writerow(["Dividend per share",     run.get("dividend_per_share")])
        if run.get("period_from"):
            w.writerow(["Period from",        run.get("period_from")])
        if run.get("period_to"):
            w.writerow(["Period to",          run.get("period_to")])

    w.writerow(["Minimum shares",   run.get("min_shares_units")])
    w.writerow(["Payout mode",      run.get("payout_mode")])
    w.writerow(["Total dividend",   run.get("total_dividend")])
    w.writerow(["Currency",         run.get("currency")])
    w.writerow(["Recipients",       run.get("total_recipients")])
    w.writerow(["Status",           run.get("status")])
    w.writerow(["Declared at",      run.get("declared_at")])
    if run.get("approved_at"):
        w.writerow(["Approved at",  run.get("approved_at")])
    if run.get("posted_at"):
        w.writerow(["Posted at",    run.get("posted_at")])
    if run.get("reversed_at"):
        w.writerow(["Reversed at",  run.get("reversed_at")])
        w.writerow(["Reversal reason", run.get("reversal_reason") or ""])
    w.writerow(["Generated",        _now_iso()])
    w.writerow([])

    # ---- payments table ---------------------------------------------------
    w.writerow([
        "Member ID", "Member", "Phone",
        "Shares", "Share price", "Dividend",
        "Currency", "Payout mode", "Status", "Posted at",
    ])

    total_amt = ZERO
    by_status = {}
    by_mode   = {}

    for p in payments:
        m = p.get("members") or {}
        amt = _to_decimal(p.get("dividend_amount"))
        total_amt += amt

        st = p.get("status") or "pending"
        by_status[st] = by_status.get(st, ZERO) + amt

        md = p.get("payout_mode") or run.get("payout_mode") or ""
        by_mode[md] = by_mode.get(md, ZERO) + amt

        w.writerow([
            m.get("member_id") or "",
            f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            m.get("phone") or "",
            p.get("shares_units_at_run") or 0,
            p.get("share_price_at_run") or 0,
            float(_money(amt)),
            p.get("currency") or run.get("currency") or "",
            md,
            st,
            p.get("posted_at") or "",
        ])

    # ---- subtotals --------------------------------------------------------
    w.writerow([])
    w.writerow(["SUBTOTAL", "", "", "", "", float(_money(total_amt))])

    if len(by_status) > 1 or "posted" not in by_status:
        w.writerow([])
        w.writerow(["By status"])
        for k in sorted(by_status.keys()):
            w.writerow([k, "", "", "", "", float(_money(by_status[k]))])

    if len(by_mode) > 1:
        w.writerow([])
        w.writerow(["By payout mode"])
        for k in sorted(by_mode.keys()):
            w.writerow([k, "", "", "", "", float(_money(by_mode[k]))])

    # ---- respond ----------------------------------------------------------
    filename = f"dividends_{run.get('run_number')}.csv"
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/csv; charset=utf-8",
        },
    )


# ---------------------------------------------------------------------------
# MEMBER SEARCH (helper for the frontend)
# ---------------------------------------------------------------------------
@dividends_bp.route("/api/members/search", methods=["GET"])
@role_required("admin")
def api_members_search():
    q = (request.args.get("q") or "").strip()
    query = (
        _admin().table("members")
        .select("id, member_id, first_name, last_name, phone, shares_units")
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


# ---------------------------------------------------------------------------
# Source of truth for percent_of_profit: YTD net surplus
# ---------------------------------------------------------------------------
def _ytd_net_surplus(org_id: str, year: int) -> dict:
    """
    Reads every completed account_transaction in the given year and returns:
        { income, expense, net_surplus, from_date, to_date }

    income  = sum(amount) where type='income'
    expense = sum(amount) where type='expense'
    net     = income - expense
    """
    from_date = date(year, 1, 1)
    today     = _today()
    to_date   = today if year == today.year else date(year, 12, 31)

    rows = (
        _admin().table("account_transactions")
        .select("type, amount")
        .eq("org_id", org_id).eq("status", "completed")
        .gte("txn_date", from_date.isoformat())
        .lte("txn_date", to_date.isoformat())
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

    return {
        "income":      _money(inc),
        "expense":     _money(exp),
        "net_surplus": _money(inc - exp),
        "from_date":   from_date,
        "to_date":     to_date,
    }


def _compute_from_profit(org_id: str, *, financial_year: int,
                         declare_percent, min_shares_units,
                         only_active: bool = True) -> dict:
    """
    Dividends are declared as a percent of the year's net surplus.
    The remaining (100 - declare_percent) stays in retained earnings.

    Split of the declare amount across members is proportional to
    shares held, so:
        dividend_per_share = declare_amount / total_eligible_shares
        member_amount      = shares_held × dividend_per_share
    """
    admin = _admin()

    # 1. YTD source figures
    pnl = _ytd_net_surplus(org_id, financial_year)
    net_surplus = pnl["net_surplus"]
    if net_surplus <= 0:
        raise ValueError(
            f"No distributable surplus for {financial_year} "
            f"(net surplus is {net_surplus})."
        )

    pct = _to_decimal(declare_percent)
    if pct <= 0 or pct > 100:
        raise ValueError("declare_percent must be between 0 and 100")

    declare_amount = _money(net_surplus * pct / Decimal("100"))
    retained_amount = _money(net_surplus - declare_amount)

    # 2. Eligible members and total shares
    q = (
        admin.table("members")
        .select("id, member_id, first_name, last_name, status, shares_units")
        .eq("org_id", org_id)
    )
    if only_active:
        q = q.eq("status", "active")
    members = q.execute().data or []

    min_units = _to_decimal(min_shares_units)
    eligible = []
    total_shares = ZERO
    for m in members:
        u = _to_decimal(m.get("shares_units"))
        if u < min_units:
            continue
        if u <= 0:
            continue
        eligible.append({
            "member_id":    m["id"],
            "member_code":  m.get("member_id"),
            "member_name":  f"{m.get('first_name','')} {m.get('last_name','')}".strip(),
            "shares_units": _shares(u),
            "status":       m.get("status"),
        })
        total_shares += u

    if total_shares <= 0:
        raise ValueError("No eligible shares to distribute against")

    dps = _money(declare_amount / total_shares)  # dividend per share

    # 3. Compute per member — using exact proportional math, last row absorbs rounding
    rows = []
    running_total = ZERO
    for i, e in enumerate(eligible):
        u = e["shares_units"]
        if i < len(eligible) - 1:
            amt = _money(u * dps)
        else:
            amt = _money(declare_amount - running_total)
        running_total += amt
        rows.append({
            "member_id":      e["member_id"],
            "member_code":    e["member_code"],
            "member_name":    e["member_name"],
            "shares_units":   float(u),
            "share_price":    0.0,
            "dividend_amount":float(amt),
            "currency":       pnl.get("currency") or "UGX",
            "status":         e["status"],
        })

    # currency
    s = (
        admin.table("institution_settings")
        .select("currency")
        .eq("org_id", org_id).limit(1).execute()
    ).data or [{}]
    currency = s[0].get("currency") or "UGX"
    for r in rows:
        r["currency"] = currency

    return {
        "members":          rows,
        "total":            _money(declare_amount),
        "count":            len(rows),
        "currency":         currency,
        "net_surplus":      net_surplus,
        "declare_percent":  float(pct),
        "declare_amount":   declare_amount,
        "retained_amount":  retained_amount,
        "eligible_shares":  _shares(total_shares),
        "dividend_per_share": dps,
        "period_from":      pnl["from_date"].isoformat(),
        "period_to":        pnl["to_date"].isoformat(),
        "income":           pnl["income"],
        "expense":          pnl["expense"],
    }