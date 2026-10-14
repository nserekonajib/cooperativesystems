"""
Member Profile blueprint.

A logged-in member can view their profile, update self-editable fields,
and change their password.

Endpoints:
    GET    /member/profile                           -> page
    GET    /member/profile/api/me                    -> full profile + accounts
    PATCH  /member/profile/api/me                    -> update editable fields
    POST   /member/profile/api/change-password       -> change password

Design:
- Reads / writes go through the admin Supabase client because we
  mediate the auth layer (member auth users use synthetic emails).
- Only a whitelist of fields is writable.
- Password change requires the current password (verified via sign-in).
- After a successful password change we clear must_change_password
  in user_metadata and in session.
"""

import os
import logging
from datetime import datetime, timezone

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
        "%(asctime)s [%(levelname)s] member_profile: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

member_profile_bp = Blueprint("member_profile", __name__, url_prefix="/member/profile")

_supabase_admin: Client | None = None
_supabase_anon: Client | None = None


def _admin() -> Client:
    global _supabase_admin
    if _supabase_admin is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY missing")
        _supabase_admin = create_client(url, key)
    return _supabase_admin


def _anon() -> Client:
    global _supabase_anon
    if _supabase_anon is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_ANON_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL / SUPABASE_ANON_KEY missing")
        _supabase_anon = create_client(url, key)
    return _supabase_anon


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _bad(msg: str, code: int = 400, reason: str | None = None):
    log.warning("REJECT [%s] %s (reason=%s)", code, msg, reason)
    return jsonify({"ok": False, "error": msg, "reason": reason}), code


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _member_session() -> dict | None:
    return session.get("member")


def _auth_email_for(member: dict) -> str:
    """
    Reconstruct the member's synthetic auth email from the session:
        <member_id>@<org_slug>.sacco
    """
    slug = member.get("org_slug") or "org"
    return f"{member['member_id']}@{slug}.sacco"


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
@member_profile_bp.route("/", methods=["GET"])
def page():
    if not session.get("member"):
        return redirect(url_for("member_auth.login_page"))
    return render_template("memberProfile/memberProfile.html")


# ---------------------------------------------------------------------------
# API: GET profile
# ---------------------------------------------------------------------------
@member_profile_bp.route("/api/me", methods=["GET"])
def api_get_me():
    m = _member_session()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    org_id    = m["org_id"]
    member_pk = m["id"]
    admin     = _admin()

    try:
        # member row
        rows = (
            admin.table("members")
            .select(
                "id, member_id, first_name, last_name, other_names, "
                "gender, date_of_birth, national_id, phone, email, "
                "address_line, city, occupation, member_type, category, "
                "status, joined_at, shares_units, shares_value, "
                "savings_balance, loan_balance"
            )
            .eq("org_id", org_id).eq("id", member_pk).limit(1).execute()
        ).data
        if not rows:
            return _bad("Member not found", 404, reason="not_found")
        member = rows[0]

        # accounts (savings + loan numbers)
        accts = (
            admin.table("member_accounts")
            .select("account_type, account_number, balance, currency, status")
            .eq("org_id", org_id).eq("member_id", member_pk)
            .execute()
        ).data or []

        # active loans count
        loans = (
            admin.table("loans")
            .select("id", count="exact")
            .eq("org_id", org_id).eq("member_id", member_pk)
            .in_("status", ["active", "overdue"])
            .limit(1).execute()
        ).count or 0

    except Exception as e:
        log.exception("profile fetch failed")
        return _bad(f"Could not load profile: {e}", 500, reason="db_error")

    return jsonify({
        "ok": True,
        "member": member,
        "accounts": accts,
        "active_loans": loans,
        "session": {
            "must_change_password": m.get("must_change_password", False),
            "org_name": m.get("org_name"),
            "org_slug": m.get("org_slug"),
        },
    })


# ---------------------------------------------------------------------------
# API: PATCH profile (whitelisted fields only)
# ---------------------------------------------------------------------------
PROFILE_EDITABLE = {
    "phone", "email", "address_line", "city", "occupation",
}

PHONE_RE = None  # compiled lazily


def _validate_email(s: str) -> bool:
    import re
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s or ""))


def _validate_phone(s: str) -> bool:
    import re
    return bool(re.match(r"^\+?[0-9\s\-()]{7,20}$", s or ""))


@member_profile_bp.route("/api/me", methods=["PATCH"])
def api_update_me():
    m = _member_session()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    data = request.get_json(silent=True) or {}

    # Whitelist
    upd = {}
    for k in PROFILE_EDITABLE:
        if k in data:
            v = (data.get(k) or "").strip() if isinstance(data.get(k), str) else data.get(k)
            upd[k] = v or None

    if not upd:
        return _bad("No editable fields provided", 400, reason="empty")

    if "email" in upd and upd["email"] and not _validate_email(upd["email"]):
        return _bad("Invalid email address", reason="bad_email")
    if "phone" in upd and upd["phone"] and not _validate_phone(upd["phone"]):
        return _bad("Invalid phone number", reason="bad_phone")

    upd["updated_at"] = _now_iso()

    try:
        resp = (
            _admin().table("members")
            .update(upd)
            .eq("org_id", m["org_id"]).eq("id", m["id"])
            .execute()
        )
    except Exception as e:
        log.exception("profile update failed")
        return _bad(f"Could not update profile: {e}", 500, reason="db_error")

    if not resp.data:
        return _bad("Member not found", 404, reason="not_found")

    # mirror first/last name + email into session (used by the topbar)
    row = resp.data[0]
    m["phone"] = row.get("phone") or m.get("phone")
    m["email"] = row.get("email") or m.get("email")
    session["member"] = m

    return jsonify({"ok": True, "message": "Profile updated", "member": row})


# ---------------------------------------------------------------------------
# API: change password
# ---------------------------------------------------------------------------
@member_profile_bp.route("/api/change-password", methods=["POST"])
def api_change_password():
    m = _member_session()
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    data = request.get_json(silent=True) or {}
    current = (data.get("current_password") or "").strip()
    new_pw  = data.get("new_password") or ""
    confirm = data.get("confirm_password") or ""

    if not current or not new_pw or not confirm:
        return _bad("All three password fields are required", reason="missing_fields")
    if new_pw != confirm:
        return _bad("New password and confirmation do not match", reason="mismatch")
    if len(new_pw) < 8:
        return _bad("New password must be at least 8 characters", reason="too_short")
    if new_pw == current:
        return _bad("New password must differ from the current password", reason="same_pw")

    auth_email = _auth_email_for(m)

    # 1. Verify current password by signing in
    try:
        resp = _anon().auth.sign_in_with_password({
            "email": auth_email,
            "password": current,
        })
    except Exception:
        return _bad("Current password is incorrect", 401, reason="bad_current")

    if not resp.user or not resp.session:
        return _bad("Current password is incorrect", 401, reason="bad_current")

    # 2. Update via admin API (we control the auth user)
    try:
        _admin().auth.admin.update_user_by_id(m["user_id"], {
            "password": new_pw,
            "user_metadata": {
                **(resp.user.user_metadata or {}),
                "must_change_password": False,
            },
        })
    except Exception as e:
        log.exception("password update failed")
        return _bad(f"Could not change password: {e}", 500, reason="db_error")

    # 3. Update session
    m["must_change_password"] = False
    session["member"] = m

    log.info("member %s changed password", m["member_id"])

    return jsonify({
        "ok": True,
        "message": "Password changed successfully. Please use it next time you sign in.",
    })