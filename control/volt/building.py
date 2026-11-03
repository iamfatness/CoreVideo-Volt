"""Show building: the writes an operator needs to make a show without a developer.

Create a show, add / rename / reorder / remove rundown items, detach a span, add / remove cues, and ingest a
file from an allowed folder. serve.py only reads the body, calls these, and maps BuildError to HTTP.

Every change that depends on what it just read (the next position, whether an item is still ready, the
current order) runs in one `store.transaction(show_lock(show_id))`, the same lock go, take, skip and kill use, and
ends in a write that re-states its condition. So a build edit and a take that race get one outcome, in this
process or another, on SQLite and on Postgres. A refusal changes nothing.

Only an item that is `ready` can be edited. Once it is on air or has aired, the as-run log is the record of it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from volt import cues as cue_mod
from volt import discuss, leases, story
from volt.dispatch import HANDLERS
from volt.disk import DiskLowError
from volt.ingest import IngestError, request as ingest_request
from volt.rundown import show_lock
from volt.store import Store, _id, _now

log = logging.getLogger("volt.building")

CUE_ROOM = "corevideo.pro"
# Commands whose adapter has been run against a live core and recorded in rooms.md.
# show-input.take has a handler and FakeRoom evidence; it is not live-proven until a Program take is.
LIVE_PROVEN = frozenset({"overlay.take", "record.start", "show-input.take", "stream.start"})


class BuildError(Exception):
    """A refusal. `status` is the HTTP status, `code` is stable, `reason` is for people. Nothing changed."""

    def __init__(self, status: int, code: str, reason: str, **extra):
        super().__init__(reason)
        self.status, self.code, self.reason, self.extra = status, code, reason, extra


# ---- shows and items ------------------------------------------------------------------------------------

def order_version(store: Store, show_id: str) -> str:
    """A token for the show's item order and membership. It changes when an item is added, removed or moved,
    so a reorder that quotes it fails if anyone changed the list since the page loaded."""
    rows = store.conn.execute(
        "SELECT id FROM item WHERE show_id = ? ORDER BY position, id", (show_id,)).fetchall()
    return hashlib.sha256(",".join(r["id"] for r in rows).encode()).hexdigest()[:16]


def create_show(store: Store, title: str, show_id: Optional[str] = None) -> tuple[str, bool]:
    """Returns (show id, created). With a caller-chosen `show_id` this is idempotent: the same id and title
    again is `created=False`; the same id with another title is a conflict (the id is taken)."""
    new_id = show_id or _id()
    with store.transaction():
        cur = store.conn.execute(
            "INSERT INTO show (id, title, created_at) VALUES (?, ?, ?) ON CONFLICT (id) DO NOTHING",
            (new_id, title, _now()))
        created = cur.rowcount == 1
        if not created:
            row = store.conn.execute("SELECT title FROM show WHERE id = ?", (new_id,)).fetchone()
            if row is None or row["title"] != title:
                raise BuildError(409, "id_conflict", f"show id {new_id} is already used by another show")
    return new_id, created


def _item_row(store: Store, item_id: str):
    return store.conn.execute(
        "SELECT show_id, status, slug, script, as_run_in FROM item WHERE id = ?", (item_id,)).fetchone()


def _need_ready(row, action: str) -> None:
    if row["status"] != "ready":
        raise BuildError(409, "not_editable", f"cannot {action} a {row['status']} item")


def add_item(store: Store, show_id: str, slug: str, script: str = "",
             item_id: Optional[str] = None) -> tuple[str, bool]:
    """Append an item. Returns (item id, created). A caller-chosen `item_id` makes a retry harmless."""
    new_id = item_id or _id()
    with store.transaction(show_lock(show_id)):
        if item_id:
            row = store.conn.execute(
                "SELECT show_id, slug, script FROM item WHERE id = ?", (item_id,)).fetchone()
            if row is not None:
                if (row["show_id"], row["slug"], row["script"] or "") != (show_id, slug, script):
                    raise BuildError(409, "id_conflict", f"item id {item_id} is already used by another item")
                return item_id, False
        position = store.conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM item WHERE show_id = ?", (show_id,)).fetchone()[0]
        store.conn.execute(
            """INSERT INTO item (id, show_id, position, slug, script, status, span_id, created_at)
               VALUES (?, ?, ?, ?, ?, 'ready', NULL, ?)""",
            (new_id, show_id, position, slug, script, _now()))
    return new_id, True


def item_version(store: Store, item_id: str) -> str:
    """A token for an item's content (its slug and script): sha256, first 16 hex, like `order_version`. It changes when either changes, so a
    write that quotes it fails if someone else saved since the page loaded. No column: it is computed from what is there."""
    row = _item_row(store, item_id)
    if row is None:
        raise BuildError(404, "not_found", f"no such item: {item_id}")
    return hashlib.sha256(f"{row['slug']}\x00{row['script'] or ''}".encode("utf-8")).hexdigest()[:16]


def guard_edit(store: Store, item_id: str, actor: str, client_id: Optional[str], now: Optional[float]) -> None:
    """Refuse (409 `leased`) a write to an item whose live lease belongs to someone else. Call it inside the show's transaction. With the lease
    tables unreadable it passes: a lease is advisory, and the versioned write still protects the text. Never used by the on-air path."""
    try:
        if not leases.available(store):
            return
        leases.check_write(store, "item", item_id, actor, client_id, time.time() if now is None else now)
    except leases.LeaseError as exc:
        raise BuildError(exc.status, exc.code, exc.reason, **exc.extra) from None
    except Exception:  # noqa: BLE001 - the lease tables cannot be read: no lease to honour; the version check still guards the text
        log.warning("lease check skipped: the lease tables cannot be read", exc_info=True)


def rename_item(store: Store, item_id: str, slug: Optional[str], script: Optional[str], actor: str = "desk",
                version: Optional[str] = None, client_id: Optional[str] = None, now: Optional[float] = None) -> None:
    """Set the slug and/or script of a ready item. The change goes through story.set_script (same revision history,
    approval drop and limits as the script route), with no base revision: the old callers never sent one.

    Two optional guards, both checked inside the same transaction as the write: a live lease held by someone else (or another tab of the same
    person) is 409 `leased`; a `version` that no longer matches the item's content is 409 `stale_item` carrying the current text. With neither
    a lease nor a `version` (older clients, scripts) it behaves as it always did. A refusal changes nothing."""
    row = _item_row(store, item_id)
    if row is None:
        raise BuildError(404, "not_found", f"no such item: {item_id}")
    with store.transaction(show_lock(row["show_id"])):
        row = _item_row(store, item_id)
        if row is None:
            raise BuildError(404, "not_found", f"no such item: {item_id}")
        _need_ready(row, "rename")
        guard_edit(store, item_id, actor, client_id, now)
        if version is not None and version != item_version(store, item_id):
            raise BuildError(409, "stale_item", "someone changed this item since you opened it; nothing was saved",
                             current={"slug": row["slug"], "script": row["script"] or "", "version": item_version(store, item_id)})
        story.set_script(store, item_id, script=script, slug=slug, base_rev=None, actor=actor)


_PLAYED = ("on air", "aired")


def reorder(store: Store, show_id: str, ids: list[str], version: Optional[str] = None) -> None:
    """Set the order, atomically and only if `version` (when given) still matches. The ids must be exactly the
    show's items. Items that are on air or aired keep their relative order, and every ready item must stay
    after all of them: go walks forward from the item on air, so a ready item moved above it could never air."""
    with store.transaction(show_lock(show_id)):
        rows = store.conn.execute(
            "SELECT id, status FROM item WHERE show_id = ? ORDER BY position, id", (show_id,)).fetchall()
        if version is not None and version != order_version(store, show_id):
            raise BuildError(409, "stale_order",
                             "the rundown changed since you loaded it; reload and try again")
        status = {r["id"]: r["status"] for r in rows}
        if len(set(ids)) != len(ids) or set(ids) != set(status):
            raise BuildError(422, "bad_input", "items must list every item of the show exactly once")
        played_before = [r["id"] for r in rows if r["status"] in _PLAYED]
        played_after = [i for i in ids if status[i] in _PLAYED]
        last_played = max((n for n, i in enumerate(ids) if status[i] in _PLAYED), default=-1)
        if played_after != played_before or any(
                status[i] == "ready" and n < last_played for n, i in enumerate(ids)):
            raise BuildError(409, "order_locked",
                             "ready items cannot move above an item that is on air or has aired")
        for position, item_id in enumerate(ids, start=1):
            store.conn.execute(
                "UPDATE item SET position = ? WHERE id = ? AND show_id = ?", (position, item_id, show_id))


_REMOVABLE = ("ready", "killed", "skipped")


def remove_item(store: Store, item_id: str) -> str:
    """Delete an item that has not aired, with its cues. Returns the show id. An item that is on air or has aired
    is refused: it is the as-run record. (kill keeps an item in the list as killed; this takes it out.)"""
    row = _item_row(store, item_id)
    if row is None:
        raise BuildError(404, "not_found", f"no such item: {item_id}")
    with store.transaction(show_lock(row["show_id"])):
        row = _item_row(store, item_id)
        if row is None:
            raise BuildError(404, "not_found", f"no such item: {item_id}")
        if row["status"] not in _REMOVABLE or row["as_run_in"] is not None:
            raise BuildError(409, "not_removable", f"cannot remove a {row['status']} item")
        store.conn.execute("DELETE FROM cue WHERE item_id = ?", (item_id,))
        cur = store.conn.execute(
            "DELETE FROM item WHERE id = ? AND status IN ('ready', 'killed', 'skipped') AND as_run_in IS NULL",
            (item_id,))
        if cur.rowcount != 1:
            raise BuildError(409, "not_removable", "the item changed while it was being removed")
        story.forget(store, item_id)
        discuss.forget_item(store, item_id)        # its comments go with it (quietly skipped if those tables are not there)
        return row["show_id"]


def _archive_items(store: Store, show_id: str) -> int:
    """Snapshot every story of a show into story_archive. Called inside delete_show's transaction: if this fails the delete fails."""
    title = store.conn.execute("SELECT title FROM show WHERE id = ?", (show_id,)).fetchone()["title"]
    at = _now()
    rows = store.conn.execute(
        "SELECT id, slug, script, story_status, approved_by, approved_at FROM item WHERE show_id = ? ORDER BY position, id", (show_id,)).fetchall()
    for r in rows:      # delete_show only reaches here when nothing aired, so there is no aired_at to keep
        store.conn.execute(
            "INSERT INTO story_archive (id, original_item_id, show_title, slug, script, story_status, approved_by, approved_at, aired_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (_id(), r["id"], title, r["slug"], r["script"] or "", r["story_status"] or "draft", r["approved_by"], r["approved_at"], at))
    return len(rows)


def delete_show(store: Store, show_id: str) -> dict:
    """Delete a show that has not played, with its items and cues. Returns {"items": n, "cues": n}.

    Refused (409 `show_not_deletable`, nothing changed) when any item is on air or has aired (status `on air` /
    `aired`, or an as-run time is set): the as-run log is the record. Otherwise the ready, killed and skipped
    items and their cues go in one transaction under the show lock, the one go and take hold. Spans and assets
    are not touched. `kill` and `skip` leave items behind with no as-run, so those are removable."""
    if store.conn.execute("SELECT 1 FROM show WHERE id = ?", (show_id,)).fetchone() is None:
        raise BuildError(404, "not_found", f"no such show: {show_id}")
    with store.transaction(show_lock(show_id)):
        if store.conn.execute("SELECT 1 FROM show WHERE id = ?", (show_id,)).fetchone() is None:
            raise BuildError(404, "not_found", f"no such show: {show_id}")
        played = store.conn.execute(
            "SELECT id, status FROM item WHERE show_id = ? AND (status NOT IN ('ready', 'killed', 'skipped') "
            "OR as_run_in IS NOT NULL OR as_run_out IS NOT NULL) ORDER BY position, id", (show_id,)).fetchall()
        if played:
            raise BuildError(409, "show_not_deletable",
                             f"{len(played)} item(s) are on air or have aired; the as-run log keeps the show",
                             items=[{"id": r["id"], "status": r["status"]} for r in played])
        _archive_items(store, show_id)      # the stories outlive the show; a failure here fails the whole delete
        store.conn.execute("DELETE FROM story_rev WHERE item_id IN (SELECT id FROM item WHERE show_id = ?)", (show_id,))
        store.conn.execute("DELETE FROM story_event WHERE item_id IN (SELECT id FROM item WHERE show_id = ?)", (show_id,))
        cues = store.conn.execute(
            "DELETE FROM cue WHERE item_id IN (SELECT id FROM item WHERE show_id = ?)", (show_id,)).rowcount
        items = store.conn.execute(
            "DELETE FROM item WHERE show_id = ? AND status IN ('ready', 'killed', 'skipped') "
            "AND as_run_in IS NULL AND as_run_out IS NULL", (show_id,)).rowcount
        left = store.conn.execute("SELECT COUNT(*) FROM item WHERE show_id = ?", (show_id,)).fetchone()[0]
        if left:  # an item changed state between the check and the delete: undo it all
            raise BuildError(409, "show_not_deletable", "an item changed while the show was being deleted")
        # Its access list goes with it: a later show given the same id must not inherit who could see this one.
        store.conn.execute("DELETE FROM show_acl WHERE show_id = ?", (show_id,))
        discuss.forget_show(store, show_id)        # and its comments, and those on its items
        store.conn.execute("DELETE FROM show WHERE id = ?", (show_id,))
        return {"items": items, "cues": cues}


def detach_span(store: Store, item_id: str) -> tuple[str, bool]:
    """Take the span off a ready item. Returns (show id, whether one was attached)."""
    row = store.conn.execute("SELECT show_id FROM item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise BuildError(404, "not_found", f"no such item: {item_id}")
    with store.transaction(show_lock(row["show_id"])):
        cur_row = store.conn.execute("SELECT status, span_id FROM item WHERE id = ?", (item_id,)).fetchone()
        if cur_row is None:
            raise BuildError(404, "not_found", f"no such item: {item_id}")
        if cur_row["status"] != "ready":
            raise BuildError(409, "not_attachable", f"cannot detach media from a {cur_row['status']} item")
        cur = store.conn.execute(
            "UPDATE item SET span_id = NULL WHERE id = ? AND status = 'ready' AND span_id IS NOT NULL", (item_id,))
        return row["show_id"], cur.rowcount == 1


# ---- cues -----------------------------------------------------------------------------------------------

def cue_commands() -> list[dict]:
    """Every command the corevideo.pro room accepts (and the hyperdeck commands when VOLT_HYPERDECK_HOSTS names a deck).

    `adapter` is whether dispatch has a handler. `verified` is whether that handler has been
    proven against a live core (rooms.md). A cue without an adapter can be stored and armed,
    but a take stops at it with `cue_unsupported`. A cue with an adapter that is not live-proven
    still runs on take; a command leaves this list only when rooms.md records a live run.
    """
    import os
    from volt import hyperdeck
    out = [{"room": CUE_ROOM, "command": c, "adapter": c in HANDLERS, "verified": c in LIVE_PROVEN}
           for c in sorted(cue_mod.COREVIDEO_COMMANDS)]
    if os.environ.get("VOLT_HYPERDECK_HOSTS"):     # offered only where a deck is configured; a stored deck cue is accepted regardless
        out += [{"room": hyperdeck.ROOM, "command": c, "adapter": True, "verified": False} for c in hyperdeck.DECK_COMMANDS]
    from volt import adapters
    for kind, (var, _module) in sorted(adapters.REGISTRY.items()):
        if os.environ.get(var):                    # only the kinds the environment configures; nothing set, nothing listed
            out += [{"room": kind, "command": c, "adapter": True, "verified": False, "previewable": spec.prepare is not None}
                    for c, spec in sorted(adapters.actions(kind).items())]
    return out


def add_cue(store: Store, item_id: str, room: str, command: str, payload: str) -> tuple[str, str, bool]:
    """Returns (cue id, show id, verified) — verified means live-proven, not merely that an adapter exists."""
    from volt import adapters, hyperdeck
    kinds = adapters.kinds()
    if room not in (CUE_ROOM, hyperdeck.ROOM, *kinds):
        raise BuildError(422, "bad_input", f"room must be one of {', '.join([CUE_ROOM, hyperdeck.ROOM, *kinds])}: they are the rooms with a command list")
    if room in kinds:
        known = set(adapters.actions(room))
    else:
        known = cue_mod.COREVIDEO_COMMANDS if room == CUE_ROOM else set(hyperdeck.DECK_COMMANDS)
    if command not in known:
        raise BuildError(422, "bad_command", f"unknown {room} command: {command}", commands=sorted(known))
    if room == hyperdeck.ROOM or room in kinds:
        try:
            hyperdeck.parse_payload(payload) if room == hyperdeck.ROOM else adapters.actions(room)[command].parse(payload)
        except ValueError as exc:
            raise BuildError(422, "bad_input", str(exc)) from None
    row = store.conn.execute("SELECT show_id FROM item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise BuildError(404, "not_found", f"no such item: {item_id}")
    with store.transaction(show_lock(row["show_id"])):
        cur_row = store.conn.execute("SELECT status FROM item WHERE id = ?", (item_id,)).fetchone()
        if cur_row is None:
            raise BuildError(404, "not_found", f"no such item: {item_id}")
        _need_ready(cur_row, "add a cue to")
        cue_id = cue_mod.add_cue(store, item_id, room, command, payload)
    return cue_id, row["show_id"], command in LIVE_PROVEN


def remove_cue(store: Store, cue_id: str) -> tuple[str, str]:
    """Returns (item id, show id). Only an unfired cue on a ready item can go."""
    row = store.conn.execute(
        "SELECT c.item_id, i.show_id FROM cue c JOIN item i ON i.id = c.item_id WHERE c.id = ?", (cue_id,)).fetchone()
    if row is None:
        raise BuildError(404, "not_found", f"no such cue: {cue_id}")
    with store.transaction(show_lock(row["show_id"])):
        state = store.conn.execute(
            "SELECT c.status AS cue_status, i.status AS item_status FROM cue c JOIN item i ON i.id = c.item_id "
            "WHERE c.id = ?", (cue_id,)).fetchone()
        if state is None:
            raise BuildError(404, "not_found", f"no such cue: {cue_id}")
        _need_ready({"status": state["item_status"]}, "remove a cue from")
        if state["cue_status"] == "fired":
            raise BuildError(409, "cue_fired", "that cue already fired in the room; it stays on the record")
        store.conn.execute("DELETE FROM cue WHERE id = ? AND status != 'fired'", (cue_id,))
    return row["item_id"], row["show_id"]


# ---- ingest ---------------------------------------------------------------------------------------------

def ingest_roots(raw: Optional[str] = None) -> list[Path]:
    """The folders an operator may ingest from: VOLT_INGEST_ROOTS, split on the OS path separator."""
    raw = os.environ.get("VOLT_INGEST_ROOTS", "") if raw is None else raw
    return [Path(p).resolve() for p in raw.split(os.pathsep) if p.strip()]


def _inside(path: Path, root: Path) -> bool:
    try:
        a, b = os.path.normcase(str(path)), os.path.normcase(str(root))
        return os.path.commonpath([a, b]) == b
    except ValueError:  # another drive
        return False


def ingest_allowed(store: Store, path: str, quarantine: Path, owner: str,
                   show_name: Optional[str], camera: Optional[str]) -> dict:
    """Enqueue ingest of one file from an allowed folder (mint, job; the runner copies). `path` is only ever
    opened after it is resolved (symlinks and `..` followed) and found inside an allowed root. A path outside
    every root is refused the same way whether or not it exists."""
    roots = ingest_roots()
    if not roots:
        raise BuildError(503, "ingest_disabled", "no ingest folder is configured (set VOLT_INGEST_ROOTS)")
    if "\x00" in path or not os.path.isabs(path):
        raise BuildError(422, "bad_input", "path must be an absolute file path")
    resolved = Path(path).resolve()
    if not any(_inside(resolved, root) for root in roots):
        raise BuildError(403, "path_not_allowed", "that path is not inside an allowed ingest folder")
    if not resolved.is_file():
        raise BuildError(404, "not_found", "no such file")
    if not shutil.which("ffprobe"):
        raise BuildError(503, "engine_missing", "ffprobe is not installed on the server")
    st = resolved.stat()
    key = f"file:{os.path.normcase(str(resolved))}:{st.st_size}:{st.st_mtime_ns}"
    try:
        result = ingest_request(
            store, resolved, owner=owner, quarantine=quarantine, source_key=key,
            show_name=show_name, camera=camera)
    except IngestError as exc:
        if isinstance(exc.__cause__, DiskLowError):  # not a missing file: the disk is too full to take the copy
            raise BuildError(507, "disk_low", str(exc)) from None
        raise BuildError(404, "not_found", str(exc)) from None
    jobs = store.conn.execute(
        "SELECT id, type, status, error FROM job WHERE asset_id = ? ORDER BY created_at, id", (result.asset_id,)).fetchall()
    return {"asset": result.asset_id, "essence": result.essence_id or None, "minted": result.minted,
            "status": result.status, "job": result.job_id, "quarantined": result.status == "quarantine",
            "jobs": [{"id": j["id"], "type": j["type"], "status": j["status"], "error": j["error"]} for j in jobs]}
