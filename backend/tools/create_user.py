"""Create a user in the app database -- how the first admin gets in.

    python backend/tools/create_user.py --name "Rahul Neogi" --email rahul@example.com --admin

Uses APP_DB_* from backend/.env (or .env) and creates the users and jobs tables on first
use. The password is asked for twice and never echoed; pass --temporary instead to
generate one that must be changed at first sign-in. After this, admins add everyone
else from the Users page.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from api import app_db, auth  # noqa: E402
from api.errors import ApiError  # noqa: E402
from api.routes.users import UserCreate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--phone")
    parser.add_argument("--admin", action="store_true", help="may manage users and see every job")
    parser.add_argument("--expires", help="account expiry, e.g. 2027-03-31 or 2027-03-31T18:00+05:30")
    parser.add_argument("--temporary", action="store_true", help="generate a password to change at first sign-in")
    args = parser.parse_args()

    try:
        body = UserCreate(user_name=args.name, user_email_id=args.email, user_phone_number=args.phone,
                          is_admin=args.admin, expiry_date=datetime.fromisoformat(args.expires) if args.expires else None)
    except ValueError as error:
        print(f"Invalid input: {error}", file=sys.stderr)
        return 2

    if args.temporary:
        password = auth.temporary_password()
    else:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            print("The passwords don't match.", file=sys.stderr)
            return 2
        try:
            auth.check_new_password(password, body.user_email_id)
        except ApiError as error:
            print(error.message, file=sys.stderr)
            return 2

    from psycopg import errors, sql

    try:
        with app_db.connection() as conn:
            conn.execute(sql.SQL(
                "INSERT INTO {} (user_name, user_email_id, password_hash, is_admin, user_phone_number, "
                "must_change_password, expiry_date) VALUES (%s, %s, %s, %s, %s, %s, %s)").format(app_db.table("users")),
                [body.user_name, body.user_email_id, auth.hash_password(password), body.is_admin,
                 body.user_phone_number, args.temporary, body.expiry_date])
    except errors.UniqueViolation:
        print(f"{body.user_email_id} already has an account.", file=sys.stderr)
        return 1
    except ApiError as error:
        print(f"{error.message} {error.advice or ''}\n{error.detail or ''}".strip(), file=sys.stderr)
        return 1
    finally:
        app_db.close()

    role = "admin" if body.is_admin else "user"
    print(f"Created {role} {body.user_email_id}.")
    if args.temporary:
        print(f"Temporary password (shown once): {password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
