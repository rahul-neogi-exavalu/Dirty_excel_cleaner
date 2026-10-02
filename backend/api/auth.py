"""Who is signed in: password hashing, the session cookie, and the request guards.

Every hash here is SHA-256 based, standard library only:

* Passwords: PBKDF2-HMAC-SHA256, a random salt per password and ``PBKDF2_ITERATIONS``
  rounds. A single SHA-256 of a password can be brute-forced on a GPU at billions of
  guesses a second; the rounds are what make a stolen hash expensive to attack. The
  round count is stored with the hash, so it can be raised and old hashes upgrade the
  next time their owner signs in.
* The session cookie: a small JSON payload signed with HMAC-SHA256.

The cookie is HttpOnly, so page scripts can't read it, and it rides along on downloads
and uploads without any client code. Every request re-reads the user (cached briefly),
so deactivation, expiry and password resets take effect within ``USER_CACHE_SECONDS``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

from fastapi import Depends, Request, Response

from . import app_db, config
from .errors import ApiError

log = logging.getLogger("ahi.auth")

COOKIE = "ahi_session"
# Unsafe methods must carry this header: a form on another site can't set it.
CLIENT_HEADER = "X-AHI-Client"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

PBKDF2_ITERATIONS = 600_000  # OWASP's figure for PBKDF2-HMAC-SHA256
_SCHEME = "pbkdf2_sha256"

PASSWORD_MIN = 8
PASSWORD_MAX = 128

USER_CACHE_SECONDS = 30.0
# Failed sign-ins tolerated per window before a 429. An office shares one address, so
# the per-address limit is looser than the per-account one.
THROTTLE_WINDOW = 15 * 60
THROTTLE_PER_EMAIL = 5
THROTTLE_PER_IP = 20

_SECRET = (config.APP_SESSION_SECRET or secrets.token_urlsafe(48)).encode()
if not config.APP_SESSION_SECRET:
    log.warning("APP_SESSION_SECRET is not set: sessions end whenever the API restarts.")


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{_SCHEME}${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != _SCHEME:
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(iterations))
        return hmac.compare_digest(candidate, _unb64(digest))
    except (ValueError, TypeError):
        return False


def needs_rehash(stored: str) -> bool:
    try:
        return int(stored.split("$")[1]) < PBKDF2_ITERATIONS
    except (IndexError, ValueError):
        return True


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """Checked when the email is unknown, so a miss costs as long as a wrong password and
    response times don't reveal which addresses have accounts."""
    return hash_password(secrets.token_urlsafe(16))


def check_new_password(password: str, email: str | None = None) -> None:
    if len(password) < PASSWORD_MIN:
        raise ApiError(422, "weak_password", f"Use at least {PASSWORD_MIN} characters.", field="new_password")
    if len(password) > PASSWORD_MAX:
        raise ApiError(422, "weak_password", f"Use at most {PASSWORD_MAX} characters.", field="new_password")
    if email and password.strip().lower() == email.strip().lower():
        raise ApiError(422, "weak_password", "The password can't be your email address.", field="new_password")


def temporary_password() -> str:
    """What an admin hands over; the user must replace it at first sign-in."""
    return secrets.token_urlsafe(12)


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #

_COLUMNS = ("user_id, user_name, user_email_id, password_hash, is_admin, user_phone_number, is_active, "
            "must_change_password, last_login_at, expiry_date, created_at, modified_at")


@dataclass
class User:
    user_id: str
    user_name: str
    user_email_id: str
    password_hash: str
    is_admin: bool
    user_phone_number: str | None
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None
    expiry_date: datetime | None
    created_at: datetime | None = None
    modified_at: datetime | None = None

    @classmethod
    def from_row(cls, row) -> "User":
        values = list(row)
        values[0] = str(values[0])
        return cls(*values)

    @property
    def expired(self) -> bool:
        return self.expiry_date is not None and self.expiry_date <= datetime.now(timezone.utc)

    @property
    def state(self) -> str:
        if not self.is_active:
            return "inactive"
        return "expired" if self.expired else "active"

    def public(self) -> dict:
        """What the UI may see -- never the hash."""
        return {
            "user_id": self.user_id,
            "user_name": self.user_name,
            "user_email_id": self.user_email_id,
            "user_phone_number": self.user_phone_number,
            "is_admin": self.is_admin,
            "is_active": self.is_active,
            "state": self.state,
            "must_change_password": self.must_change_password,
            "last_login_at": _iso(self.last_login_at),
            "expiry_date": _iso(self.expiry_date),
            "created_at": _iso(self.created_at),
            "modified_at": _iso(self.modified_at),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def select_users(where: str = "", params: list | None = None, order: str = "") -> list[User]:
    from psycopg import sql

    query = sql.SQL("SELECT " + _COLUMNS + " FROM {} " + where + " " + order).format(app_db.table("users"))
    with app_db.connection() as conn:
        return [User.from_row(row) for row in conn.execute(query, params or []).fetchall()]


def user_by_id(user_id: str) -> User | None:
    found = select_users("WHERE user_id = %s", [user_id])
    return found[0] if found else None


def user_by_email(email: str) -> User | None:
    found = select_users("WHERE lower(user_email_id) = lower(%s)", [email.strip()])
    return found[0] if found else None


_cache: dict[str, tuple[User, float]] = {}
_cache_lock = threading.Lock()


def cached_user(user_id: str) -> User | None:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(user_id)
    if hit and now - hit[1] < USER_CACHE_SECONDS:
        return hit[0]
    user = user_by_id(user_id)
    with _cache_lock:
        if user is None:
            _cache.pop(user_id, None)
        else:
            _cache[user_id] = (user, now)
    return user


def forget(user_id: str) -> None:
    """Drop a cached user after a change, so it applies on their next request."""
    with _cache_lock:
        _cache.pop(user_id, None)


# --------------------------------------------------------------------------- #
# Session cookie
# --------------------------------------------------------------------------- #


def _sign(payload: bytes) -> str:
    return _b64(hmac.new(_SECRET, payload, hashlib.sha256).digest())


def _password_version(user: User) -> str:
    """Changes whenever the password does, which ends every older session."""
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


def issue(user: User, issued_at: float | None = None) -> str:
    payload = json.dumps({"uid": user.user_id, "iat": int(issued_at or time.time()), "pv": _password_version(user)},
                         separators=(",", ":")).encode()
    return f"{_b64(payload)}.{_sign(payload)}"


def read(token: str | None) -> dict | None:
    """The session's claims, or None when missing, forged or too old."""
    if not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    try:
        payload = _unb64(body)
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(payload), signature):
        return None
    try:
        claims = json.loads(payload)
    except ValueError:
        return None
    if time.time() - claims.get("iat", 0) > config.APP_SESSION_HOURS * 3600:
        return None
    return claims


