"""Read-only state for the operator UI. One call returns everything a screen needs.

The /rundown shape ({hold, items}) is passed through untouched as `rundown`; this adds
what that shape does not carry (cues, media clearance, assets, spans with versions and
the room's lifecycle) so the page never has to guess. Nothing here writes.
"""

from __future__ import annotations

from typing import Optional, Sequence

from volt.cues import cues
from volt.rooms import LiveError
from volt.store import Store

SPAN_LIMIT = 200


def _blocked_by(cleared: bool, legal_hold: bool) -> Optional[str]:
    """Why the asset cannot air or publish, in the words the page shows. None when it can."""
    if legal_hold:
        return "legal hold"
    if not cleared:
        return "not cleared"
    return None


def shows(store: Store) -> list[dict]:
    rows = store.conn.execute(
        "SELECT id, title, held_at FROM show ORDER BY created_at, id"
    ).fetchall()
    return [{"id": r["id"], "title": r["title"], "held": r["held_at"] is not None} for r in rows]


def assets(store: Store) -> list[dict]:
    rows = store.conn.execute(
        """SELECT a.id, a.owner, a.show_name, a.camera, a.cleared, a.legal_hold, a.source_key,
                  a.created_at, e.location, e.checksum, e.open,
                  EXISTS (SELECT 1 FROM essence p WHERE p.asset_id = a.id AND p.role = 'proxy') AS has_proxy
           FROM asset a
           LEFT JOIN essence e ON e.asset_id = a.id AND e.role = 'hi-res'
           WHERE a.deleted_at IS NULL
           ORDER BY a.created_at, a.id"""
    ).fetchall()
    # Work that is not finished (queued, running or failed), so the Library can show an ingest's progress on the
    # existing poll. A job that is not listed has finished.
    pending: dict = {}
    for j in store.conn.execute(
            "SELECT asset_id, type, status, error FROM job WHERE status != 'done' ORDER BY created_at, id").fetchall():
        pending.setdefault(j["asset_id"], {})[j["type"]] = {"type": j["type"], "status": j["status"], "error": j["error"]}
    out = []
    for r in rows:
        cleared, held = bool(r["cleared"]), bool(r["legal_hold"])
        out.append({
            "id": r["id"], "owner": r["owner"], "show": r["show_name"], "camera": r["camera"],
            "sourceKey": r["source_key"], "cleared": cleared, "legalHold": held,
            "blockedBy": _blocked_by(cleared, held),
            "file": r["location"], "checksum": r["checksum"],
            "open": bool(r["open"]) if r["open"] is not None else False,
            "hasProxy": bool(r["has_proxy"]),
            "jobs": list(pending.get(r["id"], {}).values()),
        })
    return out


PUBLISH_KINDS = ("clip", "shot")
_EXTRA_LIMIT = 500


def _span_rows(store: Store, where: str, params: Sequence, limit: int) -> list:
    return store.conn.execute(
        f"""SELECT s.id, s.asset_id, s.kind, s.tc_in, s.tc_out, s.text, s.label, a.cleared, a.legal_hold, a.camera
           FROM span s JOIN asset a ON a.id = s.asset_id AND a.deleted_at IS NULL
           {where}
           ORDER BY s.tc_in, s.id LIMIT ?""",
        (*params, limit),
    ).fetchall()


