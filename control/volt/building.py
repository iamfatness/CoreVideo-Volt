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
import os
import shutil
from pathlib import Path
from typing import Optional

from volt import cues as cue_mod
from volt.dispatch import HANDLERS
from volt.disk import DiskLowError
from volt.ingest import IngestError, ingest_file
from volt.rundown import show_lock
from volt.store import Store, _id, _now

CUE_ROOM = "corevideo.pro"


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


def rename_item(store: Store, item_id: str, slug: Optional[str], script: Optional[str]) -> None:
    row = _item_row(store, item_id)
    if row is None:
        raise BuildError(404, "not_found", f"no such item: {item_id}")
    with store.transaction(show_lock(row["show_id"])):
        row = _item_row(store, item_id)
        if row is None:
            raise BuildError(404, "not_found", f"no such item: {item_id}")
        _need_ready(row, "rename")
        store.conn.execute(
            "UPDATE item SET slug = ?, script = ? WHERE id = ? AND status = 'ready'",
            (row["slug"] if slug is None else slug, row["script"] if script is None else script, item_id))


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
        return row["show_id"]


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
    """Every command the corevideo.pro room accepts, and whether dispatch has a verified adapter for it. A cue for
    an unverified command can be stored and armed, but a take stops at it with `cue_unsupported`."""
    return [{"room": CUE_ROOM, "command": c, "verified": c in HANDLERS}
            for c in sorted(cue_mod.COREVIDEO_COMMANDS)]


def add_cue(store: Store, item_id: str, room: str, command: str, payload: str) -> tuple[str, str, bool]:
    """Returns (cue id, show id, verified)."""
    if room != CUE_ROOM:
        raise BuildError(422, "bad_input", f"room must be {CUE_ROOM}: it is the only room with a command list")
    if command not in cue_mod.COREVIDEO_COMMANDS:
        raise BuildError(422, "bad_command", f"unknown {CUE_ROOM} command: {command}",
                         commands=sorted(cue_mod.COREVIDEO_COMMANDS))
    row = store.conn.execute("SELECT show_id FROM item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise BuildError(404, "not_found", f"no such item: {item_id}")
    with store.transaction(show_lock(row["show_id"])):
        cur_row = store.conn.execute("SELECT status FROM item WHERE id = ?", (item_id,)).fetchone()
        if cur_row is None:
            raise BuildError(404, "not_found", f"no such item: {item_id}")
        _need_ready(cur_row, "add a cue to")
        cue_id = cue_mod.add_cue(store, item_id, room, command, payload)
    return cue_id, row["show_id"], command in HANDLERS


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
    """Ingest one file from an allowed folder through ingest.ingest_file (mint, quarantine copy, probe, checksum,
    follow-on jobs). `path` is only ever opened after it is resolved (symlinks and `..` followed) and found inside
    an allowed root. A path outside every root is refused the same way whether or not it exists."""
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
        result = ingest_file(store, resolved, quarantine, owner, source_key=key, show_name=show_name, camera=camera)
    except IngestError as exc:
        if isinstance(exc.__cause__, DiskLowError):  # not a missing file: the disk is too full to take the copy
            raise BuildError(507, "disk_low", str(exc)) from None
        raise BuildError(404, "not_found", str(exc)) from None
    jobs = store.conn.execute(
        "SELECT id, type, status, error FROM job WHERE asset_id = ? ORDER BY created_at, id", (result.asset_id,)).fetchall()
    return {"asset": result.asset_id, "essence": result.essence_id or None, "minted": result.minted,
            "status": result.status, "quarantined": result.status == "quarantine",
            "jobs": [{"id": j["id"], "type": j["type"], "status": j["status"], "error": j["error"]} for j in jobs]}
