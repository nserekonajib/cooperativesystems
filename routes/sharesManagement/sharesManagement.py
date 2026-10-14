"""
Shares Management blueprint.

Endpoints (admin-only JSON unless noted):
    GET    /shares/                                        -> page
    GET    /shares/api/stats                               -> KPI cards
    GET    /shares/api/list                                -> paginated list of members + shares
    GET    /shares/api/members/search                      -> lookup for create modal
    GET    /shares/api/<uuid:member_uuid>                  -> member detail with share history
    GET    /shares/api/<uuid:member_uuid>/transactions     -> paginated share txns
    GET    /shares/api/transactions                        -> global share tx feed
    GET    /shares/api/transactions/<uuid:tx_uuid>         -> single txn + receipt
    GET    /shares/api/transactions/<uuid:tx_uuid>/receipt -> printable HTML receipt
    GET    /shares/api/settings                            -> current share price / min shares
    GET    /shares/api/export                              -> CSV of member shareholdings

    POST   /shares/api/<uuid:member_uuid>/buy              -> cash: apply now;
                                                              online: pawaPay + stage
    POST   /shares/api/payment/<ref>/verify                -> verify online share purchase
    POST   /shares/api/transactions/<uuid:tx_uuid>/reverse -> reverse a completed purchase

Design notes:
- Shares are EQUITY to the SACCO, not revenue. That's why they have their own
  ledger table (share_transactions) rather than living under loans or savings.
- price per share comes from institution_settings.min_share_price (the current
  share price). When buying, the server snapshots the price so historical
  purchases stay accurate if the price changes later.
- Cash purchases apply immediately; online uses the same pawaPay-first,
  stage-JSON, verify-on-callback flow as the rest of the app.
- Every purchase updates:
    members.shares_units
    members.shares_value
    share_transactions (state)
    member_transactions (general ledger mirror)
"""

import os
import io
import csv
import json
import secrets
import logging
from pathlib import Path
from datetime import datetime, timezone
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
        "%(asctime)s [%(levelname)s] shares: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

shares_bp = Blueprint("shares", __name__, url_prefix="/shares")

PAWAPAY_MESSAGE = "SACCO shares"
MONEY_Q  = Decimal("0.01")
UNITS_Q  = Decimal("0.0001")
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


def _units(v) -> Decimal:
    d = v if isinstance(v, Decimal) else Decimal(str(v if v is not None else 0))
    return d.quantize(UNITS_Q, rounding=ROUND_HALF_UP)


def _normalize_phone(p: str) -> str:
    digits = "".join(c for c in (p or "") if c.isdigit())
    if digits.startswith("0"):
        digits = "256" + digits[1:]
    elif not digits.startswith("256") and len(digits) == 9:
        digits = "256" + digits
    return digits


def _next_txn_number(org_id: str) -> str:
    try:
        resp = _admin().rpc("next_share_txn_number", {"p_org": org_id}).execute()
    except Exception as e:
        log.exception("next_share_txn_number failed")
        raise RuntimeError(f"Could not allocate share txn number: {e}")
    raw = resp.data
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, dict):
        raw = next(iter(raw.values()))
    return f"SH-{int(raw):06d}"


# ---------------------------------------------------------------------------
# Pending online staging
# ---------------------------------------------------------------------------
def _pending_path(deposit_id: str) -> Path:
    p = Path(current_app.instance_path) / "pending_shares"
    p.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (deposit_id or "") if c.isalnum() or c in "-_")
    return p / f"{safe}.json"


