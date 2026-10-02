from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .. import app_db, auth
from ..auth import User
from ..errors import ApiError

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Login(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=auth.PASSWORD_MAX)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=auth.PASSWORD_MAX)
    new_password: str = Field(min_length=1, max_length=auth.PASSWORD_MAX)


def _sign_in(email: str, password: str, ip: str) -> User:
    email = email.strip().lower()
    auth.check_throttle(email, ip)
    user = auth.user_by_email(email)
    # Always pay for one hash, so an unknown address answers as slowly as a known one.
    valid = auth.verify_password(password, user.password_hash if user else auth.dummy_hash())
    if not (user and valid):
        auth.record_failure(email, ip)
        raise ApiError(401, "invalid_credentials", "Email or password is incorrect.")
    # The password was right, so saying why the account can't be used reveals nothing.
    problem = auth.refusal(user)
    if problem:
        raise problem
    auth.clear_failures(email)
    from psycopg import sql

    with app_db.connection() as conn:
        if auth.needs_rehash(user.password_hash):
            user.password_hash = auth.hash_password(password)
            conn.execute(sql.SQL("UPDATE {} SET password_hash = %s WHERE user_id = %s").format(app_db.table("users")),
                         [user.password_hash, user.user_id])
        conn.execute(sql.SQL("UPDATE {} SET last_login_at = now() WHERE user_id = %s").format(app_db.table("users")),
                     [user.user_id])
    auth.forget(user.user_id)
    return auth.user_by_id(user.user_id) or user


@router.post("/login")
async def login(body: Login, request: Request, response: Response) -> dict:
    if not request.headers.get(auth.CLIENT_HEADER):
        raise ApiError(403, "client_header_missing", "This request was refused.", "Reload the page and try again.")
    ip = request.client.host if request.client else "unknown"
    user = await run_in_threadpool(_sign_in, body.email, body.password, ip)
    auth.set_cookie(response, user)
    return {"user": user.public()}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    if not request.headers.get(auth.CLIENT_HEADER):
        raise ApiError(403, "client_header_missing", "This request was refused.", "Reload the page and try again.")
    auth.clear_cookie(response)
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(auth.current_user)) -> dict:
    return {"user": user.public()}


def _change(user: User, current: str, new: str) -> User:
    if not auth.verify_password(current, user.password_hash):
        raise ApiError(422, "wrong_password", "Current password is incorrect.", field="current_password")
    if current == new:
        raise ApiError(422, "same_password", "Choose a password you haven't used here.", field="new_password")
    auth.check_new_password(new, user.user_email_id)
    from psycopg import sql

    with app_db.connection() as conn:
        conn.execute(
            sql.SQL("UPDATE {} SET password_hash = %s, must_change_password = false, modified_by = %s "
                    "WHERE user_id = %s").format(app_db.table("users")),
            [auth.hash_password(new), user.user_id, user.user_id],
        )
    auth.forget(user.user_id)
    return auth.user_by_id(user.user_id)


@router.post("/password")
async def change_password(body: PasswordChange, response: Response,
                          user: User = Depends(auth.current_user)) -> dict:
    updated = await run_in_threadpool(_change, user, body.current_password, body.new_password)
    # The old session was tied to the old password; this one replaces it.
    auth.set_cookie(response, updated)
    return {"user": updated.public()}
