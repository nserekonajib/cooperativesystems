"""
Repayments blueprint.

Endpoints (admin-only JSON unless noted):
    GET    /repayments/                                          -> page
    GET    /repayments/api/stats                                 -> KPI cards (revenue split)
    GET    /repayments/api/loans                                 -> paginated loans
    GET    /repayments/api/loans/<uuid:loan_uuid>                -> loan detail
    GET    /repayments/api/loans/<uuid:loan_uuid>/schedule       -> expected schedule
    GET    /repayments/api/loans/<uuid:loan_uuid>/repayments     -> repayment history
    GET    /repayments/api/repayments                            -> global repayment feed
    GET    /repayments/api/repayments/<uuid:r_uuid>              -> single repayment + allocations
    GET    /repayments/api/repayments/<uuid:r_uuid>/receipt      -> printable HTML receipt

    POST   /repayments/api/loans/<uuid:loan_uuid>/pay            -> cash apply now; online pawaPay + stage
    POST   /repayments/api/payment/<ref>/verify                  -> verify online deposit
    POST   /repayments/api/repayments/<uuid:r_uuid>/reverse      -> reverse a completed repayment
    POST   /repayments/api/loans/<uuid:loan_uuid>/accrue-penalties

    GET    /repayments/api/statement/<uuid:member_uuid>          -> member statement CSV

Design notes:
- Interest is REVENUE to the SACCO, tracked on loans.collected_interest
  and repayment_allocations.to_interest. /api/stats exposes the split.
- Allocation order (SACCO-favouring):
      1. Penalties
      2. Interest
      3. Fees
      4. Principal
      5. Any excess -> suspense (overpayment)
- Every completed repayment writes an allocation row. Reversal restores
  the loan balances and flips the repayment to 'reversed'.
- Online payments follow the pawaPay-first pattern used elsewhere in the app:
  call pawaPay, stage spec to instance/pending_repayments/<id>.json, apply
  only when /verify reports COMPLETED.
- No N+1: list queries are single joined queries.
"""

import os
import io
import csv
import json
import secrets
import logging
from pathlib import Path
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import (
    Blueprint, render_template, request, jsonify, session,
    redirect, url_for, Response, current_app,
)
from dotenv import load_dotenv
from supabase import create_client, Client

from routes.auth.auth import role_required
from routes.payments.pawapay import (
    deposit_mtn_ug,
    deposit_airtel_ug,
    verify_deposit,
)

load_dotenv()

log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] repayments: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

repayments_bp = Blueprint("repayments", __name__, url_prefix="/repayments")

PAWAPAY_MESSAGE = "SACCO repay"   # ≤ 22 chars
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


def _to_decimal(v, default="0") -> Decimal:
    if v is None or v == "":
        return Decimal(default)
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid number: {v}")


def _q(v: Decimal) -> Decimal:
    return v.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _normalize_phone(p: str) -> str:
    digits = "".join(c for c in (p or "") if c.isdigit())
    if digits.startswith("0"):
        digits = "256" + digits[1:]
    elif not digits.startswith("256") and len(digits) == 9:
        digits = "256" + digits
    return digits


def _parse_date(v):
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _next_receipt(org_id: str) -> str:
    try:
        resp = _admin().rpc("next_repayment_receipt", {"p_org": org_id}).execute()
    except Exception as e:
        log.exception("next_repayment_receipt failed")
        raise RuntimeError(f"Could not allocate receipt number: {e}")
    raw = resp.data
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, dict):
        raw = next(iter(raw.values()))
    return f"RCP-{int(raw):06d}"


# ---------------------------------------------------------------------------
# Pending online staging
# ---------------------------------------------------------------------------
def _pending_path(deposit_id: str) -> Path:
    p = Path(current_app.instance_path) / "pending_repayments"
    p.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (deposit_id or "") if c.isalnum() or c in "-_")
    return p / f"{safe}.json"


