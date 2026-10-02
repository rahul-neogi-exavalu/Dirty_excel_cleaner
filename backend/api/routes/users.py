"""User management, for admins. Accounts are deactivated, never deleted, so the job
history keeps pointing at whoever ran each job."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

from .. import app_db, auth
from ..auth import User
from ..errors import ApiError, not_found

router = APIRouter(prefix="/api/users", tags=["users"])

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_email(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().lower()
    if not _EMAIL.match(value):
        raise ValueError("Enter a valid email address.")
    return value


def _clean_name(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value.strip()) < 2:
        raise ValueError("Enter a name of at least 2 characters.")
    return value.strip()


def _clean_phone(value: str | None) -> str | None:
    return (value or "").strip() or None


class UserCreate(BaseModel):
    user_name: str = Field(min_length=2, max_length=100)
    user_email_id: str = Field(min_length=3, max_length=255)
    user_phone_number: str | None = Field(default=None, max_length=20)
    is_admin: bool = False
    expiry_date: datetime | None = None

    _email = field_validator("user_email_id")(lambda value: _clean_email(value))
    _name = field_validator("user_name")(lambda value: _clean_name(value))
    _phone = field_validator("user_phone_number")(lambda value: _clean_phone(value))


class UserUpdate(BaseModel):
    """Only the fields sent are changed; send null to clear the phone or expiry date."""

    user_name: str | None = Field(default=None, min_length=2, max_length=100)
    user_email_id: str | None = Field(default=None, min_length=3, max_length=255)
    user_phone_number: str | None = Field(default=None, max_length=20)
    is_admin: bool | None = None
    is_active: bool | None = None
    expiry_date: datetime | None = None

    _email = field_validator("user_email_id")(lambda value: _clean_email(value))
    _name = field_validator("user_name")(lambda value: _clean_name(value))
    _phone = field_validator("user_phone_number")(lambda value: _clean_phone(value))

    def changes(self) -> dict:
        nullable = {"user_phone_number", "expiry_date"}
        return {name: value for name, value in self.model_dump(exclude_unset=True).items()
                if value is not None or name in nullable}


def _admins_left(conn, excluding: str) -> int:
    from psycopg import sql

    return conn.execute(sql.SQL(
        "SELECT count(*) FROM {} WHERE is_admin AND is_active AND (expiry_date IS NULL OR expiry_date > now()) "
        "AND user_id <> %s").format(app_db.table("users")), [excluding]).fetchone()[0]


def _email_taken(error: Exception) -> bool:
    import psycopg

    return isinstance(error, psycopg.errors.UniqueViolation)


def _create(body: UserCreate, admin: User) -> tuple[User, str]:
    from psycopg import sql

    password = auth.temporary_password()
    try:
        with app_db.connection() as conn:
            row = conn.execute(sql.SQL(
                "INSERT INTO {} (user_name, user_email_id, password_hash, is_admin, user_phone_number, "
                "must_change_password, expiry_date, created_by, modified_by) "
                "VALUES (%s, %s, %s, %s, %s, true, %s, %s, %s) RETURNING user_id").format(app_db.table("users")),
                [body.user_name, body.user_email_id, auth.hash_password(password), body.is_admin,
                 body.user_phone_number, body.expiry_date, admin.user_id, admin.user_id]).fetchone()
    except Exception as error:
        if _email_taken(error):
            raise ApiError(409, "email_taken", "That email already has an account.", field="user_email_id") from error
        raise
    return auth.user_by_id(str(row[0])), password


def _removes_access(changes: dict) -> bool:
    """Whether these changes take away an account's admin rights or its sign-in."""
    expiry = changes.get("expiry_date")
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return (changes.get("is_admin") is False or changes.get("is_active") is False
            or (expiry is not None and expiry <= datetime.now(timezone.utc)))


def _update(user_id: str, body: UserUpdate, admin: User) -> User:
    from psycopg import sql

    target = auth.user_by_id(user_id)
    if target is None:
        raise not_found("That user")
    changes = body.changes()
    if target.user_id == admin.user_id and _removes_access(changes):
        raise ApiError(409, "self_lockout", "You can't remove your own access.", "Ask another admin to do it.")
    if not changes:
        return target
    try:
        with app_db.connection() as conn:
            losing_admin = target.is_admin and _removes_access(changes)
            if losing_admin and _admins_left(conn, target.user_id) == 0:
                raise ApiError(409, "last_admin", "This is the last active admin.", "Make someone else an admin first.")
            assignments = sql.SQL(", ").join(
                sql.SQL("{} = {}").format(sql.Identifier(name), sql.Placeholder(name)) for name in changes)
            conn.execute(sql.SQL("UPDATE {} SET {}, modified_by = %(modified_by)s WHERE user_id = %(user_id)s")
                         .format(app_db.table("users"), assignments),
                         {**changes, "modified_by": admin.user_id, "user_id": target.user_id})
    except ApiError:
        raise
    except Exception as error:
        if _email_taken(error):
            raise ApiError(409, "email_taken", "That email already has an account.", field="user_email_id") from error
        raise
    auth.forget(target.user_id)
    return auth.user_by_id(target.user_id)


def _reset(user_id: str, admin: User) -> tuple[User, str]:
    from psycopg import sql

    target = auth.user_by_id(user_id)
    if target is None:
        raise not_found("That user")
    password = auth.temporary_password()
    with app_db.connection() as conn:
        conn.execute(sql.SQL("UPDATE {} SET password_hash = %s, must_change_password = true, modified_by = %s "
                             "WHERE user_id = %s").format(app_db.table("users")),
                     [auth.hash_password(password), admin.user_id, target.user_id])
    auth.forget(target.user_id)  # their sessions end: the password version changed
    return auth.user_by_id(target.user_id), password


@router.get("")
async def list_users(_: User = Depends(auth.require_admin)) -> dict:
    users = await run_in_threadpool(auth.select_users, "", None, "ORDER BY lower(user_name)")
    return {"users": [user.public() for user in users]}


@router.post("", status_code=201)
async def create_user(body: UserCreate, admin: User = Depends(auth.require_admin)) -> dict:
    user, password = await run_in_threadpool(_create, body, admin)
    # Shown once; only its hash is stored.
    return {"user": user.public(), "temporary_password": password}


@router.patch("/{user_id}")
async def update_user(user_id: str, body: UserUpdate, admin: User = Depends(auth.require_admin)) -> dict:
    return {"user": (await run_in_threadpool(_update, user_id, body, admin)).public()}


@router.post("/{user_id}/reset-password")
async def reset_password(user_id: str, admin: User = Depends(auth.require_admin)) -> dict:
    user, password = await run_in_threadpool(_reset, user_id, admin)
    return {"user": user.public(), "temporary_password": password}
