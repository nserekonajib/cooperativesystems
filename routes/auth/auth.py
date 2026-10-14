"""
Auth blueprint: registration, login, logout, session, dashboard guard.

Design notes:
- We DO NOT use Supabase's built-in signup. We use the Admin API
  (service role) to create users so we control metadata + roles.
- Organisation details are stored in `organisations` table (one row per org).
- The user->org link + role is stored in `user_organisations` table.
  This is batch-friendly (one insert covers org + link).
- Every API endpoint returns JSON. Templates never do full reloads.
- Role decorator: @role_required("admin", "member") ...
- No N+1: we join via PostgREST selects.
"""

import os
import re
import functools
from datetime import datetime, timezone

from flask import (
    Blueprint, request, jsonify, session,
    render_template, redirect, url_for, current_app,
)
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

# ---------------------------------------------------------------------------
# Supabase clients
# ---------------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

_supabase_anon: Client | None = None
_supabase_admin: Client | None = None


def supabase_anon() -> Client:
    """Public client — used for login (sign_in_with_password)."""
    global _supabase_anon
    if _supabase_anon is None:
        _supabase_anon = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    return _supabase_anon


def supabase_admin() -> Client:
    """Service-role client — used for admin user creation & DB writes."""
    global _supabase_admin
    if _supabase_admin is None:
        _supabase_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _supabase_admin


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9\s\-()]{7,20}$")

VALID_ROLES = {"admin", "member"}  # extend later