def _save_pending(deposit_id: str, payload: dict) -> None:
    path = _pending_path(deposit_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    tmp.replace(path)
    log.info("pending repayment saved: %s", path)


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
@repayments_bp.route("/", methods=["GET"])
def page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    if session["user"].get("role") != "admin":
        return redirect(url_for("auth.dashboard"))
    return render_template("repayments/repayments.html")


# ---------------------------------------------------------------------------
# STATS — revenue split
# ---------------------------------------------------------------------------
@repayments_bp.route("/api/stats", methods=["GET"])
@role_required("admin")
def api_stats():
    org_id = _org_id()
    try:
        loans = (
            _admin().table("loans")
            .select(
                "outstanding_principal, outstanding_interest, "
                "outstanding_penalties, "
                "collected_principal, collected_interest, "
                "collected_penalties, collected_fees, status"
            )
            .eq("org_id", org_id)
            .execute()
        ).data or []

        out_principal = ZERO
        out_interest  = ZERO
        out_penalties = ZERO
        col_principal = ZERO
        col_interest  = ZERO
        col_penalties = ZERO
        col_fees      = ZERO
        active_loans  = 0
        overdue_loans = 0

        for l in loans:
            out_principal += _to_decimal(l.get("outstanding_principal"))
            out_interest  += _to_decimal(l.get("outstanding_interest"))
            out_penalties += _to_decimal(l.get("outstanding_penalties"))
            col_principal += _to_decimal(l.get("collected_principal"))
            col_interest  += _to_decimal(l.get("collected_interest"))
            col_penalties += _to_decimal(l.get("collected_penalties"))
            col_fees      += _to_decimal(l.get("collected_fees"))
            if l.get("status") == "active":
                active_loans += 1
            elif l.get("status") == "overdue":
                overdue_loans += 1

        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        recent = (
            _admin().table("repayments")
            .select("amount")
            .eq("org_id", org_id).eq("status", "completed")
            .gte("paid_at", since)
            .execute()
        ).data or []
        collected_30d = sum(_to_decimal(r.get("amount")) for r in recent)

    except Exception as e:
        log.exception("repayment stats failed: %s", e)
        return _bad("Could not load stats", 500)

    return jsonify({
        "ok": True,
        "stats": {
            "outstanding_principal": float(_q(out_principal)),
            "outstanding_interest":  float(_q(out_interest)),
            "outstanding_penalties": float(_q(out_penalties)),
            # Revenue
            "collected_interest":    float(_q(col_interest)),
            "collected_penalties":   float(_q(col_penalties)),
            "collected_fees":        float(_q(col_fees)),
            # Balance sheet
            "collected_principal":   float(_q(col_principal)),
            "active_loans":          active_loans,
            "overdue_loans":         overdue_loans,
            "collected_30d":         float(_q(collected_30d)),
        },
    })


# ---------------------------------------------------------------------------
# LOANS — list / detail / schedule / repayments
# ---------------------------------------------------------------------------
@repayments_bp.route("/api/loans", methods=["GET"])
@role_required("admin")
def api_loans():
    org_id = _org_id()
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
        search   = (request.args.get("q") or "").strip()
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
            "collected_penalties, collected_fees, "
            "interest_rate_percent, interest_method, "
            "repayment_frequency, term_months, currency, "
            "disbursed_at, maturity_date, status, "
            "members!inner(id, member_id, first_name, last_name, phone, email)",
            count="exact",
        )
        .eq("org_id", org_id)
        .order("disbursed_at", desc=True)
        .range(start, end)
    )
    if status:
        q = q.eq("status", status)
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
        log.exception("loans list failed: %s", e)
        return _bad("Could not load loans", 500)

    return jsonify({
        "ok": True,
        "loans": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


@repayments_bp.route("/api/loans/<uuid:loan_uuid>", methods=["GET"])
@role_required("admin")
def api_loan_detail(loan_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("loans")
            .select(
                "*, "
                "members!inner(id, member_id, first_name, last_name, phone, "
                "email, shares_units, savings_balance, loan_balance, status)"
            )
            .eq("org_id", org_id).eq("id", str(loan_uuid)).limit(1).execute()
        )
    except Exception as e:
        log.exception("loan detail failed: %s", e)
        return _bad("Could not load loan", 500)
    if not resp.data:
        return _bad("Loan not found", 404)

    loan = resp.data[0]
    total_outstanding = (
        _to_decimal(loan["outstanding_principal"]) +
        _to_decimal(loan["outstanding_interest"]) +
        _to_decimal(loan["outstanding_penalties"]) +
        _to_decimal(loan.get("outstanding_fees") or 0)
    )
    loan["total_outstanding"] = float(_q(total_outstanding))

    return jsonify({"ok": True, "loan": loan})


@repayments_bp.route("/api/loans/<uuid:loan_uuid>/repayments", methods=["GET"])
@role_required("admin")
def api_loan_repayments(loan_uuid):
    org_id = _org_id()
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(200, max(10, int(request.args.get("per_page", 50))))
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    try:
        resp = (
            _admin().table("repayments")
            .select(
                "id, receipt_number, amount, currency, method, provider, "
                "status, paid_at, narration, created_at, "
                "repayment_allocations(to_interest, to_principal, "
                "to_penalties, to_fees, to_suspense, total)",
                count="exact",
            )
            .eq("org_id", org_id).eq("loan_id", str(loan_uuid))
            .order("created_at", desc=True)
            .range(start, end)
            .execute()
        )
    except Exception as e:
        log.exception("loan repayments list failed: %s", e)
        return _bad("Could not load repayments", 500)

    return jsonify({
        "ok": True,
        "repayments": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


@repayments_bp.route("/api/loans/<uuid:loan_uuid>/schedule", methods=["GET"])
@role_required("admin")
def api_loan_schedule(loan_uuid):
    """Rebuild the expected schedule from loan terms. No DB writes."""
    org_id = _org_id()
    try:
        resp = (
            _admin().table("loans")
            .select("*")
            .eq("org_id", org_id).eq("id", str(loan_uuid)).limit(1).execute()
        )
    except Exception:
        log.exception("schedule fetch failed")
        return _bad("Could not load loan", 500)
    if not resp.data:
        return _bad("Loan not found", 404)
    loan = resp.data[0]

    # import the simulator from loanProducts
    try:
        from routes.loanProducts.loanProducts import _simulate
    except Exception as e:
        log.exception("simulator import failed")
        return _bad(f"Could not build schedule: {e}", 500)

    snap = {
        "interest_method":       loan.get("interest_method") or "flat",
        "interest_rate_percent": loan.get("interest_rate_percent") or 0,
        "interest_period":       "monthly",   # not stored on loan
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
        return _bad(f"Could not simulate schedule: {e}", 500)

    return jsonify({
        "ok": True,
        "schedule":  sim["schedule"],
        "summary":   sim,
        "collected": {
            "principal": float(_to_decimal(loan["collected_principal"])),
            "interest":  float(_to_decimal(loan["collected_interest"])),
        },
    })


# ---------------------------------------------------------------------------
# REPAYMENTS — feed + detail + receipt
# ---------------------------------------------------------------------------
@repayments_bp.route("/api/repayments", methods=["GET"])
@role_required("admin")
def api_repayments():
    org_id = _org_id()
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
        status   = (request.args.get("status") or "").strip()
        method   = (request.args.get("method") or "").strip()
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    q = (
        _admin().table("repayments")
        .select(
            "id, receipt_number, amount, currency, method, provider, status, "
            "paid_at, created_at, narration, "
            "loans!inner(id, loan_number), "
            "members!inner(id, member_id, first_name, last_name, phone)",
            count="exact",
        )
        .eq("org_id", org_id)
        .order("created_at", desc=True)
        .range(start, end)
    )
    if status:
        q = q.eq("status", status)
    if method:
        q = q.eq("method", method)

    try:
        resp = q.execute()
    except Exception as e:
        log.exception("repayment feed failed: %s", e)
        return _bad("Could not load repayments", 500)

    return jsonify({
        "ok": True,
        "repayments": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


@repayments_bp.route("/api/repayments/<uuid:r_uuid>", methods=["GET"])
@role_required("admin")
def api_repayment_detail(r_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("repayments")
            .select(
                "*, "
                "loans!inner(loan_number, interest_rate_percent, "
                "interest_method, term_months, currency), "
                "members!inner(member_id, first_name, last_name, phone, email), "
                "repayment_allocations(*)"
            )
            .eq("org_id", org_id).eq("id", str(r_uuid)).limit(1).execute()
        )
    except Exception as e:
        log.exception("repayment detail failed: %s", e)
        return _bad("Could not load repayment", 500)
    if not resp.data:
        return _bad("Repayment not found", 404)
    return jsonify({"ok": True, "repayment": resp.data[0]})


def _render_receipt(r: dict, settings: dict) -> str:
    alloc = (r.get("repayment_allocations") or [{}])[0]
    m     = r["members"]
    ln    = r["loans"]

    display   = settings.get("display_name") or "SACCO Pro"
    addr      = " ".join(filter(None, [
        settings.get("address_line") or "",
        settings.get("city") or "",
        settings.get("country") or "",
    ]))
    contact   = " · ".join(filter(None, [
        settings.get("support_phone") or "",
        settings.get("support_email") or "",
    ]))
    receipt   = r.get("receipt_number") or ""
    paid_at   = r.get("paid_at") or r.get("created_at") or ""
    method    = r.get("method") or ""
    provider  = r.get("provider")
    method_line = method + (f" · {provider}" if provider else "")
    currency  = r.get("currency") or ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Receipt {receipt}</title>
<style>
  body{{font-family:Inter,system-ui,sans-serif;padding:40px;color:#1E293B;background:#F4F6F9;}}
  .card{{max-width:640px;margin:auto;background:#fff;border:1px solid #E2E8F0;
         border-radius:16px;padding:32px;}}
  h1{{font-size:20px;margin:0 0 4px;}}
  h2{{font-size:15px;margin:0;}}
  h3{{font-size:13px;color:#64748B;text-transform:uppercase;letter-spacing:.06em;margin:24px 0 6px;}}
  .sub{{color:#64748B;font-size:12px;margin:0 0 24px;}}
  .row{{display:flex;justify-content:space-between;padding:8px 0;
        border-bottom:1px dashed #E2E8F0;font-size:14px;}}
  .row:last-child{{border-bottom:none;}}
  .label{{color:#64748B;}}
  .total{{font-weight:800;font-size:18px;color:#1A62FF;}}
  .tag{{display:inline-block;background:#E6F4EA;color:#065F46;
        padding:4px 10px;border-radius:999px;font-size:11px;font-weight:600;}}
  .rev{{color:#065F46;font-weight:600;}}
  .foot{{text-align:center;color:#94A3B8;font-size:11px;margin-top:24px;}}
  @media print{{body{{padding:0;background:#fff;}} .card{{border:none;}}}}
</style>
</head>
<body>
<div class="card">
  <h1>{display}</h1>
  <p class="sub">{addr}<br>{contact}</p>

  <span class="tag">PAID</span>
  <h2 style="margin-top:16px;">Repayment Receipt</h2>
  <p class="sub">Receipt <strong>{receipt}</strong> · {paid_at}</p>

  <div class="row"><span class="label">Member</span><span>{m['first_name']} {m['last_name']}</span></div>
  <div class="row"><span class="label">Member ID</span><span>{m['member_id']}</span></div>
  <div class="row"><span class="label">Loan number</span><span>{ln['loan_number']}</span></div>
  <div class="row"><span class="label">Method</span><span>{method_line}</span></div>
  <div class="row"><span class="label">Amount received</span>
       <span class="total">{r['amount']} {currency}</span></div>

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
</div>
</body>
</html>"""


@repayments_bp.route("/api/repayments/<uuid:r_uuid>/receipt", methods=["GET"])
@role_required("admin")
def api_receipt(r_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("repayments")
            .select(
                "*, "
                "loans!inner(loan_number, currency), "
                "members!inner(member_id, first_name, last_name, phone, email), "
                "repayment_allocations(*)"
            )
            .eq("org_id", org_id).eq("id", str(r_uuid)).limit(1).execute()
        )
    except Exception:
        log.exception("receipt fetch failed")
        return _bad("Could not load repayment", 500)
    if not resp.data:
        return _bad("Repayment not found", 404)
    r = resp.data[0]

    try:
        s = (
            _admin().table("institution_settings")
            .select("display_name, address_line, city, country, "
                    "support_phone, support_email")
            .eq("org_id", org_id).limit(1).execute()
        ).data
    except Exception:
        log.exception("settings fetch failed (non-fatal)")
        s = []
    settings = (s or [{}])[0]

    html = _render_receipt(r, settings)
    return Response(html, mimetype="text/html")


# ---------------------------------------------------------------------------
# ALLOCATION ENGINE
# ---------------------------------------------------------------------------
def _allocate(amount: Decimal, outstanding: dict) -> dict:
    """
    Split a payment across outstanding balances, SACCO-favouring order:
        1. Penalties
        2. Interest
        3. Fees
        4. Principal
        5. Excess -> suspense
    """
    remaining = amount

    to_penalties = min(remaining, outstanding["penalties"])
    remaining -= to_penalties

    to_interest = min(remaining, outstanding["interest"])
    remaining -= to_interest

    to_fees = min(remaining, outstanding["fees"])
    remaining -= to_fees

    to_principal = min(remaining, outstanding["principal"])
    remaining -= to_principal

    to_suspense = remaining

    return {
        "to_penalties": to_penalties,
        "to_interest":  to_interest,
        "to_fees":      to_fees,
        "to_principal": to_principal,
        "to_suspense":  to_suspense,
        "total":        amount,
        "new_outstanding": {
            "principal": outstanding["principal"] - to_principal,
            "interest":  outstanding["interest"]  - to_interest,
            "penalties": outstanding["penalties"] - to_penalties,
            "fees":      outstanding["fees"]      - to_fees,
        },
    }


# ---------------------------------------------------------------------------
# APPLY REPAYMENT
# ---------------------------------------------------------------------------
def _apply_repayment(org_id: str, loan_id: str, spec: dict) -> dict:
    """
    spec: amount, currency?, method, provider?, pay_phone?, deposit_id?,
          narration?, created_by?
    """
    admin = _admin()

    amount = _to_decimal(spec["amount"])
    if amount <= 0:
        raise ValueError("amount must be > 0")

    # 1. Fetch loan
    loan_resp = (
        admin.table("loans")
        .select(
            "id, loan_number, member_id, currency, status, "
            "outstanding_principal, outstanding_interest, "
            "outstanding_penalties, outstanding_fees, "
            "collected_principal, collected_interest, "
            "collected_penalties, collected_fees"
        )
        .eq("org_id", org_id).eq("id", loan_id).limit(1).execute()
    )
    if not loan_resp.data:
        raise RuntimeError("Loan not found")
    loan = loan_resp.data[0]

    if loan["status"] not in ("active", "overdue"):
        raise RuntimeError(f"Cannot repay a loan in status '{loan['status']}'")

    outstanding = {
        "principal": _to_decimal(loan["outstanding_principal"]),
        "interest":  _to_decimal(loan["outstanding_interest"]),
        "penalties": _to_decimal(loan["outstanding_penalties"]),
        "fees":      _to_decimal(loan.get("outstanding_fees") or 0),
    }

    alloc = _allocate(amount, outstanding)
    new_out = alloc.pop("new_outstanding")

    receipt = _next_receipt(org_id)

    closes_loan = (
        new_out["principal"] <= 0 and
        new_out["interest"]  <= 0 and
        new_out["penalties"] <= 0 and
        new_out["fees"]      <= 0
    )

    # 2. Insert repayment
    row = {
        "org_id":         org_id,
        "loan_id":        loan["id"],
        "member_id":      loan["member_id"],
        "receipt_number": receipt,
        "amount":         float(_q(amount)),
        "currency":       spec.get("currency") or loan["currency"] or "UGX",
        "method":         spec["method"],
        "status":         "completed",
        "paid_at":        _now_iso(),
        "narration":      spec.get("narration"),
        "created_by":     spec.get("created_by") or _user_id(),
        "balance_before": {k: float(_q(v)) for k, v in outstanding.items()},
        "balance_after":  {k: float(_q(v)) for k, v in new_out.items()},
    }
    if spec.get("provider"):
        row["provider"] = spec["provider"]
    if spec.get("deposit_id"):
        row["provider_ref"] = spec["deposit_id"]
    if spec.get("pay_phone"):
        row["phone"] = spec["pay_phone"]

    r_resp = admin.table("repayments").insert(row).execute()
    if not r_resp.data:
        raise RuntimeError("repayment insert returned no data")
    repayment = r_resp.data[0]

    # 3. Insert allocation
    admin.table("repayment_allocations").insert({
        "org_id":       org_id,
        "repayment_id": repayment["id"],
        "loan_id":      loan["id"],
        "to_penalties": float(_q(alloc["to_penalties"])),
        "to_interest":  float(_q(alloc["to_interest"])),
        "to_fees":      float(_q(alloc["to_fees"])),
        "to_principal": float(_q(alloc["to_principal"])),
        "to_suspense":  float(_q(alloc["to_suspense"])),
        "total":        float(_q(alloc["total"])),
    }).execute()

    # 4. Update loan
    loan_upd = {
        "outstanding_principal": float(_q(new_out["principal"])),
        "outstanding_interest":  float(_q(new_out["interest"])),
        "outstanding_penalties": float(_q(new_out["penalties"])),
        "outstanding_fees":      float(_q(new_out["fees"])),
        "collected_principal": float(_q(
            _to_decimal(loan["collected_principal"]) + alloc["to_principal"])),
        "collected_interest": float(_q(
            _to_decimal(loan["collected_interest"]) + alloc["to_interest"])),
        "collected_penalties": float(_q(
            _to_decimal(loan["collected_penalties"]) + alloc["to_penalties"])),
        "collected_fees": float(_q(
            _to_decimal(loan["collected_fees"]) + alloc["to_fees"])),
        "updated_at": _now_iso(),
    }
    if closes_loan:
        loan_upd["status"]    = "closed"
        loan_upd["closed_at"] = _now_iso()

    admin.table("loans").update(loan_upd).eq("id", loan["id"]).execute()
    
    try:
        from routes.accounts.accounts import record_asset_movement

        # Which income account does interest roll into? Use 4010.
        # Penalties -> 4020. Fees -> 4030.
        # Each component posts separately so P&L shows the split correctly.
        ref = f"Repayment {receipt}"
        method_key = "cash" if spec["method"] == "cash" else "online"

        if alloc["to_interest"] > 0:
            record_asset_movement(
                org_id=org_id, direction="in",
                amount=alloc["to_interest"], method=method_key,
                counterpart_account_code="4010",
                category="bank_interest" if method_key == "bank" else "other_income",
                narration=f"{ref} · interest",
                reference=receipt,
                created_by=spec.get("created_by"),
                meta={"source": "repayment", "repayment_id": repayment["id"]},
            )

        if alloc["to_penalties"] > 0:
            record_asset_movement(
                org_id=org_id, direction="in",
                amount=alloc["to_penalties"], method=method_key,
                counterpart_account_code="4020",
                category="membership_penalty",
                narration=f"{ref} · penalty",
                reference=receipt,
                created_by=spec.get("created_by"),
                meta={"source": "repayment", "repayment_id": repayment["id"]},
            )

        if alloc["to_fees"] > 0:
            record_asset_movement(
                org_id=org_id, direction="in",
                amount=alloc["to_fees"], method=method_key,
                counterpart_account_code="4030",
                category="other_income",
                narration=f"{ref} · fees",
                reference=receipt,
                created_by=spec.get("created_by"),
                meta={"source": "repayment", "repayment_id": repayment["id"]},
            )

        # Principal isn't revenue — it reduces the loan asset, but for the
        # SACCO's operating chart we don't post it (it would double-count
        # with the loan-disbursement entry that already reduced cash).
    except Exception:
        log.exception("chart-of-accounts post for repayment failed (non-fatal)")

    # 5. Mirror a member_transactions entry (non-fatal if it fails)
    try:
        admin.table("member_transactions").insert({
            "org_id":       org_id,
            "member_id":    loan["member_id"],
            "type":         "loan_repayment",
            "method":       spec["method"],
            "amount":       float(_q(amount)),
            "currency":     row["currency"],
            "status":       "completed",
            "narration":    f"Loan repayment · {receipt}",
            "provider":     spec.get("provider"),
            "provider_ref": spec.get("deposit_id"),
            "phone":        spec.get("pay_phone"),
            "created_by":   spec.get("created_by") or _user_id(),
        }).execute()
    except Exception:
        log.exception("member_transactions mirror failed (non-fatal)")

    # 6. Sync members.loan_balance
    try:
        admin.table("members").update({
            "loan_balance": float(_q(new_out["principal"])),
            "updated_at":   _now_iso(),
        }).eq("id", loan["member_id"]).execute()
    except Exception:
        log.exception("member loan_balance sync failed (non-fatal)")

    return {
        "id":             repayment["id"],
        "receipt_number": receipt,
        "amount":         float(_q(amount)),
        "currency":       row["currency"],
        "allocation": {
            "penalties": float(_q(alloc["to_penalties"])),
            "interest":  float(_q(alloc["to_interest"])),
            "fees":      float(_q(alloc["to_fees"])),
            "principal": float(_q(alloc["to_principal"])),
            "suspense":  float(_q(alloc["to_suspense"])),
        },
        "new_outstanding": {k: float(_q(v)) for k, v in new_out.items()},
        "loan_closed":     closes_loan,
    }


# ---------------------------------------------------------------------------
# API: pay (cash / online)
# ---------------------------------------------------------------------------
@repayments_bp.route("/api/loans/<uuid:loan_uuid>/pay", methods=["POST"])
@role_required("admin")
def api_pay(loan_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}

    try:
        amount = _to_decimal(data.get("amount"))
        if amount <= 0:
            return _bad("Amount must be > 0", reason="bad_amount")
    except ValueError:
        return _bad("Invalid amount", reason="bad_amount")

    method = (data.get("method") or "cash").lower()
    if method not in ("cash", "online"):
        return _bad("method must be 'cash' or 'online'", reason="bad_method")

    provider  = None
    pay_phone = None
    if method == "online":
        provider  = (data.get("provider") or "").upper()
        pay_phone = (data.get("phone") or "").strip()
        if provider not in ("MTN", "AIRTEL"):
            return _bad("provider must be 'MTN' or 'AIRTEL'", reason="bad_provider")
        if not pay_phone:
            return _bad("phone required for online payment", reason="missing_phone")
        pay_phone = _normalize_phone(pay_phone)

    try:
        loan_resp = (
            _admin().table("loans")
            .select("id, loan_number, currency, status, member_id")
            .eq("org_id", org_id).eq("id", str(loan_uuid)).limit(1).execute()
        )
    except Exception:
        log.exception("loan fetch failed")
        return _bad("Could not load loan", 500, reason="db_error")

    if not loan_resp.data:
        return _bad("Loan not found", 404, reason="not_found")
    loan = loan_resp.data[0]
    if loan["status"] not in ("active", "overdue"):
        return _bad(f"Loan is {loan['status']}", 400, reason="bad_state")

    # ---- CASH ----
    if method == "cash":
        try:
            result = _apply_repayment(org_id, str(loan_uuid), {
                "amount":     str(amount),
                "method":     "cash",
                "currency":   loan["currency"],
                "narration":  (data.get("narration") or "").strip() or None,
                "created_by": _user_id(),
            })
        except Exception as e:
            log.exception("cash repayment failed")
            return _bad(f"Could not record repayment: {e}", 500, reason="apply_failed")
        return jsonify({"ok": True, "repayment": result}), 201

    # ---- ONLINE ----
    try:
        if provider == "MTN":
            pay_resp = deposit_mtn_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"rep-{secrets.token_hex(6)}",
                customer_message=PAWAPAY_MESSAGE,
            )
        else:
            pay_resp = deposit_airtel_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"rep-{secrets.token_hex(6)}",
                customer_message=PAWAPAY_MESSAGE,
            )
    except Exception as e:
        log.exception("pawaPay preflight failed")
        return _bad(f"Payment could not be initiated: {e}.", 502, reason="pawapay_raised")

    deposit_id = pay_resp.get("depositId") or pay_resp.get("deposit_id")
    pay_status = (pay_resp.get("status") or "").upper()
    if not deposit_id or pay_status in ("REJECTED", "FAILED"):
        reason = (pay_resp.get("failureReason") or {}).get("failureMessage") \
                 or pay_status or "unknown"
        return _bad(f"Payment was rejected: {reason}", 400, reason="payment_rejected")

    spec = {
        "action":     "repay",
        "org_id":     org_id,
        "loan_id":    str(loan_uuid),
        "amount":     str(amount),
        "currency":   loan["currency"],
        "provider":   provider,
        "pay_phone":  pay_phone,
        "deposit_id": deposit_id,
        "narration":  (data.get("narration") or "").strip() or None,
        "created_by": _user_id(),
        "staged_at":  _now_iso(),
    }
    _save_pending(deposit_id, spec)

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
# API: verify online repayment
# ---------------------------------------------------------------------------
@repayments_bp.route("/api/payment/<deposit_id>/verify", methods=["POST"])
@role_required("admin")
def api_verify(deposit_id):
    org_id = _org_id()
    staged = _load_pending(deposit_id)
    if not staged:
        return _bad("No pending repayment for this reference", 404, reason="not_found")

    try:
        result = verify_deposit(deposit_id)
    except Exception as e:
        log.exception("verify failed")
        return _bad(f"Verification error: {e}", 502, reason="provider_error")

    status = result.get("status")

    if result.get("completed"):
        try:
            applied = _apply_repayment(org_id, staged["loan_id"], {
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


# ---------------------------------------------------------------------------
# API: reverse a repayment
# ---------------------------------------------------------------------------
@repayments_bp.route("/api/repayments/<uuid:r_uuid>/reverse", methods=["POST"])
@role_required("admin")
def api_reverse(r_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip() or None
    if not reason:
        return _bad("reason is required", reason="missing_reason")

    try:
        r_resp = (
            _admin().table("repayments")
            .select("*, repayment_allocations(*)")
            .eq("org_id", org_id).eq("id", str(r_uuid)).limit(1).execute()
        )
    except Exception:
        log.exception("reverse fetch failed")
        return _bad("Could not load repayment", 500)
    if not r_resp.data:
        return _bad("Repayment not found", 404, reason="not_found")

    r = r_resp.data[0]
    if r["status"] != "completed":
        return _bad(f"Cannot reverse a repayment in status '{r['status']}'",
                    400, reason="bad_state")

    alloc = (r.get("repayment_allocations") or [{}])[0]

    try:
        loan_resp = (
            _admin().table("loans")
            .select("*")
            .eq("org_id", org_id).eq("id", r["loan_id"]).limit(1).execute()
        )
    except Exception:
        log.exception("loan fetch failed")
        return _bad("Could not load loan", 500)
    if not loan_resp.data:
        return _bad("Loan not found", 404, reason="not_found")
    loan = loan_resp.data[0]

    new_principal = _q(_to_decimal(loan["outstanding_principal"]) + _to_decimal(alloc.get("to_principal")))
    new_interest  = _q(_to_decimal(loan["outstanding_interest"])  + _to_decimal(alloc.get("to_interest")))
    new_penalties = _q(_to_decimal(loan["outstanding_penalties"]) + _to_decimal(alloc.get("to_penalties")))
    new_fees      = _q(_to_decimal(loan.get("outstanding_fees") or 0) + _to_decimal(alloc.get("to_fees")))

    col_principal = _q(max(ZERO, _to_decimal(loan["collected_principal"]) - _to_decimal(alloc.get("to_principal"))))
    col_interest  = _q(max(ZERO, _to_decimal(loan["collected_interest"])  - _to_decimal(alloc.get("to_interest"))))
    col_penalties = _q(max(ZERO, _to_decimal(loan["collected_penalties"]) - _to_decimal(alloc.get("to_penalties"))))
    col_fees      = _q(max(ZERO, _to_decimal(loan["collected_fees"])      - _to_decimal(alloc.get("to_fees"))))

    try:
        _admin().table("loans").update({
            "outstanding_principal": float(new_principal),
            "outstanding_interest":  float(new_interest),
            "outstanding_penalties": float(new_penalties),
            "outstanding_fees":      float(new_fees),
            "collected_principal":   float(col_principal),
            "collected_interest":    float(col_interest),
            "collected_penalties":   float(col_penalties),
            "collected_fees":        float(col_fees),
            "status":                "active",
            "closed_at":             None,
            "updated_at":            _now_iso(),
        }).eq("id", loan["id"]).execute()

        _admin().table("repayments").update({
            "status":          "reversed",
            "reversed_at":     _now_iso(),
            "reversed_by":     _user_id(),
            "reversal_reason": reason,
            "updated_at":      _now_iso(),
        }).eq("id", r["id"]).execute()

        _admin().table("members").update({
            "loan_balance": float(new_principal),
            "updated_at":   _now_iso(),
        }).eq("id", loan["member_id"]).execute()
    except Exception as e:
        log.exception("reverse update failed")
        return _bad(f"Could not reverse: {e}", 500, reason="db_error")

    return jsonify({"ok": True, "message": "Repayment reversed",
                    "loan_status": "active"})


# ---------------------------------------------------------------------------
# API: accrue penalties for a loan
# ---------------------------------------------------------------------------
@repayments_bp.route("/api/loans/<uuid:loan_uuid>/accrue-penalties", methods=["POST"])
@role_required("admin")
def api_accrue_penalties(loan_uuid):
    org_id = _org_id()

    try:
        loan_rows = (
            _admin().table("loans")
            .select("*, loan_applications!inner(product_snapshot)")
            .eq("org_id", org_id).eq("id", str(loan_uuid)).limit(1).execute()
        ).data
    except Exception:
        log.exception("penalty fetch failed")
        return _bad("Could not load loan", 500)
    if not loan_rows:
        return _bad("Loan not found", 404, reason="not_found")
    loan = loan_rows[0]

    if loan["status"] not in ("active", "overdue"):
        return _bad(f"Loan is {loan['status']}", 400, reason="bad_state")

    snap = (loan.get("loan_applications") or {}).get("product_snapshot") or {}
    rate_pct = _to_decimal(snap.get("penalty_rate_percent"))
    grace    = int(snap.get("penalty_grace_days") or 0)

    if rate_pct <= 0:
        return jsonify({"ok": True, "accrued": 0,
                        "reason": "No penalty rate configured for this product."})

    last_pay = (
        _admin().table("repayments")
        .select("paid_at")
        .eq("org_id", org_id).eq("loan_id", loan["id"])
        .eq("status", "completed")
        .order("paid_at", desc=True).limit(1).execute()
    ).data
    last_paid = _parse_date(last_pay[0]["paid_at"]) if last_pay else None
    start = last_paid or _parse_date(loan.get("disbursed_at"))

    today = _today()
    days_since = (today - start).days if start else 0
    days_overdue = max(0, days_since - grace)

    if days_overdue <= 0:
        return jsonify({"ok": True, "accrued": 0, "days_overdue": 0})

    outstanding_total = (
        _to_decimal(loan["outstanding_principal"]) +
        _to_decimal(loan["outstanding_interest"])
    )
    if outstanding_total <= 0:
        return jsonify({"ok": True, "accrued": 0})

    penalty = _q(outstanding_total * rate_pct / Decimal("100") * Decimal(days_overdue))

    existing = (
        _admin().table("loan_penalties")
        .select("id")
        .eq("org_id", org_id).eq("loan_id", loan["id"])
        .eq("accrual_date", today.isoformat())
        .limit(1).execute()
    ).data
    if existing:
        return jsonify({"ok": True, "accrued": 0, "message": "Already accrued today"})

    try:
        _admin().table("loan_penalties").insert({
            "org_id":       org_id,
            "loan_id":      loan["id"],
            "member_id":    loan["member_id"],
            "accrual_date": today.isoformat(),
            "days_overdue": days_overdue,
            "base_amount":  float(_q(outstanding_total)),
            "rate_percent": float(rate_pct),
            "amount":       float(penalty),
            "status":       "charged",
        }).execute()

        _admin().table("loans").update({
            "outstanding_penalties": float(_q(
                _to_decimal(loan["outstanding_penalties"]) + penalty
            )),
            "status":     "overdue",
            "updated_at": _now_iso(),
        }).eq("id", loan["id"]).execute()
    except Exception as e:
        log.exception("penalty insert failed")
        return _bad(f"Could not accrue penalty: {e}", 500, reason="db_error")

    return jsonify({"ok": True, "accrued": float(penalty),
                    "days_overdue": days_overdue})


# ---------------------------------------------------------------------------
# API: member statement CSV
# ---------------------------------------------------------------------------
@repayments_bp.route("/api/statement/<uuid:member_uuid>", methods=["GET"])
@role_required("admin")
def api_member_statement(member_uuid):
    org_id = _org_id()

    try:
        m_resp = (
            _admin().table("members")
            .select("member_id, first_name, last_name, phone, email")
            .eq("org_id", org_id).eq("id", str(member_uuid)).limit(1).execute()
        )
    except Exception:
        log.exception("statement member fetch failed")
        return _bad("Could not load member", 500)
    if not m_resp.data:
        return _bad("Member not found", 404)
    m = m_resp.data[0]

    try:
        loans = (
            _admin().table("loans")
            .select("id, loan_number, principal_amount, total_payable, "
                    "outstanding_principal, outstanding_interest, "
                    "outstanding_penalties, collected_principal, "
                    "collected_interest, collected_penalties, collected_fees, "
                    "currency, disbursed_at, status")
            .eq("org_id", org_id).eq("member_id", str(member_uuid))
            .order("disbursed_at", asc=True).execute()
        ).data or []

        reps = (
            _admin().table("repayments")
            .select("receipt_number, amount, currency, method, provider, "
                    "status, paid_at, loan_id, "
                    "repayment_allocations(to_interest, to_principal, "
                    "to_penalties, to_fees, to_suspense)")
            .eq("org_id", org_id).eq("member_id", str(member_uuid))
            .order("paid_at", asc=True).execute()
        ).data or []
    except Exception as e:
        log.exception("statement rows failed")
        return _bad("Could not load statement data", 500)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["SACCO Pro — Member Loan Statement"])
    w.writerow(["Member", f"{m['first_name']} {m['last_name']}"])
    w.writerow(["Member ID", m["member_id"]])
    w.writerow(["Phone", m.get("phone") or ""])
    w.writerow(["Generated", _now_iso()])
    w.writerow([])

    w.writerow(["LOANS"])
    w.writerow([
        "Loan", "Principal", "Total Payable", "Outstanding Principal",
        "Outstanding Interest", "Outstanding Penalties",
        "Collected Principal", "Collected Interest",
        "Collected Penalties", "Collected Fees",
        "Currency", "Disbursed", "Status",
    ])
    for l in loans:
        w.writerow([
            l["loan_number"], l["principal_amount"], l["total_payable"],
            l["outstanding_principal"], l["outstanding_interest"],
            l["outstanding_penalties"],
            l["collected_principal"], l["collected_interest"],
            l["collected_penalties"], l["collected_fees"],
            l["currency"], l["disbursed_at"], l["status"],
        ])

    w.writerow([])
    w.writerow(["REPAYMENTS"])
    w.writerow([
        "Receipt", "Date", "Amount", "Currency", "Method", "Provider",
        "Status", "To Interest", "To Principal", "To Penalties",
        "To Fees", "Suspense",
    ])
    for r in reps:
        a = (r.get("repayment_allocations") or [{}])[0]
        w.writerow([
            r["receipt_number"], r.get("paid_at") or "",
            r["amount"], r["currency"], r["method"], r.get("provider") or "",
            r["status"],
            a.get("to_interest", 0), a.get("to_principal", 0),
            a.get("to_penalties", 0), a.get("to_fees", 0),
            a.get("to_suspense", 0),
        ])

    filename = f"loan_statement_{m['member_id']}.csv"
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/csv; charset=utf-8",
        },
    )
    
def _q(v) -> Decimal:
    """Quantize any numeric-ish value to 2 decimal places as Decimal."""
    if isinstance(v, Decimal):
        d = v
    elif v is None:
        d = Decimal("0")
    elif isinstance(v, int):
        d = Decimal(v)
    elif isinstance(v, float):
        d = Decimal(str(v))
    else:
        d = Decimal(str(v))
    return d.quantize(MONEY_Q, rounding=ROUND_HALF_UP)

