"""Roles above and beyond a user's own: the read-only `viewer`, groups, and the effective role (docs/superpowers/specs/2026-11-01-collaboration-identity-design.md).

A user has a direct role (viewer, operator or admin). A group may carry a role too; a member's **effective** role is the highest of
their direct role and the roles of the groups they belong to, so a group can only raise access, never lower it. The effective role is
read on every request, so a change (removing someone from a group, disabling a user) takes effect on the very next request.
"""

from __future__ import annotations

import re
from typing import Optional

from volt import users
from volt.users import UserError

RANK = {"viewer": 0, "operator": 1, "admin": 2}
_GROUP = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


class AccessError(Exception):
    def __init__(self, status: int, code: str, reason: str):
        super().__init__(reason)
        self.status, self.code, self.reason = status, code, reason


def _stamp() -> str:
    from volt.store import _now_precise

    return _now_precise()


def groups_of(store, user_name: str) -> tuple:
    rows = store.conn.execute(
        "SELECT group_name FROM group_member WHERE user_name = ? ORDER BY group_name", (user_name,)).fetchall()
    return tuple(r["group_name"] for r in rows)


def effective_role(store, user_name: str, direct_role: str) -> str:
    """The highest of the direct role and the roles of the user's groups."""
    best = direct_role if direct_role in RANK else "viewer"
    for r in store.conn.execute(
            """SELECT g.role FROM volt_group g JOIN group_member m ON m.group_name = g.name
               WHERE m.user_name = ? AND g.role IS NOT NULL""", (user_name,)).fetchall():
        if r["role"] in RANK and RANK[r["role"]] > RANK[best]:
            best = r["role"]
    return best


def _group(store, name: str):
    row = store.conn.execute("SELECT name, role, created_at FROM volt_group WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise AccessError(404, "no_such_group", f"no such group: {name}")
    return row


def _public_group(store, name: str) -> dict:
    row = _group(store, name)
    members = [r["user_name"] for r in store.conn.execute(
        "SELECT user_name FROM group_member WHERE group_name = ? ORDER BY user_name", (name,)).fetchall()]
    created = row["created_at"]
    return {"name": row["name"], "role": row["role"], "members": members,
            "created_at": created if isinstance(created, str) or created is None else str(created)}


def list_groups(store) -> list:
    return [_public_group(store, r["name"]) for r in store.conn.execute("SELECT name FROM volt_group ORDER BY name").fetchall()]


def create_group(store, name, role: Optional[str] = None) -> dict:
    if not isinstance(name, str) or not _GROUP.fullmatch(name):
        raise AccessError(422, "bad_name", "a group name is 1-100 characters from [A-Za-z0-9._:-]")
    if role is not None and role not in RANK:
        raise AccessError(422, "bad_role", "role must be viewer, operator or admin, or left out")
    if store.conn.execute("SELECT 1 FROM volt_group WHERE name = ?", (name,)).fetchone():
        raise AccessError(409, "group_exists", f"group {name} already exists")
    store.conn.execute("INSERT INTO volt_group (name, role, created_at) VALUES (?, ?, ?)", (name, role, _stamp()))
    store.conn.commit()
    return _public_group(store, name)


def set_members(store, name: str, add=(), remove=(), source: str = "manual") -> dict:
    _group(store, name)
    for user in list(add):
        if not isinstance(user, str) or store.conn.execute("SELECT 1 FROM volt_user WHERE name = ?", (user,)).fetchone() is None:
            raise AccessError(422, "no_such_user", f"no such user: {user}")
    with store.transaction():
        for user in list(add):
            store.conn.execute(
                "INSERT INTO group_member (group_name, user_name, source) VALUES (?, ?, ?) ON CONFLICT (group_name, user_name) DO NOTHING",
                (name, user, source))
        for user in list(remove):
            store.conn.execute("DELETE FROM group_member WHERE group_name = ? AND user_name = ?", (name, user))
    return _public_group(store, name)


def delete_group(store, name: str) -> None:
    _group(store, name)
    with store.transaction():
        store.conn.execute("DELETE FROM group_member WHERE group_name = ?", (name,))
        store.conn.execute("DELETE FROM volt_group WHERE name = ?", (name,))


def set_user_role(store, name: str, role: str, legacy_admin: bool = False) -> dict:
    """Change a user's direct role. Refuses to demote the last enabled admin unless a legacy VOLT_TOKEN still administers."""
    if role not in RANK:
        raise UserError(422, "bad_role", "role must be viewer, operator or admin")
    row = users._row(store, name)
    if row["role"] == "admin" and role != "admin" and row["disabled_at"] is None and not legacy_admin:
        others = store.conn.execute(
            "SELECT 1 FROM volt_user WHERE role = 'admin' AND disabled_at IS NULL AND name <> ? LIMIT 1", (name,)).fetchone()
        if others is None:
            raise UserError(409, "last_admin", "this is the last enabled admin; add another admin first")
    store.conn.execute("UPDATE volt_user SET role = ? WHERE name = ?", (role, name))
    store.conn.commit()
    return users._public(users._row(store, name))
