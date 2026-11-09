"""Admin commands, run by Brandon only (from a terminal with DATABASE_URL set).

    nutrition-admin create-invite "Name"
    nutrition-admin list-users
    nutrition-admin rotate-key USER_ID
    nutrition-admin disable-user USER_ID
    nutrition-admin delete-user USER_ID --yes

None of these show anyone's source passwords, codes or connector key.
"""

import argparse
import os
import sys
from datetime import timedelta

from sqlalchemy import select

from nutrition_server import db
from nutrition_server.security import hash_secret, new_invite_code

INVITE_LIFETIME = timedelta(days=7)


def create_invite(name: str) -> str:
    """Make a one-time invite and return its link. The code is shown once
    and only its hash is stored."""
    code = new_invite_code()
    with db.open_session() as s:
        s.add(
            db.Invite(
                code_hash=hash_secret(code),
                name=name,
                expires_at=db.utcnow() + INVITE_LIFETIME,
            )
        )
        s.commit()
        db.record_audit(s, None, "admin:create-invite", True)
    base = os.environ.get("PUBLIC_URL", "http://localhost:8000").rstrip("/")
    return f"{base}/invite/{code}"


def list_users() -> list[tuple[int, str, bool]]:
    with db.open_session() as s:
        users = s.scalars(select(db.User).order_by(db.User.id)).all()
        return [(u.id, u.display_name, u.active) for u in users]


def rotate_key(user_id: int) -> bool:
    """Kill the user's current connector link. They make a new one on the
    website, so Brandon never sees it."""
    with db.open_session() as s:
        user = s.get(db.User, user_id)
        if user is None:
            return False
        user.key_hash = None
        s.commit()
        db.record_audit(s, user_id, "admin:rotate-key", True)
        return True


def disable_user(user_id: int) -> bool:
    with db.open_session() as s:
        user = s.get(db.User, user_id)
        if user is None:
            return False
        user.active = False
        s.commit()
        db.record_audit(s, user_id, "admin:disable-user", True)
        return True


def delete_user(user_id: int) -> bool:
    with db.open_session() as s:
        if s.get(db.User, user_id) is None:
            return False
        db.delete_user_data(s, user_id)
        # The user's own audit rows are gone with them; this records only
        # that an admin deleted someone, with no id or name.
        db.record_audit(s, None, "admin:delete-user", True)
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nutrition-admin")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-invite", help="make a one-time invite link (7 days)")
    p.add_argument("name")
    sub.add_parser("list-users", help="show user ids and names")
    for name, help_text in [
        ("rotate-key", "kill a user's connector link"),
        ("disable-user", "block a user without deleting data"),
        ("delete-user", "remove a user and all their data"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("user_id", type=int)
        if name == "delete-user":
            p.add_argument("--yes", action="store_true", help="confirm deletion")
    args = parser.parse_args(argv)

    if args.command == "create-invite":
        print(f"Invite for {args.name} (works once, expires in 7 days):")
        print(create_invite(args.name))
        return 0
    if args.command == "list-users":
        for uid, name, active in list_users():
            print(f"{uid}\t{name}\t{'active' if active else 'disabled'}")
        return 0
    if args.command == "delete-user" and not args.yes:
        print("This removes the user and all their data. Re-run with --yes.", file=sys.stderr)
        return 1
    action = {"rotate-key": rotate_key, "disable-user": disable_user, "delete-user": delete_user}
    if not action[args.command](args.user_id):
        print(f"No user with id {args.user_id}.", file=sys.stderr)
        return 1
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
