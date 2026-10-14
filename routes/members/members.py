"""
Members blueprint.

Endpoints (all admin-only JSON unless noted):
    GET    /members/                          -> page
    GET    /members/api/list                  -> paginated list
    GET    /members/api/settings              -> form defaults
    GET    /members/api/<member_uuid>         -> single member with accounts
    POST   /members/api/create                -> cash: commit now;
                                                 online: call pawaPay, stage to JSON
    POST   /members/api/payment/<ref>/verify  -> poll pawaPay; commit staged member on success

Design:
- 8-digit member_id + 12-digit account numbers generated in Python (secrets)
  with per-org uniqueness checks; DB unique constraints are the final guard.
- Cash: member + auth user + accounts + 2 completed transactions, atomically.
- Online: pawaPay FIRST. If accepted, write full spec to
    instance/pending_members/<deposit_id>.json
  and return pending. NOTHING is inserted into the DB yet.
  On /verify COMPLETED: read JSON, run _commit_member(), delete JSON.
  On /verify FAILED   : delete JSON, nothing inserted.
  On /verify PENDING  : keep JSON, keep polling.
- Auth user email=<member_id>@<org_slug>.sacco, password '1235678'.
- Rollback: if commit fails after auth user creation, delete the user.
"""

import os
import json
import secrets
import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import (
    Blueprint, render_template, request, jsonify, session, redirect, url_for,
    current_app,
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

# Ensure our own logger surfaces to the console even without Flask config.
log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] members: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False

members_bp = Blueprint("members", __name__, url_prefix="/members")

PAWAPAY_MESSAGE = "SACCO payment"

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
# Staging directory for pending online registrations
# ---------------------------------------------------------------------------
def _pending_dir() -> Path:
    p = Path(current_app.instance_path) / "pending_members"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _pending_file(deposit_id: str) -> Path:
    safe = "".join(c for c in (deposit_id or "") if c.isalnum() or c in "-_")
    return _pending_dir() / f"{safe}.json"


def _save_pending(deposit_id: str, payload: dict) -> None:
    path = _pending_file(deposit_id)
    tmp  = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic within the same filesystem
    log.info("pending registration saved: %s", path)


