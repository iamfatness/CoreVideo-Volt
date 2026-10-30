"""Per-user tokens with two roles: operator (runs the show) and admin (operator, plus users and destructive actions).

A token is 32 random bytes, shown once when it is made (add, rotate) and stored only as its sha256. The hash is
looked up by equality and then compared in constant time. Disabling or rotating a user takes effect on the very
next request, because every request reads the table.

    volt user add NAME --role operator|admin     print the new token once
    volt user list [--json]
    volt user disable NAME | enable NAME
    volt user rotate NAME                        print the replacement token once

The legacy VOLT_TOKEN is not a row here; serve.py treats it as an admin named `desk`.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass
from typing import Optional

ROLES = ("operator", "admin")
NAME = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")  # the same shape as an audit actor
RESERVED = {"desk"}  # the audit name of the legacy VOLT_TOKEN
PREFIX = "vtk_"


class UserError(Exception):
    def __init__(self, status: int, code: str, reason: str):
        super().__init__(reason)
        self.status, self.code, self.reason = status, code, reason


@dataclass(frozen=True)
class User:
    name: str
    role: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def _stamp() -> str:
    from volt.store import _now_precise

    return _now_precise()


def _text(value) -> Optional[str]:
    if value is None or isinstance(value, str):
        return value
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(value, "strftime") else str(value)


def _public(row) -> dict:
    return {"name": row["name"], "role": row["role"], "created_at": _text(row["created_at"]),
            "disabled_at": _text(row["disabled_at"])}


def _row(store, name: str):
    row = store.conn.execute(
        "SELECT name, role, created_at, disabled_at FROM volt_user WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise UserError(404, "no_such_user", f"no such user: {name}")
    return row


def add(store, name: str, role: str) -> tuple[dict, str]:
    """Create an enabled user. Returns (public row, plaintext token); the token is not recoverable afterwards."""
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in RESERVED:
        raise UserError(422, "bad_name", "name must be 1-100 characters matching [A-Za-z0-9._:-] and not be 'desk'")
    if role not in ROLES:
        raise UserError(422, "bad_role", "role must be operator or admin")
    if store.conn.execute("SELECT 1 FROM volt_user WHERE name = ?", (name,)).fetchone():
        raise UserError(409, "user_exists", f"user {name} already exists")
    token = new_token()
    try:
        store.conn.execute(
            "INSERT INTO volt_user (name, role, token_hash, created_at, disabled_at) VALUES (?, ?, ?, ?, NULL)",
            (name, role, hash_token(token), _stamp()))
        store.conn.commit()
    except store.IntegrityError:
        store.conn.rollback()
        raise UserError(409, "user_exists", f"user {name} already exists") from None
    return _public(_row(store, name)), token


def list_users(store) -> list:
    rows = store.conn.execute(
        "SELECT name, role, created_at, disabled_at FROM volt_user ORDER BY name").fetchall()
    return [_public(r) for r in rows]


def authenticate(store, token: str) -> Optional[User]:
    """The enabled user this token belongs to, else None."""
    if not token:
        return None
    digest = hash_token(token)
    row = store.conn.execute(
        "SELECT name, role, token_hash FROM volt_user WHERE token_hash = ? AND disabled_at IS NULL",
        (digest,)).fetchone()
    if row is None or not hmac.compare_digest(row["token_hash"].encode("ascii"), digest.encode("ascii")):
        return None
    return User(row["name"], row["role"])


def any_users(store) -> bool:
    return store.conn.execute("SELECT 1 FROM volt_user LIMIT 1").fetchone() is not None


def has_enabled_admin(store) -> bool:
    return store.conn.execute(
        "SELECT 1 FROM volt_user WHERE role = 'admin' AND disabled_at IS NULL LIMIT 1").fetchone() is not None


def disable(store, name: str, legacy_admin: bool = False) -> dict:
    """Revoke at once. Refuses to remove the last enabled admin unless a legacy VOLT_TOKEN still administers."""
    row = _row(store, name)
    if row["disabled_at"] is None and row["role"] == "admin" and not legacy_admin:
        others = store.conn.execute(
            "SELECT 1 FROM volt_user WHERE role = 'admin' AND disabled_at IS NULL AND name <> ? LIMIT 1",
            (name,)).fetchone()
        if others is None:
            raise UserError(409, "last_admin", "this is the last enabled admin; add another admin first")
    if row["disabled_at"] is None:
        store.conn.execute("UPDATE volt_user SET disabled_at = ? WHERE name = ?", (_stamp(), name))
        store.conn.commit()
    return _public(_row(store, name))


def enable(store, name: str) -> dict:
    _row(store, name)
    store.conn.execute("UPDATE volt_user SET disabled_at = NULL WHERE name = ?", (name,))
    store.conn.commit()
    return _public(_row(store, name))


def rotate(store, name: str) -> str:
    """Replace the token; the old one stops working at once. Returns the new plaintext token."""
    row = _row(store, name)
    if row["disabled_at"] is not None:
        raise UserError(409, "user_disabled", f"user {name} is disabled; enable it first")
    token = new_token()
    store.conn.execute("UPDATE volt_user SET token_hash = ? WHERE name = ?", (hash_token(token), name))
    store.conn.commit()
    return token


def db_has_enabled_admin(db: str) -> bool:
    """Read-only check used before the server binds beyond loopback. Any failure counts as no."""
    from volt import pg

    try:
        if pg.is_dsn(db):
            from volt.store import Store

            store = Store(db)
            try:
                return has_enabled_admin(store)
            finally:
                store.close()
        import sqlite3
        from pathlib import Path

        if not Path(db).is_file():
            return False
        conn = sqlite3.connect(f"file:{Path(db).resolve().as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            return conn.execute(
                "SELECT 1 FROM volt_user WHERE role = 'admin' AND disabled_at IS NULL LIMIT 1").fetchone() is not None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - missing table, unreachable database: not provably an admin
        return False


def _default_db() -> str:
    from volt import preflight

    return os.environ.get("VOLT_DB") or preflight.default_db()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="volt user", description="Manage per-user tokens (operator and admin roles).")
    ap.add_argument("--db", default=None, help="SQLite path or postgres:// DSN (default: VOLT_DB)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add", help="create a user and print its token once")
    p.add_argument("name")
    p.add_argument("--role", required=True, choices=ROLES)
    p = sub.add_parser("list", help="list users (never tokens)")
    p.add_argument("--json", action="store_true")
    for cmd, text in (("disable", "revoke a user's token at once"), ("enable", "restore a disabled user"),
                      ("rotate", "replace a user's token and print the new one once")):
        sub.add_parser(cmd, help=text).add_argument("name")
    args = ap.parse_args(argv)

    from volt.store import Store

    store = Store(args.db or _default_db())
    try:
        if args.cmd == "add":
            user, token = add(store, args.name, args.role)
            print(f"user {user['name']} ({user['role']}) created")
            print(f"token {token}")
            print("this token is shown once and cannot be recovered; `volt user rotate` replaces it")
        elif args.cmd == "list":
            rows = list_users(store)
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                for r in rows:
                    state = "disabled " + r["disabled_at"] if r["disabled_at"] else "enabled"
                    print(f"{r['name']:<24} {r['role']:<9} {state}  created {r['created_at']}")
                if not rows:
                    print("no users")
        elif args.cmd == "disable":
            disable(store, args.name, legacy_admin=bool(os.environ.get("VOLT_TOKEN")))
            print(f"user {args.name} disabled")
        elif args.cmd == "enable":
            enable(store, args.name)
            print(f"user {args.name} enabled")
        else:
            token = rotate(store, args.name)
            print(f"user {args.name} token rotated")
            print(f"token {token}")
            print("this token is shown once and cannot be recovered")
        return 0
    except UserError as exc:
        print(f"volt: {exc.code}: {exc.reason}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
