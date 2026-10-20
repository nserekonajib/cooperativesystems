"""
SMS Settings blueprint.

Admin endpoints:
    GET    /sms-settings/                       -> page
    GET    /sms-settings/api/settings           -> get current settings
    PATCH  /sms-settings/api/settings           -> update settings
    POST   /sms-settings/api/test               -> send test SMS to a number
    GET    /sms-settings/api/log                -> recent SMS log (paginated)

Design:
- One row per org in `sms_settings`.
- Toggle `enabled` to globally disable all outbound SMS.
- The API key stored here is what `routes.sms.send_sms()` uses.
- `test` sends a real SMS (only if enabled and key present).
"""

import os
import logging
from datetime import datetime, timezone

from flask import (
    Blueprint, render_template, request, jsonify, session, redirect, url_for,
)
from dotenv import load_dotenv
from supabase import create_client, Client

from routes.auth.auth import role_required
from routes.sms.sms import send_sms, get_sms_settings

load_dotenv()

log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] sms_settings: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

sms_settings_bp = Blueprint("sms_settings", __name__, url_prefix="/sms-settings")

_supabase_admin: Client | None = None


def _admin() -> Client:
    global _supabase_admin
    if _supabase_admin is None:
        _supabase_admin = create_client(
            os.getenv("SUPABASE_URL"),
            os.getenv("SUPABASE_SERVICE_KEY"),
        )
    return _supabase_admin


def _bad(msg: str, code: int = 400, reason: str | None = None):
    log.warning("REJECT [%s] %s (reason=%s)", code, msg, reason)
    return jsonify({"ok": False, "error": msg, "reason": reason}), code


def _org_id() -> str:
    return session["user"]["org_id"]