def _save_pending(deposit_id: str, payload: dict) -> None:
    path = _pending_path(deposit_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    tmp.replace(path)
    log.info("pending share purchase saved: %s", path)


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
@shares_bp.route("/", methods=["GET"])
def page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    if session["user"].get("role") != "admin":
        return redirect(url_for("auth.dashboard"))
    return render_template("sharesManagement/sharesManagement.html")


# ---------------------------------------------------------------------------
# SETTINGS (share price, minimum shares)
# ---------------------------------------------------------------------------
@shares_bp.route("/api/settings", methods=["GET"])
@role_required("admin")
def api_settings():
    org_id = _org_id()
    try:
        s = (
            _admin().table("institution_settings")
            .select("min_shares, min_share_price, currency")
            .eq("org_id", org_id).limit(1).execute()
        ).data
    except Exception as e:
        log.exception("settings fetch failed: %s", e)
        return _bad("Could not load settings", 500)

    s = (s or [{}])[0]
    min_units = _to_decimal(s.get("min_shares"))
    price     = _to_decimal(s.get("min_share_price"))
    return jsonify({
        "ok": True,
        "settings": {
            "min_shares":      float(_units(min_units)),
            "share_price":     float(_money(price)),
            "currency":        s.get("currency") or "UGX",
        },
    })


# ---------------------------------------------------------------------------
# STATS
# ---------------------------------------------------------------------------
@shares_bp.route("/api/stats", methods=["GET"])
@role_required("admin")
def api_stats():
    org_id = _org_id()
    try:
        s = (
            _admin().table("institution_settings")
            .select("min_share_price, min_shares, currency")
            .eq("org_id", org_id).limit(1).execute()
        ).data or [{}]
        price     = _to_decimal(s[0].get("min_share_price"))
        min_units = _to_decimal(s[0].get("min_shares"))
        currency  = s[0].get("currency") or "UGX"

        members = (
            _admin().table("members")
            .select("id, shares_units, shares_value, status")
            .eq("org_id", org_id)
            .execute()
        ).data or []

        total_units  = ZERO
        total_value  = ZERO
        above_min    = 0
        below_min    = 0
        active_hold  = 0

        for m in members:
            u = _to_decimal(m.get("shares_units"))
            total_units += u
            total_value += _money(u * price)
            if u >= min_units and min_units > 0:
                above_min += 1
            elif min_units > 0:
                below_min += 1
            if m.get("status") == "active" and u > 0:
                active_hold += 1

        # purchases in last 30 days (units + count)
        since = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=30)).isoformat()
        recent = (
            _admin().table("share_transactions")
            .select("units, amount, currency")
            .eq("org_id", org_id).eq("status", "completed")
            .eq("type", "purchase")
            .gte("created_at", since)
            .execute()
        ).data or []
        units_30d  = sum((_to_decimal(r.get("units")) for r in recent), ZERO)
        amount_30d = sum((_to_decimal(r.get("amount")) for r in recent), ZERO)

    except Exception as e:
        log.exception("shares stats failed: %s", e)
        return _bad("Could not load stats", 500)

    return jsonify({
        "ok": True,
        "stats": {
            "currency":            currency,
            "share_price":         float(_money(price)),
            "total_units":         float(_units(total_units)),
            "total_value":         float(_money(total_value)),
            "active_holders":      active_hold,
            "above_minimum":       above_min,
            "below_minimum":       below_min,
            "members_count":       len(members),
            "purchased_units_30d": float(_units(units_30d)),
            "purchased_value_30d": float(_money(amount_30d)),
        },
    })


