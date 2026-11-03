"""Comments, mentions and in-app notifications (docs/superpowers/specs/2026-11-01-collaboration-identity-design.md).

A comment belongs to an item, a show or an asset. It is readable, writable and notifiable only by someone who can *view* the show it sits on
(an asset comment has no show, and assets are not show-scoped, so any signed-in person can read it). That is decided in three places and they
agree: serve.py gates the routes, `notify` refuses to write a notification for someone who cannot view the show, and the inbox re-checks access
on every read, so a person removed from a show stops seeing its pings at once and sees them again if they are added back.

Things this module never does:
  * put a comment body anywhere but the `comment` table (not in a notification, the audit table, the log or a URL; a deleted comment's body is
    blanked at rest, not just hidden);
  * say whether a name exists: an `@name` that is not an enabled user who can view the show stays plain text and notifies nobody;
  * touch the on-air path. take, go, hold, release, skip and kill never read these tables, and the callers that clean up after a removed item,
    show or asset ask `available` first, so a missing table is no reason to refuse the removal.

Time is always passed in (`now`, epoch seconds). A person comments as their user name; the legacy desk token comments as `desk`, which is not a
user row and so is never mentioned or notified.
"""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from typing import Optional

from volt import access as acl
from volt.sessions import _epoch, _stamp
from volt.store import _id

log = logging.getLogger("volt.discuss")

KINDS = ("item", "show", "asset")
NOTIFY_KINDS = ("mention", "reply", "lease_taken")
BODY_MAX = 4000
RATE_LIMIT = 30                 # comments per author per window
RATE_WINDOW = 60                # seconds
NOTIFICATION_DAYS = 90          # older notifications are deleted by the runner
MAX_MENTIONS = 20               # people one comment can ping, so one comment cannot flood anyone's bell
INBOX_LIMIT = 100               # what GET /api/notifications returns at most, and the cap on the unread count
_SCAN = 500                     # rows of an inbox read while looking for INBOX_LIMIT visible ones

_MENTION = re.compile(r"(?<![A-Za-z0-9._:@-])@([A-Za-z0-9._:-]{1,100})")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")     # anything but tab, newline and carriage return


class DiscussError(Exception):
    """A refusal. `status` is the HTTP status, `code` is stable, `reason` is for people (and never contains a comment body). Nothing changed."""

    def __init__(self, status: int, code: str, reason: str, **extra):
        super().__init__(reason)
        self.status, self.code, self.reason, self.extra = status, code, reason, extra


def available(store) -> bool:
    """Whether the three tables can be read. Callers that clean up after a removal ask first, so a missing table is skipped, not an error."""
    names = ("comment", "comment_mention", "notification")
    if store.postgres:
        sql = "SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema = current_schema() AND table_name IN ('comment', 'comment_mention', 'notification')"
    else:
        sql = "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name IN ('comment', 'comment_mention', 'notification')"
    return store.conn.execute(sql).fetchone()["n"] == len(names)


# ---- who may see what ---------------------------------------------------------

def _principal(store, name: str):
    """The access-relevant facts of an enabled user, read now: None for a name that is not one (or is disabled)."""
    row = store.conn.execute("SELECT name, role FROM volt_user WHERE name = ? AND disabled_at IS NULL", (name,)).fetchone()
    if row is None:
        return None
    return SimpleNamespace(name=row["name"], role=acl.effective_role(store, row["name"], row["role"]), legacy=False,
                           groups=acl.groups_of(store, row["name"]))


def can_view(store, user: str, show_id: Optional[str]) -> bool:
    """May this enabled user view the show (None = an asset, which any user can)? Read now, from the current roles, groups and access list."""
    who = _principal(store, user)
    if who is None:
        return False
    return show_id is None or acl.show_level(store, who, show_id) is not None


def roster(store, show_id: Optional[str]) -> list:
    """The enabled users who can view the show: who the mention picker offers. Sorted by name."""
    out = []
    for row in store.conn.execute("SELECT name, display_name FROM volt_user WHERE disabled_at IS NULL ORDER BY name").fetchall():
        if show_id is None or can_view(store, row["name"], show_id):
            out.append({"name": row["name"], "displayName": row["display_name"] or row["name"]})
    return out


