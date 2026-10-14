"""
Loan Products blueprint.

Endpoints (all admin-only JSON unless noted):
    GET    /loan-products/                            -> page
    GET    /loan-products/api/stats                   -> KPI cards
    GET    /loan-products/api/list                    -> paginated list
    GET    /loan-products/api/<uuid:lp_uuid>          -> single product
    POST   /loan-products/api/create                  -> create product
    PATCH  /loan-products/api/<uuid:lp_uuid>          -> update product
    PATCH  /loan-products/api/<uuid:lp_uuid>/toggle   -> activate / deactivate
    POST   /loan-products/api/<uuid:lp_uuid>/duplicate -> clone as new draft
    POST   /loan-products/api/simulate                -> preview schedule (no DB)

Design:
- Pure definitional data. Loan applications/disbursements reference products by id.
- Interest math (amortization, flat, reducing, compound) is computed on demand
  by `_simulate`, which the admin UI uses for live preview.
- Validation is strict; every numeric is checked against a range.
- `_apply_defaults` guarantees NOT NULL columns always get a value, even if the
  client sends blank strings or omits them entirely.
"""

import os
import logging
from datetime import datetime, timezone
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
        "%(asctime)s [%(levelname)s] loan_products: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

loan_products_bp = Blueprint("loan_products", __name__, url_prefix="/loan-products")

MONEY_Q = Decimal("0.01")

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


def _to_decimal(v, default="0") -> Decimal:
    if v is None or v == "":
        return Decimal(default)
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid number: {v}")


def _q(v: Decimal) -> Decimal:
    return v.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Field whitelists
# ---------------------------------------------------------------------------
STRING_FIELDS = {
    "name", "code", "description", "category",
    "interest_method", "interest_period", "compound_frequency",
    "repayment_frequency", "repayment_type",
    "other_fee_label", "collateral_note",
}

NUMERIC_FIELDS = {
    "min_amount", "max_amount", "amount_multiple_of",
    "interest_rate_percent",
    "processing_fee_percent", "processing_fee_flat",
    "insurance_fee_percent", "insurance_fee_flat",
    "other_fee_flat",
    "penalty_rate_percent",
    "min_shares_units", "min_savings_balance",
    "early_settle_penalty_percent",
}

INT_FIELDS = {
    "min_term_months", "max_term_months",
    "grace_periods",
    "penalty_grace_days",
    "min_membership_months",
    "min_guarantors",
    "max_loans_per_member",
}

BOOL_FIELDS = {
    "require_guarantors", "requires_collateral",
    "allows_topup", "allows_early_settle",
    "is_active", "penalty_compounding",
}

DATE_FIELDS = {"effective_from", "effective_to"}

ENUM_FIELDS = {
    "interest_method":     {"flat", "reducing_balance", "compound"},
    "interest_period":     {"monthly", "quarterly", "annually", "one_time"},
    "compound_frequency":  {"monthly", "quarterly", "annually"},
    "repayment_frequency": {"weekly", "biweekly", "monthly", "quarterly", "lump_sum"},
    "repayment_type":      {"amortized", "equal_principal", "interest_only", "bullet"},
}

CATEGORIES = {"general", "emergency", "education", "business", "asset", "agriculture", "other"}

# DB defaults for NOT NULL columns.
# Whenever the client sends "" / None / omits these, they get these values.
DEFAULTS = {
    # numerics
    "min_amount":                   0,
    "max_amount":                   0,
    "amount_multiple_of":           0,
    "interest_rate_percent":        0,
    "processing_fee_percent":       0,
    "processing_fee_flat":          0,
    "insurance_fee_percent":        0,
    "insurance_fee_flat":           0,
    "other_fee_flat":               0,
    "penalty_rate_percent":         0,
    "min_shares_units":             0,
    "min_savings_balance":          0,
    "early_settle_penalty_percent": 0,
    # ints
    "min_term_months":              1,
    "max_term_months":              60,
    "grace_periods":                0,
    "penalty_grace_days":           0,
    "min_membership_months":        0,
    "min_guarantors":               0,
    "max_loans_per_member":         1,
    # strings
    "category":                     "general",
    "interest_method":              "flat",
    "interest_period":              "monthly",
    "compound_frequency":           "monthly",
    "repayment_frequency":          "monthly",
    "repayment_type":               "amortized",
    # bools
    "require_guarantors":           False,
    "requires_collateral":          False,
    "allows_topup":                 False,
    "allows_early_settle":          True,
    "penalty_compounding":          False,
    "is_active":                    True,
}


