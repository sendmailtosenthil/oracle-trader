"""Manage logins from the host — the way back in when nobody can sign in.

Users normally live in the app (Setup › User Management); this covers the cases
the UI can't: a forgotten administrator password, or seeding the first login on
a fresh database.

    python scripts/manage_users.py list
    python scripts/manage_users.py add sasi --grant bees.dashboard=read \
                                            --grant downloader.download=read \
                                            --grant ztrade.dashboard=edit \
                                            --grant ztrade.manage=edit
    python scripts/manage_users.py add newadmin --admin
    python scripts/manage_users.py grant sasi --grant momentum.dashboard=read
    python scripts/manage_users.py passwd senthil
    python scripts/manage_users.py delete sasi
    python scripts/manage_users.py pages          # what can be granted

Passwords are prompted for (never passed on the command line, where they would
land in shell history); ``--password`` exists for scripted setup.
"""
import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import database
from common import permissions as P
from common import users as U


def _die(message):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _ask_password(supplied):
    if supplied:
        return supplied, supplied
    pw = getpass.getpass("New password: ")
    return pw, getpass.getpass("Confirm password: ")


def _parse_grants(pairs):
    """``["bees.ledger=read", …]`` → ``{"bees.ledger": "read"}``."""
    perms = {}
    for pair in pairs or []:
        key, _, level = pair.partition("=")
        page = P.BY_KEY.get(key)
        if page is None or page.admin_only:
            _die(f"unknown page '{key}' — run `pages` to list them")
        if level not in page.levels:
            _die(f"'{key}' accepts {'/'.join(page.levels)}, not '{level or '(empty)'}'")
        perms[key] = level
    return perms


def cmd_pages(_args, _db):
    for section in P.SECTIONS:
        print(section)
        for page in P.pages_in(section):
            note = " (administrators only)" if page.admin_only else ""
            print(f"  {page.key:<24} {'/'.join(page.levels)}{note}")


def cmd_list(_args, db):
    users = U.list_users(db)
    if not users:
        print("No users. Create one with `add`.")
        return
    for user in users:
        print(f"{user.username:<16} {P.summarise(user)}")


def cmd_add(args, db):
    password, confirm = _ask_password(args.password)
    _, err = U.create_user(db, args.username, password, confirm,
                           _parse_grants(args.grant), args.admin)
    if err:
        _die(err)
    print(f"Created '{U.normalise_username(args.username)}'.")


def cmd_grant(args, db):
    user = U.get_user(db, args.username)
    if user is None:
        _die(f"no such user '{args.username}'")
    # Grants are merged onto what the user already has; --replace starts fresh.
    perms = {} if args.replace else P.loads(user.permissions)
    perms.update(_parse_grants(args.grant))
    is_admin = True if args.admin else (False if args.no_admin else None)
    _, err = U.set_permissions(db, user, perms, is_admin=is_admin)
    if err:
        _die(err)
    print(f"{user.username}: {P.summarise(user)}")


def cmd_passwd(args, db):
    user = U.get_user(db, args.username)
    if user is None:
        _die(f"no such user '{args.username}'")
    password, confirm = _ask_password(args.password)
    _, err = U.set_password(db, user, password, confirm)
    if err:
        _die(err)
    print(f"Password changed for '{user.username}' — its browser sessions were signed out.")


def cmd_delete(args, db):
    user = U.get_user(db, args.username)
    if user is None:
        _die(f"no such user '{args.username}'")
    _, err = U.delete_user(db, user)
    if err:
        _die(err)
    print(f"Deleted '{args.username}'.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("pages", help="list grantable pages and their levels").set_defaults(fn=cmd_pages)
    sub.add_parser("list", help="list users and their access").set_defaults(fn=cmd_list)

    add = sub.add_parser("add", help="create a user")
    add.add_argument("username")
    add.add_argument("--password", help="skip the prompt (avoid: lands in shell history)")
    add.add_argument("--admin", action="store_true", help="full access to every page")
    add.add_argument("--grant", action="append", metavar="PAGE=LEVEL",
                     help="repeatable, e.g. --grant bees.dashboard=read")
    add.set_defaults(fn=cmd_add)

    grant = sub.add_parser("grant", help="change an existing user's access")
    grant.add_argument("username")
    grant.add_argument("--grant", action="append", metavar="PAGE=LEVEL")
    grant.add_argument("--replace", action="store_true",
                       help="drop existing grants instead of merging")
    grant.add_argument("--admin", action="store_true", help="make administrator")
    grant.add_argument("--no-admin", action="store_true", dest="no_admin",
                       help="remove administrator")
    grant.set_defaults(fn=cmd_grant)

    passwd = sub.add_parser("passwd", help="set a user's password")
    passwd.add_argument("username")
    passwd.add_argument("--password", help="skip the prompt")
    passwd.set_defaults(fn=cmd_passwd)

    delete = sub.add_parser("delete", help="remove a user")
    delete.add_argument("username")
    delete.set_defaults(fn=cmd_delete)

    args = parser.parse_args()

    if args.command == "pages":
        return args.fn(args, None)

    database.init_db()
    db = database.SessionLocal()
    try:
        args.fn(args, db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
