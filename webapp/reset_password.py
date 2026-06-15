"""Local CLI to reset a user's password (lockout recovery).

Usage (from project root):

    python -m webapp.reset_password <username-or-email>
    python -m webapp.reset_password <username-or-email> --password NEWPASS
    python -m webapp.reset_password --list

With no --password, a strong random password is generated and printed.
This talks directly to app_data.sqlite3 and needs no running server.
"""
from __future__ import annotations

import argparse
import secrets
import string

from . import users


def _random_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset a HerHub webapp user's password locally.")
    parser.add_argument("login", nargs="?", help="username or email")
    parser.add_argument("--password", "-p", help="new password (>= 6 chars); random if omitted")
    parser.add_argument("--list", action="store_true", help="list all users and exit")
    args = parser.parse_args()

    users.init_db()

    if args.list:
        rows = users.list_users(None, 1000, 0)
        if not rows:
            print("(no users)")
            return 0
        for r in rows:
            admin = " [admin]" if users.is_admin(r) else ""
            vip = " [vip]" if users.is_vip(r) else ""
            print(f"#{r['id']:<4} {r['username']:<24} {r['email'] or '':<28}{admin}{vip}")
        return 0

    if not args.login:
        parser.error("provide a username/email, or use --list")

    user = users.get_by_login(args.login)
    if not user:
        print(f"No user matches '{args.login}'. Use --list to see all users.")
        return 1

    new_pw = args.password or _random_password()
    if len(new_pw) < 6:
        print("Password must be at least 6 characters.")
        return 1

    users.set_password(user["id"], new_pw)
    print(f"Password for '{user['username']}' (#{user['id']}) has been reset.")
    print(f"New password: {new_pw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
