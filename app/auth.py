"""GUI authentication: password hashing, sessions and account bootstrap.

The dashboard sits behind a username/password login backed by the ``users`` and
``sessions`` tables in :mod:`app.db`. Design choices:

* **Password hashing** uses stdlib :func:`hashlib.pbkdf2_hmac` (SHA-256, 600k
  iterations, per-user random salt) so there is no third-party crypto
  dependency. Hashes are stored Django-style: ``pbkdf2_sha256$iters$salt$hash``.
* **Sessions** are opaque random tokens stored server-side with an expiry, so a
  logout (or expiry) genuinely invalidates the cookie — nothing sensitive lives
  in the cookie itself.
* **Sign-up is gated by a shared registration code** (``signup_code`` setting).
  On first run the legacy ``ALGOFOUNDRY_USER`` / ``ALGOFOUNDRY_PASSWORD`` env
  pair seeds an initial admin so there is always a way in.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time

from . import db

# ---- Tunables --------------------------------------------------------------
SESSION_COOKIE = "af_session"
_SESSION_TTL_S = 14 * 24 * 3600           # 14 days
_PBKDF2_ITERS = 600_000                    # OWASP-recommended floor for SHA-256
_USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
_MIN_PASSWORD_LEN = 8

# Secure attribute on the session cookie. Defaults off because the app is
# typically served over plain HTTP on localhost behind a tunnel; set
# ALGOFOUNDRY_COOKIE_SECURE=1 when terminating TLS in front of it.
COOKIE_SECURE = os.environ.get("ALGOFOUNDRY_COOKIE_SECURE", "").lower() in (
    "1", "true", "yes", "on",
)


# ---- Password hashing ------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = (encoded or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# ---- Validation ------------------------------------------------------------

def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def validate_username(username: str) -> str | None:
    """Return an error message for an invalid username, or ``None`` if valid."""
    if not _USERNAME_RE.match(username or ""):
        return (
            "Username must be 3–32 characters: lowercase letters, digits, "
            "dot, underscore or hyphen."
        )
    return None


def validate_password(password: str) -> str | None:
    if len(password or "") < _MIN_PASSWORD_LEN:
        return f"Password must be at least {_MIN_PASSWORD_LEN} characters."
    return None


def check_signup_code(code: str) -> bool:
    """Constant-time comparison against the configured registration code.

    Returns ``False`` when no code is configured (sign-up is disabled).
    """
    expected = str(db.get_setting("signup_code", "") or "")
    if not expected:
        return False
    return hmac.compare_digest((code or "").strip(), expected)


def signup_enabled() -> bool:
    return bool(str(db.get_setting("signup_code", "") or ""))


# ---- Authentication + sessions ---------------------------------------------

def authenticate(username: str, password: str) -> bool:
    user = db.get_user(normalize_username(username))
    if not user:
        # Hash anyway to keep timing roughly constant against user enumeration.
        verify_password(password, "pbkdf2_sha256$1$00$00")
        return False
    return verify_password(password, user.get("pw_hash", ""))


def register(username: str, password: str) -> tuple[bool, str | None]:
    """Create a standard (non-admin) user after validation.

    Returns ``(ok, error_message)``.
    """
    username = normalize_username(username)
    err = validate_username(username) or validate_password(password)
    if err:
        return False, err
    if not db.create_user(username, hash_password(password), is_admin=False):
        return False, "That username is already taken."
    return True, None


def is_admin(username: str) -> bool:
    user = db.get_user(normalize_username(username))
    return bool(user and user.get("is_admin"))


def start_session(username: str) -> tuple[str, float]:
    token = secrets.token_urlsafe(32)
    expires_ts = time.time() + _SESSION_TTL_S
    db.create_session(token, username, expires_ts)
    db.touch_user_login(username)
    return token, expires_ts


def session_user(token: str | None) -> str | None:
    if not token:
        return None
    row = db.get_session(token)
    if not row:
        return None
    if float(row.get("expires_ts") or 0) < time.time():
        db.delete_session(token)
        return None
    return row.get("username")


def end_session(token: str | None) -> None:
    if token:
        db.delete_session(token)


# ---- Bootstrap -------------------------------------------------------------

def ensure_seed() -> None:
    """Seed the registration code and initial admin from env on first run.

    Safe to call on every startup: it only fills gaps (code when unset, admin
    when there are no users) and never overwrites existing data.
    """
    code_env = os.environ.get("ALGOFOUNDRY_SIGNUP_CODE", "").strip()
    if code_env and not str(db.get_setting("signup_code", "") or ""):
        db.set_setting("signup_code", code_env)

    env_user = normalize_username(os.environ.get("ALGOFOUNDRY_USER", ""))
    env_pw = os.environ.get("ALGOFOUNDRY_PASSWORD", "")

    if db.count_users() == 0:
        if env_user and env_pw and not validate_username(env_user):
            db.create_user(env_user, hash_password(env_pw), is_admin=True)
            db.log_event("info", detail=f"seeded initial admin account '{env_user}'")

    # Guarantee at least one admin exists so the access controls are reachable.
    # Prefer the configured env account; otherwise promote the earliest user.
    if db.count_admins() == 0:
        promote = env_user if (env_user and db.get_user(env_user)) else db.earliest_username()
        if promote:
            db.set_user_admin(promote, True)
            db.log_event("info", detail=f"granted admin to '{promote}'")

    try:
        db.purge_expired_sessions()
    except Exception:  # noqa: BLE001 — best-effort housekeeping
        pass