def parse_mentions(store, body: str, show_id: Optional[str]) -> list:
    """The `@name` tokens of a body that name an enabled user who can view the show, in order of first appearance, once each, at most
    MAX_MENTIONS. Anything else (a nobody, a disabled user, someone who cannot see the show, `a@b.com`) is plain text: no entry, no leak."""
    found: list = []
    for token in _MENTION.findall(body)[:MAX_MENTIONS * 3]:
        name = token
        while name:
            if name in found:
                break
            if store.conn.execute("SELECT 1 FROM volt_user WHERE name = ? AND disabled_at IS NULL", (name,)).fetchone():
                if can_view(store, name, show_id):
                    found.append(name)
                break
            if name[-1] not in ".:-":      # `@bob.` and `@bob:` end a sentence; `@a-b` is a name until proven otherwise
                break
            name = name[:-1]
        if len(found) >= MAX_MENTIONS:
            break
    return found


# ---- writing -----------------------------------------------------------------

def _need_body(body) -> str:
    if not isinstance(body, str):
        raise DiscussError(422, "bad_input", f"body must be text of 1 to {BODY_MAX} characters")
    text = body.strip()
    if not text or len(text) > BODY_MAX or _CONTROL.search(text):
        raise DiscussError(422, "bad_input", f"body must be text of 1 to {BODY_MAX} characters, with no control characters")
    return text


def show_of(store, kind, target) -> Optional[str]:
    """The show a comment target belongs to (None for an asset). 422 for an unknown kind, 404 for a target that does not exist."""
    if kind not in KINDS:
        raise DiscussError(422, "bad_input", f"kind must be one of {', '.join(KINDS)}")
    if not isinstance(target, str) or not target:
        raise DiscussError(422, "bad_input", "target must be an id")
    if kind == "asset":
        if store.conn.execute("SELECT 1 FROM asset WHERE id = ?", (target,)).fetchone() is None:
            raise DiscussError(404, "not_found", f"no such asset: {target}")
        return None
    show = acl.show_of(store, kind, target)
    if show is None:
        raise DiscussError(404, "not_found", f"no such {kind}: {target}")
    return show


