"""Sign-in: SHA-256 password hashing, the signed session cookie, and the request guards.

Runs without a database: the user lookups are replaced, so what is pinned here is the
behaviour of the guards themselves.
"""

import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api import auth  # noqa: E402
from api.main import app  # noqa: E402

FAST = 1_000  # rounds for tests that are not about the round count

HEADERS = {auth.CLIENT_HEADER: "web"}


def _user(**changes) -> auth.User:
    base = auth.User(
        user_id="11111111-1111-4111-8111-111111111111", user_name="Ada Lovelace",
        user_email_id="ada@example.com", password_hash=auth.hash_password("correct horse battery", FAST),
        is_admin=False, user_phone_number=None, is_active=True, must_change_password=False,
        last_login_at=None, expiry_date=None,
    )
    return replace(base, **changes)


@pytest.fixture
def real_auth(signed_in, monkeypatch):
    """Undo the suite's signed-in override, and serve users from memory."""
    app.dependency_overrides.pop(auth.current_user, None)
    users: dict[str, auth.User] = {}
    monkeypatch.setattr(auth, "cached_user", lambda user_id: users.get(user_id))
    monkeypatch.setattr(auth, "user_by_id", lambda user_id: users.get(user_id))
    monkeypatch.setattr(auth, "user_by_email",
                        lambda email: next((u for u in users.values() if u.user_email_id == email.lower()), None))
    with auth._failures_lock:
        auth._failures.clear()
    yield users


def _client_as(user: auth.User | None) -> TestClient:
    client = TestClient(app)
    if user is not None:
        client.cookies.set(auth.COOKIE, auth.issue(user))
    return client


# --- passwords -------------------------------------------------------------------


def test_passwords_are_pbkdf2_sha256_with_a_salt_per_hash():
    first, second = auth.hash_password("same password", FAST), auth.hash_password("same password", FAST)
    assert first.startswith(f"pbkdf2_sha256${FAST}$")
    assert first != second  # unique salt
    assert auth.verify_password("same password", first)
    assert not auth.verify_password("Same password", first)
    assert not auth.verify_password("same password", "not-a-hash")


def test_the_default_round_count_is_current_and_lower_counts_are_upgraded():
    assert auth.hash_password("x" * 12).split("$")[1] == str(auth.PBKDF2_ITERATIONS)
    assert auth.needs_rehash(auth.hash_password("x" * 12, FAST))
    assert not auth.needs_rehash(f"pbkdf2_sha256${auth.PBKDF2_ITERATIONS}$a$b")


def test_new_passwords_need_eight_characters_and_not_the_email():
    from api.errors import ApiError

    with pytest.raises(ApiError):
        auth.check_new_password("short")
    with pytest.raises(ApiError):
        auth.check_new_password("ada@example.com", "ada@example.com")
    auth.check_new_password("a long enough passphrase", "ada@example.com")


# --- session cookie ----------------------------------------------------------------


def test_a_session_is_signed_and_tampering_is_refused():
    user = _user()
    token = auth.issue(user)
    assert auth.read(token)["uid"] == user.user_id
    body, signature = token.rsplit(".", 1)
    forged = auth._b64(auth._unb64(body).replace(b"1111", b"2222", 1))
    assert auth.read(f"{forged}.{signature}") is None
    assert auth.read(f"{body}.{signature[:-2]}AA") is None
    assert auth.read(None) is None and auth.read("garbage") is None


def test_an_old_session_expires():
    from api import config

    old = auth.issue(_user(), issued_at=time.time() - config.APP_SESSION_HOURS * 3600 - 5)
    assert auth.read(old) is None


# --- guards ------------------------------------------------------------------------


def test_api_routes_need_a_session(real_auth):
    client = _client_as(None)
    response = client.get("/api/history")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"
    assert client.get("/api/health").status_code == 200


def test_unsafe_requests_need_the_client_header(real_auth):
    user = _user()
    real_auth[user.user_id] = user
    client = _client_as(user)
    refused = client.post("/api/jobs", json={"workbook_id": "x", "sheets": ["a"]})
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "client_header_missing"
    # With the header the request reaches the route (and fails there, on the unknown workbook).
    assert client.post("/api/jobs", json={"workbook_id": "x", "sheets": ["a"]}, headers=HEADERS).status_code == 404


