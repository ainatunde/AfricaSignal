"""Operator administration from the host: ``python -m africasignal.admin <command>``.

Creating the first operator needs shell access on purpose: there is no public sign-up. The
password is read from a prompt (or ``ADMIN_PASSWORD`` for scripted setup), and the TOTP secret is
printed once for the operator's authenticator app.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from sqlalchemy import select

from africasignal import operators
from africasignal.db import session_scope
from africasignal.models import Operator


def _read_password() -> str:
    password = os.environ.get("ADMIN_PASSWORD")
    if password is not None:
        return password
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise ValueError("the passwords do not match")
    return first


def create_operator(email: str, role: str) -> int:
    try:
        password = _read_password()
        with session_scope() as session:
            operator, secret = operators.create_operator(session, email, password, role)
            uri = operators.provisioning_uri(operator.email, secret)
            print(f"Created {role} operator {operator.email}.")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("Add this to an authenticator app now; it is not shown again:")
    print(f"  secret: {secret}")
    print(f"  uri:    {uri}")
    return 0


def set_disabled(email: str, disabled: bool) -> int:
    from datetime import UTC, datetime

    with session_scope() as session:
        operator = session.scalars(
            select(Operator).where(Operator.email == operators.normalise_email(email))
        ).first()
        if operator is None:
            print(f"error: no operator with the email {email}", file=sys.stderr)
            return 1
        operator.disabled_at = datetime.now(UTC) if disabled else None
    print(f"{'Disabled' if disabled else 'Enabled'} {email}.")
    return 0


def get_setting(key: str, reveal: bool) -> int:
    """Print one effective setting for scripts on the host. Secrets need ``--reveal``."""
    from africasignal import settings_store

    try:
        defn = settings_store.definition(key)
    except KeyError:
        print(f"error: unknown setting {key!r}", file=sys.stderr)
        return 1
    if defn.secret and not reveal:
        print("error: that is a secret; pass --reveal to print it", file=sys.stderr)
        return 1
    with session_scope() as session:
        value = settings_store.get(session, key)
    if value is None:
        return 2  # not set anywhere: nothing printed
    print(value)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m africasignal.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-operator", help="create an operator account")
    create.add_argument("--email", required=True)
    create.add_argument("--role", choices=operators.ROLES, default="editor")
    for name, help_text in (
        ("disable-operator", "block sign-in"),
        ("enable-operator", "allow sign-in"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--email", required=True)
    getter = sub.add_parser("get-setting", help="print an effective console setting")
    getter.add_argument("key")
    getter.add_argument("--reveal", action="store_true", help="allow printing a secret")
    args = parser.parse_args(argv)
    if args.command == "get-setting":
        return get_setting(args.key, args.reveal)
    if args.command == "create-operator":
        return create_operator(args.email, args.role)
    return set_disabled(args.email, args.command == "disable-operator")


if __name__ == "__main__":
    sys.exit(main())