# ---------------------------------------------------------------------------
# LIST (member shareholdings)
# ---------------------------------------------------------------------------
@shares_bp.route("/api/list", methods=["GET"])
@role_required("admin")
def api_list():
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
        _admin().table("members")
        .select(
            "id, member_id, first_name, last_name, phone, email, status, "
            "shares_units, shares_value, joined_at",
            count="exact",
        )
        .eq("org_id", org_id)
        .order("shares_units", desc=True)
        .range(start, end)
    )
    if status:
        q = q.eq("status", status)
    if search:
        like = f"%{search}%"
        q = q.or_(
            f"first_name.ilike.{like},"
            f"last_name.ilike.{like},"
            f"member_id.ilike.{like},"
            f"phone.ilike.{like}"
        )

    try:
        resp = q.execute()
    except Exception as e:
        log.exception("shares list failed: %s", e)
        return _bad("Could not load members", 500)

    return jsonify({
        "ok": True,
        "members": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# MEMBER SEARCH (for buy modal)
# ---------------------------------------------------------------------------
@shares_bp.route("/api/members/search", methods=["GET"])
@role_required("admin")
def api_members_search():
    org_id = _org_id()
    q_str = (request.args.get("q") or "").strip()

    q = (
        _admin().table("members")
        .select("id, member_id, first_name, last_name, phone, "
                "shares_units, shares_value, status")
        .eq("org_id", org_id)
        .eq("status", "active")
        .limit(50)
    )
    if q_str:
        like = f"%{q_str}%"
        q = q.or_(
            f"first_name.ilike.{like},"
            f"last_name.ilike.{like},"
            f"member_id.ilike.{like},"
            f"phone.ilike.{like}"
        )
    try:
        members = q.execute().data or []
    except Exception as e:
        log.exception("member search failed: %s", e)
        return _bad("Could not search members", 500)
    return jsonify({"ok": True, "members": members})


# ---------------------------------------------------------------------------
# MEMBER DETAIL
# ---------------------------------------------------------------------------
@shares_bp.route("/api/<uuid:member_uuid>", methods=["GET"])
@role_required("admin")
def api_member_detail(member_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("members")
            .select("*")
            .eq("org_id", org_id).eq("id", str(member_uuid)).limit(1).execute()
        )
    except Exception as e:
        log.exception("member detail failed: %s", e)
        return _bad("Could not load member", 500)
    if not resp.data:
        return _bad("Member not found", 404)

    m = resp.data[0]

    # recent transactions
    try:
        txns = (
            _admin().table("share_transactions")
            .select("*")
            .eq("org_id", org_id).eq("member_id", str(member_uuid))
            .order("created_at", desc=True)
            .limit(50).execute()
        ).data or []
    except Exception:
        log.exception("share txns fetch failed (non-fatal)")
        txns = []

    return jsonify({"ok": True, "member": m, "transactions": txns})


@shares_bp.route("/api/<uuid:member_uuid>/transactions", methods=["GET"])
@role_required("admin")
def api_member_transactions(member_uuid):
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
            _admin().table("share_transactions")
            .select("*", count="exact")
            .eq("org_id", org_id).eq("member_id", str(member_uuid))
            .order("created_at", desc=True)
            .range(start, end).execute()
        )
    except Exception as e:
        log.exception("share txns list failed: %s", e)
        return _bad("Could not load transactions", 500)

    return jsonify({
        "ok": True,
        "transactions": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# GLOBAL TRANSACTIONS FEED
# ---------------------------------------------------------------------------
@shares_bp.route("/api/transactions", methods=["GET"])
@role_required("admin")
def api_transactions():
    org_id = _org_id()
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
        status   = (request.args.get("status") or "").strip()
        typ      = (request.args.get("type") or "").strip()
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    q = (
        _admin().table("share_transactions")
        .select(
            "id, txn_number, type, units, unit_price, amount, currency, "
            "method, provider, status, paid_at, created_at, narration, "
            "units_before, units_after, value_after, "
            "members!inner(id, member_id, first_name, last_name, phone)",
            count="exact",
        )
        .eq("org_id", org_id)
        .order("created_at", desc=True)
        .range(start, end)
    )
    if status:
        q = q.eq("status", status)
    if typ:
        q = q.eq("type", typ)

    try:
        resp = q.execute()
    except Exception as e:
        log.exception("share txns feed failed: %s", e)
        return _bad("Could not load transactions", 500)

    return jsonify({
        "ok": True,
        "transactions": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


@shares_bp.route("/api/transactions/<uuid:tx_uuid>", methods=["GET"])
@role_required("admin")
def api_transaction_detail(tx_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("share_transactions")
            .select("*, members!inner(id, member_id, first_name, last_name, "
                    "phone, email)")
            .eq("org_id", org_id).eq("id", str(tx_uuid)).limit(1).execute()
        )
    except Exception as e:
        log.exception("txn detail failed: %s", e)
        return _bad("Could not load transaction", 500)
    if not resp.data:
        return _bad("Transaction not found", 404)
    return jsonify({"ok": True, "transaction": resp.data[0]})


# ---------------------------------------------------------------------------
# RECEIPT — printable HTML
# ---------------------------------------------------------------------------
def _render_receipt(tx: dict, settings: dict) -> str:
    m = tx["members"]
    display = settings.get("display_name") or "SACCO Pro"
    addr = " ".join(filter(None, [settings.get("address_line") or "",
                                   settings.get("city") or "",
                                   settings.get("country") or ""]))
    contact = " · ".join(filter(None, [settings.get("support_phone") or "",
                                        settings.get("support_email") or ""]))

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Share Receipt {tx['txn_number']}</title>
<style>
 body{{font-family:Inter,system-ui,sans-serif;padding:40px;color:#1E293B;background:#F4F6F9;}}
 .card{{max-width:640px;margin:auto;background:#fff;border:1px solid #E2E8F0;border-radius:16px;padding:32px;}}
 h1{{font-size:20px;margin:0 0 4px;}} h2{{font-size:15px;margin:0;}}
 h3{{font-size:13px;color:#64748B;text-transform:uppercase;letter-spacing:.06em;margin:24px 0 6px;}}
 .sub{{color:#64748B;font-size:12px;margin:0 0 24px;}}
 .row{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px dashed #E2E8F0;font-size:14px;}}
 .row:last-child{{border-bottom:none;}}
 .label{{color:#64748B;}} .total{{font-weight:800;font-size:18px;color:#1A62FF;}}
 .tag{{display:inline-block;background:#E6F4EA;color:#065F46;padding:4px 10px;border-radius:999px;font-size:11px;font-weight:600;}}
 .foot{{text-align:center;color:#94A3B8;font-size:11px;margin-top:24px;}}
 @media print{{body{{padding:0;background:#fff;}} .card{{border:none;}}}}
</style></head><body>
<div class="card">
  <h1>{display}</h1>
  <p class="sub">{addr}<br>{contact}</p>
  <span class="tag">{tx['status'].upper()}</span>
  <h2 style="margin-top:16px;">Share Purchase Receipt</h2>
  <p class="sub">Receipt <strong>{tx['txn_number']}</strong> · {tx.get('paid_at') or tx.get('created_at')}</p>

  <div class="row"><span class="label">Member</span><span>{m['first_name']} {m['last_name']}</span></div>
  <div class="row"><span class="label">Member ID</span><span>{m['member_id']}</span></div>
  <div class="row"><span class="label">Method</span><span>{tx.get('method') or '—'}{' · '+tx['provider'] if tx.get('provider') else ''}</span></div>
  <div class="row"><span class="label">Units purchased</span><span>{tx['units']}</span></div>
  <div class="row"><span class="label">Price per unit</span><span>{tx['unit_price']} {tx['currency']}</span></div>
  <div class="row"><span class="label">Amount</span><span class="total">{tx['amount']} {tx['currency']}</span></div>

  <h3>Shareholding</h3>
  <div class="row"><span class="label">Units before</span><span>{tx['units_before']}</span></div>
  <div class="row"><span class="label">Units after</span><span>{tx['units_after']}</span></div>
  <div class="row"><span class="label">Total share value</span><span>{tx['value_after']} {tx['currency']}</span></div>

  <p class="foot">Generated by {display} · Thank you for your investment.</p>
</div>
</body></html>"""


@shares_bp.route("/api/transactions/<uuid:tx_uuid>/receipt", methods=["GET"])
@role_required("admin")
def api_receipt(tx_uuid):
    org_id = _org_id()
    try:
        tx = (
            _admin().table("share_transactions")
            .select("*, members!inner(member_id, first_name, last_name, phone, email)")
            .eq("org_id", org_id).eq("id", str(tx_uuid)).limit(1).execute()
        ).data
    except Exception:
        log.exception("receipt fetch failed")
        return _bad("Could not load transaction", 500)
    if not tx:
        return _bad("Transaction not found", 404)
    tx = tx[0]

    try:
        s = (
            _admin().table("institution_settings")
            .select("display_name, address_line, city, country, "
                    "support_phone, support_email")
            .eq("org_id", org_id).limit(1).execute()
        ).data
    except Exception:
        s = []
    return Response(_render_receipt(tx, (s or [{}])[0]), mimetype="text/html")


# ---------------------------------------------------------------------------
# CORE: apply a share purchase
# ---------------------------------------------------------------------------
def _apply_share_purchase(org_id: str, member_id: str, spec: dict) -> dict:
    """
    spec: units, unit_price?, method, provider?, pay_phone?, deposit_id?,
          narration?, created_by?

    Posts to the chart of accounts on completion:
        cash   -> debit 1000 Cash on hand,   credit 3000 Share capital
        online -> debit 1010 Bank account,   credit 3000 Share capital
    """
    admin = _admin()

    units = _units(spec["units"])
    if units <= 0:
        raise ValueError("units must be > 0")

    # 1. Load member (no currency here — it lives on institution_settings)
    member_resp = (
        admin.table("members")
        .select("id, member_id, first_name, last_name, shares_units, "
                "shares_value, status")
        .eq("org_id", org_id).eq("id", member_id).limit(1).execute()
    )
    if not member_resp.data:
        raise RuntimeError("Member not found")
    member = member_resp.data[0]
    if member.get("status") != "active":
        raise RuntimeError(f"Member is {member.get('status')}")

    # 2. Settings: currency + default share price
    try:
        settings_rows = (
            admin.table("institution_settings")
            .select("min_share_price, currency")
            .eq("org_id", org_id).limit(1).execute()
        ).data or [{}]
    except Exception:
        log.exception("settings fetch failed")
        raise RuntimeError("Could not read institution settings")
    settings = settings_rows[0]

    # currency comes from settings; if spec overrode it, respect that
    currency = spec.get("currency") or settings.get("currency") or "UGX"

    # 3. Price resolution: spec override -> settings default
    if spec.get("unit_price") not in (None, ""):
        unit_price = _money(spec["unit_price"])
    else:
        unit_price = _money(settings.get("min_share_price"))
    if unit_price <= 0:
        raise RuntimeError("Share price is not configured")

    amount = _money(units * unit_price)

    units_before = _units(member.get("shares_units"))
    units_after  = _units(units_before + units)
    value_after  = _money(units_after * unit_price)

    txn_number = _next_txn_number(org_id)

    # 4. Share ledger row
    st = {
        "org_id":       org_id,
        "member_id":    member_id,
        "txn_number":   txn_number,
        "type":         "purchase",
        "units":        float(units),
        "unit_price":   float(unit_price),
        "amount":       float(amount),
        "currency":     currency,
        "method":       spec["method"],
        "status":       "completed",
        "units_before": float(units_before),
        "units_after":  float(units_after),
        "value_after":  float(value_after),
        "narration":    spec.get("narration"),
        "paid_at":      _now_iso(),
        "created_by":   spec.get("created_by") or _user_id(),
    }
    if spec.get("provider"):
        st["provider"] = spec["provider"]
    if spec.get("deposit_id"):
        st["provider_ref"] = spec["deposit_id"]
    if spec.get("pay_phone"):
        st["phone"] = spec["pay_phone"]

    st_resp = admin.table("share_transactions").insert(st).execute()
    if not st_resp.data:
        raise RuntimeError("share ledger insert failed")
    st_row = st_resp.data[0]

    # 5. Update member shareholding
    admin.table("members").update({
        "shares_units": float(units_after),
        "shares_value": float(value_after),
        "updated_at":   _now_iso(),
    }).eq("id", member_id).execute()

    # 6. Mirror into the general ledger (member_transactions)
    try:
        admin.table("member_transactions").insert({
            "org_id":       org_id,
            "member_id":    member_id,
            "type":         "share_purchase",
            "method":       spec["method"],
            "amount":       float(amount),
            "currency":     currency,
            "status":       "completed",
            "narration":    spec.get("narration") or f"Share purchase · {txn_number}",
            "provider":     spec.get("provider"),
            "provider_ref": spec.get("deposit_id"),
            "phone":        spec.get("pay_phone"),
            "created_by":   spec.get("created_by") or _user_id(),
            "share_txn_id": st_row["id"],
        }).execute()
    except Exception:
        log.exception("member_transactions mirror failed (non-fatal)")

    # 7. Post to the chart of accounts (equity — not income)
    #    cash   -> Dr 1000 Cash on hand,   Cr 3000 Share capital
    #    online -> Dr 1010 Bank account,   Cr 3000 Share capital
    try:
        from routes.accounts.accounts import record_asset_movement

        method_key = "cash" if spec["method"] == "cash" else "online"

        record_asset_movement(
            org_id=org_id,
            direction="in",
            amount=amount,
            method=method_key,
            counterpart_account_code="3000",   # Share capital — members
            category="other_income",           # tag only; account category drives P&L
            narration=f"Share purchase · {txn_number} · {units} units",
            reference=txn_number,
            created_by=spec.get("created_by") or _user_id(),
            meta={
                "source":         "shares",
                "share_txn_id":   st_row["id"],
                "member_id":      member_id,
                "units":          float(units),
                "unit_price":     float(unit_price),
            },
        )
    except Exception:
        log.exception("chart-of-accounts post for share purchase failed (non-fatal)")

    return {
        "id":           st_row["id"],
        "txn_number":   txn_number,
        "units":        float(units),
        "unit_price":   float(unit_price),
        "amount":       float(amount),
        "currency":     currency,
        "units_before": float(units_before),
        "units_after":  float(units_after),
        "value_after":  float(value_after),
    }


# ---------------------------------------------------------------------------
# API: BUY
# ---------------------------------------------------------------------------
@shares_bp.route("/api/<uuid:member_uuid>/buy", methods=["POST"])
@role_required("admin")
def api_buy(member_uuid):
    """
    Body:
    {
      units,                      -- number of units
      unit_price?,                -- optional override; defaults to settings
      method: 'cash'|'online',
      provider?, phone?,          -- required for online
      narration?
    }
    """
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}

    try:
        units = _units(data.get("units"))
        if units <= 0:
            return _bad("units must be > 0", reason="bad_units")
    except ValueError:
        return _bad("Invalid units", reason="bad_units")

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
            return _bad("phone required for online", reason="missing_phone")
        pay_phone = _normalize_phone(pay_phone)

    # unit price
    unit_price_raw = data.get("unit_price")
    try:
        if unit_price_raw not in (None, ""):
            unit_price = _money(unit_price_raw)
        else:
            s = (
                _admin().table("institution_settings")
                .select("min_share_price")
                .eq("org_id", org_id).limit(1).execute()
            ).data or [{}]
            unit_price = _money(s[0].get("min_share_price"))
    except ValueError:
        return _bad("Invalid unit_price", reason="bad_price")
    if unit_price <= 0:
        return _bad("Share price is not configured", reason="no_price")

    amount = _money(units * unit_price)

    # confirm member
    try:
        m = (
            _admin().table("members")
            .select("id, status")
            .eq("org_id", org_id).eq("id", str(member_uuid)).limit(1).execute()
        ).data
    except Exception:
        log.exception("member fetch failed")
        return _bad("Could not load member", 500, reason="db_error")
    if not m:
        return _bad("Member not found", 404, reason="not_found")
    if m[0]["status"] != "active":
        return _bad(f"Member is {m[0]['status']}", 400, reason="bad_state")

    # ---- CASH ----
    if method == "cash":
        try:
            result = _apply_share_purchase(org_id, str(member_uuid), {
                "units":      str(units),
                "unit_price": str(unit_price),
                "method":     "cash",
                "narration":  (data.get("narration") or "").strip() or None,
                "created_by": _user_id(),
            })
        except Exception as e:
            log.exception("cash share purchase failed")
            return _bad(f"Could not apply purchase: {e}", 500, reason="apply_failed")
        return jsonify({"ok": True, "purchase": result}), 201

    # ---- ONLINE ----
    try:
        if provider == "MTN":
            pay_resp = deposit_mtn_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"shr-{secrets.token_hex(6)}",
                customer_message=PAWAPAY_MESSAGE,
            )
        else:
            pay_resp = deposit_airtel_ug(
                phone_number=pay_phone,
                amount=str(int(amount)),
                client_reference_id=f"shr-{secrets.token_hex(6)}",
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
        "action":     "share_purchase",
        "org_id":     org_id,
        "member_id":  str(member_uuid),
        "units":      str(units),
        "unit_price": str(unit_price),
        "amount":     str(amount),
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
            "units":      float(units),
            "currency":   "UGX",
        },
    }), 202


# ---------------------------------------------------------------------------
# VERIFY (pawaPay)
# ---------------------------------------------------------------------------
@shares_bp.route("/api/payment/<deposit_id>/verify", methods=["POST"])
@role_required("admin")
def api_verify(deposit_id):
    org_id = _org_id()
    staged = _load_pending(deposit_id)
    if not staged:
        return _bad("No pending share purchase for this reference",
                    404, reason="not_found")

    try:
        result = verify_deposit(deposit_id)
    except Exception as e:
        log.exception("verify failed")
        return _bad(f"Verification error: {e}", 502, reason="provider_error")

    status = result.get("status")

    if result.get("completed"):
        try:
            applied = _apply_share_purchase(org_id, staged["member_id"], {
                "units":      staged["units"],
                "unit_price": staged["unit_price"],
                "method":     "online",
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
                        "provider_status": status, "purchase": applied})

    if result.get("failed"):
        _delete_pending(deposit_id)
        return jsonify({"ok": True, "status": "failed",
                        "provider_status": status,
                        "reason": result.get("failure_reason")})

    return jsonify({"ok": True, "status": "pending", "provider_status": status})


# ---------------------------------------------------------------------------
# REVERSE
# ---------------------------------------------------------------------------
@shares_bp.route("/api/transactions/<uuid:tx_uuid>/reverse", methods=["POST"])
@role_required("admin")
def api_reverse(tx_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip() or None
    if not reason:
        return _bad("reason is required", reason="missing_reason")

    try:
        tx = (
            _admin().table("share_transactions")
            .select("*")
            .eq("org_id", org_id).eq("id", str(tx_uuid)).limit(1).execute()
        ).data
    except Exception:
        log.exception("reverse fetch failed")
        return _bad("Could not load transaction", 500)
    if not tx:
        return _bad("Transaction not found", 404, reason="not_found")
    tx = tx[0]

    if tx["status"] != "completed":
        return _bad(f"Cannot reverse a transaction in status '{tx['status']}'",
                    400, reason="bad_state")
    if tx["type"] != "purchase":
        return _bad("Only purchases can be reversed here", 400, reason="bad_type")

    units = _units(tx["units"])

    # member's current shares
    try:
        m = (
            _admin().table("members")
            .select("shares_units, shares_value")
            .eq("org_id", org_id).eq("id", tx["member_id"]).limit(1).execute()
        ).data
    except Exception:
        log.exception("member fetch failed")
        return _bad("Could not load member", 500, reason="db_error")
    if not m:
        return _bad("Member not found", 404, reason="not_found")
    m = m[0]

    current_units = _units(m.get("shares_units"))
    if current_units < units:
        return _bad(
            f"Cannot reverse: member now holds {current_units} units "
            f"but this txn added {units}",
            400, reason="insufficient_units",
        )

    price = _money(tx["unit_price"])
    new_units = _units(current_units - units)
    new_value = _money(new_units * price)

    try:
        _admin().table("members").update({
            "shares_units": float(new_units),
            "shares_value": float(new_value),
            "updated_at":   _now_iso(),
        }).eq("id", tx["member_id"]).execute()

        _admin().table("share_transactions").update({
            "status":          "reversed",
            "reversed_at":     _now_iso(),
            "reversed_by":     _user_id(),
            "reversal_reason": reason,
            "updated_at":      _now_iso(),
        }).eq("id", tx["id"]).execute()
    except Exception as e:
        log.exception("reverse update failed")
        return _bad(f"Could not reverse: {e}", 500, reason="db_error")

    return jsonify({"ok": True, "message": "Purchase reversed",
                    "units_after": float(new_units),
                    "value_after": float(new_value)})


# ---------------------------------------------------------------------------
# EXPORT CSV
# ---------------------------------------------------------------------------
@shares_bp.route("/api/export", methods=["GET"])
@role_required("admin")
def api_export():
    org_id = _org_id()
    try:
        rows = (
            _admin().table("members")
            .select("member_id, first_name, last_name, phone, status, "
                    "shares_units, shares_value, joined_at")
            .eq("org_id", org_id)
            .order("shares_units", desc=True)
            .execute()
        ).data or []
    except Exception as e:
        log.exception("export fetch failed")
        return _bad("Could not export", 500)

    try:
        s = (
            _admin().table("institution_settings")
            .select("min_share_price, currency")
            .eq("org_id", org_id).limit(1).execute()
        ).data or [{}]
        currency = s[0].get("currency") or "UGX"
    except Exception:
        currency = "UGX"

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["SACCO Pro — Shareholdings"])
    w.writerow(["Generated", _now_iso()])
    w.writerow([])
    w.writerow(["Member ID", "Member", "Phone", "Status",
                "Units", "Share Value", "Currency", "Joined"])
    for r in rows:
        w.writerow([
            r.get("member_id"),
            f"{r.get('first_name','')} {r.get('last_name','')}".strip(),
            r.get("phone"),
            r.get("status"),
            r.get("shares_units"),
            r.get("shares_value"),
            currency,
            r.get("joined_at"),
        ])

    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="shareholdings.csv"',
            "Content-Type": "text/csv; charset=utf-8",
        },
    )