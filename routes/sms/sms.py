"""
SMS helper — shared by all modules.

Usage from any module:

    from routes.sms import send_sms

    send_sms(
        org_id=org_id,
        phone="+256700000000",          # any format accepted
        message="Your savings deposit of 50,000 was received.",
        member_id=member_id,            # optional, for the audit log
        triggered_by=user_id,           # optional
        meta={"kind": "deposit"},
    )

Behaviour:
- Phone numbers are normalized to 256XXXXXXXXX (no '+', no leading '0').
- Messages are clamped to <= 160 characters.
- If SMS is disabled for the org, returns {sent: False, status: "skipped"}
  without making an HTTP request.
- Otherwise, calls Yoola SMS with the org's stored API key.
- Every attempt (sent / failed / skipped) is written to `sms_log`.
- Never raises — callers should treat SMS as fire-and-forget.
"""

import os
import logging
import requests
from datetime import datetime, timezone
from flask import session

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] sms: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

_supabase_admin: Client | None = None

SMS_MAX_LEN = 160


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
# Phone normalization
# ---------------------------------------------------------------------------
def normalize_phone(p: str | None) -> str:
    """
    Return the phone as bare digits with country code (default 256).

        256775885461    -> 256775885461
        +256775885461   -> 256775885461
        +256 775 885461 -> 256775885461
        0775885461      -> 256775885461
        775885461       -> 256775885461
        25488772878     -> 25488772878    (already has a country code)
    """
    digits = "".join(c for c in (p or "") if c.isdigit())
    if not digits:
        return ""
    if digits.startswith("256"):
        return digits
    if digits.startswith("0"):
        return "256" + digits[1:]
    if len(digits) == 9:
        return "256" + digits
    return digits                       # pass-through with foreign country code


# ---------------------------------------------------------------------------
# Message clamp
# ---------------------------------------------------------------------------
def clamp_sms(text: str, limit: int = SMS_MAX_LEN) -> str:
    """Trim a message to <= limit chars, appending '…' if trimmed."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Settings accessor
# ---------------------------------------------------------------------------
def get_sms_settings(org_id: str) -> dict:
    """Fetch the org's SMS settings row. Returns defaults if missing."""
    try:
        rows = (
            _admin().table("sms_settings")
            .select("*").eq("org_id", org_id).limit(1).execute()
        ).data or []
    except Exception:
        log.exception("sms settings fetch failed")
        rows = []
    if not rows:
        return {
            "org_id":              org_id,
            "enabled":             False,
            "provider":            "yoola",
            "api_key":             None,
            "base_url":            "https://yoolasms.com/api/v1",
            "sender_id":           None,
            "notify_deposit":      True,
            "notify_withdrawal":   True,
            "notify_repayment":    True,
            "notify_dividend":     True,
            "notify_subscription": True,
        }
    return rows[0]


def sms_enabled(org_id: str) -> bool:
    s = get_sms_settings(org_id)
    return bool(s.get("enabled")) and bool(s.get("api_key"))


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
def _log_attempt(org_id, member_id, phone, message, status,
                 provider=None, provider_ref=None, error=None,
                 triggered_by=None, meta=None):
    try:
        _admin().table("sms_log").insert({
            "org_id":       org_id,
            "member_id":    member_id,
            "phone":        phone,
            "message":      message,
            "status":       status,
            "provider":     provider,
            "provider_ref": provider_ref,
            "error":        error,
            "triggered_by": triggered_by,
            "meta":         meta or {},
        }).execute()
    except Exception:
        log.exception("sms_log insert failed (non-fatal)")


def _current_user_id() -> str | None:
    try:
        u = session.get("user") or {}
        return u.get("id")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core send
# ---------------------------------------------------------------------------
def send_sms(org_id: str, phone: str, message: str,
             member_id: str | None = None,
             triggered_by: str | None = None,
             meta: dict | None = None) -> dict:
    """
    Send an SMS if enabled. Never raises.

    Returns:
        {"sent": True,  "status": "sent",    ...}             on success
        {"sent": False, "status": "skipped", "reason": "..."} when disabled
        {"sent": False, "status": "failed",  "error": "..."}  on provider error
    """
    org_id = (org_id or "").strip()
    phone  = normalize_phone(phone)
    msg    = clamp_sms(message)
    user_id = triggered_by or _current_user_id()

    if not org_id or not phone or not msg:
        log.warning("send_sms called with empty args (org=%s phone=%s)", org_id, phone)
        return {"sent": False, "status": "skipped", "reason": "missing args"}

    settings = get_sms_settings(org_id)

    # ---- disabled by admin ----
    if not settings.get("enabled"):
        _log_attempt(org_id, member_id, phone, msg, "skipped",
                     triggered_by=user_id,
                     error="SMS disabled", meta=meta)
        return {"sent": False, "status": "skipped", "reason": "SMS disabled"}

    api_key = settings.get("api_key")
    if not api_key:
        _log_attempt(org_id, member_id, phone, msg, "skipped",
                     triggered_by=user_id,
                     error="API key missing", meta=meta)
        return {"sent": False, "status": "skipped", "reason": "API key missing"}

    base_url = (settings.get("base_url") or "https://yoolasms.com/api/v1").rstrip("/")
    endpoint = f"{base_url}/send"

    payload = {
        "api_key": api_key,
        "phone":   phone,
        "message": msg,
    }
    if settings.get("sender_id"):
        payload["sender_id"] = settings["sender_id"]

    try:
        resp = requests.post(endpoint, json=payload, timeout=15)
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}

        success = (
            200 <= resp.status_code < 300 and
            (not isinstance(data, dict) or data.get("status") != "error")
        )

        if success:
            ref = None
            if isinstance(data, dict):
                inner = data.get("data")
                if isinstance(inner, dict):
                    ref = inner.get("id")
                if not ref:
                    ref = data.get("message_id") or data.get("id")

            _log_attempt(org_id, member_id, phone, msg, "sent",
                         provider="yoola",
                         provider_ref=str(ref) if ref else None,
                         triggered_by=user_id, meta=meta or {})
            log.info("SMS sent to %s", phone)
            return {"sent": True, "status": "sent", "provider_ref": ref, "raw": data}

        # ---- provider error response ----
        if isinstance(data, dict):
            err = data.get("message") or data.get("error") or str(data)
        else:
            err = str(data)

        _log_attempt(org_id, member_id, phone, msg, "failed",
                     provider="yoola", error=str(err)[:500],
                     triggered_by=user_id, meta=meta or {})
        log.warning("SMS failed to %s: %s", phone, err)
        return {"sent": False, "status": "failed", "error": err, "raw": data}

    except requests.RequestException as e:
        _log_attempt(org_id, member_id, phone, msg, "failed",
                     provider="yoola", error=str(e)[:500],
                     triggered_by=user_id, meta=meta or {})
        log.warning("SMS request failed to %s: %s", phone, e)
        return {"sent": False, "status": "failed", "error": str(e)}