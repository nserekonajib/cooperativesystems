"""
Member Auth blueprint.

Members log in with their Member ID (or phone) + password.
The default password set at creation is '1235678'.

Endpoints:
    GET    /member/login                                 -> page
    POST   /member/api/login                             -> sign in
    POST   /member/api/logout                            -> sign out
    GET    /member/api/me                                -> current member
    GET    /member/dashboard                             -> member dashboard page
    GET    /member/                                      -> redirect helper

Sessions:
- Admin sessions live under session["user"].
- Member sessions live under session["member"].
- Both can coexist without clobbering each other.
"""

import os
import logging
from datetime import datetime, timezone

from flask import (
    Blueprint, render_template, request, jsonify, session,
    redirect, url_for,
)
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] member_auth: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

member_auth_bp = Blueprint("member_auth", __name__, url_prefix="/member")

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


def _slugify(name: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "org"


def _find_member(identifier: str) -> dict | None:
    """
    Resolve a member by member_id OR phone (whichever matches first).
    Returns the member row augmented with the org slug and org name.
    """
    admin = _admin()
    identifier = (identifier or "").strip()
    if not identifier:
        return None

    try:
        # try member_id first (exact match)
        rows = (
            admin.table("members")
            .select(
                "id, member_id, first_name, last_name, phone, email, status, "
                "user_id, org_id, "
                "organisations!inner(id, name, slug)"
            )
            .eq("member_id", identifier)
            .limit(1)
            .execute()
        ).data
        if rows:
            return rows[0]

        # fall back to phone
        rows = (
            admin.table("members")
            .select(
                "id, member_id, first_name, last_name, phone, email, status, "
                "user_id, org_id, "
                "organisations!inner(id, name, slug)"
            )
            .eq("phone", identifier)
            .limit(1)
            .execute()
        ).data
        if rows:
            return rows[0]
    except Exception:
        log.exception("member lookup failed for %s", identifier)
        return None

    return None


def _auth_email(member: dict) -> str:
    """
    Members log in with the auth user created when they were registered:
        <member_id>@<org_slug>.sacco
    """
    org = member.get("organisations") or {}
    slug = org.get("slug") or _slugify(org.get("name") or "org")
    return f"{member['member_id']}@{slug}.sacco"


# ---------------------------------------------------------------------------
# PAGES
# ---------------------------------------------------------------------------
@member_auth_bp.route("/login", methods=["GET"])
def login_page():
    if session.get("member"):
        return redirect(url_for("member_auth.dashboard"))
    return render_template("memberAuth/memberLogin.html")


@member_auth_bp.route("/", methods=["GET"])
def index():
    if session.get("member"):
        return redirect(url_for("member_auth.dashboard"))
    return redirect(url_for("member_auth.login_page"))


@member_auth_bp.route("/dashboard", methods=["GET"])
def dashboard():
    if not session.get("member"):
        return redirect(url_for("member_auth.login_page"))
    return render_template("memberAuth/memberDashboard.html")


# ---------------------------------------------------------------------------
# API: LOGIN
# ---------------------------------------------------------------------------
@member_auth_bp.route("/api/login", methods=["POST"])
def api_login():
    """
    Body: { identifier, password, remember? }
    identifier = member_id OR phone
    """
    data = request.get_json(silent=True) or {}
    identifier = (data.get("identifier") or "").strip()
    password   = data.get("password") or ""

    if not identifier or not password:
        return _bad("Member ID and password are required", reason="missing_fields")

    member = _find_member(identifier)
    if not member:
        return _bad("Member not found. Check your Member ID.", 404,
                    reason="not_found")

    if member.get("status") != "active":
        return _bad(
            f"Your membership is {member.get('status')}. Please contact the SACCO.",
            403, reason="inactive",
        )

    if not member.get("user_id"):
        return _bad("This member does not have a login yet. Contact the SACCO.",
                    403, reason="no_login")

    auth_email = _auth_email(member)

    # sign in with Supabase GoTrue
    try:
        resp = _anon().auth.sign_in_with_password({
            "email": auth_email,
            "password": password,
        })
    except Exception as e:
        msg = str(e).lower()
        if "invalid" in msg or "credentials" in msg or "email" in msg:
            return _bad("Invalid password. Try again.", 401, reason="bad_password")
        log.exception("member login failed for %s", identifier)
        return _bad("Login failed. Try again.", 500, reason="auth_error")

    if not resp.user or not resp.session:
        return _bad("Invalid password. Try again.", 401, reason="bad_password")

    org = member.get("organisations") or {}
    meta = resp.user.user_metadata or {}

    session["member"] = {
        "id":           member["id"],
        "member_id":    member["member_id"],
        "user_id":      member["user_id"],
        "org_id":       member["org_id"],
        "org_name":     org.get("name"),
        "org_slug":     org.get("slug"),
        "first_name":   member["first_name"],
        "last_name":    member["last_name"],
        "full_name":    f"{member['first_name']} {member['last_name']}".strip(),
        "email":        member.get("email"),
        "phone":        member.get("phone"),
        "must_change_password": bool(meta.get("must_change_password")),
        "logged_in_at": datetime.now(timezone.utc).isoformat(),
    }
    session.permanent = bool(data.get("remember"))

    log.info("member %s logged in (org=%s)", member["member_id"], member["org_id"])

    return jsonify({
        "ok": True,
        "message": "Login successful",
        "redirect": url_for("member_auth.dashboard"),
        "member": {
            "member_id":  member["member_id"],
            "full_name":  session["member"]["full_name"],
            "org_name":   org.get("name"),
        },
    })


# ---------------------------------------------------------------------------
# API: LOGOUT
# ---------------------------------------------------------------------------
@member_auth_bp.route("/api/logout", methods=["POST"])
def api_logout():
    try:
        _anon().auth.sign_out()
    except Exception:
        pass
    session.pop("member", None)
    return jsonify({"ok": True, "redirect": url_for("member_auth.login_page")})


# ---------------------------------------------------------------------------
# API: ME
# ---------------------------------------------------------------------------
@member_auth_bp.route("/api/me", methods=["GET"])
def api_me():
    m = session.get("member")
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")
    return jsonify({"ok": True, "member": m})


# ---------------------------------------------------------------------------
# API: DASHBOARD DATA (lightweight summary for the welcome page)
# ---------------------------------------------------------------------------
@member_auth_bp.route("/api/dashboard", methods=["GET"])
def api_dashboard():
    """
    Returns a snapshot the member dashboard shows on first load:
    savings balance, shares, subscriptions, active loans, dividends.
    All from tables already populated by the admin modules.
    """
    m = session.get("member")
    if not m:
        return _bad("Not authenticated", 401, reason="unauthenticated")

    org_id   = m["org_id"]
    member_id = m["id"]
    admin = _admin()

    try:
        # member row (shares + balances)
        mem = (
            admin.table("members")
            .select("shares_units, shares_value, savings_balance, loan_balance")
            .eq("org_id", org_id).eq("id", member_id).limit(1).execute()
        ).data or [{}]
        mem = mem[0]

        # savings account number
        sav = (
            admin.table("member_accounts")
            .select("account_number, balance, currency")
            .eq("org_id", org_id).eq("member_id", member_id)
            .eq("account_type", "savings").limit(1).execute()
        ).data or [{}]
        sav = sav[0]

        # active loans count + outstanding
        loans = (
            admin.table("loans")
            .select("outstanding_principal, outstanding_interest, status")
            .eq("org_id", org_id).eq("member_id", member_id)
            .in_("status", ["active", "overdue"]).execute()
        ).data or []

        # subscriptions (current year)
        year = datetime.now(timezone.utc).year
        sub = (
            admin.table("yearly_subscriptions")
            .select("amount_due, amount_paid, status, subscription_year")
            .eq("org_id", org_id).eq("member_id", member_id)
            .eq("subscription_year", year).limit(1).execute()
        ).data or [{}]
        sub = sub[0]

        # dividends received (posted) in current year
        divs = (
            admin.table("dividend_payments")
            .select("dividend_amount, status")
            .eq("org_id", org_id).eq("member_id", member_id)
            .eq("status", "posted").execute()
        ).data or []

    except Exception as e:
        log.exception("dashboard data failed")
        return _bad(f"Could not load dashboard: {e}", 500, reason="db_error")

    return jsonify({
        "ok": True,
        "member": {
            "member_id":       m["member_id"],
            "full_name":       m["full_name"],
            "org_name":        m["org_name"],
            "must_change_password": m.get("must_change_password", False),
        },
        "kpi": {
            "savings_balance":   float(mem.get("savings_balance") or 0),
            "savings_account":   sav.get("account_number"),
            "shares_units":      float(mem.get("shares_units") or 0),
            "shares_value":      float(mem.get("shares_value") or 0),
            "loan_balance":      float(mem.get("loan_balance") or 0),
            "active_loans":      len(loans),
            "subscription_year": year,
            "subscription_due":  float(sub.get("amount_due") or 0),
            "subscription_paid": float(sub.get("amount_paid") or 0),
            "subscription_status": sub.get("status"),
            "dividends_total":   sum(float(d.get("dividend_amount") or 0) for d in divs),
        },
    })