# ---------------------------------------------------------------------------
# Cleaning + validation
# ---------------------------------------------------------------------------
def _clean_payload(payload: dict) -> dict:
    """
    Whitelist and coerce types. Blank strings become None; the caller
    decides whether to apply defaults.
    """
    out = {}
    for k, v in payload.items():
        if k in STRING_FIELDS:
            if v is None:
                out[k] = None
            else:
                out[k] = str(v).strip() or None

        elif k in NUMERIC_FIELDS:
            if v is None or v == "":
                out[k] = None
            else:
                try:
                    out[k] = float(Decimal(str(v)))
                except (InvalidOperation, ValueError):
                    raise ValueError(f"{k} must be a number")

        elif k in INT_FIELDS:
            if v is None or v == "":
                out[k] = None
            else:
                try:
                    out[k] = int(v)
                except (TypeError, ValueError):
                    raise ValueError(f"{k} must be an integer")

        elif k in BOOL_FIELDS:
            # Accept true/false or "true"/"false"
            if isinstance(v, str):
                out[k] = v.strip().lower() in ("1", "true", "yes", "on")
            else:
                out[k] = bool(v)

        elif k in DATE_FIELDS:
            if not v:
                out[k] = None
            else:
                try:
                    datetime.strptime(str(v)[:10], "%Y-%m-%d")
                    out[k] = str(v)[:10]
                except ValueError:
                    raise ValueError(f"{k} must be YYYY-MM-DD")

        elif k in ENUM_FIELDS:
            if v not in ENUM_FIELDS[k]:
                raise ValueError(f"{k} must be one of {sorted(ENUM_FIELDS[k])}")
            out[k] = v

        elif k == "category":
            if v not in CATEGORIES:
                raise ValueError(f"category must be one of {sorted(CATEGORIES)}")
            out[k] = v

        # unknown fields ignored

    return out


def _validate(clean: dict, *, is_create: bool):
    """
    Semantic cross-field validation. Returns an error message or None.
    """
    if is_create and not clean.get("name"):
        return "name is required"

    # numeric sanity
    for k in ("min_amount", "max_amount", "amount_multiple_of",
              "interest_rate_percent",
              "processing_fee_percent", "processing_fee_flat",
              "insurance_fee_percent", "insurance_fee_flat",
              "other_fee_flat", "penalty_rate_percent",
              "min_shares_units", "min_savings_balance",
              "early_settle_penalty_percent"):
        v = clean.get(k)
        if v is not None and v < 0:
            return f"{k} cannot be negative"

    for k in ("processing_fee_percent", "insurance_fee_percent",
              "early_settle_penalty_percent"):
        v = clean.get(k)
        if v is not None and v > 100:
            return f"{k} must be 0..100"

    v = clean.get("interest_rate_percent")
    if v is not None and v > 1000:
        return "interest_rate_percent too large"

    v = clean.get("penalty_rate_percent")
    if v is not None and v > 100:
        return "penalty_rate_percent must be 0..100"

    mn = clean.get("min_term_months")
    mx = clean.get("max_term_months")
    if mn is not None and mx is not None and mn > mx:
        return "min_term_months cannot exceed max_term_months"
    if mx is not None and mx > 600:
        return "max_term_months cannot exceed 600"

    amin = clean.get("min_amount")
    amax = clean.get("max_amount")
    if amin is not None and amax is not None and amax > 0 and amin > amax:
        return "min_amount cannot exceed max_amount"

    ef = clean.get("effective_from")
    et = clean.get("effective_to")
    if ef and et and ef > et:
        return "effective_from must be <= effective_to"

    return None


