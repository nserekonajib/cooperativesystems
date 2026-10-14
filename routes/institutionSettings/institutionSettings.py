"""
Institution Settings blueprint.

Endpoints (all admin-only, JSON):
    GET    /institution-settings/                -> renders page
    GET    /institution-settings/api/profile     -> institution profile
    PATCH  /institution-settings/api/profile     -> update profile fields
    GET    /institution-settings/api/financial   -> min shares / fees
    PATCH  /institution-settings/api/financial   -> update financial rules

Design:
- One row per org (org_id PK), auto-created by a Postgres trigger.
- Single round-trip per endpoint. No N+1.
- All exceptions from the Supabase SDK are logged and returned as JSON.
"""

import os
import re
import logging
from datetime import datetime, timezone

from flask import (
    Blueprint, render_template, request, jsonify, session, redirect, url_for,
)
from dotenv import load_dotenv
from supabase import create_client, Client

from routes.auth.auth import role_required

load_dotenv()

log = logging.getLogger(__name__)

institution_settings_bp = Blueprint(
    "institution_settings",
    __name__,
    url_prefix="/institution-settings",
)

_supabase_admin: Client | None = None

PROFILE_COLUMNS = (
    "display_name, registration_number, tax_id, sector, "
    "founded_year, website, support_email, support_phone, "
    "address_line, city, region, country, postal_code, "
    "logo_url, currency, timezone, about, updated_at"
)

FINANCIAL_COLUMNS = (
    "min_shares, min_share_price, share_round_up_to, "
    "annual_subscription_fee, subscription_due_month, "
    "sacco_fee_type, sacco_fee_value, sacco_fee_applies_to, "
    "updated_at"
)


def _admin() -> Client:
    global _supabase_admin
    if _supabase_admin is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY missing from .env")
        _supabase_admin = create_client(url, key)
    return _supabase_admin


# ---------------------------------------------------------------------------
# Field whitelists
# ---------------------------------------------------------------------------
PROFILE_FIELDS = {
    "display_name", "registration_number", "tax_id", "sector",
    "founded_year", "website", "support_email", "support_phone",
    "address_line", "city", "region", "country", "postal_code",
    "logo_url", "currency", "timezone", "about",
}

FINANCIAL_FIELDS = {
    "min_shares", "min_share_price", "share_round_up_to",
    "annual_subscription_fee", "subscription_due_month",
    "sacco_fee_type", "sacco_fee_value", "sacco_fee_applies_to",
}

NUMERIC_FIELDS = {
    "min_shares", "min_share_price", "share_round_up_to",
    "annual_subscription_fee", "sacco_fee_value",
}

INT_FIELDS = {"founded_year", "subscription_due_month"}

