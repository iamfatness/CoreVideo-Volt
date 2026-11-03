"""Stories: the editorial side of a rundown item (docs/superpowers/specs/2026-11-01-newsroom-scripts-design.md).

This module is the one place that knows the script markup, the read-time arithmetic, the revision and approval
rules and the story status moves. serve.py only reads the body, calls these, and maps StoryError to HTTP.

Script markup is plain text with two conventions and nothing else:

* A line that starts with `[` and ends with `]` is an instruction (`[SOT 0:23]`). It is display only: it never
  creates or fires a cue, and it is not read aloud, so it is not counted. A line that starts with `[` and has no
  `]` at all is read text, and is reported once in `warnings` as `unclosed_bracket`.
* A line that starts with `//` is a private note. It never leaves the editing screens, and it is not counted.

Paragraphs are separated by a blank line. A JavaScript twin (operator.js `scriptStats`) gives live counts; one
golden fixture (tests/golden/script_markup.json) drives both, so they cannot drift.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from volt.rundown import show_lock
from volt.store import Store, _id, _now, _now_precise

DEFAULT_WPM = 160
WPM_RANGE = (60, 400)
MAX_SCRIPT = 20000          # characters, as the item create and rename routes have always limited it
MAX_SLUG = 200
KEEP_REVISIONS = 50         # newest revisions kept per item, plus the one an approval points at
NOT_EDITABLE = ("aired", "skipped", "killed")    # item.status values whose text is the as-run record


@dataclass(frozen=True)
class Block:
    kind: str   # "read" | "instruction" | "note"
    text: str
    para: int


def check_wpm(wpm) -> int:
    if isinstance(wpm, bool) or not isinstance(wpm, int) or not WPM_RANGE[0] <= wpm <= WPM_RANGE[1]:
        raise ValueError(f"wpm must be a whole number from {WPM_RANGE[0]} to {WPM_RANGE[1]}")
    return wpm


def parse_script(text: Optional[str]) -> list[Block]:
    """The script as blocks, one per non-blank line, with the paragraph each belongs to (0, 1, ...)."""
    blocks: list[Block] = []
    para = 0
    pending_break = False
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            pending_break = bool(blocks)
            continue
        if pending_break:
            para += 1
            pending_break = False
        if line.startswith("//"):
            kind = "note"
        elif line.startswith("[") and line.endswith("]"):
            kind = "instruction"
        else:
            kind = "read"
        blocks.append(Block(kind, line, para))
    return blocks


def read_text(text: Optional[str]) -> str:
    """The text the talent reads: read lines only, lines of a paragraph joined by a newline, paragraphs by a blank line."""
    paras: dict[int, list[str]] = {}
    for b in parse_script(text):
        if b.kind == "read":
            paras.setdefault(b.para, []).append(b.text)
    return "\n\n".join("\n".join(lines) for _, lines in sorted(paras.items()))


def script_stats(text: Optional[str], wpm: int = DEFAULT_WPM) -> dict:
    """{words, readSeconds, instructions, notes, warnings}. Words are whitespace-separated tokens of the read text."""
    check_wpm(wpm)
    blocks = parse_script(text)
    words = sum(len(b.text.split()) for b in blocks if b.kind == "read")
    warnings = []
    if any(b.kind == "read" and b.text.startswith("[") and "]" not in b.text for b in blocks):
        warnings.append("unclosed_bracket")
    return {
        "words": words,
        # round(words / wpm * 60), halves up, in integers so the JavaScript twin gives the same answer
        "readSeconds": (words * 120 + wpm) // (2 * wpm),
        "instructions": sum(1 for b in blocks if b.kind == "instruction"),
        "notes": sum(1 for b in blocks if b.kind == "note"),
        "warnings": warnings,
    }


def public_text(text: Optional[str]) -> str:
    """The script with its private notes taken out and everything else left as written (for the MOS story body)."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line for line in lines if not line.strip().startswith("//"))


# ---- the story on an item ---------------------------------------------------------------------------------

class StoryError(Exception):
    """A refusal. `status` is the HTTP status, `code` is stable, `reason` is for people. Nothing changed."""

    def __init__(self, status: int, code: str, reason: str, **extra):
        super().__init__(reason)
        self.status, self.code, self.reason, self.extra = status, code, reason, extra


class StaleScript(StoryError):
    """The save was based on a revision that is no longer current. `current` is what is there now."""

    def __init__(self, current: dict):
        super().__init__(409, "stale_script", "the story changed since you loaded it; nothing was saved", current=current)
        self.current = current