def _user_id() -> str:
    return session["user"]["id"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_row(org_id: str) -> bool:
    try:
        rows = (
            _admin().table("sms_settings")
            .select("org_id").eq("org_id", org_id).limit(1).execute()
        ).data
        if rows:
            return True
        _admin().table("sms_settings").insert({"org_id": org_id}).execute()
        return True
    except Exception:
        log.exception("ensure sms settings row failed")
        return False


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
@sms_settings_bp.route("/", methods=["GET"])
def page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    if session["user"].get("role") != "admin":
        return redirect(url_for("auth.dashboard"))
    _ensure_row(_org_id())
    return render_template("smsSettings/smsSettings.html")


# ---------------------------------------------------------------------------
# API: get settings
# ---------------------------------------------------------------------------
@sms_settings_bp.route("/api/settings", methods=["GET"])
@role_required("admin")
def api_get():
    _ensure_row(_org_id())
    s = get_sms_settings(_org_id())
    # Never expose the API key in full; mask it for display.
    masked = None
    if s.get("api_key"):
        k = s["api_key"]
        masked = (k[:4] + "…" + k[-4:]) if len(k) > 8 else "****"
    return jsonify({
        "ok": True,
        "settings": {
            "enabled":             bool(s.get("enabled")),
            "provider":            s.get("provider") or "yoola",
            "api_key_masked":      masked,
            "api_key_present":     bool(s.get("api_key")),
            "base_url":            s.get("base_url") or "https://yoolasms.com/api/v1",
            "sender_id":           s.get("sender_id"),
            "notify_deposit":      bool(s.get("notify_deposit")),
            "notify_withdrawal":   bool(s.get("notify_withdrawal")),
            "notify_repayment":    bool(s.get("notify_repayment")),
            "notify_dividend":     bool(s.get("notify_dividend")),
            "notify_subscription": bool(s.get("notify_subscription")),
            "updated_at":          s.get("updated_at"),
        },
    })


# ---------------------------------------------------------------------------
# API: update
# ---------------------------------------------------------------------------
BOOL_FIELDS = {
    "enabled", "notify_deposit", "notify_withdrawal", "notify_repayment",
    "notify_dividend", "notify_subscription",
}
STR_FIELDS = {"provider", "sender_id", "base_url"}
SECRET_FIELDS = {"api_key"}


@sms_settings_bp.route("/api/settings", methods=["PATCH"])
@role_required("admin")
def api_update():
    org_id = _org_id()
    _ensure_row(org_id)

    data = request.get_json(silent=True) or {}
    upd = {}

    for k in BOOL_FIELDS:
        if k in data:
            upd[k] = bool(data[k])

    for k in STR_FIELDS:
        if k in data:
            v = data[k]
            if v is None:
                upd[k] = None
            else:
                upd[k] = str(v).strip() or None

    # API key is only written when explicitly supplied and non-empty.
    # Send `null` to clear it (e.g. disable integration while keeping the toggle on).
    if "api_key" in data:
        v = (data.get("api_key") or "").strip()
        upd["api_key"] = v if v else None

    if "provider" in upd and upd["provider"] not in ("yoola",):
        return _bad("Unsupported provider. Only 'yoola' is implemented.",
                    reason="bad_provider")

    if not upd:
        return _bad("No fields to update", reason="empty")

    upd["updated_at"] = _now_iso()
    upd["updated_by"] = _user_id()

    try:
        resp = (
            _admin().table("sms_settings")
            .update(upd).eq("org_id", org_id).execute()
        )
    except Exception as e:
        log.exception("sms settings update failed")
        return _bad(f"Could not save settings: {e}", 500, reason="db_error")

    # Re-read to include the masked key
    s = get_sms_settings(org_id)
    return jsonify({
        "ok": True,
        "settings": {
            "enabled":             bool(s.get("enabled")),
            "provider":            s.get("provider"),
            "api_key_masked":      ("****" if s.get("api_key") else None),
            "api_key_present":     bool(s.get("api_key")),
            "base_url":            s.get("base_url"),
            "sender_id":           s.get("sender_id"),
            "notify_deposit":      bool(s.get("notify_deposit")),
            "notify_withdrawal":   bool(s.get("notify_withdrawal")),
            "notify_repayment":    bool(s.get("notify_repayment")),
            "notify_dividend":     bool(s.get("notify_dividend")),
            "notify_subscription": bool(s.get("notify_subscription")),
            "updated_at":          s.get("updated_at"),
        },
    })


# ---------------------------------------------------------------------------
# API: send test SMS
# ---------------------------------------------------------------------------
@sms_settings_bp.route("/api/test", methods=["POST"])
@role_required("admin")
def api_test():
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    if not phone:
        return _bad("Phone number is required", reason="missing_phone")

    result = send_sms(
        org_id=_org_id(),
        phone=phone,
        message="SACCO Pro test SMS — if you received this, SMS is configured correctly.",
        triggered_by=_user_id(),
        meta={"kind": "test"},
    )
    if result.get("sent"):
        return jsonify({"ok": True, "message": "Test SMS sent successfully."})
    if result.get("status") == "skipped":
        return _bad(f"SMS not sent: {result.get('reason')}", reason="skipped")
    return _bad(f"SMS failed: {result.get('error')}", 502, reason="provider_error")


# ---------------------------------------------------------------------------
# API: log
# ---------------------------------------------------------------------------
@sms_settings_bp.route("/api/log", methods=["GET"])
@role_required("admin")
def api_log():
    org_id = _org_id()
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
    except ValueError:
        return _bad("Invalid pagination")

    start = (page - 1) * per_page
    end   = start + per_page - 1

    try:
        resp = (
            _admin().table("sms_log")
            .select("id, member_id, phone, message, status, provider, "
                    "provider_ref, error, triggered_by, created_at",
                    count="exact")
            .eq("org_id", org_id)
            .order("created_at", desc=True)
            .range(start, end).execute()
        )
    except Exception as e:
        log.exception("sms log fetch failed")
        return _bad(f"Could not load SMS log: {e}", 500, reason="db_error")

    return jsonify({
        "ok": True,
        "log": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })