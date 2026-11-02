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
        store.conn.execute("DELETE FROM oidc_group_map WHERE group_name = ?", (name,))
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


# ---- sign-in provider mapping (volt/oidc.py) ---------------------------------

_PROVIDER = re.compile(r"^[a-z0-9_-]{1,40}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def claims_of(store, group_name: str) -> list:
    """The provider claim values an admin mapped to this group, as [{provider, value}]."""
    return [{"provider": r["provider"], "value": r["claim_value"]} for r in store.conn.execute(
        "SELECT provider, claim_value FROM oidc_group_map WHERE group_name = ? ORDER BY provider, claim_value", (group_name,)).fetchall()]


def set_claim_map(store, group_name: str, provider, value, remove: bool = False) -> dict:
    """Map (or unmap) one value of a provider's group claim to a group. Only an admin does this; an unmapped claim value does nothing."""
    _group(store, group_name)
    if not isinstance(provider, str) or not _PROVIDER.fullmatch(provider):
        raise AccessError(422, "bad_input", "provider must be 1-40 characters from [a-z0-9_-]")
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or _CONTROL.search(value):
        raise AccessError(422, "bad_input", "value must be 1-256 characters with no control characters")
    if remove:
        store.conn.execute("DELETE FROM oidc_group_map WHERE provider = ? AND claim_value = ? AND group_name = ?", (provider, value, group_name))
    else:
        store.conn.execute(
            "INSERT INTO oidc_group_map (provider, claim_value, group_name) VALUES (?, ?, ?) ON CONFLICT (provider, claim_value, group_name) DO NOTHING",
            (provider, value, group_name))
    store.conn.commit()
    return {"name": group_name, "claims": claims_of(store, group_name)}


def clean_email(value):
    """A stored address: stripped, one @, no spaces or control characters, at most 254 characters. None when it is not one."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    local, at, domain = value.partition("@")
    if not at or not local or not domain or "@" in domain or len(value) > 254 or re.search(r"[\s\x00-\x1f\x7f]", value):
        return None
    return value


def set_user_email(store, name: str, email) -> dict:
    """Pre-link a user to the address their provider will send (None clears it). One address belongs to one user (compared without case)."""
    users._row(store, name)
    if email is not None:
        shown = clean_email(email)
        if shown is None:
            raise AccessError(422, "bad_input", "email must look like name@domain, with no spaces, at most 254 characters")
        taken = store.conn.execute(
            "SELECT name FROM volt_user WHERE lower(email) = ? AND name <> ? LIMIT 1", (shown.lower(), name)).fetchone()
        if taken is not None:
            raise AccessError(409, "email_taken", "another user already has that email")
        email = shown
    store.conn.execute("UPDATE volt_user SET email = ? WHERE name = ?", (email, name))
    store.conn.commit()
    return users._public(users._row(store, name))