_ITEM_COLUMNS = ("id, show_id, slug, script, status, created_at, story_status, script_rev, approved_by, approved_at, "
                 "approved_rev, floated")


def _utc_text(value) -> Optional[str]:
    """A stored time as UTC text (Postgres hands back a datetime, SQLite the text)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value.astimezone(timezone.utc) if value.tzinfo else value).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _item_row(store: Store, item_id: str):
    return store.conn.execute(f"SELECT {_ITEM_COLUMNS} FROM item WHERE id = ?", (item_id,)).fetchone()


def _need_item(store: Store, item_id: str):
    row = _item_row(store, item_id)
    if row is None:
        raise StoryError(404, "not_found", f"no such item: {item_id}")
    return row


def rev_of(row) -> int:
    return row["script_rev"] or 1


def status_of(row) -> str:
    return row["story_status"] or "draft"


def wpm_of(store: Store, show_id: str) -> int:
    row = store.conn.execute("SELECT read_wpm FROM show WHERE id = ?", (show_id,)).fetchone()
    return (row["read_wpm"] if row is not None else None) or DEFAULT_WPM


def _revision(store: Store, item_id: str, rev: int):
    return store.conn.execute(
        "SELECT rev, slug, script, actor, at FROM story_rev WHERE item_id = ? AND rev = ?", (item_id, rev)).fetchone()


def current_of(store: Store, row) -> dict:
    """What is current on the item: {rev, script, slug, actor, at}. Actor and time come from the revision row; a
    revision that was never saved through here (the text the item was created with) has no actor."""
    rev = rev_of(row)
    saved = _revision(store, row["id"], rev)
    return {"rev": rev, "script": row["script"] or "", "slug": row["slug"],
            "actor": saved["actor"] if saved is not None else None,
            "at": _utc_text(saved["at"] if saved is not None else row["created_at"])}


def _event(store: Store, item_id: str, actor: str, kind: str, from_value, to_value, rev, note=None) -> None:
    store.conn.execute(
        "INSERT INTO story_event (id, item_id, at, actor, kind, from_value, to_value, rev, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_id(), item_id, _now_precise(), actor, kind, from_value, to_value, rev, note))


def prune_revisions(store: Store, item_id: str, keep: int = KEEP_REVISIONS) -> int:
    """Keep the newest `keep` revisions and the one an approval points at; delete the rest. Returns how many went."""
    revs = [r["rev"] for r in store.conn.execute(
        "SELECT rev FROM story_rev WHERE item_id = ? ORDER BY rev DESC", (item_id,)).fetchall()]
    pinned = store.conn.execute("SELECT approved_rev FROM item WHERE id = ?", (item_id,)).fetchone()
    pinned = pinned["approved_rev"] if pinned is not None else None
    gone = [r for r in revs[keep:] if r != pinned]
    for rev in gone:
        store.conn.execute("DELETE FROM story_rev WHERE item_id = ? AND rev = ?", (item_id, rev))
    return len(gone)


def forget(store: Store, item_id: str) -> None:
    """Remove an item's history with the item (the caller holds the transaction)."""
    store.conn.execute("DELETE FROM story_rev WHERE item_id = ?", (item_id,))
    store.conn.execute("DELETE FROM story_event WHERE item_id = ?", (item_id,))


def _editable(row) -> None:
    if row["status"] in NOT_EDITABLE:
        raise StoryError(409, "not_editable", f"cannot edit the script of a {row['status']} item")
    if status_of(row) == "killed":
        raise StoryError(409, "story_killed", "this story is killed; revive it before editing")


