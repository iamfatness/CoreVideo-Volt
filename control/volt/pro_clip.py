"""Local, pre-rendered rundown cuts for CoreVideo Pro's Windows media bin."""

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from volt import deliverables
from volt.roomreg import COREVIDEO, as_registry
from volt.rooms import LiveError
from volt.rundown import show_lock
from volt.show_input import show_input_take
from volt.store import ArchivedError, NotClearedError

BIN = "Volt Rundown"
COMMAND = "media.rundown.take"
STAGE_LEASE_SECONDS = 180


def target():
    """Opt in with a dedicated scene and video route on the same Windows workstation."""
    appdata = os.environ.get("LOCALAPPDATA")
    raw = os.environ.get("VOLT_PRO_CLIP_TARGET")
    if not appdata or not raw:
        return None
    try:
        values = json.loads(raw)
        scene, route = values["scene"], values.get("route", "")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if not isinstance(scene, str) or not scene.strip() or not isinstance(route, str):
        return None
    return Path(appdata) / "CoreVideoPro" / "media" / BIN, scene, route


def _room(client):
    room = as_registry(client).get(COREVIDEO)
    if room is None or urlsplit(getattr(room, "base", "")).hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise LiveError("Volt rundown clips need CoreVideo Pro on this workstation.")
    return room


def _item(store, item_id):
    return store.conn.execute(
        """SELECT s.*, i.id AS item_id, i.show_id, i.slug, i.status, i.pro_clip,
                  e.id AS essence_id, e.checksum AS essence_checksum, e.open AS essence_open,
                  e.tier, a.cleared, a.legal_hold, a.deleted_at
           FROM item i LEFT JOIN span s ON s.id = i.span_id
           LEFT JOIN asset a ON a.id = s.asset_id
           LEFT JOIN essence e ON e.asset_id = s.asset_id AND e.role = 'hi-res'
           WHERE i.id = ?""", (item_id,)).fetchone()


def _key(row):
    clip = re.sub(r"[^a-z0-9]+", "-", row["slug"].lower()).strip("-")[:38].rstrip("-") or "clip"
    identity = [row[k] for k in ("item_id", "id", "tc_in", "tc_out", "essence_id", "essence_checksum")]
    digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
    return f"{clip}-{digest}"


def _checked(store, row):
    if row is None or not row["id"] or not row["essence_id"] or row["essence_open"]:
        raise LiveError("Attach a clip with a finalized high-resolution recording first.")
    try:
        store.assert_publishable(row["asset_id"])
    except (ArchivedError, KeyError, NotClearedError) as exc:
        raise LiveError("This clip must be cleared for air, online, and free of legal hold.") from exc
    if store.conn.execute("SELECT 1 FROM cue WHERE item_id = ? AND command = 'show-input.take'",
                          (row["item_id"],)).fetchone():
        raise LiveError("Remove the item's Input to program cue before using a staged clip.")


def _check_current(store, item_id, key, pending):
    current = _item(store, item_id)
    _checked(store, current)
    if current["status"] != "ready" or _key(current) != key or current["pro_clip"] != pending:
        raise LiveError("The rundown clip changed during staging. Send the current clip again.")


def stage(store, client, item_id):
    config = target()
    if config is None:
        raise LiveError("Set VOLT_PRO_CLIP_TARGET to the Pro scene and route.")
    room = _room(client)
    row = _item(store, item_id)
    if row is None:
        raise KeyError(item_id)
    lock = show_lock(row["show_id"])
    with store.transaction(lock):
        row = _item(store, item_id)
        _checked(store, row)
        if row["status"] != "ready":
            raise LiveError("Only a ready rundown item can be staged.")
        key = _key(row)
        pending = json.dumps({"status": "staging", "key": key, "started": time.time()})
        store.conn.execute("UPDATE item SET pro_clip = ? WHERE id = ?", (pending, item_id))

    folder = config[0]
    destination = folder / (key + ".mp4")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if not destination.is_file():
            # Pro ignores dot folders. Render on the same volume and expose only the finished MP4.
            with TemporaryDirectory(prefix=".volt-", dir=folder) as scratch:
                render_dir = Path(scratch)
                deliverables.render(store, row, "house", render_dir, timeout=90, verify_frames=True)
                rendered = render_dir / row["asset_id"] / (row["id"] + "-house.mp4")
                with store.transaction(lock):
                    _check_current(store, item_id, key, pending)
                    rendered.replace(destination)
        reply = room.invoke("media.rundown.load", [key])
        data = reply.get("data") if isinstance(reply, dict) else None
        source = data.get("sourceId") if isinstance(data, dict) else None
        if not isinstance(source, str) or not re.fullmatch(r"media:media-[a-f0-9]{12}", source):
            raise LiveError("CoreVideo Pro did not identify the clip in its media bin.")
        with store.transaction(lock):
            _check_current(store, item_id, key, pending)
            store.conn.execute("UPDATE item SET pro_clip = ? WHERE id = ?",
                               (json.dumps({"status": "ready", "key": key, "source": source}), item_id))
    except (LiveError, OSError, deliverables.DeliverableError, subprocess.TimeoutExpired) as exc:
        reason = str(exc) if isinstance(exc, LiveError) else "Could not render or write the clip to the local Pro media bin."
        with store.transaction(lock):
            store.conn.execute("UPDATE item SET pro_clip = ? WHERE id = ? AND pro_clip = ?",
                               (json.dumps({"status": "failed", "key": key, "detail": reason}), item_id, pending))
        raise LiveError(reason) from exc


def _status(row, config):
    if row is None or not row["pro_clip"]:
        return None
    staged = json.loads(row["pro_clip"])
    if staged["key"] != _key(row) or config is None:
        return {"status": "failed", "detail": "The clip or local Pro configuration changed. Send it again."}
    if not row["cleared"] or row["legal_hold"] or row["deleted_at"] or row["essence_open"] or row["tier"] in ("archived", "restoring"):
        return {"status": "failed", "detail": "This clip is no longer cleared and online."}
    if staged["status"] == "staging" and time.time() - staged.get("started", 0) >= STAGE_LEASE_SECONDS:
        return {"status": "failed", "detail": "Sending the clip was interrupted. Send it again."}
    if staged["status"] == "ready" and not (config[0] / (staged["key"] + ".mp4")).is_file():
        return {"status": "failed", "detail": "The staged clip is missing. Send it again."}
    return {k: staged[k] for k in ("status", "detail") if k in staged}


def status(store, item_id):
    return _status(_item(store, item_id), target())


def _ready(store, client, row):
    _checked(store, row)
    config = target()
    observed = _status(row, config)
    if not observed or observed["status"] != "ready":
        raise LiveError("Send this clip to Pro and wait until it is ready.")
    staged = json.loads(row["pro_clip"])
    _, scene, route = config
    return _room(client), staged["source"], [staged["key"], scene] + ([route] if route else [])


def preview(store, client, item_id):
    row = _item(store, item_id)
    if row is None:
        raise KeyError(item_id)
    if row["status"] != "ready":
        raise LiveError("Only a ready rundown item can be previewed.")
    room, _, args = _ready(store, client, row)
    room.invoke("media.rundown.preview", args)


def take(store, client, item_id, attempts, interval, sleep):
    room, source, args = _ready(store, client, _item(store, item_id))
    show_input_take(room, source, attempts, interval, sleep, actions=[], take_command=(COMMAND, args), expected_scene=args[1])


def check_ready(store, client, item_id):
    _ready(store, client, _item(store, item_id))