def _apply_defaults(clean: dict, *, fill_missing: bool) -> dict:
    """
    Replace None with DB defaults, and (on create) fill any missing keys
    with their defaults so NOT NULL columns always get a value.
    """
    for k, default in DEFAULTS.items():
        if k in clean and clean[k] is None:
            clean[k] = default
        elif fill_missing and k not in clean:
            clean[k] = default
    return clean


# ---------------------------------------------------------------------------
# Interest math
# ---------------------------------------------------------------------------
def _periods_per_year(freq: str) -> int:
    return {"weekly": 52, "biweekly": 26, "monthly": 12,
            "quarterly": 4, "lump_sum": 1}.get(freq, 12)


def _rate_per_period(rate_percent: Decimal, interest_period: str,
                     repayment_frequency: str) -> Decimal:
    """
    Convert a rate quoted per `interest_period` into a rate per repayment
    period. Simple proportional conversion.
    """
    r = rate_percent / Decimal("100")
    ppy = {
        "monthly":   12,
        "quarterly": 4,
        "annually":  1,
        "one_time":  None,
    }.get(interest_period, 12)

    rpy = _periods_per_year(repayment_frequency)

    if ppy is None:
        # one_time: total rate over the whole term; treat as per-period rate
        return r / Decimal(_periods_per_year(repayment_frequency)) if rpy else r

    return r * Decimal(ppy) / Decimal(rpy)