def set_script(store: Store, item_id: str, *, script: Optional[str] = None, slug: Optional[str] = None,
               base_rev: Optional[int] = None, actor: str) -> dict:
    """Change an item's slug and/or script text. Returns {rev, changed, storyStatus}.

    One transaction under the show's lock (the one go and take hold): it re-reads the revision, refuses a save based
    on an older one (StaleScript, carrying what is current), writes the item with a conditional update, stores the
    new revision (and, the first time, the text it replaced), drops an approval, and prunes. Saving what is already
    there is a no-op at the same revision. `base_rev=None` skips the revision check (the old rename route)."""
    if script is None and slug is None:
        raise StoryError(422, "bad_input", "give slug, script or both")
    if script is not None and (not isinstance(script, str) or len(script) > MAX_SCRIPT):
        raise StoryError(422, "bad_input", f"script must be a string of at most {MAX_SCRIPT} characters")
    if slug is not None:
        if not isinstance(slug, str) or len(slug.strip()) > MAX_SLUG:
            raise StoryError(422, "bad_input", f"slug must be a string of at most {MAX_SLUG} characters")
        slug = slug.strip()
        if not slug:
            raise StoryError(422, "bad_input", "slug cannot be empty")
    found = _need_item(store, item_id)
    with store.transaction(show_lock(found["show_id"])):
        row = _need_item(store, item_id)
        _editable(row)
        rev = rev_of(row)
        if base_rev is not None and base_rev != rev:
            raise StaleScript(current_of(store, row))
        new_slug = row["slug"] if slug is None else slug
        new_script = (row["script"] or "") if script is None else script
        if (new_slug, new_script) == (row["slug"], row["script"] or ""):
            return {"rev": rev, "changed": False, "storyStatus": status_of(row)}
        if _revision(store, item_id, rev) is None:   # the text the item started with becomes revision history too
            store.conn.execute(
                "INSERT INTO story_rev (item_id, rev, slug, script, actor, at) VALUES (?, ?, ?, ?, NULL, ?)",
                (item_id, rev, row["slug"], row["script"] or "", row["created_at"]))
        new_rev, was = rev + 1, status_of(row)
        dropped = was == "approved"
        cur = store.conn.execute(
            "UPDATE item SET slug = ?, script = ?, script_rev = ?, story_status = ?, approved_by = ?, approved_at = ?, approved_rev = ? "
            "WHERE id = ? AND COALESCE(script_rev, 1) = ?",
            (new_slug, new_script, new_rev, "ready" if dropped else row["story_status"],
             None if dropped else row["approved_by"], None if dropped else row["approved_at"],
             None if dropped else row["approved_rev"], item_id, rev))
        if cur.rowcount != 1:     # a writer outside this lock got in first: same outcome as a stale base
            raise StaleScript(current_of(store, _need_item(store, item_id)))
        store.conn.execute(
            "INSERT INTO story_rev (item_id, rev, slug, script, actor, at) VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, new_rev, new_slug, new_script, actor, _now_precise()))
        if dropped:
            _event(store, item_id, actor, "status", "approved", "ready", new_rev, "edited after approval")
        prune_revisions(store, item_id)
        return {"rev": new_rev, "changed": True, "storyStatus": "ready" if dropped else was}


# ---- status and approval ----------------------------------------------------------------------------------

STATUSES = ("draft", "ready", "approved", "killed")
# Story status is independent of item.status: killing a story does not take an item off air, and a playout kill does
# not kill the story. "any -> killed" means any other status.
TRANSITIONS = {
    "draft": {"ready", "killed"},
    "ready": {"draft", "approved", "killed"},
    "approved": {"ready", "killed"},
    "killed": {"draft"},
}
_APPROVER_MOVES = {("ready", "approved"), ("approved", "ready")}


def approvers() -> Optional[set]:
    """The names VOLT_APPROVERS limits approval to, or None when it is unset or empty (any signed-in operator may approve)."""
    names = {n.strip() for n in os.environ.get("VOLT_APPROVERS", "").split(",") if n.strip()}
    return names or None


def can_approve(principal, item_row) -> bool:
    """May this principal approve (or take back the approval of) this story? The one seam the identity work fills.

    With no VOLT_APPROVERS, any signed-in operator or admin. With it set, only a per-user token whose name is listed:
    the shared VOLT_TOKEN (or open loopback) is refused, because its actor name is only a header anyone can write."""
    allowed = approvers()
    if allowed is None:
        return True
    if principal is None or getattr(principal, "legacy", False):
        return False
    return getattr(principal, "name", None) in allowed


def require_approval() -> bool:
    """VOLT_REQUIRE_APPROVAL=1: only an approved story, at its current revision, may go to air. Off by default."""
    return os.environ.get("VOLT_REQUIRE_APPROVAL") == "1"


def is_approved(row) -> bool:
    return status_of(row) == "approved" and row["approved_rev"] == rev_of(row)


def check_airable(store: Store, item_id: str) -> None:
    """Refuse an item that must not go to air. A floated story is never taken (PlayoutError). With VOLT_REQUIRE_APPROVAL=1
    a story that is not approved at its current revision is refused too (playout.StoryNotApproved). Called by
    rundown.take, the one place every path to air (go, take, the clock, cue arm) passes."""
    row = _item_row(store, item_id)
    if row is None:
        return
    from volt.playout import PlayoutError, StoryNotApproved
    if row["floated"] and row["status"] == "ready":
        raise PlayoutError(f"item {item_id} is floated, a backup story; un-float it before it can go to air")
    if require_approval() and not is_approved(row):
        raise StoryNotApproved(item_id, status_of(row), rev_of(row))


# ---- floating and reading pace ----------------------------------------------------------------------------

def set_floated(store: Store, item_id: str, floated: bool, actor: str) -> dict:
    """Mark an item as a backup story (or put it back). Only a ready item can be floated; un-floating is always allowed,
    and the item returns to the position it kept. Returns {floated, changed}."""
    if not isinstance(floated, bool):
        raise StoryError(422, "bad_input", "floated must be true or false")
    found = _need_item(store, item_id)
    with store.transaction(show_lock(found["show_id"])):
        row = _need_item(store, item_id)
        was = bool(row["floated"])
        if was == floated:
            return {"floated": was, "changed": False}
        if floated and row["status"] != "ready":
            raise StoryError(409, "not_floatable", f"cannot float a {row['status']} item; only a ready item can be a backup")
        cur = store.conn.execute("UPDATE item SET floated = ? WHERE id = ? AND COALESCE(floated, ?) = ?",
                                 (floated, item_id, False, was))
        if cur.rowcount != 1:
            raise StoryError(409, "stale_state", "the item changed while it was being floated; reload and try again")
        _event(store, item_id, actor, "float", "floated" if was else "unfloated", "floated" if floated else "unfloated",
               rev_of(row))
        return {"floated": floated, "changed": True}


def set_pace(store: Store, show_id: str, wpm: Optional[int], actor: str) -> dict:
    """Set or clear (None) the show's reading pace in words per minute. Returns {readWpm} (the pace now in effect)."""
    if wpm is not None:
        try:
            check_wpm(wpm)
        except ValueError as exc:
            raise StoryError(422, "bad_input", str(exc)) from None
    with store.transaction(show_lock(show_id)):
        cur = store.conn.execute("UPDATE show SET read_wpm = ? WHERE id = ?", (wpm, show_id))
        if cur.rowcount != 1:
            raise StoryError(404, "not_found", f"no such show: {show_id}")
    return {"readWpm": wpm or DEFAULT_WPM}


def set_status(store: Store, item_id: str, status: str, *, base_rev: int, actor: str, principal=None,
               note: Optional[str] = None) -> dict:
    """Move a story to `status` if the move is legal, quoting the revision it was decided on.

    Approving, or taking an approval back, needs can_approve. Approving also needs an item that can still air (not killed
    from air, skipped or aired). Leaving `approved` by any route clears the approval record. Every move writes a
    story_event, so "who approved revision 7, and when" is one query."""
    if status not in STATUSES:
        raise StoryError(422, "bad_input", f"status must be one of {', '.join(STATUSES)}")
    if note is not None and (not isinstance(note, str) or len(note) > 500):
        raise StoryError(422, "bad_input", "note must be a string of at most 500 characters")
    found = _need_item(store, item_id)
    with store.transaction(show_lock(found["show_id"])):
        row = _need_item(store, item_id)
        rev = rev_of(row)
        if base_rev != rev:
            raise StaleScript(current_of(store, row))
        was = status_of(row)
        if status not in TRANSITIONS[was]:
            raise StoryError(409, "bad_transition", f"a {was} story cannot become {status}",
                             **{"from": was, "to": status, "allowed": sorted(TRANSITIONS[was])})
        if status == "approved" and row["status"] in NOT_EDITABLE:
            raise StoryError(409, "not_editable", f"cannot approve the story of a {row['status']} item")
        if (was, status) in _APPROVER_MOVES and not can_approve(principal, row):
            raise StoryError(403, "not_approver", "you are not on the list of people who can approve stories")
        at = _now()
        if status == "approved":
            sets, params = "approved_by = ?, approved_at = ?, approved_rev = ?", (actor, at, rev)
        else:
            sets, params = "approved_by = NULL, approved_at = NULL, approved_rev = NULL", ()
        cur = store.conn.execute(
            f"UPDATE item SET story_status = ?, {sets} WHERE id = ? AND COALESCE(script_rev, 1) = ? "
            "AND COALESCE(story_status, 'draft') = ?", (status, *params, item_id, rev, was))
        if cur.rowcount != 1:
            raise StaleScript(current_of(store, _need_item(store, item_id)))
        _event(store, item_id, actor, "status", was, status, rev, note)
        return {"rev": rev, "storyStatus": status, "approvedBy": actor if status == "approved" else None,
                "approvedAt": at if status == "approved" else None, "approvedRev": rev if status == "approved" else None}


def get_story(store: Store, item_id: str, include_notes: bool = True) -> dict:
    """Everything the editor needs about one story. The private `//` notes are in `script` unless `include_notes` is
    false (the read-only viewer role: a note is for the people who edit the story)."""
    row = _need_item(store, item_id)
    wpm = wpm_of(store, row["show_id"])
    revisions = [{"rev": r["rev"], "actor": r["actor"], "at": _utc_text(r["at"])} for r in store.conn.execute(
        "SELECT rev, actor, at FROM story_rev WHERE item_id = ? ORDER BY rev DESC", (item_id,)).fetchall()]
    rev = rev_of(row)
    if rev not in {r["rev"] for r in revisions}:       # never saved through here: the text it was created with
        revisions.insert(0, {"rev": rev, "actor": None, "at": _utc_text(row["created_at"])})
    events = [{"id": e["id"], "at": _utc_text(e["at"]), "actor": e["actor"], "kind": e["kind"], "from": e["from_value"],
               "to": e["to_value"], "rev": e["rev"], "note": e["note"]} for e in store.conn.execute(
        "SELECT id, at, actor, kind, from_value, to_value, rev, note FROM story_event WHERE item_id = ? "
        "ORDER BY at DESC, id LIMIT 200", (item_id,)).fetchall()]
    return {"ok": True, "item": item_id, "slug": row["slug"], "script": (row["script"] or "") if include_notes else public_text(row["script"]), "rev": rev,
            "storyStatus": status_of(row), "approvedBy": row["approved_by"], "approvedAt": _utc_text(row["approved_at"]),
            "approvedRev": row["approved_rev"], "floated": bool(row["floated"]), "itemStatus": row["status"],
            "stats": script_stats(row["script"], wpm), "revisions": revisions, "events": events}


# ---- the prompter feed and the plain-text export ----------------------------------------------------------

def _airable_rows(store: Store, show_id: str) -> list:
    """The items a prompter may show, in rundown order: not killed (from air, or as a story), not floated, not aired or
    skipped, and with VOLT_REQUIRE_APPROVAL=1 only those approved at their current revision."""
    gate = require_approval()
    out = []
    for row in store.conn.execute(
            "SELECT id, show_id, slug, script, status, story_status, script_rev, approved_rev, floated FROM item "
            "WHERE show_id = ? ORDER BY position", (show_id,)).fetchall():
        if row["status"] in ("killed", "aired", "skipped") or row["floated"] or status_of(row) == "killed":
            continue
        if gate and not is_approved(row):
            continue
        out.append(row)
    return out


def prompter_feed(store: Store, show_id: str) -> dict:
    """What a prompter shows: {show, version, wpm, current, items:[{id, slug, status, rev, blocks:[{kind, text, para}]}]}.
    Private `//` notes are removed here, on the server, and never leave it. `version` is a hash of the content, so a page
    repaints only when something it shows has changed. `current` is the on-air or taking item (it may be absent from `items`
    if it is not airable, for example a killed story)."""
    import hashlib
    import json
    if store.conn.execute("SELECT 1 FROM show WHERE id = ?", (show_id,)).fetchone() is None:
        raise StoryError(404, "not_found", f"no such show: {show_id}")
    wpm = wpm_of(store, show_id)
    items = [{"id": r["id"], "slug": r["slug"], "status": r["status"], "rev": rev_of(r),
              "blocks": [{"kind": b.kind, "text": b.text, "para": b.para} for b in parse_script(r["script"]) if b.kind != "note"]}
             for r in _airable_rows(store, show_id)]
    cur = store.conn.execute("SELECT id FROM item WHERE show_id = ? AND status IN ('on air', 'taking') ORDER BY position", (show_id,)).fetchone()
    current = cur["id"] if cur else None
    body = {"show": show_id, "wpm": wpm, "current": current, "items": items}
    # The hash covers what the page shows, not the revision number: a change to a private note bumps the revision but not the page.
    shown = {**body, "items": [{k: v for k, v in i.items() if k != "rev"} for i in items]}
    version = hashlib.sha256(json.dumps(shown, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return {"ok": True, "version": version, **body}


def script_text(store: Store, show_id: str) -> str:
    """The show's read text as plain text, for print or a prompter that pulls text: each airable story's slug in capitals,
    then what the talent reads (no instructions, no notes), stories separated by a blank line."""
    if store.conn.execute("SELECT 1 FROM show WHERE id = ?", (show_id,)).fetchone() is None:
        raise StoryError(404, "not_found", f"no such show: {show_id}")
    parts = []
    for r in _airable_rows(store, show_id):
        body = read_text(r["script"])
        parts.append(r["slug"].upper() + (chr(10) * 2 + body if body else ""))
    return (chr(10) * 3).join(parts) + (chr(10) if parts else "")
