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
                  a.created_at, e.location, e.checksum, e.open
           FROM asset a
           LEFT JOIN essence e ON e.asset_id = a.id AND e.role = 'hi-res'
           ORDER BY a.created_at, a.id"""
    ).fetchall()
    out = []
    for r in rows:
        cleared, held = bool(r["cleared"]), bool(r["legal_hold"])
        out.append({
            "id": r["id"], "owner": r["owner"], "show": r["show_name"], "camera": r["camera"],
            "sourceKey": r["source_key"], "cleared": cleared, "legalHold": held,
            "blockedBy": _blocked_by(cleared, held),
            "file": r["location"], "checksum": r["checksum"],
            "open": bool(r["open"]) if r["open"] is not None else False,
        })
    return out


def spans(store: Store, kinds: Optional[Sequence[str]] = None) -> list[dict]:
    """Up to SPAN_LIMIT spans, ordered by tc_in. `kinds=None` (the default) lists every kind, exactly as before;
    which kinds the Publish screen should list is the owner's call (spec open question 9), not made here.
    A kind list narrows to those kinds; an empty list lists nothing."""
    where, params = "", []
    if kinds is not None:
        kinds = list(kinds)
        if not kinds:
            return []
        where = f"WHERE s.kind IN ({', '.join('?' for _ in kinds)})"
        params = kinds
    rows = store.conn.execute(
        f"""SELECT s.id, s.asset_id, s.kind, s.tc_in, s.tc_out, s.text, a.cleared, a.legal_hold, a.camera
           FROM span s JOIN asset a ON a.id = s.asset_id
           {where}
           ORDER BY s.tc_in, s.id LIMIT ?""",
        (*params, SPAN_LIMIT),
    ).fetchall()
    versions: dict[str, list[dict]] = {}
    for v in store.conn.execute(
        "SELECT id, span_id, kind, status, url FROM version ORDER BY span_id, kind"
    ).fetchall():
        versions.setdefault(v["span_id"], []).append(
            {"id": v["id"], "kind": v["kind"], "status": v["status"], "url": v["url"]})
    return [{
        "id": r["id"], "assetId": r["asset_id"], "kind": r["kind"], "tcIn": r["tc_in"],
        "tcOut": r["tc_out"], "text": r["text"], "camera": r["camera"],
        "blockedBy": _blocked_by(bool(r["cleared"]), bool(r["legal_hold"])),
        "versions": versions.get(r["id"], []),
    } for r in rows]


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
        "spans": spans(store),
        "room": room,
    }