ENUM_FIELDS = {
    "sacco_fee_type":       {"percentage", "fixed"},
    "sacco_fee_applies_to": {"loan", "savings", "both"},
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
URL_RE   = re.compile(r"^https?://[^\s]+$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _bad(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


def _clean(fields: dict, whitelist: set) -> dict:
    """Keep only whitelisted fields, coerce types. Blank string -> None (clears)."""
    out = {}
    for k, v in fields.items():
        if k not in whitelist:
            continue
        if v is None or v == "":
            out[k] = None
            continue

        if k in NUMERIC_FIELDS:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number")
        elif k in INT_FIELDS:
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be an integer")
        elif k in ENUM_FIELDS:
            if v not in ENUM_FIELDS[k]:
                raise ValueError(f"{k} must be one of {sorted(ENUM_FIELDS[k])}")
            out[k] = v
        else:
            out[k] = str(v).strip()
    return out


def _validate_profile(data: dict):
    if data.get("founded_year") is not None:
        y = data["founded_year"]
        if y < 1900 or y > datetime.now().year:
            return "founded_year out of range"
    if data.get("support_email") and not EMAIL_RE.match(data["support_email"]):
        return "Invalid support_email"
    if data.get("website") and not URL_RE.match(data["website"]):
        return "Website must start with http:// or https://"
    return None


def _validate_financial(data: dict):
    if data.get("subscription_due_month") is not None:
        m = data["subscription_due_month"]
        if not (1 <= m <= 12):
            return "subscription_due_month must be 1..12"
    for f in ("min_shares", "min_share_price", "share_round_up_to",
              "annual_subscription_fee", "sacco_fee_value"):
        if data.get(f) is not None and data[f] < 0:
            return f"{f} cannot be negative"
    if data.get("sacco_fee_type") == "percentage":
        v = data.get("sacco_fee_value")
        if v is not None and v > 100:
            return "sacco_fee_value cannot exceed 100 for percentage"
    return None


def _org_id() -> str:
    return session["user"]["org_id"]


def _ensure_row(org_id: str) -> bool:
    """Insert a row if missing (trigger may not be installed)."""
    try:
        check = (
            _admin().table("institution_settings")
            .select("org_id").eq("org_id", org_id).limit(1).execute()
        )
        if check.data:
            return True
        _admin().table("institution_settings").insert({
            "org_id": org_id,
            "display_name": session["user"].get("org_name"),
        }).execute()
        return True
    except Exception:
        log.exception("ensure_row failed for org %s", org_id)
        return False


def _do_update(table: str, payload: dict, org_id: str):
    """
    Update + fetch updated row in one round trip.
    NOTE: in supabase-py 2.x, .update().eq().execute() already returns
    the updated rows in .data — do NOT chain .select() after .update().
    """
    try:
        resp = (
            _admin()
            .table(table)
            .update(payload)
            .eq("org_id", org_id)
            .execute()
        )
    except Exception as e:
        log.exception("Supabase update failed on %s for org %s: %s", table, org_id, e)
        msg = getattr(e, "message", None) or str(e)
        return False, f"Database error: {msg}", None

    if not resp.data:
        log.warning("Update returned no row for org %s on %s", org_id, table)
        return False, "No row updated. Ensure your organisation settings row exists.", None

    return True, "Saved", resp.data[0]


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
@institution_settings_bp.route("/", methods=["GET"])
def page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    if session["user"].get("role") != "admin":
        return redirect(url_for("auth.dashboard"))
    _ensure_row(_org_id())
    return render_template("institutionSettings/institutionSettings.html")


# ---------------------------------------------------------------------------
# API: Profile
# ---------------------------------------------------------------------------
@institution_settings_bp.route("/api/profile", methods=["GET"])
@role_required("admin")
def api_get_profile():
    org_id = _org_id()
    _ensure_row(org_id)
    try:
        resp = (
            _admin().table("institution_settings")
            .select(PROFILE_COLUMNS)
            .eq("org_id", org_id).limit(1).execute()
        )
    except Exception as e:
        log.exception("api_get_profile failed: %s", e)
        return _bad("Could not load profile", 500)
    return jsonify({"ok": True, "profile": (resp.data or [{}])[0]})


@institution_settings_bp.route("/api/profile", methods=["PATCH"])
@role_required("admin")
def api_update_profile():
    org_id = _org_id()
    if not _ensure_row(org_id):
        return _bad("Institution settings row missing and could not be created", 500)

    payload = request.get_json(silent=True) or {}
    log.info("PATCH profile org=%s keys=%s", org_id, list(payload.keys()))

    try:
        clean = _clean(payload, PROFILE_FIELDS)
    except ValueError as e:
        return _bad(str(e))

    if not clean:
        return _bad("No valid fields to update")

    err = _validate_profile(clean)
    if err:
        return _bad(err)

    clean["updated_at"] = datetime.now(timezone.utc).isoformat()

    ok, msg, row = _do_update("institution_settings", clean, org_id)
    if not ok:
        return _bad(msg, 500)
    return jsonify({"ok": True, "message": "Profile updated", "profile": row})


# ---------------------------------------------------------------------------
# API: Financial rules
# ---------------------------------------------------------------------------
@institution_settings_bp.route("/api/financial", methods=["GET"])
@role_required("admin")
def api_get_financial():
    org_id = _org_id()
    _ensure_row(org_id)
    try:
        resp = (
            _admin().table("institution_settings")
            .select(FINANCIAL_COLUMNS)
            .eq("org_id", org_id).limit(1).execute()
        )
    except Exception as e:
        log.exception("api_get_financial failed: %s", e)
        return _bad("Could not load financial settings", 500)
    return jsonify({"ok": True, "financial": (resp.data or [{}])[0]})


@institution_settings_bp.route("/api/financial", methods=["PATCH"])
@role_required("admin")
def api_update_financial():
    org_id = _org_id()
    if not _ensure_row(org_id):
        return _bad("Institution settings row missing and could not be created", 500)

    payload = request.get_json(silent=True) or {}
    log.info("PATCH financial org=%s keys=%s", org_id, list(payload.keys()))

    try:
        clean = _clean(payload, FINANCIAL_FIELDS)
    except ValueError as e:
        return _bad(str(e))

    if not clean:
        return _bad("No valid fields to update")

    err = _validate_financial(clean)
    if err:
        return _bad(err)

    clean["updated_at"] = datetime.now(timezone.utc).isoformat()

    ok, msg, row = _do_update("institution_settings", clean, org_id)
    if not ok:
        return _bad(msg, 500)
    return jsonify({"ok": True, "message": "Financial settings updated", "financial": row})