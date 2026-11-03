"""Story and show templates (docs/superpowers/specs/2026-11-01-newsroom-scripts-design.md, "Templates").

A `story` template is a slug pattern and a script skeleton. A `show` template is a list of story skeletons with their cues, plus slot
length and reading pace. Cues are carried (as the recurring-show clone does); media spans are not, since a span belongs to a past asset.
Instantiating always makes `draft` stories at revision 1 with no approval. Private `//` notes stay in the template for the people who
edit it: they are text like any other until the story reaches a prompter, which never shows them.

A slug may hold `{date}`, replaced by today's date (UTC, YYYY-MM-DD) when the template is used.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from volt import building, story
from volt.rundown import show_lock
from volt.store import Store, _id, _now

KINDS = ("story", "show")
MAX_BODY_BYTES = 200 * 1024
MAX_STORIES = 100
MAX_CUES = 20
MAX_NAME = 120
_STORY_KEYS = {"slug", "script"}
_SHOW_KEYS = {"slotSeconds", "wpm", "stories"}
_SHOW_STORY_KEYS = {"slug", "script", "cues"}
_CUE_KEYS = {"room", "command", "payload"}


class TemplateError(Exception):
    def __init__(self, status: int, code: str, reason: str):
        super().__init__(reason)
        self.status, self.code, self.reason = status, code, reason


def _bad(reason: str) -> TemplateError:
    return TemplateError(422, "bad_input", reason)


def _check_story(body, keys: set, where: str) -> dict:
    if not isinstance(body, dict):
        raise _bad(f"{where} must be an object")
    extra = sorted(set(body) - keys)
    if extra:
        raise _bad(f"{where}: unknown field(s) {', '.join(extra)}")
    slug, script = body.get("slug"), body.get("script", "")
    if not isinstance(slug, str) or not slug.strip() or len(slug.strip()) > story.MAX_SLUG:
        raise _bad(f"{where}: slug must be a non-empty string of at most {story.MAX_SLUG} characters")
    if not isinstance(script, str) or len(script) > story.MAX_SCRIPT:
        raise _bad(f"{where}: script must be a string of at most {story.MAX_SCRIPT} characters")
    out = {"slug": slug.strip(), "script": script}
    if "cues" in keys:
        cues = body.get("cues", [])
        if not isinstance(cues, list) or len(cues) > MAX_CUES:
            raise _bad(f"{where}: cues must be a list of at most {MAX_CUES}")
        clean = []
        for c in cues:
            if not isinstance(c, dict) or set(c) - _CUE_KEYS or not all(isinstance(c.get(k), str) and 0 < len(c[k]) <= 500 for k in ("room", "command")) \
                    or not isinstance(c.get("payload", ""), str) or len(c.get("payload", "")) > 500:
                raise _bad(f"{where}: each cue is {{room, command, payload}} of short strings")
            clean.append({"room": c["room"], "command": c["command"], "payload": c.get("payload", "")})
        out["cues"] = clean
    return out


def check_body(kind: str, body) -> dict:
    """Validate and normalise a template body for its kind. Unknown keys, wrong types and oversize bodies are 422."""
    if kind not in KINDS:
        raise _bad(f"kind must be one of {', '.join(KINDS)}")
    if len(json.dumps(body, default=str)) > MAX_BODY_BYTES:
        raise _bad(f"a template body can be at most {MAX_BODY_BYTES // 1024} KB")
    if kind == "story":
        return _check_story(body, _STORY_KEYS, "template")
    if not isinstance(body, dict):
        raise _bad("template must be an object")
    extra = sorted(set(body) - _SHOW_KEYS)
    if extra:
        raise _bad(f"template: unknown field(s) {', '.join(extra)}")
    slot, wpm, stories = body.get("slotSeconds"), body.get("wpm"), body.get("stories", [])
    if slot is not None and (isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 86399):
        raise _bad("slotSeconds must be a whole number from 1 to 86399, or null")
    if wpm is not None:
        try:
            story.check_wpm(wpm)
        except ValueError as exc:
            raise _bad(str(exc)) from None
    if not isinstance(stories, list) or len(stories) > MAX_STORIES:
        raise _bad(f"stories must be a list of at most {MAX_STORIES}")
    return {"slotSeconds": slot, "wpm": wpm, "stories": [_check_story(s, _SHOW_STORY_KEYS, f"story {n}") for n, s in enumerate(stories, 1)]}


def _name(name) -> str:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME:
        raise _bad(f"name must be a non-empty string of at most {MAX_NAME} characters")
    return name.strip()


def create(store: Store, kind: str, name: str, body, actor: str) -> str:
    clean, name = check_body(kind, body), _name(name)
    template_id = _id()
    store.conn.execute("INSERT INTO story_template (id, kind, name, body, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (template_id, kind, name, json.dumps(clean), actor, _now()))
    store.conn.commit()
    return template_id


def from_item(store: Store, item_id: str, name: str, actor: str) -> str:
    """A story template from an existing story: its slug and script as they stand, private notes included."""
    row = store.conn.execute("SELECT slug, script FROM item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise TemplateError(404, "not_found", f"no such item: {item_id}")
    return create(store, "story", name, {"slug": row["slug"], "script": row["script"] or ""}, actor)


def from_show(store: Store, show_id: str, name: str, actor: str) -> str:
    """A show template from a show: every story except those killed, skipped or floated (a backup is not part of the format), in
    order, with its cues; the slot length and reading pace. No media spans, no as-run times, no approvals."""
    show = store.conn.execute("SELECT slot_seconds, read_wpm FROM show WHERE id = ?", (show_id,)).fetchone()
    if show is None:
        raise TemplateError(404, "not_found", f"no such show: {show_id}")
    stories = []
    for r in store.conn.execute("SELECT id, slug, script, status, story_status, floated FROM item WHERE show_id = ? ORDER BY position, id", (show_id,)).fetchall():
        if r["status"] in ("killed", "skipped") or r["floated"] or (r["story_status"] or "draft") == "killed":
            continue
        cues = [{"room": c["room"], "command": c["command"], "payload": c["payload"] or ""} for c in store.conn.execute(
            "SELECT room, command, payload FROM cue WHERE item_id = ? ORDER BY position", (r["id"],)).fetchall()]
        stories.append({"slug": r["slug"], "script": r["script"] or "", "cues": cues[:MAX_CUES]})
    return create(store, "show", name, {"slotSeconds": show["slot_seconds"], "wpm": show["read_wpm"], "stories": stories[:MAX_STORIES]}, actor)


def _row(store: Store, template_id: str, kind: Optional[str] = None):
    row = store.conn.execute("SELECT id, kind, name, body, created_by, created_at FROM story_template WHERE id = ?", (template_id,)).fetchone()
    if row is None:
        raise TemplateError(404, "not_found", f"no such template: {template_id}")
    if kind and row["kind"] != kind:
        raise TemplateError(409, "wrong_kind", f"this is a {row['kind']} template, not a {kind} template")
    return row


def _ts(value) -> Optional[str]:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(value, datetime) else (None if value is None else str(value))


def listing(store: Store) -> dict:
    out = []
    for r in store.conn.execute("SELECT id, kind, name, body, created_by, created_at FROM story_template ORDER BY name, id").fetchall():
        body = json.loads(r["body"])
        out.append({"id": r["id"], "kind": r["kind"], "name": r["name"], "createdBy": r["created_by"], "createdAt": _ts(r["created_at"]),
                    "stories": len(body.get("stories", [])) if r["kind"] == "show" else None,
                    "slotSeconds": body.get("slotSeconds") if r["kind"] == "show" else None})
    return {"ok": True, "templates": out}


def _slug(pattern: str) -> str:
    return pattern.replace("{date}", datetime.now(timezone.utc).strftime("%Y-%m-%d"))[:story.MAX_SLUG]


def _new_story(store: Store, show_id: str, spec: dict, template_id: str, actor: str) -> str:
    item_id, _ = building.add_item(store, show_id, _slug(spec["slug"]), spec["script"])
    store.conn.execute("UPDATE item SET story_status = 'draft', script_rev = 1, approved_by = NULL, approved_at = NULL, approved_rev = NULL, "
                       "floated = ? WHERE id = ?", (False, item_id))
    story._event(store, item_id, actor, "template", None, template_id, 1, "from template")
    return item_id


def instantiate_story(store: Store, template_id: str, show_id: str, position: Optional[int], actor: str) -> dict:
    """A draft story from a story template, at the end of (or at `position` in) a show. Returns {item, placed}."""
    if position is not None and (isinstance(position, bool) or not isinstance(position, int) or position < 1):
        raise _bad("position must be a whole number from 1")
    with store.transaction(show_lock(show_id)):
        row = _row(store, template_id, "story")
        if store.conn.execute("SELECT 1 FROM show WHERE id = ?", (show_id,)).fetchone() is None:
            raise TemplateError(404, "not_found", f"no such show: {show_id}")
        item_id = _new_story(store, show_id, json.loads(row["body"]), template_id, actor)
        placed = True
        if position is not None:
            ids = [r["id"] for r in store.conn.execute("SELECT id FROM item WHERE show_id = ? ORDER BY position, id", (show_id,)).fetchall()]
            ids.remove(item_id)
            ids.insert(min(position - 1, len(ids)), item_id)
            try:
                building.reorder(store, show_id, ids)
            except building.BuildError:
                placed = False
    return {"item": item_id, "placed": placed}


def instantiate_show(store: Store, template_id: str, title: str, actor: str) -> dict:
    """A new show from a show template: draft stories with their cues, the slot length and the reading pace. Returns
    {show, items, skippedCues} (a cue whose command the server no longer accepts is left out and counted)."""
    from volt.cues import add_cue
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
        raise _bad("title must be a non-empty string of at most 200 characters")
    skipped = 0
    with store.transaction():
        body = json.loads(_row(store, template_id, "show")["body"])
        show_id, _ = building.create_show(store, title.strip())
        if body.get("slotSeconds") is not None:
            store.conn.execute("UPDATE show SET slot_seconds = ? WHERE id = ?", (body["slotSeconds"], show_id))
        if body.get("wpm") is not None:
            store.conn.execute("UPDATE show SET read_wpm = ? WHERE id = ?", (body["wpm"], show_id))
        items = []
        for spec in body["stories"]:
            item_id = _new_story(store, show_id, spec, template_id, actor)
            items.append(item_id)
            for cue in spec.get("cues", []):
                try:
                    add_cue(store, item_id, cue["room"], cue["command"], cue["payload"])
                except ValueError:
                    skipped += 1
    return {"show": show_id, "items": items, "skippedCues": skipped}


def delete(store: Store, template_id: str, actor: str) -> dict:
    _row(store, template_id)
    store.conn.execute("DELETE FROM story_template WHERE id = ?", (template_id,))
    store.conn.commit()
    return {"deleted": template_id}
