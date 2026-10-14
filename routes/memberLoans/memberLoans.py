"""
Member Loans blueprint (self-service).

A logged-in member can:
  - browse active loan products
  - apply for a loan (creates a `submitted` application, source='member')
  - view their applications and current loans
  - repay an active loan ONLINE ONLY
  - view the repayment schedule and history of each loan
  - print a receipt for a repayment

Endpoints:
    GET    /member/loans                                   -> page
    GET    /member/loans/api/products                      -> active products + eligibility
    GET    /member/loans/api/applications                  -> paginated own applications
    GET    /member/loans/api/applications/<uuid:app>       -> single application (with events)
    GET    /member/loans/api/loans                         -> paginated own loans
    GET    /member/loans/api/loans/<uuid:loan>             -> loan detail
    GET    /member/loans/api/loans/<uuid:loan>/schedule    -> expected repayment schedule
    GET    /member/loans/api/loans/<uuid:loan>/repayments  -> repayment history
    GET    /member/loans/api/repayments/<uuid:tx>/receipt  -> printable HTML receipt

    POST   /member/loans/api/eligibility-check             -> pre-flight check
    POST   /member/loans/api/apply                         -> submit application
    POST   /member/loans/api/loans/<uuid:loan>/pay         -> ONLINE ONLY repayment
    POST   /member/loans/api/payment/<deposit_id>/verify   -> verify pawaPay deposit

Design:
- Members cannot self-approve or self-disburse; those are admin actions.
- Cash repayments are REFUSED at the API boundary.
- All money math reuses the same primitives the admin module uses.
- All DB reads are scoped by `member["id"]` and `member["org_id"]`.
"""

import os
import json
import secrets
import logging
from pathlib import Path
from datetime import datetime, timezone, date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import (
    Blueprint, render_template, request, jsonify, session,
    redirect, url_for, Response, current_app,
)
from dotenv import load_dotenv
from supabase import create_client, Client

from routes.payments.pawapay import (
    deposit_mtn_ug,
    deposit_airtel_ug,
    verify_deposit,
)

# Reuse the exact same primitives the admin module uses.
from routes.loanApplications.loanApplications import (
    _load_product,
    _product_snapshot,
    _fees,
    _simulate,
    _eligibility,
    _log_event,
    _next_app_number,
)
from routes.repayments.repayments import _apply_repayment

load_dotenv()

log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] member_loans: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

member_loans_bp = Blueprint(
    "member_loans", __name__, url_prefix="/member/loans",
)

PAWAPAY_MESSAGE = "SACCO loan"          # ≤ 22 chars
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


def _normalize_phone(p: str) -> str:
    digits = "".join(c for c in (p or "") if c.isdigit())
    if digits.startswith("0"):
        digits = "256" + digits[1:]
    elif not digits.startswith("256") and len(digits) == 9:
        digits = "256" + digits
    return digits


def _member() -> dict | None:
    return session.get("member")


# ---------------------------------------------------------------------------
# Pending online staging (isolated dir)
# ---------------------------------------------------------------------------
def _pending_path(deposit_id: str) -> Path:
    p = Path(current_app.instance_path) / "pending_member_loans"
    p.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (deposit_id or "") if c.isalnum() or c in "-_")
    return p / f"{safe}.json"