def add_comment(store, author: str, kind, target, body, parent=None, *, now: float) -> dict:
    """Post a comment (or one reply to a top-level comment on the same target). The caller has already checked that `author` may view the
    show. Mentions of people who may view it write `comment_mention` rows and a `mention` notification each; a reply tells the other people
    in the thread (`reply`) unless they were already pinged by name. Raises DiscussError; on any error nothing was written."""
    text = _need_body(body)
    if parent is not None and not isinstance(parent, str):
        raise DiscussError(422, "bad_input", "parent must be a comment id")
    show_id = show_of(store, kind, target)
    with store.transaction(f"comment:{author}"):
        recent = store.conn.execute("SELECT COUNT(*) AS n FROM comment WHERE author = ? AND created_at > ?",
                                    (author, _stamp(now - RATE_WINDOW))).fetchone()["n"]
        if recent >= RATE_LIMIT:
            raise DiscussError(429, "rate_limited", f"at most {RATE_LIMIT} comments a minute; wait a moment")
        thread = []
        if parent is not None:
            root = store.conn.execute(
                "SELECT id, target_kind, target_id, parent_id, deleted_at FROM comment WHERE id = ?", (parent,)).fetchone()
            if root is None:
                raise DiscussError(404, "not_found", "no such comment to reply to")
            if root["parent_id"] is not None:
                raise DiscussError(422, "reply_depth", "reply to the first comment of the thread; replies have no replies")
            if (root["target_kind"], root["target_id"]) != (kind, target):
                raise DiscussError(422, "bad_input", "a reply goes on the same item, show or asset as its thread")
            if root["deleted_at"] is not None:
                raise DiscussError(409, "parent_deleted", "that comment was deleted")
            thread = [r["author"] for r in store.conn.execute(
                "SELECT author FROM comment WHERE (id = ? OR parent_id = ?) AND deleted_at IS NULL", (parent, parent)).fetchall()]
        mentions = parse_mentions(store, text, show_id)
        cid = _id()
        store.conn.execute(
            "INSERT INTO comment (id, target_kind, target_id, show_id, author, body, parent_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, kind, target, show_id, author, text, parent, _stamp(now)))
        for name in mentions:
            store.conn.execute("INSERT INTO comment_mention (comment_id, user_name) VALUES (?, ?) ON CONFLICT (comment_id, user_name) DO NOTHING",
                               (cid, name))
        told = {author}
        for name in mentions:
            if name not in told:
                told.add(name)
                notify(store, name, "mention", show_id=show_id, target_kind=kind, target_id=target, comment_id=cid, actor=author, now=now)
        for name in dict.fromkeys(thread):
            if name not in told:
                told.add(name)
                notify(store, name, "reply", show_id=show_id, target_kind=kind, target_id=target, comment_id=cid, actor=author, now=now)
    return _public(store, cid, author)


def resolve(store, comment_id: str, by: str, resolved, *, now: float) -> dict:
    """Mark a thread resolved, or reopen it. Only a top-level comment is a thread; the caller has checked `edit` on the show."""
    if not isinstance(resolved, bool):
        raise DiscussError(422, "bad_input", "resolved must be true or false")
    with store.transaction():
        row = store.conn.execute("SELECT parent_id, deleted_at FROM comment WHERE id = ?", (comment_id,)).fetchone()
        if row is None:
            raise DiscussError(404, "not_found", "no such comment")
        if row["parent_id"] is not None:
            raise DiscussError(422, "not_a_thread", "resolve the first comment of a thread, not a reply")
        if row["deleted_at"] is not None:
            raise DiscussError(409, "deleted", "that comment was deleted")
        if resolved:
            store.conn.execute("UPDATE comment SET resolved_at = ?, resolved_by = ? WHERE id = ?", (_stamp(now), by, comment_id))
        else:
            store.conn.execute("UPDATE comment SET resolved_at = NULL, resolved_by = NULL WHERE id = ?", (comment_id,))
    return _public(store, comment_id, by)


def delete(store, comment_id: str, by: str, is_admin: bool, *, now: float) -> dict:
    """Soft-delete: the author or an admin. The body is blanked (not only hidden), its mentions and the pings it caused are removed.
    Deleting twice is fine. The row stays so a thread keeps its shape and the rate limit still counts it."""
    with store.transaction():
        row = store.conn.execute("SELECT author, deleted_at FROM comment WHERE id = ?", (comment_id,)).fetchone()
        if row is None:
            raise DiscussError(404, "not_found", "no such comment")
        if row["author"] != by and not is_admin:
            raise DiscussError(403, "forbidden_role", "only the author or an admin can delete a comment")
        if row["deleted_at"] is None:
            store.conn.execute("UPDATE comment SET deleted_at = ?, body = '' WHERE id = ?", (_stamp(now), comment_id))
            store.conn.execute("DELETE FROM comment_mention WHERE comment_id = ?", (comment_id,))
            store.conn.execute("DELETE FROM notification WHERE comment_id = ?", (comment_id,))
    return _public(store, comment_id, by)


# ---- reading -------------------------------------------------------------------

def _stamped(value) -> Optional[str]:
    return None if value is None else _stamp(_epoch(value))


def _shape(row, mentions: list, viewer: str) -> dict:
    gone = row["deleted_at"] is not None
    return {"id": row["id"], "kind": row["target_kind"], "target": row["target_id"], "show": row["show_id"], "author": row["author"],
            "body": None if gone else row["body"], "deleted": gone, "parent": row["parent_id"],
            "createdAt": _stamped(row["created_at"]), "resolved": row["resolved_at"] is not None,
            "resolvedAt": _stamped(row["resolved_at"]), "resolvedBy": row["resolved_by"],
            "mentions": [] if gone else mentions, "mine": row["author"] == viewer}


_COLUMNS = "id, target_kind, target_id, show_id, author, body, parent_id, created_at, resolved_at, resolved_by, deleted_at"


def _public(store, comment_id: str, viewer: str) -> dict:
    row = store.conn.execute(f"SELECT {_COLUMNS} FROM comment WHERE id = ?", (comment_id,)).fetchone()
    names = [r["user_name"] for r in store.conn.execute(
        "SELECT user_name FROM comment_mention WHERE comment_id = ? ORDER BY user_name", (comment_id,)).fetchall()]
    return _shape(row, names, viewer)


def list_comments(store, kind: str, target: str, viewer: str) -> list:
    """Every comment on a target, oldest first (a reply follows its thread by time; the page nests them by `parent`). The caller has checked
    that `viewer` may view the show. A deleted comment has no body and no mentions."""
    rows = store.conn.execute(
        f"SELECT {_COLUMNS} FROM comment WHERE target_kind = ? AND target_id = ? ORDER BY created_at, id", (kind, target)).fetchall()
    ids = [r["id"] for r in rows]
    names: dict = {}
    if ids:
        marks = ", ".join("?" for _ in ids)
        for r in store.conn.execute(f"SELECT comment_id, user_name FROM comment_mention WHERE comment_id IN ({marks}) ORDER BY user_name", tuple(ids)).fetchall():
            names.setdefault(r["comment_id"], []).append(r["user_name"])
    return [_shape(r, names.get(r["id"], []), viewer) for r in rows]


def open_counts(store, show_id: str) -> dict:
    """Open (unresolved, undeleted) threads per item of a show: the chip on a rundown row."""
    rows = store.conn.execute(
        "SELECT target_id, COUNT(*) AS n FROM comment WHERE show_id = ? AND target_kind = 'item' AND parent_id IS NULL "
        "AND deleted_at IS NULL AND resolved_at IS NULL GROUP BY target_id", (show_id,)).fetchall()
    return {r["target_id"]: r["n"] for r in rows}


# ---- notifications ---------------------------------------------------------------

def notify(store, user: str, kind: str, *, show_id: Optional[str] = None, target_kind: Optional[str] = None, target_id: Optional[str] = None,
           comment_id: Optional[str] = None, actor: Optional[str] = None, now: float) -> Optional[str]:
    """Write one notification and return its id, or None when `user` is not an enabled user who can view the show: this is the one place
    that decides, so no caller can ping someone about a show they cannot see. Carries ids only, never a comment body."""
    if kind not in NOTIFY_KINDS:
        raise DiscussError(422, "bad_input", f"notification kind must be one of {', '.join(NOTIFY_KINDS)}")
    if not can_view(store, user, show_id):
        return None
    nid = _id()
    with store.transaction():
        store.conn.execute(
            "INSERT INTO notification (id, user_name, kind, show_id, target_kind, target_id, comment_id, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (nid, user, kind, show_id, target_kind, target_id, comment_id, actor, _stamp(now)))
    return nid


def on_lease_taken(store, *, previous: str, actor: str, item: str, show: Optional[str], now: float) -> None:
    """leases.ON_TAKEN: tell the person who lost an edit lease. Skipped (quietly) when the tables are not there; a failure here never undoes
    the takeover because leases.takeover logs and swallows it."""
    if available(store):
        notify(store, previous, "lease_taken", show_id=show, target_kind="item", target_id=item, actor=actor, now=now)


def _label(store, cache: dict, target_kind, target_id, show_id) -> Optional[str]:
    key = (target_kind, target_id, show_id)
    if key not in cache:
        label = None
        if target_kind == "item":
            row = store.conn.execute("SELECT slug FROM item WHERE id = ?", (target_id,)).fetchone()
            label = row["slug"] if row else None
        elif target_kind == "show":
            row = store.conn.execute("SELECT title FROM show WHERE id = ?", (target_id,)).fetchone()
            label = row["title"] if row else None
        cache[key] = label
    return cache[key]


def notifications(store, user: str, unread_only: bool = False, limit: int = INBOX_LIMIT) -> list:
    """One user's notifications, newest first, only those about a show they can view *now*. Another user's rows are never returned."""
    who = _principal(store, user)
    if who is None:
        return []
    sql = ("SELECT id, kind, show_id, target_kind, target_id, comment_id, actor, created_at, read_at FROM notification WHERE user_name = ?"
           + (" AND read_at IS NULL" if unread_only else "") + " ORDER BY created_at DESC, id DESC")
    seen: dict = {}
    labels: dict = {}
    out = []
    for r in store.conn.execute(sql, (user,)).fetchall()[:_SCAN]:
        show = r["show_id"]
        if show is not None:
            if show not in seen:
                seen[show] = acl.show_level(store, who, show) is not None
            if not seen[show]:
                continue
        out.append({"id": r["id"], "kind": r["kind"], "show": show, "targetKind": r["target_kind"], "target": r["target_id"],
                    "comment": r["comment_id"], "actor": r["actor"], "createdAt": _stamped(r["created_at"]), "read": r["read_at"] is not None,
                    "label": _label(store, labels, r["target_kind"], r["target_id"], show)})
        if len(out) >= limit:
            break
    return out


def unread_count(store, user: str) -> int:
    return len(notifications(store, user, unread_only=True))


def mark_read(store, user: str, ids=None, *, now: float) -> int:
    """Mark some of one user's notifications read (`ids`), or all of them (`ids=None`). Ids that are someone else's or do not exist change
    nothing. Returns how many were newly marked."""
    stamp = _stamp(now)
    with store.transaction():
        if ids is None:
            return store.conn.execute("UPDATE notification SET read_at = ? WHERE user_name = ? AND read_at IS NULL", (stamp, user)).rowcount
        total = 0
        wanted = list(dict.fromkeys(i for i in ids if isinstance(i, str)))
        for start in range(0, len(wanted), 100):
            chunk = wanted[start:start + 100]
            marks = ", ".join("?" for _ in chunk)
            total += store.conn.execute(
                f"UPDATE notification SET read_at = ? WHERE user_name = ? AND read_at IS NULL AND id IN ({marks})",
                (stamp, user, *chunk)).rowcount
        return total


def purge_notifications(store, *, now: float, days: int = NOTIFICATION_DAYS) -> int:
    """Delete notifications at least `days` old, read or not (the runner calls this). Comments are kept. Returns how many went."""
    with store.transaction():
        return store.conn.execute("DELETE FROM notification WHERE created_at <= ?", (_stamp(now - days * 86400),)).rowcount


# ---- going with their subject -----------------------------------------------------

def _forget(store, comment_where: str, notice_where: str, params: tuple) -> None:
    store.conn.execute(f"DELETE FROM notification WHERE comment_id IN (SELECT id FROM comment WHERE {comment_where})", params)
    store.conn.execute(f"DELETE FROM notification WHERE {notice_where}", params)
    store.conn.execute(f"DELETE FROM comment_mention WHERE comment_id IN (SELECT id FROM comment WHERE {comment_where})", params)
    store.conn.execute(f"DELETE FROM comment WHERE {comment_where}", params)


def forget_item(store, item_id: str) -> None:
    """Remove an item's comments, mentions and notifications with the item (the caller holds the transaction). Skipped if the tables are absent."""
    if available(store):
        _forget(store, "target_kind = 'item' AND target_id = ?", "target_kind = 'item' AND target_id = ?", (item_id,))


def forget_show(store, show_id: str) -> None:
    """Remove every comment on a show and on its items (they carry the show id), and every notification about it."""
    if available(store):
        _forget(store, "show_id = ?", "show_id = ?", (show_id,))


def forget_asset(store, asset_id: str) -> None:
    if available(store):
        _forget(store, "target_kind = 'asset' AND target_id = ?", "target_kind = 'asset' AND target_id = ?", (asset_id,))