def set_cookie(response: Response, user: User) -> None:
    response.set_cookie(
        COOKIE, issue(user), max_age=int(config.APP_SESSION_HOURS * 3600), httponly=True,
        secure=config.APP_COOKIE_SECURE, samesite="lax", path="/",
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", httponly=True, secure=config.APP_COOKIE_SECURE, samesite="lax")


# --------------------------------------------------------------------------- #
# Sign-in throttle (in memory: one API process, like the job store)
# --------------------------------------------------------------------------- #

_failures: dict[str, deque] = {}
_failures_lock = threading.Lock()


def _recent(key: str, now: float) -> deque:
    found = _failures.setdefault(key, deque())
    while found and now - found[0] > THROTTLE_WINDOW:
        found.popleft()
    return found


def check_throttle(email: str, ip: str) -> None:
    now = time.time()
    with _failures_lock:
        by_email, by_ip = _recent(f"email:{email}", now), _recent(f"ip:{ip}", now)
        blocked = [q for q, limit in ((by_email, THROTTLE_PER_EMAIL), (by_ip, THROTTLE_PER_IP)) if len(q) >= limit]
        if not blocked:
            return
        wait = max(THROTTLE_WINDOW - (now - q[0]) for q in blocked)
    minutes = max(1, int(-(-wait // 60)))
    raise ApiError(429, "too_many_attempts", f"Too many attempts. Try again in {minutes} min.",
                   detail=str(minutes))


def record_failure(email: str, ip: str) -> None:
    now = time.time()
    with _failures_lock:
        _recent(f"email:{email}", now).append(now)
        _recent(f"ip:{ip}", now).append(now)


def clear_failures(email: str) -> None:
    with _failures_lock:
        _failures.pop(f"email:{email}", None)


# --------------------------------------------------------------------------- #
# Request guards
# --------------------------------------------------------------------------- #


def not_signed_in() -> ApiError:
    return ApiError(401, "not_authenticated", "Sign in to continue.")


def refusal(user: User) -> ApiError | None:
    """Why this account may not be used right now, if it may not."""
    if not user.is_active:
        return ApiError(403, "account_inactive", "Account inactive. Ask an admin.")
    if user.expired:
        return ApiError(403, "account_expired", "Account expired. Ask an admin.")
    return None


def current_user(request: Request, response: Response) -> User:
    """The signed-in user, allowed even while a password change is pending."""
    if request.method not in SAFE_METHODS and not request.headers.get(CLIENT_HEADER):
        raise ApiError(403, "client_header_missing", "This request was refused.",
                       "Reload the page and try again.")
    claims = read(request.cookies.get(COOKIE))
    if claims is None:
        raise not_signed_in()
    user = cached_user(str(claims.get("uid", "")))
    if user is None or claims.get("pv") != _password_version(user):
        raise not_signed_in()
    problem = refusal(user)
    if problem:
        raise problem
    # Sliding session: re-issued once half its life has passed.
    if time.time() - claims.get("iat", 0) > config.APP_SESSION_HOURS * 1800:
        set_cookie(response, user)
    request.state.user = user
    return user


def require_user(user: User = Depends(current_user)) -> User:
    """The guard on every route except sign-in: a signed-in user with a usable password."""
    if user.must_change_password:
        raise ApiError(403, "password_change_required", "Set a new password to continue.")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise ApiError(403, "admin_only", "Only an admin can do that.")
    return user