def _n_periods(term_months: int, repayment_frequency: str) -> int:
    if repayment_frequency == "weekly":
        return max(1, int(round(term_months * 52 / 12)))
    if repayment_frequency == "biweekly":
        return max(1, int(round(term_months * 26 / 12)))
    if repayment_frequency == "monthly":
        return max(1, term_months)
    if repayment_frequency == "quarterly":
        return max(1, term_months // 3)
    if repayment_frequency == "lump_sum":
        return 1
    return max(1, term_months)


def _simulate(principal: Decimal, term_months: int, product: dict) -> dict:
    """
    Build a repayment schedule. Returns:
      { method, repayment_type, periods, total_interest, total_payable,
        schedule: [{period, payment, principal, interest, balance}, ...] }
    """
    P = principal
    n = _n_periods(term_months, product["repayment_frequency"])
    rate = _rate_per_period(
        Decimal(str(product.get("interest_rate_percent") or 0)),
        product.get("interest_period") or "monthly",
        product.get("repayment_frequency") or "monthly",
    )

    method = product.get("interest_method") or "flat"
    rtype  = product.get("repayment_type") or "amortized"

    schedule = []

    if method == "flat":
        total_interest = _q(P * rate * Decimal(n))
        total_payable  = _q(P + total_interest)

        if rtype == "amortized":
            pay = _q(total_payable / Decimal(n))
            princ_period = _q(P / Decimal(n))
            running_total = Decimal("0")
            running_principal = Decimal("0")
            for i in range(1, n + 1):
                amount = pay if i < n else _q(total_payable - running_total)
                princ_i = princ_period if i < n else _q(P - running_principal)
                interest_i = _q(amount - princ_i)
                running_total += amount
                running_principal += princ_i
                schedule.append({
                    "period": i,
                    "payment": float(amount),
                    "principal": float(princ_i),
                    "interest": float(interest_i),
                    "balance": float(_q(max(Decimal("0"), P - running_principal))),
                })
        elif rtype == "equal_principal":
            princ_p = _q(P / Decimal(n))
            interest_p = _q(P * rate)
            running = P
            for i in range(1, n + 1):
                princ_i = princ_p if i < n else _q(P - princ_p * (n - 1))
                running = _q(max(Decimal("0"), running - princ_i))
                schedule.append({
                    "period": i,
                    "payment": float(_q(princ_i + interest_p)),
                    "principal": float(princ_i),
                    "interest":  float(interest_p),
                    "balance":   float(running),
                })
        elif rtype == "interest_only":
            interest_p = _q(P * rate)
            for i in range(1, n + 1):
                is_last = i == n
                schedule.append({
                    "period": i,
                    "payment": float(_q(interest_p + (P if is_last else Decimal("0")))),
                    "principal": float(P if is_last else Decimal("0")),
                    "interest":  float(interest_p),
                    "balance":   float(P if not is_last else Decimal("0")),
                })
        else:  # bullet
            schedule.append({
                "period": 1,
                "payment": float(total_payable),
                "principal": float(P),
                "interest":  float(total_interest),
                "balance":   0.0,
            })

    else:  # reducing_balance or compound
        if rate == 0:
            pay = _q(P / Decimal(n))
        else:
            one_plus_r = Decimal(1) + rate
            factor = one_plus_r ** n
            pay = _q(P * rate * factor / (factor - Decimal(1)))

        running = P
        total_interest = Decimal("0")
        for i in range(1, n + 1):
            interest_p = _q(running * rate)
            princ_p = _q(pay - interest_p)
            if i == n or princ_p > running:
                princ_p = running
                pay_i = _q(princ_p + interest_p)
            else:
                pay_i = pay
            running = _q(running - princ_p)
            total_interest += interest_p
            schedule.append({
                "period": i,
                "payment": float(pay_i),
                "principal": float(princ_p),
                "interest":  float(interest_p),
                "balance":   float(_q(max(Decimal("0"), running))),
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


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
@loan_products_bp.route("/", methods=["GET"])
def page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    if session["user"].get("role") != "admin":
        return redirect(url_for("auth.dashboard"))
    return render_template("loanProducts/loanProducts.html")


# ---------------------------------------------------------------------------
# STATS
# ---------------------------------------------------------------------------
@loan_products_bp.route("/api/stats", methods=["GET"])
@role_required("admin")
def api_stats():
    org_id = _org_id()
    try:
        rows = (
            _admin().table("loan_products")
            .select("is_active, interest_rate_percent, min_amount, max_amount")
            .eq("org_id", org_id)
            .execute()
        ).data or []
        total = len(rows)
        active = sum(1 for r in rows if r.get("is_active"))
        avg_rate = 0.0
        if total:
            avg_rate = sum(float(r.get("interest_rate_percent") or 0) for r in rows) / total
        min_amount = min((float(r.get("min_amount") or 0) for r in rows), default=0.0)
        max_amount = max((float(r.get("max_amount") or 0) for r in rows), default=0.0)
    except Exception as e:
        log.exception("lp stats failed: %s", e)
        return _bad("Could not load stats", 500)

    return jsonify({
        "ok": True,
        "stats": {
            "total_products": total,
            "active_products": active,
            "inactive_products": total - active,
            "avg_rate_percent": round(avg_rate, 3),
            "min_amount": min_amount,
            "max_amount": max_amount,
        },
    })


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------
@loan_products_bp.route("/api/list", methods=["GET"])
@role_required("admin")
def api_list():
    org_id = _org_id()
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
        search   = (request.args.get("q") or "").strip()
        status   = (request.args.get("status") or "").strip()
        category = (request.args.get("category") or "").strip()
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    q = (
        _admin().table("loan_products")
        .select(
            "id, name, code, category, description, "
            "min_amount, max_amount, "
            "min_term_months, max_term_months, "
            "interest_method, interest_rate_percent, interest_period, "
            "repayment_frequency, repayment_type, "
            "processing_fee_percent, processing_fee_flat, "
            "insurance_fee_percent, insurance_fee_flat, "
            "penalty_rate_percent, "
            "require_guarantors, min_guarantors, "
            "is_active, effective_from, effective_to, "
            "created_at, updated_at",
            count="exact",
        )
        .eq("org_id", org_id)
        .order("created_at", desc=True)
        .range(start, end)
    )
    if status == "active":
        q = q.eq("is_active", True)
    elif status == "inactive":
        q = q.eq("is_active", False)
    if category:
        q = q.eq("category", category)
    if search:
        like = f"%{search}%"
        q = q.or_(f"name.ilike.{like},code.ilike.{like},description.ilike.{like}")

    try:
        resp = q.execute()
    except Exception as e:
        log.exception("lp list failed: %s", e)
        return _bad("Could not load products", 500)

    return jsonify({
        "ok": True,
        "products": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# DETAIL
# ---------------------------------------------------------------------------
@loan_products_bp.route("/api/<uuid:lp_uuid>", methods=["GET"])
@role_required("admin")
def api_detail(lp_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("loan_products")
            .select("*")
            .eq("org_id", org_id)
            .eq("id", str(lp_uuid))
            .limit(1)
            .execute()
        )
    except Exception as e:
        log.exception("lp detail failed: %s", e)
        return _bad("Could not load product", 500)

    if not resp.data:
        return _bad("Product not found", 404)
    return jsonify({"ok": True, "product": resp.data[0]})


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------
@loan_products_bp.route("/api/create", methods=["POST"])
@role_required("admin")
def api_create():
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}

    try:
        clean = _clean_payload(data)
    except ValueError as e:
        return _bad(str(e), reason="validation")

    err = _validate(clean, is_create=True)
    if err:
        return _bad(err, reason="validation")

    # Fill every missing NOT NULL field with its default
    clean = _apply_defaults(clean, fill_missing=True)

    # Uniqueness on name within org
    try:
        existing = (
            _admin().table("loan_products")
            .select("id")
            .eq("org_id", org_id)
            .ilike("name", clean["name"])
            .limit(1)
            .execute()
        )
        if existing.data:
            return _bad("A product with this name already exists", 409,
                        reason="duplicate_name")
    except Exception:
        log.exception("uniqueness check failed")
        return _bad("Could not verify uniqueness", 500, reason="db_error")

    clean["org_id"]     = org_id
    clean["created_by"] = _user_id()
    clean["updated_at"] = _now_iso()

    log.info("creating loan product: keys=%s", sorted(clean.keys()))
    try:
        resp = _admin().table("loan_products").insert(clean).execute()
    except Exception as e:
        log.exception("lp insert failed; payload=%s", clean)
        return _bad(f"Could not create product: {e}", 500, reason="db_error")

    log.info("loan product created: %s", resp.data[0]["id"])
    return jsonify({"ok": True, "product": resp.data[0]}), 201


# ---------------------------------------------------------------------------
# UPDATE
# ---------------------------------------------------------------------------
@loan_products_bp.route("/api/<uuid:lp_uuid>", methods=["PATCH"])
@role_required("admin")
def api_update(lp_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}

    try:
        clean = _clean_payload(data)
    except ValueError as e:
        return _bad(str(e), reason="validation")

    if not clean:
        return _bad("No valid fields to update", reason="empty")

    # Fetch current for cross-field re-validation
    try:
        cur = (
            _admin().table("loan_products")
            .select("*")
            .eq("org_id", org_id)
            .eq("id", str(lp_uuid))
            .limit(1)
            .execute()
        )
    except Exception:
        log.exception("lp fetch failed")
        return _bad("Could not load product", 500, reason="db_error")
    if not cur.data:
        return _bad("Product not found", 404, reason="not_found")

    merged = {**cur.data[0], **clean}

    err = _validate(merged, is_create=False)
    if err:
        return _bad(err, reason="validation")

    # Replace any explicitly-null NOT NULL columns with their defaults
    clean = _apply_defaults(clean, fill_missing=False)

    # Name uniqueness if changing
    if "name" in clean and clean["name"] != cur.data[0]["name"]:
        dup = (
            _admin().table("loan_products")
            .select("id")
            .eq("org_id", org_id)
            .ilike("name", clean["name"])
            .neq("id", str(lp_uuid))
            .limit(1)
            .execute()
        )
        if dup.data:
            return _bad("Another product already uses this name", 409,
                        reason="duplicate_name")

    clean["updated_at"] = _now_iso()

    log.info("updating loan product %s keys=%s", lp_uuid, sorted(clean.keys()))
    try:
        resp = (
            _admin().table("loan_products")
            .update(clean)
            .eq("org_id", org_id)
            .eq("id", str(lp_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("lp update failed; payload=%s", clean)
        return _bad(f"Could not update product: {e}", 500, reason="db_error")

    return jsonify({"ok": True, "product": resp.data[0]})


# ---------------------------------------------------------------------------
# TOGGLE active
# ---------------------------------------------------------------------------
@loan_products_bp.route("/api/<uuid:lp_uuid>/toggle", methods=["PATCH"])
@role_required("admin")
def api_toggle(lp_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    if "is_active" not in data:
        return _bad("is_active is required", reason="missing_field")
    active = bool(data["is_active"])

    try:
        resp = (
            _admin().table("loan_products")
            .update({"is_active": active, "updated_at": _now_iso()})
            .eq("org_id", org_id)
            .eq("id", str(lp_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("lp toggle failed")
        return _bad("Could not toggle product", 500, reason="db_error")

    if not resp.data:
        return _bad("Product not found", 404, reason="not_found")
    return jsonify({"ok": True, "product": resp.data[0]})


# ---------------------------------------------------------------------------
# DUPLICATE
# ---------------------------------------------------------------------------
@loan_products_bp.route("/api/<uuid:lp_uuid>/duplicate", methods=["POST"])
@role_required("admin")
def api_duplicate(lp_uuid):
    org_id = _org_id()
    try:
        src = (
            _admin().table("loan_products")
            .select("*")
            .eq("org_id", org_id)
            .eq("id", str(lp_uuid))
            .limit(1)
            .execute()
        )
    except Exception:
        log.exception("lp dup fetch failed")
        return _bad("Could not load source product", 500, reason="db_error")
    if not src.data:
        return _bad("Product not found", 404, reason="not_found")

    row = dict(src.data[0])
    for k in ("id", "created_at", "updated_at", "created_by"):
        row.pop(k, None)
    row["name"] = f"{row['name']} (copy)"
    row["is_active"] = False
    row["org_id"] = org_id
    row["created_by"] = _user_id()
    row["updated_at"] = _now_iso()

    # Ensure nothing is None on NOT NULL columns
    row = _apply_defaults(row, fill_missing=True)

    try:
        resp = _admin().table("loan_products").insert(row).execute()
    except Exception as e:
        log.exception("lp duplicate failed; payload=%s", row)
        return _bad(f"Could not duplicate product: {e}", 500, reason="db_error")

    return jsonify({"ok": True, "product": resp.data[0]}), 201


# ---------------------------------------------------------------------------
# SIMULATE
# ---------------------------------------------------------------------------
@loan_products_bp.route("/api/simulate", methods=["POST"])
@role_required("admin")
def api_simulate():
    """
    Body: { principal, term_months, product: {...fields...} }
    Returns the full repayment schedule + totals. No DB writes.
    """
    data = request.get_json(silent=True) or {}
    try:
        principal = _to_decimal(data.get("principal"))
        if principal <= 0:
            return _bad("principal must be > 0", reason="bad_principal")
    except ValueError:
        return _bad("Invalid principal", reason="bad_principal")

    try:
        term_months = int(data.get("term_months") or 0)
        if term_months < 1:
            return _bad("term_months must be >= 1", reason="bad_term")
    except (TypeError, ValueError):
        return _bad("Invalid term_months", reason="bad_term")

    product = data.get("product") or {}

    # Fill defaults so the simulator works even on partial drafts
    for k, v in {
        "interest_method": "flat",
        "interest_period": "monthly",
        "interest_rate_percent": 0,
        "repayment_frequency": "monthly",
        "repayment_type": "amortized",
    }.items():
        product.setdefault(k, v)

    try:
        result = _simulate(principal, term_months, product)
    except Exception as e:
        log.exception("simulate failed")
        return _bad(f"Could not simulate: {e}", 500, reason="simulate_error")

    return jsonify({"ok": True, "simulation": result})