def _bad(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


def _validate_register(data: dict):
    required = ["email", "password", "full_name", "org_name", "phone"]
    missing = [f for f in required if not (data.get(f) or "").strip()]
    if missing:
        return f"Missing fields: {', '.join(missing)}"

    if not EMAIL_RE.match(data["email"]):
        return "Invalid email address"
    if not PHONE_RE.match(data["phone"]):
        return "Invalid phone number"
    if len(data["password"]) < 8:
        return "Password must be at least 8 characters"
    if len(data["org_name"]) < 2:
        return "Organisation name too short"
    return None


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "org"


# ---------------------------------------------------------------------------
# Role decorator
# ---------------------------------------------------------------------------
def role_required(*allowed_roles: str):
    """
    Usage:
        @role_required("admin")
        @role_required("admin", "member")
    Reads role from session (set at login). No DB round-trip per request.
    """
    allowed = set(allowed_roles) & VALID_ROLES

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            user = session.get("user")
            if not user:
                return jsonify({"ok": False, "error": "Unauthenticated"}), 401
            if user.get("role") not in allowed:
                return jsonify({"ok": False, "error": "Forbidden"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@auth_bp.route("/", methods=["GET"])
def auth_page():
    if session.get("user"):
        return redirect(url_for("auth.dashboard"))
    return render_template("auth/auth.html")


@auth_bp.route("/dashboard", methods=["GET"])
def dashboard():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    return render_template("auth/dashboard.html")


# ---------------------------------------------------------------------------
# API: Register
# ---------------------------------------------------------------------------
@auth_bp.route("/api/register", methods=["POST"])
def api_register():
    """
    Creates:
      1. Supabase auth user (admin API, email_confirm=True to skip verification)
      2. organisations row
      3. user_organisations link with role='admin'

    Batched: only 3 Supabase calls, no loops, no N+1.
    """
    data = request.get_json(silent=True) or {}
    err = _validate_register(data)
    if err:
        return _bad(err)

    email = data["email"].strip().lower()
    password = data["password"]
    full_name = data["full_name"].strip()
    phone = data["phone"].strip()
    org_name = data["org_name"].strip()
    org_slug = _slugify(org_name)

    admin = supabase_admin()

    # 1. Create the auth user (admin API)
    try:
        created = admin.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True,
            "user_metadata": {
                "full_name": full_name,
                "phone": phone,
            },
        })
        auth_user = created.user
        user_id = auth_user.id
    except Exception as e:
        msg = str(e)
        if "already" in msg.lower() or "registered" in msg.lower():
            return _bad("An account with this email already exists", 409)
        current_app.logger.exception("Supabase create_user failed")
        return _bad("Could not create account. Try again.", 500)

    # 2. Insert organisation + 3. user link.
    #    We do org insert first (need org.id for the link).
    try:
        org_resp = (
            admin.table("organisations")
            .insert({
                "name": org_name,
                "slug": org_slug,
                "owner_id": user_id,
                "email": email,
                "phone": phone,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            .execute()
        )
        if not org_resp.data:
            raise RuntimeError("org insert returned no data")
        org_id = org_resp.data[0]["id"]

        admin.table("user_organisations").insert({
            "user_id": user_id,
            "org_id": org_id,
            "role": "admin",        # default role
            "status": "active",
        }).execute()
    except Exception:
        current_app.logger.exception("Org / link insert failed")
        # Roll back the auth user so email isn't orphaned
        try:
            admin.auth.admin.delete_user(user_id)
        except Exception:
            current_app.logger.exception("Rollback delete_user failed")
        return _bad("Could not create organisation. Try again.", 500)

    return jsonify({
        "ok": True,
        "message": "Account created. You can now log in.",
        "user": {
            "id": user_id,
            "email": email,
            "full_name": full_name,
            "role": "admin",
            "org_id": org_id,
            "org_name": org_name,
        },
    }), 201


# ---------------------------------------------------------------------------
# API: Login
# ---------------------------------------------------------------------------
@auth_bp.route("/api/login", methods=["POST"])
def api_login():
    """
    Signs in via Supabase GoTrue, then loads role + org in ONE query
    (PostgREST nested select — no N+1).
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return _bad("Email and password are required")

    try:
        auth_resp = supabase_anon().auth.sign_in_with_password({
            "email": email,
            "password": password,
        })
    except Exception as e:
        msg = str(e).lower()
        if "invalid" in msg or "credentials" in msg:
            return _bad("Invalid email or password", 401)
        current_app.logger.exception("Supabase login failed")
        return _bad("Login failed. Try again.", 500)

    if not auth_resp.user or not auth_resp.session:
        return _bad("Invalid email or password", 401)

    user_id = auth_resp.user.id

    # Single joined query: user_organisations -> organisations
    link_resp = (
        supabase_admin()
        .table("user_organisations")
        .select("role, status, org_id, organisations(id, name, slug)")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    link = (link_resp.data or [None])[0]
    if not link or link.get("status") != "active":
        return _bad("No active organisation linked to this account", 403)

    org = link.get("organisations") or {}

    session["user"] = {
        "id": user_id,
        "email": auth_resp.user.email,
        "full_name": (auth_resp.user.user_metadata or {}).get("full_name", ""),
        "role": link["role"],
        "org_id": org.get("id"),
        "org_name": org.get("name"),
        "access_token": auth_resp.session.access_token,
    }
    session.permanent = bool(data.get("remember"))

    return jsonify({
        "ok": True,
        "message": "Login successful",
        "redirect": url_for("auth.dashboard"),
        "user": {
            "id": user_id,
            "email": auth_resp.user.email,
            "full_name": session["user"]["full_name"],
            "role": link["role"],
            "org_id": org.get("id"),
            "org_name": org.get("name"),
        },
    })


# ---------------------------------------------------------------------------
# API: Logout
# ---------------------------------------------------------------------------
@auth_bp.route("/api/logout", methods=["POST"])
def api_logout():
    try:
        supabase_anon().auth.sign_out()
    except Exception:
        pass
    session.clear()
    return jsonify({"ok": True, "redirect": url_for("auth.auth_page")})


# ---------------------------------------------------------------------------
# API: Current user
# ---------------------------------------------------------------------------
@auth_bp.route("/api/me", methods=["GET"])
@role_required("admin", "member")
def api_me():
    return jsonify({"ok": True, "user": session["user"]})


# ---------------------------------------------------------------------------
# Example: admin-only endpoint
# ---------------------------------------------------------------------------
@auth_bp.route("/api/admin/overview", methods=["GET"])
@role_required("admin")
def api_admin_overview():
    org_id = session["user"]["org_id"]
    # One query, single round trip
    resp = (
        supabase_admin()
        .table("user_organisations")
        .select("role, status, user_id", count="exact")
        .eq("org_id", org_id)
        .execute()
    )
    return jsonify({
        "ok": True,
        "org_id": org_id,
        "member_count": resp.count,
        "members": resp.data,
    })