def spans(store: Store, kinds: Optional[Sequence[str]] = PUBLISH_KINDS, include_ids: Sequence[str] = ()) -> list[dict]:
    """Up to SPAN_LIMIT spans of the given kinds, ordered by tc_in. The default is the editorial spans (clips and
    shots): Publish lists selects, not transcript words (spec open question 9, owner ruling). `kinds=None` lists
    every kind; an empty list lists nothing. `include_ids` and every span that already has a published version are
    added whatever their kind, so a word-span that was attached to a rundown item or published earlier does not
    vanish from the screen that publishes it. Words stay searchable through library.search and the Library transcript."""
    if kinds is not None:
        kinds = list(kinds)
    rows = []
    if kinds is None:
        rows = _span_rows(store, "", (), SPAN_LIMIT)
    elif kinds:
        rows = _span_rows(store, f"WHERE s.kind IN ({', '.join('?' for _ in kinds)})", kinds, SPAN_LIMIT)
    if kinds is not None:
        have = {r["id"] for r in rows}
        ids = [i for i in dict.fromkeys(include_ids) if i not in have][:_EXTRA_LIMIT]
        extra = []
        if ids:
            extra = _span_rows(store, f"WHERE s.id IN ({', '.join('?' for _ in ids)})", ids, _EXTRA_LIMIT)
        extra += _span_rows(store, "WHERE s.id IN (SELECT span_id FROM version)", (), _EXTRA_LIMIT)
        for r in extra:
            if r["id"] not in have:
                have.add(r["id"])
                rows.append(r)
        if extra:
            rows.sort(key=lambda r: (r["tc_in"], r["id"]))
    versions: dict[str, list[dict]] = {}
    for v in store.conn.execute(
        "SELECT id, span_id, kind, status, url FROM version ORDER BY span_id, kind"
    ).fetchall():
        versions.setdefault(v["span_id"], []).append(
            {"id": v["id"], "kind": v["kind"], "status": v["status"], "url": v["url"]})
    return [{
        "id": r["id"], "assetId": r["asset_id"], "kind": r["kind"], "tcIn": r["tc_in"],
        "tcOut": r["tc_out"], "text": r["text"], "label": r["label"], "camera": r["camera"],
        "blockedBy": _blocked_by(bool(r["cleared"]), bool(r["legal_hold"])),
        "versions": versions.get(r["id"], []),
    } for r in rows]


def _item_span_ids(store: Store, show_id: str) -> list[str]:
    return [r["span_id"] for r in store.conn.execute(
        "SELECT span_id FROM item WHERE show_id = ? AND span_id IS NOT NULL", (show_id,))]


def item_cues(store: Store, show_id: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in store.conn.execute("SELECT id FROM item WHERE show_id = ? ORDER BY position", (show_id,)):
        out[row["id"]] = [{"id": c.id, "room": c.room, "command": c.command,
                           "payload": c.payload, "status": c.status,
                           "failure": None if c.fail_status is None
                           else {"status": c.fail_status, "detail": c.fail_detail}} for c in cues(store, row["id"])]
    return out


def item_media(store: Store, show_id: str) -> dict[str, dict]:
    """Per item: the span it plays and whether its asset may air. Items with no span map to {}."""
    out: dict[str, dict] = {}
    rows = store.conn.execute(
        """SELECT i.id, i.span_id, s.tc_in, s.tc_out, s.text, a.cleared, a.legal_hold
           FROM item i LEFT JOIN span s ON s.id = i.span_id LEFT JOIN asset a ON a.id = s.asset_id
           WHERE i.show_id = ?""",
        (show_id,),
    ).fetchall()
    for r in rows:
        if r["span_id"] is None:
            out[r["id"]] = {"span": None, "blockedBy": "no media"}
        else:
            out[r["id"]] = {"span": r["span_id"], "tcIn": r["tc_in"], "tcOut": r["tc_out"],
                            "text": r["text"],
                            "blockedBy": _blocked_by(bool(r["cleared"]), bool(r["legal_hold"]))}
    return out


def room_state(room_factory) -> dict:
    """What the room reports right now. Never raises: an unreachable room is a state the page shows."""
    try:
        room = room_factory()
    except Exception as exc:  # no room configured (ApiError) or a bad config
        return {"configured": False, "reachable": False, "error": getattr(exc, "reason", None) or str(exc)}
    try:
        o = room.observe()
    except LiveError as exc:
        return {"configured": True, "reachable": False, "error": str(exc)}
    return {"configured": True, "reachable": True, "state": o.state, "health": o.health,
            "finalized": o.finalized, "sessionId": o.session_id, "artifactPath": o.artifact_path,
            "stale": o.stale, "error": o.error}


def build(store: Store, rundown: Optional[dict], show_id: Optional[str], room: dict) -> dict:
    """`rundown` is the already-built /rundown payload, so the shape lives in one place (serve.py)."""
    listing = shows(store)
    title = next((s["title"] for s in listing if s["id"] == show_id), None)
    return {
        "shows": listing,
        "show": None if show_id is None else {"id": show_id, "title": title},
        "rundown": rundown,
        "cues": {} if show_id is None else item_cues(store, show_id),
        "media": {} if show_id is None else item_media(store, show_id),
        "assets": assets(store),
        "spans": spans(store, include_ids=[] if show_id is None else _item_span_ids(store, show_id)),
        "room": room,
    }