def _save_pending(deposit_id: str, payload: dict) -> None:
    path = _pending_path(deposit_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    tmp.replace(path)
    log.info("pending member loan repayment saved: %s", path)


def _load_pending(deposit_id: str) -> dict | None:
    path = _pending_path(deposit_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("failed to read pending file %s", path)
        return None


def _delete_pending(deposit_id: str) -> None:
    path = _pending_path(deposit_id)
    try:
        if path.exists():
            path.unlink()
    except Exception:
        log.exception("failed to delete pending file %s", path)


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
@member_loans_bp.route("/", methods=["GET"])
def page():
    if not session.get("member"):
        return redirect(url_for("member_auth.login_page"))
    return render_template("memberLoans/memberLoans.html")


# ---------------------------------------------------------------------------
# API: loan products available to the member
# ---------------------------------------------------------------------------
@member_loans_bp.route("/api/products", methods=["GET"])
def api_products():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        rows = (
            _admin().table("loan_products")
            .select(
                "id, name, code, category, description, "
                "min_amount, max_amount, "
                "min_term_months, max_term_months, "
                "interest_method, interest_rate_percent, interest_period, "
                "repayment_frequency, repayment_type, "
                "min_shares_units, min_savings_balance, min_membership_months, "
                "require_guarantors, min_guarantors, requires_collateral"
            )
            .eq("org_id", m["org_id"]).eq("is_active", True)
            .order("name")
            .execute()
        ).data or []
    except Exception as e:
        log.exception("products fetch failed")
        return _bad(f"Could not load products: {e}", 500, reason="db_error")

    # attach a lightweight eligibility preview for the member's default request
    out = []
    for p in rows:
        out.append({
            "id":                   p["id"],
            "name":                 p["name"],
            "code":                 p.get("code"),
            "category":             p.get("category"),
            "description":          p.get("description"),
            "min_amount":           float(_to_decimal(p.get("min_amount"))),
            "max_amount":           float(_to_decimal(p.get("max_amount"))),
            "min_term_months":      p.get("min_term_months"),
            "max_term_months":      p.get("max_term_months"),
            "interest_method":      p.get("interest_method"),
            "interest_rate_percent":float(_to_decimal(p.get("interest_rate_percent"))),
            "interest_period":      p.get("interest_period"),
            "repayment_frequency":  p.get("repayment_frequency"),
            "repayment_type":       p.get("repayment_type"),
            "min_shares_units":     float(_to_decimal(p.get("min_shares_units"))),
            "min_savings_balance":  float(_to_decimal(p.get("min_savings_balance"))),
            "min_membership_months":p.get("min_membership_months"),
            "require_guarantors":   bool(p.get("require_guarantors")),
            "min_guarantors":       p.get("min_guarantors") or 0,
            "requires_collateral":  bool(p.get("requires_collateral")),
        })

    return jsonify({"ok": True, "products": out})


# ---------------------------------------------------------------------------
# API: eligibility pre-check
# ---------------------------------------------------------------------------
@member_loans_bp.route("/api/eligibility-check", methods=["POST"])
def api_eligibility():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    data = request.get_json(silent=True) or {}
    pid  = (data.get("product_id") or "").strip()
    if not pid:
        return _bad("product_id required", reason="missing_fields")

    try:
        amt  = _to_decimal(data.get("requested_amount"))
        term = int(data.get("requested_term_months") or 0)
        if amt <= 0 or term < 1:
            raise ValueError()
    except (ValueError, TypeError):
        return _bad("Invalid amount or term", reason="bad_amount")

    product = _load_product(m["org_id"], pid)
    if not product:
        return _bad("Product not found", 404, reason="not_found")

    rules = _eligibility(
        m["org_id"], m["id"], product, amt, term,
    )
    return jsonify({"ok": True, "eligibility": rules})


# ---------------------------------------------------------------------------
# API: apply for a loan
# ---------------------------------------------------------------------------
@member_loans_bp.route("/api/apply", methods=["POST"])
def api_apply():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    data = request.get_json(silent=True) or {}
    pid  = (data.get("product_id") or "").strip()
    if not pid:
        return _bad("product_id required", reason="missing_fields")

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

    product = _load_product(m["org_id"], pid)
    if not product:
        return _bad("Product not found", 404, reason="not_found")
    if not product.get("is_active"):
        return _bad("Product is inactive", 400, reason="product_inactive")

    # soft eligibility — return the rules but don't block submission
    elig = _eligibility(m["org_id"], m["id"], product, amount, term)

    # currency from settings
    try:
        s_rows = (
            _admin().table("institution_settings")
            .select("currency")
            .eq("org_id", m["org_id"]).limit(1).execute()
        ).data or [{}]
        currency = s_rows[0].get("currency") or "UGX"
    except Exception:
        currency = "UGX"

    snap = _product_snapshot(product)

    try:
        app_number = _next_app_number(m["org_id"])
    except Exception as e:
        return _bad(f"Could not allocate application number: {e}", 500,
                    reason="number_alloc")

    row = {
        "org_id":                m["org_id"],
        "member_id":             m["id"],
        "product_id":            pid,
        "app_number":            app_number,
        "source":                "member",     # ← distinguishes member applications
        "created_by":            m["user_id"],
        "requested_amount":      float(amount),
        "requested_term_months": term,
        "purpose":               (data.get("purpose") or "").strip() or None,
        "currency":              currency,
        "product_snapshot":      snap,
        "status":                "submitted",
        "submitted_at":          _now_iso(),
    }

    try:
        resp = _admin().table("loan_applications").insert(row).execute()
    except Exception as e:
        log.exception("member loan application insert failed")
        return _bad(f"Could not create application: {e}", 500, reason="db_error")

    app = resp.data[0]
    # log the event attributed to the member
    try:
        _admin().table("loan_application_events").insert({
            "org_id":         m["org_id"],
            "application_id": app["id"],
            "actor_id":       m["user_id"],
            "event":          "submitted",
            "note":           "Submitted from member portal",
            "meta":           {"source": "member", "amount": float(amount),
                               "term": term},
        }).execute()
    except Exception:
        log.exception("event log failed (non-fatal)")

    return jsonify({
        "ok": True,
        "application": {
            "id":           app["id"],
            "app_number":   app["app_number"],
            "status":       app["status"],
            "amount":       float(amount),
            "term":         term,
        },
        "eligibility": elig,
    }), 201


# ---------------------------------------------------------------------------
# API: my applications
# ---------------------------------------------------------------------------
@member_loans_bp.route("/api/applications", methods=["GET"])
def api_applications():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    try:
        resp = (
            _admin().table("loan_applications")
            .select(
                "id, app_number, requested_amount, requested_term_months, "
                "approved_amount, approved_term_months, status, currency, "
                "purpose, submitted_at, approved_at, disbursed_at, created_at, "
                "loan_products!inner(name, code, category)",
                count="exact",
            )
            .eq("org_id", m["org_id"]).eq("member_id", m["id"])
            .order("created_at", desc=True)
            .range(start, end)
            .execute()
        )
    except Exception as e:
        log.exception("member applications list failed")
        return _bad(f"Could not load applications: {e}", 500, reason="db_error")

    return jsonify({
        "ok": True,
        "applications": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# API: single application (owned by the member)
# ---------------------------------------------------------------------------
@member_loans_bp.route("/api/applications/<uuid:app_uuid>", methods=["GET"])
def api_application_detail(app_uuid):
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        rows = (
            _admin().table("loan_applications")
            .select("*, loan_products!inner(name, code, category)")
            .eq("org_id", m["org_id"]).eq("id", str(app_uuid)).limit(1).execute()
        ).data
    except Exception as e:
        log.exception("member application detail failed")
        return _bad(f"Could not load application: {e}", 500, reason="db_error")
    if not rows:
        return _bad("Application not found", 404, reason="not_found")
    app = rows[0]
    if app["member_id"] != m["id"]:
        return _bad("Not your application", 403, reason="forbidden")

    events = (
        _admin().table("loan_application_events")
        .select("id, event, note, meta, created_at, actor_id")
        .eq("org_id", m["org_id"]).eq("application_id", str(app_uuid))
        .order("created_at", desc=True)
        .execute()
    ).data or []

    return jsonify({"ok": True, "application": app, "events": events})


# ---------------------------------------------------------------------------
# API: my loans (active + closed)
# ---------------------------------------------------------------------------
@member_loans_bp.route("/api/loans", methods=["GET"])
def api_loans():
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
        status   = (request.args.get("status") or "").strip()
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
            "interest_rate_percent, interest_method, "
            "repayment_frequency, term_months, currency, "
            "disbursed_at, maturity_date, status",
            count="exact",
        )
        .eq("org_id", m["org_id"]).eq("member_id", m["id"])
        .order("disbursed_at", desc=True)
        .range(start, end)
    )
    if status:
        q = q.eq("status", status)

    try:
        resp = q.execute()
    except Exception as e:
        log.exception("member loans list failed")
        return _bad(f"Could not load loans: {e}", 500, reason="db_error")

    return jsonify({
        "ok": True,
        "loans": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


@member_loans_bp.route("/api/loans/<uuid:loan_uuid>", methods=["GET"])
def api_loan_detail(loan_uuid):
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        rows = (
            _admin().table("loans")
            .select("*")
            .eq("org_id", m["org_id"]).eq("id", str(loan_uuid)).limit(1).execute()
        ).data
    except Exception as e:
        log.exception("member loan detail failed")
        return _bad(f"Could not load loan: {e}", 500, reason="db_error")
    if not rows:
        return _bad("Loan not found", 404, reason="not_found")
    loan = rows[0]
    if loan["member_id"] != m["id"]:
        return _bad("Not your loan", 403, reason="forbidden")

    # total outstanding for display
    total = (
        _to_decimal(loan["outstanding_principal"]) +
        _to_decimal(loan["outstanding_interest"]) +
        _to_decimal(loan.get("outstanding_penalties") or 0) +
        _to_decimal(loan.get("outstanding_fees") or 0)
    )
    loan["total_outstanding"] = float(_q(total))

    return jsonify({"ok": True, "loan": loan})


@member_loans_bp.route("/api/loans/<uuid:loan_uuid>/schedule", methods=["GET"])
def api_loan_schedule(loan_uuid):
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        rows = (
            _admin().table("loans")
            .select("*")
            .eq("org_id", m["org_id"]).eq("id", str(loan_uuid)).limit(1).execute()
        ).data
    except Exception as e:
        log.exception("schedule fetch failed")
        return _bad(f"Could not load loan: {e}", 500, reason="db_error")
    if not rows:
        return _bad("Loan not found", 404, reason="not_found")
    loan = rows[0]
    if loan["member_id"] != m["id"]:
        return _bad("Not your loan", 403, reason="forbidden")

    snap = {
        "interest_method":       loan.get("interest_method") or "flat",
        "interest_rate_percent": loan.get("interest_rate_percent") or 0,
        "interest_period":       "monthly",
        "repayment_frequency":   loan.get("repayment_frequency") or "monthly",
        "repayment_type":        loan.get("repayment_type") or "amortized",
    }
    try:
        sim = _simulate(
            _to_decimal(loan["principal_amount"]),
            int(loan["term_months"]),
            snap,
        )
    except Exception as e:
        log.exception("simulate failed")
        return _bad(f"Could not build schedule: {e}", 500, reason="db_error")

    return jsonify({
        "ok":         True,
        "schedule":   sim["schedule"],
        "summary":    sim,
        "collected":  {
            "principal": float(_to_decimal(loan.get("collected_principal"))),
            "interest":  float(_to_decimal(loan.get("collected_interest"))),
        },
    })


@member_loans_bp.route("/api/loans/<uuid:loan_uuid>/repayments", methods=["GET"])
def api_loan_repayments(loan_uuid):
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(200, max(10, int(request.args.get("per_page", 50))))
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    # Ensure the loan belongs to the member first
    try:
        own = (
            _admin().table("loans")
            .select("id, member_id")
            .eq("org_id", m["org_id"]).eq("id", str(loan_uuid)).limit(1).execute()
        ).data
    except Exception:
        return _bad("Could not load loan", 500, reason="db_error")
    if not own or own[0]["member_id"] != m["id"]:
        return _bad("Not your loan", 403, reason="forbidden")

    try:
        resp = (
            _admin().table("repayments")
            .select(
                "id, receipt_number, amount, currency, method, provider, "
                "status, paid_at, created_at, narration, "
                "repayment_allocations(to_interest, to_principal, "
                "to_penalties, to_fees, to_suspense, total)",
                count="exact",
            )
            .eq("org_id", m["org_id"]).eq("loan_id", str(loan_uuid))
            .order("created_at", desc=True)
            .range(start, end).execute()
        )
    except Exception as e:
        log.exception("member loan repayments failed")
        return _bad(f"Could not load repayments: {e}", 500, reason="db_error")

    return jsonify({
        "ok": True,
        "repayments": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# API: receipt for a repayment
# ---------------------------------------------------------------------------
@member_loans_bp.route("/api/repayments/<uuid:tx_uuid>/receipt", methods=["GET"])
def api_receipt(tx_uuid):
    m = _member()
    if not m:
        return redirect(url_for("member_auth.login_page"))

    try:
        rows = (
            _admin().table("repayments")
            .select(
                "*, loans!inner(loan_number, currency), "
                "members!inner(member_id, first_name, last_name, phone), "
                "repayment_allocations(*)"
            )
            .eq("org_id", m["org_id"]).eq("id", str(tx_uuid)).limit(1).execute()
        ).data
    except Exception:
        log.exception("receipt fetch failed")
        return _bad("Could not load repayment", 500)
    if not rows:
        return _bad("Repayment not found", 404)
    r = rows[0]

    # Ownership check
    if r["member_id"] != m["id"]:
        return _bad("Not your repayment", 403, reason="forbidden")

    try:
        s = (
            _admin().table("institution_settings")
            .select("display_name, address_line, city, country, "
                    "support_phone, support_email")
            .eq("org_id", m["org_id"]).limit(1).execute()
        ).data or [{}]
    except Exception:
        s = [{}]

    return Response(_render_receipt(r, s[0]), mimetype="text/html")


def _render_receipt(r: dict, settings: dict) -> str:
    alloc = (r.get("repayment_allocations") or [{}])[0]
    m   = r["members"]
    ln  = r["loans"]
    display = settings.get("display_name") or "SACCO Pro"
    addr = " ".join(filter(None, [
        settings.get("address_line") or "",
        settings.get("city") or "",
        settings.get("country") or "",
    ]))
    contact = " · ".join(filter(None, [
        settings.get("support_phone") or "",
        settings.get("support_email") or "",
    ]))
    receipt = r.get("receipt_number") or ""
    paid_at = r.get("paid_at") or r.get("created_at") or ""
    method  = r.get("method") or ""
    provider= r.get("provider")
    method_line = method + (f" · {provider}" if provider else "")
    currency = r.get("currency") or ""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Receipt {receipt}</title>
<style>
 body{{font-family:Inter,system-ui,sans-serif;padding:40px;color:#1E293B;background:#F4F6F9;}}
 .card{{max-width:640px;margin:auto;background:#fff;border:1px solid #E2E8F0;
        border-radius:16px;padding:32px;}}
 h1{{font-size:20px;margin:0 0 4px;}} h2{{font-size:15px;margin:0;}}
 h3{{font-size:13px;color:#64748B;text-transform:uppercase;letter-spacing:.06em;margin:24px 0 6px;}}
 .sub{{color:#64748B;font-size:12px;margin:0 0 24px;}}
 .row{{display:flex;justify-content:space-between;padding:8px 0;
       border-bottom:1px dashed #E2E8F0;font-size:14px;}}
 .row:last-child{{border-bottom:none;}}
 .label{{color:#64748B;}} .total{{font-weight:800;font-size:18px;color:#1A62FF;}}
 .tag{{display:inline-block;background:#E6F4EA;color:#065F46;padding:4px 10px;
       border-radius:999px;font-size:11px;font-weight:600;}}
 .rev{{color:#065F46;font-weight:600;}}
 .foot{{text-align:center;color:#94A3B8;font-size:11px;margin-top:24px;}}
 @media print{{body{{padding:0;background:#fff;}} .card{{border:none;}}}}
</style></head><body>
<div class="card">
  <h1>{display}</h1>
  <p class="sub">{addr}<br>{contact}</p>
  <span class="tag">PAID</span>
  <h2 style="margin-top:16px;">Loan Repayment Receipt</h2>
  <p class="sub">Receipt <strong>{receipt}</strong> · {paid_at}</p>

  <div class="row"><span class="label">Member</span><span>{m['first_name']} {m['last_name']}</span></div>
  <div class="row"><span class="label">Member ID</span><span>{m['member_id']}</span></div>
  <div class="row"><span class="label">Loan number</span><span>{ln['loan_number']}</span></div>
  <div class="row"><span class="label">Method</span><span>{method_line}</span></div>
  <div class="row"><span class="label">Amount paid</span><span class="total">{r['amount']} {currency}</span></div>

  <h3>Allocation</h3>
  <div class="row"><span class="label">Interest <span class="rev">(SACCO revenue)</span></span>
       <span>{alloc.get('to_interest', 0)}</span></div>
  <div class="row"><span class="label">Penalties <span class="rev">(SACCO revenue)</span></span>
       <span>{alloc.get('to_penalties', 0)}</span></div>
  <div class="row"><span class="label">Fees <span class="rev">(SACCO revenue)</span></span>
       <span>{alloc.get('to_fees', 0)}</span></div>
  <div class="row"><span class="label">Principal</span>
       <span>{alloc.get('to_principal', 0)}</span></div>
  <div class="row"><span class="label">Overpayment held</span>
       <span>{alloc.get('to_suspense', 0)}</span></div>

  <p class="foot">Generated by {display} · Thank you for your payment.</p>
</div></body></html>"""


# ---------------------------------------------------------------------------
# API: PAY (online only)
# ---------------------------------------------------------------------------
@member_loans_bp.route("/api/loans/<uuid:loan_uuid>/pay", methods=["POST"])
def api_pay(loan_uuid):
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    data = request.get_json(silent=True) or {}

    # ── METHOD: online only ────────────────────────────────────────────────
    method = (data.get("method") or "online").lower()
    if method == "cash":
        return _bad(
            "Cash repayments are only accepted at the SACCO counter. "
            "Please use online (mobile money) to repay from your portal.",
            400, reason="cash_not_allowed",
        )
    if method != "online":
        return _bad("method must be 'online'", 400, reason="bad_method")

    try:
        amount = _to_decimal(data.get("amount"))
        if amount <= 0:
            return _bad("Amount must be > 0", 400, reason="bad_amount")
    except ValueError:
        return _bad("Invalid amount", 400, reason="bad_amount")

    provider  = (data.get("provider") or "").upper()
    pay_phone = (data.get("phone") or "").strip()
    if provider not in ("MTN", "AIRTEL"):
        return _bad("provider must be 'MTN' or 'AIRTEL'", 400,
                    reason="bad_provider")
    if not pay_phone:
        return _bad("phone is required", 400, reason="missing_phone")
    pay_phone = _normalize_phone(pay_phone)

    # ── Verify loan belongs to the member ──────────────────────────────────
    try:
        rows = (
            _admin().table("loans")
            .select("id, member_id, loan_number, currency, status")
            .eq("org_id", m["org_id"]).eq("id", str(loan_uuid)).limit(1).execute()
        ).data
    except Exception as e:
        log.exception("loan fetch failed")
        return _bad(f"Could not load loan: {e}", 500, reason="db_error")
    if not rows:
        return _bad("Loan not found", 404, reason="not_found")
    loan = rows[0]
    if loan["member_id"] != m["id"]:
        return _bad("Not your loan", 403, reason="forbidden")
    if loan["status"] not in ("active", "overdue"):
        return _bad(f"Loan is {loan['status']}", 400, reason="bad_state")

    # ── pawaPay preflight ──────────────────────────────────────────────────
    try:
        if provider == "MTN":
            pay_resp = deposit_mtn_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"mln-{secrets.token_hex(6)}",
                customer_message=PAWAPAY_MESSAGE,
            )
        else:
            pay_resp = deposit_airtel_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"mln-{secrets.token_hex(6)}",
                customer_message=PAWAPAY_MESSAGE,
            )
    except Exception as e:
        log.exception("pawaPay preflight failed")
        return _bad(f"Payment could not be initiated: {e}", 502,
                    reason="pawapay_raised")

    deposit_id = pay_resp.get("depositId") or pay_resp.get("deposit_id")
    pay_status = (pay_resp.get("status") or "").upper()
    if not deposit_id or pay_status in ("REJECTED", "FAILED"):
        reason = (pay_resp.get("failureReason") or {}).get("failureMessage") \
                 or pay_status or "unknown"
        return _bad(f"Payment was rejected: {reason}", 400,
                    reason="payment_rejected")

    # ── Stage ──────────────────────────────────────────────────────────────
    spec = {
        "action":     "member_loan_repayment",
        "org_id":     m["org_id"],
        "loan_id":    str(loan_uuid),
        "member_id":  m["id"],
        "amount":     str(amount),
        "currency":   loan["currency"],
        "method":     "online",
        "provider":   provider,
        "pay_phone":  pay_phone,
        "deposit_id": deposit_id,
        "narration":  (data.get("narration") or "").strip() or None,
        "created_by": m["user_id"],
        "staged_at":  _now_iso(),
    }
    try:
        _save_pending(deposit_id, spec)
    except Exception as e:
        log.exception("staging failed")
        return _bad(f"Payment accepted but staging failed: {e}", 500,
                    reason="staging_failed")

    return jsonify({
        "ok": True,
        "pending": True,
        "payment": {
            "method":     "online",
            "status":     "pending",
            "provider":   provider,
            "deposit_id": deposit_id,
            "phone":      pay_phone,
            "amount":     float(amount),
            "currency":   loan["currency"],
        },
    }), 202


# ---------------------------------------------------------------------------
# API: VERIFY (pawaPay)
# ---------------------------------------------------------------------------
@member_loans_bp.route("/api/payment/<deposit_id>/verify", methods=["POST"])
def api_verify(deposit_id):
    m = _member()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    staged = _load_pending(deposit_id)
    if not staged:
        return _bad("No pending payment for this reference", 404,
                    reason="not_found")

    # Ownership check
    if staged.get("member_id") != m["id"]:
        return _bad("Not your payment", 403, reason="forbidden")

    try:
        result = verify_deposit(deposit_id)
    except Exception as e:
        log.exception("verify failed")
        return _bad(f"Verification error: {e}", 502, reason="provider_error")

    status = result.get("status")

    if result.get("completed"):
        try:
            applied = _apply_repayment(staged["org_id"], staged["loan_id"], {
                "amount":     staged["amount"],
                "method":     "online",
                "currency":   staged.get("currency"),
                "provider":   staged.get("provider"),
                "pay_phone":  staged.get("pay_phone"),
                "deposit_id": staged.get("deposit_id"),
                "narration":  staged.get("narration"),
                "created_by": staged.get("created_by"),
            })
        except Exception as e:
            log.exception("apply after verify failed")
            return _bad(f"Payment succeeded but apply failed: {e}",
                        500, reason="apply_failed")
        _delete_pending(deposit_id)
        return jsonify({"ok": True, "status": "completed",
                        "provider_status": status, "repayment": applied})

    if result.get("failed"):
        _delete_pending(deposit_id)
        return jsonify({"ok": True, "status": "failed",
                        "provider_status": status,
                        "reason": result.get("failure_reason")})

    return jsonify({"ok": True, "status": "pending", "provider_status": status})