def test_inactive_and_expired_accounts_are_refused_mid_session(real_auth):
    user = _user()
    client = _client_as(user)
    real_auth[user.user_id] = replace(user, is_active=False)
    assert client.get("/api/auth/me").json()["error"]["code"] == "account_inactive"
    real_auth[user.user_id] = replace(user, expiry_date=datetime.now(timezone.utc) - timedelta(minutes=1))
    assert client.get("/api/auth/me").json()["error"]["code"] == "account_expired"


def test_a_password_change_ends_older_sessions(real_auth):
    user = _user()
    client = _client_as(user)
    real_auth[user.user_id] = replace(user, password_hash=auth.hash_password("a different password", FAST))
    assert client.get("/api/auth/me").status_code == 401


def test_a_pending_password_change_blocks_everything_but_auth(real_auth):
    user = _user(must_change_password=True)
    real_auth[user.user_id] = user
    client = _client_as(user)
    assert client.get("/api/auth/me").status_code == 200
    blocked = client.get("/api/history")
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "password_change_required"


def test_user_management_is_for_admins(real_auth):
    user = _user()
    real_auth[user.user_id] = user
    response = _client_as(user).get("/api/users")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "admin_only"


# --- signing in --------------------------------------------------------------------


@pytest.fixture
def fake_db(monkeypatch):
    statements = []

    class Conn:
        def execute(self, query, params=None):
            statements.append((query, params))

    @contextmanager
    def connection():
        yield Conn()

    from api import app_db

    monkeypatch.setattr(app_db, "connection", connection)
    return statements


def test_sign_in_sets_an_httponly_cookie_and_upgrades_old_hashes(real_auth, fake_db):
    user = _user()
    real_auth[user.user_id] = user
    client = _client_as(None)
    response = client.post("/api/auth/login", json={"email": "ADA@example.com ", "password": "correct horse battery"},
                           headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()["user"]
    assert body["user_email_id"] == "ada@example.com" and "password_hash" not in body
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    # The stored hash used fewer rounds than today's default, so it was re-hashed.
    assert any("password_hash" in str(query) for query, _ in fake_db)


def test_wrong_and_unknown_logins_get_the_same_answer(real_auth, fake_db):
    user = _user()
    real_auth[user.user_id] = user
    client = _client_as(None)
    wrong = client.post("/api/auth/login", json={"email": user.user_email_id, "password": "nope"}, headers=HEADERS)
    unknown = client.post("/api/auth/login", json={"email": "who@example.com", "password": "nope"}, headers=HEADERS)
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_repeated_failures_are_throttled(real_auth, fake_db):
    user = _user()
    real_auth[user.user_id] = user
    client = _client_as(None)
    for _ in range(auth.THROTTLE_PER_EMAIL):
        client.post("/api/auth/login", json={"email": user.user_email_id, "password": "nope"}, headers=HEADERS)
    blocked = client.post("/api/auth/login", json={"email": user.user_email_id, "password": "correct horse battery"},
                          headers=HEADERS)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "too_many_attempts"


def test_disabled_accounts_are_named_only_after_the_right_password(real_auth, fake_db):
    user = _user(is_active=False)
    real_auth[user.user_id] = user
    client = _client_as(None)
    wrong = client.post("/api/auth/login", json={"email": user.user_email_id, "password": "nope"}, headers=HEADERS)
    assert wrong.json()["error"]["code"] == "invalid_credentials"
    right = client.post("/api/auth/login", json={"email": user.user_email_id, "password": "correct horse battery"},
                        headers=HEADERS)
    assert right.status_code == 403 and right.json()["error"]["code"] == "account_inactive"


def test_an_expiry_in_the_past_counts_as_removing_access():
    from api.routes.users import _removes_access

    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=30)
    assert _removes_access({"expiry_date": past})
    assert _removes_access({"expiry_date": past.replace(tzinfo=None)})  # naive is read as UTC
    assert not _removes_access({"expiry_date": future})
    assert not _removes_access({"expiry_date": None})  # clearing an expiry restores access
    assert _removes_access({"is_admin": False}) and _removes_access({"is_active": False})