def _load_pending(deposit_id: str) -> dict | None:
    path = _pending_file(deposit_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("failed to read pending file %s", path)
        return None


def _delete_pending(deposit_id: str) -> None:
    path = _pending_file(deposit_id)
    try:
        if path.exists():
            path.unlink()
            log.info("pending file deleted: %s", path)
    except Exception:
        log.exception("failed to delete pending file %s", path)


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


def _org_slug_from_session() -> str:
    name = (session["user"].get("org_name") or "org").lower()
    keep = "".join(c if c.isalnum() else "-" for c in name)
    slug = "-".join(filter(None, keep.split("-")))
    return slug or "org"


def _normalize_phone(p: str) -> str:
    """
    pawaPay wants bare digits: country code + number, no '+', no leading '0'.
        0751994531   -> 256751994531
        256751994531 -> 256751994531
        +256751994531-> 256751994531
        751994531    -> 256751994531
    """
    digits = "".join(c for c in (p or "") if c.isdigit())
    if digits.startswith("0"):
        digits = "256" + digits[1:]
    elif not digits.startswith("256") and len(digits) == 9:
        digits = "256" + digits
    return digits


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------
def _rand_digits(n: int) -> str:
    if not (1 <= n <= 18):
        raise ValueError("n must be 1..18")
    first = str(secrets.randbelow(9) + 1)
    rest  = "".join(str(secrets.randbelow(10)) for _ in range(n - 1))
    return first + rest


def _gen_unique_member_id(org_id: str, max_tries: int = 20) -> str:
    admin = _admin()
    for attempt in range(max_tries):
        candidate = _rand_digits(8)
        try:
            resp = (
                admin.table("members")
                .select("id")
                .eq("org_id", org_id)
                .eq("member_id", candidate)
                .limit(1)
                .execute()
            )
        except Exception as e:
            log.exception("member_id uniqueness check failed")
            raise RuntimeError(f"DB error during member_id check: {e}")
        if not resp.data:
            log.debug("member_id allocated: %s (attempt %d)", candidate, attempt + 1)
            return candidate
    raise RuntimeError("Could not generate unique 8-digit member_id")


def _gen_unique_account_number(
    org_id: str, exclude: set | None = None, max_tries: int = 20
) -> str:
    admin = _admin()
    exclude = exclude or set()
    for attempt in range(max_tries):
        candidate = _rand_digits(12)
        if candidate in exclude:
            continue
        try:
            resp = (
                admin.table("member_accounts")
                .select("id")
                .eq("org_id", org_id)
                .eq("account_number", candidate)
                .limit(1)
                .execute()
            )
        except Exception as e:
            log.exception("account_number uniqueness check failed")
            raise RuntimeError(f"DB error during account_number check: {e}")
        if not resp.data:
            log.debug("account_number allocated: %s (attempt %d)", candidate, attempt + 1)
            return candidate
    raise RuntimeError("Could not generate unique 12-digit account_number")


# ---------------------------------------------------------------------------
# Commit routine — used by cash path AND verify path
# ---------------------------------------------------------------------------
def _commit_member(org_id: str, spec: dict) -> dict:
    """
    Insert auth user + member + accounts + transactions + yearly_subscription,
    and post the initial money movements to the chart of accounts.

    `spec` may contain either Decimal or str numerics (JSON round-trip
    stringifies Decimals). We coerce every numeric field up front so the
    logic is identical for both paths.
    """
    admin = _admin()

    # --- Coerce numerics once ---
    try:
        shares_units     = _to_decimal(spec["shares_units"])
        shares_value     = _to_decimal(spec["shares_value"])
        savings_balance  = _to_decimal(spec["savings_balance"])
        loan_balance     = _to_decimal(spec["loan_balance"])
        due_shares       = _to_decimal(spec["due_shares"])
        due_subscription = _to_decimal(spec["due_subscription"])
        total_due        = _to_decimal(spec["total_due"])
    except (KeyError, ValueError) as e:
        raise RuntimeError(f"Invalid spec numbers: {e}")

    user_id_created = None

    try:
        # --- IDs ---
        member_id = _gen_unique_member_id(org_id)
        acc_sav   = _gen_unique_account_number(org_id)
        acc_loan  = _gen_unique_account_number(org_id, exclude={acc_sav})
        log.info("allocated: member_id=%s sav=%s loan=%s", member_id, acc_sav, acc_loan)

        # --- Auth user ---
        auth_email = f"{member_id}@{_org_slug_from_session()}.sacco"
        try:
            created = admin.auth.admin.create_user({
                "email": auth_email,
                "password": "1235678",
                "email_confirm": True,
                "user_metadata": {
                    "full_name": f"{spec['first']} {spec['last']}",
                    "member_id": member_id,
                    "must_change_password": True,
                },
            })
            user_id_created = created.user.id
            log.info("auth user created: %s (%s)", user_id_created, auth_email)
        except Exception as e:
            log.exception("auth create_user failed")
            raise RuntimeError(f"Could not create login for member: {e}")

        # --- Member ---
        member_row = {
            "org_id":          org_id,
            "user_id":         user_id_created,
            "member_id":       member_id,
            "first_name":      spec["first"],
            "last_name":       spec["last"],
            "other_names":     spec.get("other_names"),
            "gender":          spec.get("gender"),
            "date_of_birth":   spec.get("date_of_birth"),
            "national_id":     spec.get("national_id"),
            "phone":           spec["phone"],
            "email":           spec.get("email"),
            "address_line":    spec.get("address_line"),
            "city":            spec.get("city"),
            "occupation":      spec.get("occupation"),
            "member_type":     spec.get("member_type", "individual"),
            "category":        spec["category"],
            "status":          "active",
            "shares_units":    float(shares_units),
            "shares_value":    float(shares_value),
            "savings_balance": float(savings_balance),
            "loan_balance":    float(loan_balance),
            "notes":           spec.get("notes"),
            "created_by":      spec.get("created_by") or _user_id(),
        }
        try:
            m_resp = admin.table("members").insert(member_row).execute()
        except Exception as e:
            if "23505" in str(e) and "member_id" in str(e):
                member_id = _gen_unique_member_id(org_id)
                member_row["member_id"] = member_id
                m_resp = admin.table("members").insert(member_row).execute()
            else:
                raise

        member_pk = m_resp.data[0]["id"]
        log.info("member inserted: pk=%s member_id=%s", member_pk, member_id)

        # --- Accounts ---
        accounts_payload = [
            {
                "org_id":         org_id,
                "member_id":      member_pk,
                "account_type":   "savings",
                "account_number": acc_sav,
                "currency":       spec["currency"],
                "balance":        float(savings_balance),
            },
            {
                "org_id":         org_id,
                "member_id":      member_pk,
                "account_type":   "loan",
                "account_number": acc_loan,
                "currency":       spec["currency"],
                "balance":        float(loan_balance),
            },
        ]
        try:
            admin.table("member_accounts").insert(accounts_payload).execute()
            log.info("accounts inserted: sav=%s loan=%s", acc_sav, acc_loan)
        except Exception as e:
            if "23505" in str(e) and "account_number" in str(e):
                acc_sav  = _gen_unique_account_number(org_id)
                acc_loan = _gen_unique_account_number(org_id, exclude={acc_sav})
                accounts_payload[0]["account_number"] = acc_sav
                accounts_payload[1]["account_number"] = acc_loan
                admin.table("member_accounts").insert(accounts_payload).execute()
                log.info("accounts re-inserted: sav=%s loan=%s", acc_sav, acc_loan)
            else:
                raise

        # --- Transactions + linked subscription ---
        method     = spec["method"]
        provider   = spec.get("provider")
        pay_phone  = spec.get("pay_phone")
        deposit_id = spec.get("deposit_id")

        tx_common = {
            "org_id":     org_id,
            "member_id":  member_pk,
            "method":     method,
            "currency":   spec["currency"],
            "created_by": spec.get("created_by") or _user_id(),
        }
        if provider:
            tx_common["provider"] = provider
        if pay_phone:
            tx_common["phone"] = pay_phone
        if deposit_id:
            tx_common["provider_ref"] = deposit_id

        status_for_tx = "completed" if method == "cash" else "pending"

        tx_rows = []
        if due_shares > 0:
            tx_rows.append({
                **tx_common,
                "type":      "share_purchase",
                "amount":    float(due_shares),
                "narration": "Initial minimum shares",
                "status":    status_for_tx,
            })
        if due_subscription > 0:
            tx_rows.append({
                **tx_common,
                "type":      "subscription",
                "amount":    float(due_subscription),
                "narration": "Annual subscription fee",
                "status":    status_for_tx,
            })
        if tx_rows:
            admin.table("member_transactions").insert(tx_rows).execute()
            log.info("tx inserted: %d rows (method=%s)", len(tx_rows), method)

        # --- Yearly subscription (state layer) ---
        subscription_id = None
        if due_subscription > 0:
            year = datetime.now(timezone.utc).year

            # due_date = the org's configured subscription_due_month of this year
            due_date_str = None
            try:
                s = (
                    admin.table("institution_settings")
                    .select("subscription_due_month")
                    .eq("org_id", org_id).limit(1).execute()
                ).data or [{}]
                due_month = int(s[0].get("subscription_due_month") or 1)
                due_month = max(1, min(12, due_month))
                due_date_str = date(year, due_month, 1).isoformat()
            except Exception:
                log.exception("due month fetch failed (non-fatal)")

            # cash at registration -> already paid; online -> pending
            sub_status     = "paid" if method == "cash" else "pending"
            sub_paid       = float(due_subscription) if method == "cash" else 0.0
            sub_paid_at    = _now_iso() if method == "cash" else None

            sub_row = {
                "org_id":            org_id,
                "member_id":         member_pk,
                "subscription_year": year,
                "amount_due":        float(due_subscription),
                "amount_paid":       sub_paid,
                "currency":          spec["currency"],
                "status":            sub_status,
                "source":            "system",
                "due_date":          due_date_str,
                "last_payment_at":   sub_paid_at,
                "paid_at":           sub_paid_at,
                "created_by":        spec.get("created_by") or _user_id(),
            }
            try:
                sub_resp = admin.table("yearly_subscriptions").insert(sub_row).execute()
                if sub_resp.data:
                    subscription_id = sub_resp.data[0]["id"]

                # link the ledger tx to the subscription row
                if subscription_id:
                    try:
                        admin.table("member_transactions").update({
                            "subscription_id": subscription_id,
                        }).eq("org_id", org_id) \
                           .eq("member_id", member_pk) \
                           .eq("type", "subscription") \
                           .execute()
                    except Exception:
                        log.exception("subscription_id link failed (non-fatal)")
            except Exception:
                log.exception("yearly_subscriptions insert failed (non-fatal)")

        # --- Chart of accounts: post the initial money movements ---
        # Cash path: money received at the counter -> 1000 Cash on hand
        # Online path: money will arrive via pawaPay -> 1010 Bank account
        try:
            from routes.accounts.accounts import record_asset_movement

            asset_method = "cash" if method == "cash" else "online"

            # 1. Share capital (equity, not income) -> account 3000
            if due_shares > 0:
                record_asset_movement(
                    org_id=org_id,
                    direction="in",
                    amount=due_shares,
                    method=asset_method,
                    counterpart_account_code="3000",   # Share capital — members
                    category="other_income",           # ledger tag
                    narration=f"Initial shares · member {member_id}",
                    reference=member_id,
                    created_by=spec.get("created_by") or _user_id(),
                    meta={
                        "source":    "member_registration",
                        "member_id": member_pk,
                        "component": "shares",
                        "deposit_id": deposit_id,
                        "provider":   provider,
                    },
                )

            # 2. Subscription (income) -> account 4000
            if due_subscription > 0:
                record_asset_movement(
                    org_id=org_id,
                    direction="in",
                    amount=due_subscription,
                    method=asset_method,
                    counterpart_account_code="4000",   # Membership subscription income
                    category="other_income",
                    narration=f"Annual subscription · member {member_id}",
                    reference=member_id,
                    created_by=spec.get("created_by") or _user_id(),
                    meta={
                        "source":    "member_registration",
                        "member_id": member_pk,
                        "component": "subscription",
                        "deposit_id": deposit_id,
                        "provider":   provider,
                    },
                )
        except Exception:
            log.exception("chart-of-accounts post for member creation failed (non-fatal)")

        return {
            "id":              member_pk,
            "member_id":       member_id,
            "auth_email":      auth_email,
            "accounts":        {"savings": acc_sav, "loan": acc_loan},
            "subscription_id": subscription_id,
        }

    except Exception:
        if user_id_created:
            try:
                _admin().auth.admin.delete_user(user_id_created)
                log.info("rollback: deleted auth user %s", user_id_created)
            except Exception:
                log.exception("rollback delete_user failed")
        raise

# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
@members_bp.route("/", methods=["GET"])
def page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    if session["user"].get("role") != "admin":
        return redirect(url_for("auth.dashboard"))
    return render_template("members/members.html")


# ---------------------------------------------------------------------------
# API: form defaults
# ---------------------------------------------------------------------------
@members_bp.route("/api/settings", methods=["GET"])
@role_required("admin")
def api_settings():
    try:
        resp = (
            _admin().table("institution_settings")
            .select(
                "min_shares, min_share_price, "
                "annual_subscription_fee, subscription_due_month, currency"
            )
            .eq("org_id", _org_id()).limit(1).execute()
        )
    except Exception as e:
        log.exception("settings fetch failed: %s", e)
        return _bad("Could not load settings", 500)

    s = (resp.data or [{}])[0]
    min_units = _to_decimal(s.get("min_shares"))
    price     = _to_decimal(s.get("min_share_price"))
    sub_fee   = _to_decimal(s.get("annual_subscription_fee"))
    currency  = s.get("currency") or "UGX"

    shares_value = (min_units * price).quantize(Decimal("0.01"))
    total_due    = (shares_value + sub_fee).quantize(Decimal("0.01"))

    return jsonify({
        "ok": True,
        "settings": {
            "min_shares":              float(min_units),
            "min_share_price":         float(price),
            "annual_subscription_fee": float(sub_fee),
            "subscription_due_month":  s.get("subscription_due_month") or 1,
            "currency":                currency,
            "min_shares_value":        float(shares_value),
            "total_registration_due":  float(total_due),
        },
    })


# ---------------------------------------------------------------------------
# API: list
# ---------------------------------------------------------------------------
@members_bp.route("/api/list", methods=["GET"])
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
            "id, member_id, first_name, last_name, other_names, phone, email, "
            "status, category, member_type, joined_at, "
            "shares_units, shares_value, savings_balance, loan_balance",
            count="exact",
        )
        .eq("org_id", org_id)
        .order("created_at", desc=True)
        .range(start, end)
    )
    if status:
        q = q.eq("status", status)
    if search:
        like = f"%{search}%"
        q = q.or_(
            f"first_name.ilike.{like},"
            f"last_name.ilike.{like},"
            f"phone.ilike.{like},"
            f"member_id.ilike.{like}"
        )

    try:
        resp = q.execute()
    except Exception as e:
        log.exception("list members failed: %s", e)
        return _bad("Could not load members", 500)

    return jsonify({
        "ok": True,
        "members": resp.data or [],
        "total": resp.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# API: single member (with accounts)
# ---------------------------------------------------------------------------
@members_bp.route("/api/<uuid:member_uuid>", methods=["GET"])
@role_required("admin")
def api_detail(member_uuid):
    org_id = _org_id()
    try:
        resp = (
            _admin().table("members")
            .select(
                "*, member_accounts(account_type, account_number, "
                "balance, currency, status)"
            )
            .eq("org_id", org_id)
            .eq("id", str(member_uuid))
            .limit(1)
            .execute()
        )
    except Exception as e:
        log.exception("get member failed: %s", e)
        return _bad("Could not load member", 500)

    if not resp.data:
        return _bad("Member not found", 404)
    return jsonify({"ok": True, "member": resp.data[0]})


# ---------------------------------------------------------------------------
# API: create member
# ---------------------------------------------------------------------------
@members_bp.route("/api/create", methods=["POST"])
@role_required("admin")
def api_create():
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}

    log.info("=== CREATE MEMBER ===")
    log.info("org_id=%s keys=%s", org_id, list(data.keys()))
    log.info("payment=%s", data.get("payment"))

    # ---------------------------------------------------------------
    # 1. Validate
    # ---------------------------------------------------------------
    first = (data.get("first_name") or "").strip()
    last  = (data.get("last_name")  or "").strip()
    phone = (data.get("phone")      or "").strip()
    if not first or not last or not phone:
        return _bad("First name, last name, and phone are required",
                    reason="missing_names_or_phone")

    category = (data.get("category") or "new").lower()
    if category not in ("new", "existing"):
        return _bad("category must be 'new' or 'existing'",
                    reason="bad_category")

    payment = data.get("payment") or {}
    method  = (payment.get("method") or "").lower()
    if method not in ("cash", "online"):
        return _bad("payment.method must be 'cash' or 'online'",
                    reason="bad_method")

    provider  = None
    pay_phone = None
    if method == "online":
        provider  = (payment.get("provider") or "").upper()
        pay_phone = (payment.get("phone") or "").strip()
        if provider not in ("MTN", "AIRTEL"):
            return _bad("payment.provider must be 'MTN' or 'AIRTEL'",
                        reason="bad_provider")
        if not pay_phone:
            return _bad("payment.phone is required for online payments",
                        reason="missing_pay_phone")
        pay_phone = _normalize_phone(pay_phone)

    # ---------------------------------------------------------------
    # 2. Load institution settings
    # ---------------------------------------------------------------
    try:
        s_resp = (
            _admin().table("institution_settings")
            .select("min_shares, min_share_price, annual_subscription_fee, currency")
            .eq("org_id", org_id).limit(1).execute()
        )
    except Exception as e:
        log.exception("settings fetch failed: %s", e)
        return _bad("Could not read institution settings", 500,
                    reason="settings_db_error")

    s = (s_resp.data or [{}])[0]
    min_units = _to_decimal(s.get("min_shares"))
    price     = _to_decimal(s.get("min_share_price"))
    sub_fee   = _to_decimal(s.get("annual_subscription_fee"))
    currency  = s.get("currency") or "UGX"

    if price <= 0:
        return _bad("Share price is not configured. Set it in Institution Settings.",
                    reason="no_share_price")

    # ---------------------------------------------------------------
    # 3. Derive balances + amounts
    # ---------------------------------------------------------------
    if category == "new":
        shares_units     = min_units
        shares_value     = (min_units * price).quantize(Decimal("0.01"))
        savings_balance  = Decimal("0")
        loan_balance     = Decimal("0")
        due_shares       = shares_value
        due_subscription = sub_fee
    else:
        try:
            shares_units    = _to_decimal(data.get("shares_units"))
            savings_balance = _to_decimal(data.get("savings_balance"))
            loan_balance    = _to_decimal(data.get("loan_balance"))
        except ValueError as e:
            return _bad(str(e), reason="bad_balances")
        shares_value     = (shares_units * price).quantize(Decimal("0.01"))
        due_shares       = Decimal("0")
        due_subscription = sub_fee

    total_due = (due_shares + due_subscription).quantize(Decimal("0.01"))
    if total_due <= 0:
        return _bad("Nothing to charge — check shares and subscription settings.",
                    reason="zero_total")

    # Optional cross-check with client total
    client_total = payment.get("amount")
    if client_total not in (None, ""):
        try:
            ct = _to_decimal(client_total).quantize(Decimal("0.01"))
            if ct != total_due:
                return _bad(
                    f"Amount mismatch. Expected {total_due}. Client sent {ct}.",
                    reason="amount_mismatch",
                )
        except ValueError:
            return _bad("Invalid payment amount", reason="bad_client_total")

    # Build spec used by both cash path and staging (strings for JSON safety)
    spec = {
        "first":            first,
        "last":             last,
        "other_names":      (data.get("other_names")  or "").strip() or None,
        "gender":           (data.get("gender")       or "").strip() or None,
        "date_of_birth":    data.get("date_of_birth") or None,
        "national_id":      (data.get("national_id")  or "").strip() or None,
        "phone":            phone,
        "email":            (data.get("email")        or "").strip() or None,
        "address_line":     (data.get("address_line") or "").strip() or None,
        "city":             (data.get("city")         or "").strip() or None,
        "occupation":       (data.get("occupation")   or "").strip() or None,
        "member_type":      (data.get("member_type")  or "individual").lower(),
        "category":         category,
        "notes":            (data.get("notes")        or "").strip() or None,
        "currency":         currency,
        "shares_units":     str(shares_units),
        "shares_value":     str(shares_value),
        "savings_balance":  str(savings_balance),
        "loan_balance":     str(loan_balance),
        "due_shares":       str(due_shares),
        "due_subscription": str(due_subscription),
        "total_due":        str(total_due),
        "method":           method,
        "created_by":       _user_id(),
        "org_id":           org_id,
    }

    # ---------------------------------------------------------------
    # 4. CASH — commit immediately
    # ---------------------------------------------------------------
    if method == "cash":
        try:
            committed = _commit_member(org_id, spec)
        except Exception as e:
            log.exception("cash commit failed")
            return _bad(f"Could not create member: {e}", 500,
                        reason="commit_failed")

        return jsonify({
            "ok": True,
            "member": {
                "id":         committed["id"],
                "member_id":  committed["member_id"],
                "auth_email": committed["auth_email"],
            },
            "accounts": committed["accounts"],
            "payment": {
                "method":              "cash",
                "currency":            currency,
                "total":               float(total_due),
                "shares_amount":       float(due_shares),
                "subscription_amount": float(due_subscription),
                "status":              "completed",
                "provider":            "cash",
            },
        }), 201

    # ---------------------------------------------------------------
    # 5. ONLINE — pawaPay first, then stage to JSON
    # ---------------------------------------------------------------
    log.info("initiating pawaPay: provider=%s phone=%s amount=%s",
             provider, pay_phone, int(total_due))
    try:
        if provider == "MTN":
            pay_resp = deposit_mtn_ug(
                phone_number=pay_phone,
                amount=str(int(total_due)),
                client_reference_id=f"reg-{secrets.token_hex(6)}",
                customer_message=PAWAPAY_MESSAGE,
            )
        else:
            pay_resp = deposit_airtel_ug(
                phone_number=pay_phone,
                amount=str(int(total_due)),
                client_reference_id=f"reg-{secrets.token_hex(6)}",
                customer_message=PAWAPAY_MESSAGE,
            )
    except Exception as e:
        log.exception("pawaPay preflight deposit failed")
        return _bad(
            f"Payment could not be initiated: {e}. Member was NOT created.",
            502, reason="pawapay_raised",
        )

    log.info("pawaPay response: %s", pay_resp)

    deposit_id = pay_resp.get("depositId") or pay_resp.get("deposit_id")
    pay_status = (pay_resp.get("status") or "").upper()

    if not deposit_id or pay_status in ("REJECTED", "FAILED"):
        reason = (pay_resp.get("failureReason") or {}).get("failureMessage") \
                 or pay_status or "unknown"
        return _bad(f"Payment was rejected: {reason}. Member NOT created.",
                    400, reason="payment_rejected")

    # pawaPay accepted — stage the spec to disk
    spec.update({
        "provider":   provider,
        "pay_phone":  pay_phone,
        "deposit_id": deposit_id,
        "staged_at":  _now_iso(),
    })
    try:
        _save_pending(deposit_id, spec)
    except Exception as e:
        log.exception("failed to write pending file")
        return _bad(f"Payment accepted but staging failed: {e}", 500,
                    reason="staging_failed")

    return jsonify({
        "ok": True,
        "pending": True,
        "member": None,
        "accounts": None,
        "payment": {
            "method":              "online",
            "currency":            currency,
            "total":               float(total_due),
            "shares_amount":       float(due_shares),
            "subscription_amount": float(due_subscription),
            "status":              "pending",
            "provider":            provider,
            "deposit_id":          deposit_id,
            "phone":               pay_phone,
            "instructions":        "Approve the prompt on the phone. Member "
                                   "will be created once payment is confirmed.",
        },
    }), 202


# ---------------------------------------------------------------------------
# API: verify online payment
# ---------------------------------------------------------------------------
@members_bp.route("/api/payment/<deposit_id>/verify", methods=["POST"])
@role_required("admin")
def api_verify_payment(deposit_id):
    org_id = _org_id()
    log.info("VERIFY deposit_id=%s", deposit_id)

    staged = _load_pending(deposit_id)

    # If nothing staged, fall back to checking existing DB transactions
    # (covers legacy records or a re-verify after commit).
    if staged is None:
        try:
            tx_resp = (
                _admin().table("member_transactions")
                .select("id, member_id, amount, type, status")
                .eq("org_id", org_id)
                .eq("provider_ref", deposit_id)
                .execute()
            )
        except Exception:
            log.exception("tx fetch failed")
            return _bad("Could not load transaction", 500)

        txs = tx_resp.data or []
        if not txs:
            return _bad("Transaction not found", 404)
        if all(t["status"] == "completed" for t in txs):
            return jsonify({"ok": True, "status": "completed"})
        # in-flight rows still pending; keep polling
        return jsonify({"ok": True, "status": "pending"})

    # -------- staged path --------
    try:
        result = verify_deposit(deposit_id)
    except Exception as e:
        log.exception("verify_deposit failed")
        return _bad(f"Verification error: {e}", 502)

    status = result.get("status")
    log.info("verify: provider_status=%s", status)

    if result.get("completed"):
        try:
            committed = _commit_member(org_id, staged)
        except Exception as e:
            log.exception("commit after payment failed")
            # Keep the file so we can retry; do not delete.
            return _bad(f"Payment succeeded but member commit failed: {e}",
                        500, reason="commit_failed")

        _delete_pending(deposit_id)

        return jsonify({
            "ok": True,
            "status": "completed",
            "provider_status": status,
            "member": {
                "id":         committed["id"],
                "member_id":  committed["member_id"],
                "auth_email": committed["auth_email"],
            },
            "accounts": committed["accounts"],
        })

    if result.get("failed"):
        _delete_pending(deposit_id)
        return jsonify({
            "ok": True,
            "status": "failed",
            "provider_status": status,
            "reason": result.get("failure_reason"),
        })

    return jsonify({"ok": True, "status": "pending", "provider_status": status})

# ---------------------------------------------------------------------------
# API: update member status
# ---------------------------------------------------------------------------
VALID_MEMBER_STATUSES = {"active", "dormant", "suspended", "exited"}


@members_bp.route("/api/<uuid:member_uuid>/status", methods=["PATCH"])
@role_required("admin")
def api_update_status(member_uuid):
    org_id = _org_id()
    data   = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip().lower()

    if status not in VALID_MEMBER_STATUSES:
        return _bad(
            f"status must be one of {sorted(VALID_MEMBER_STATUSES)}",
            reason="bad_status",
        )

    try:
        resp = (
            _admin().table("members")
            .update({
                "status":     status,
                "updated_at": _now_iso(),
            })
            .eq("org_id", org_id)
            .eq("id", str(member_uuid))
            .execute()
        )
    except Exception as e:
        log.exception("status update failed: %s", e)
        return _bad(f"Could not update status: {e}", 500, reason="db_error")

    if not resp.data:
        return _bad("Member not found", 404, reason="not_found")

    row = resp.data[0]
    log.info("status updated: %s -> %s", member_uuid, status)
    return jsonify({
        "ok": True,
        "message": f"Status set to {status}",
        "member": {
            "id":         row["id"],
            "member_id":  row.get("member_id"),
            "status":     row["status"],
            "updated_at": row.get("updated_at"),
        },
    })
    
# ---------------------------------------------------------------------------
# API: member statistics (KPI cards)
# ---------------------------------------------------------------------------
@members_bp.route("/api/stats", methods=["GET"])
@role_required("admin")
def api_stats():
    """
    Returns:
        total_members, active_members, dormant_members, suspended_members,
        exited_members,
        total_shares_value, total_savings, total_loans
    Single aggregated query, plus one head-count query per status.
    """
    org_id = _org_id()

    try:
        # aggregate balances
        agg = (
            _admin().table("members")
            .select("shares_value, savings_balance, loan_balance")
            .eq("org_id", org_id)
            .execute()
        )
        rows = agg.data or []
        total_shares_value = sum(float(r.get("shares_value") or 0) for r in rows)
        total_savings      = sum(float(r.get("savings_balance") or 0) for r in rows)
        total_loans        = sum(float(r.get("loan_balance") or 0) for r in rows)
        total_members      = len(rows)

        # counts per status (one query per status; no N+1 across rows)
        def _count(status: str) -> int:
            r = (
                _admin().table("members")
                .select("id", count="exact")
                .eq("org_id", org_id)
                .eq("status", status)
                .limit(1)
                .execute()
            )
            return r.count or 0

        active    = _count("active")
        dormant   = _count("dormant")
        suspended = _count("suspended")
        exited    = _count("exited")

    except Exception as e:
        log.exception("stats fetch failed: %s", e)
        return _bad("Could not load stats", 500)

    return jsonify({
        "ok": True,
        "stats": {
            "total_members":      total_members,
            "active_members":     active,
            "dormant_members":    dormant,
            "suspended_members":  suspended,
            "exited_members":     exited,
            "total_shares_value": total_shares_value,
            "total_savings":      total_savings,
            "total_loans":        total_loans,
        },
    })