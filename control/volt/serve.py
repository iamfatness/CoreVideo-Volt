"""Local session and operator API. python3 -m volt.serve from control/.

GET routes serve the session shell. POST /api/... routes are the operator's hands:
JSON in, JSON out, one store connection per request. See docs/specs/operator-api.md.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from volt import building, media, nle, operator_state, shots
from volt.cues import cues as cues_of
from volt.live import finish_recording, start_recording
from volt import pg
from volt.pg import is_dsn
from volt.playout import HeldError, PlayoutError, advance, hold, hold_state, release, skip
from volt.publish import publish_files
from volt.record import RecordError
from volt.rooms import CoreVideoProRoom, LiveError
from volt.rundown import attach, kill, reorder
from volt.session import list_assets, rundown, seek_hit
from volt.shots import ShotError
from volt.store import NotClearedError, Store
from volt.take import take_item

ROOT = Path(__file__).resolve().parents[1] / "session"
# The operator UI: three same-origin static files, no build step. Open without a token
# (like "/"), so a page can ask for the token; every data route stays gated.
STATIC = {
    "/operator": ("operator/index.html", "text/html; charset=utf-8"),
    "/operator/operator.css": ("operator/operator.css", "text/css; charset=utf-8"),
    "/operator/operator.js": ("operator/operator.js", "text/javascript; charset=utf-8"),
}
# VOLT_DB is a SQLite path or a postgres:// DSN (needs psycopg). Default: local SQLite.
DB_PATH = os.environ.get("VOLT_DB") or str(Path(__file__).resolve().parents[1] / "volt.db")
OUT_DIR = Path(os.environ.get("VOLT_OUT") or Path(__file__).resolve().parents[1] / "out")

# Bearer token. When set, every non-static request must carry it. main() refuses to
# bind off loopback without one.
TOKEN = os.environ.get("VOLT_TOKEN") or None
MIN_REMOTE_TOKEN = 16
MAX_BODY = 64 * 1024

# The room cues and recording talk to. Tests set a FakeRoom here; otherwise it comes
# from VOLT_ROOM_URL / VOLT_ROOM_TOKEN, and with neither, room-backed routes say no_room.
ROOM = None
# How hard take and record wait for the room to show evidence. Tests shrink these.
POLL = {"attempts": 40, "interval": 0.25, "sleep": time.sleep}
# Renders the three versions of a span. Tests swap in a renderer that needs no ffmpeg.
PUBLISH = publish_files

# Take, go and the other sequencing writes run one at a time in this process, so two
# operators cannot both advance the same rundown. Hold and release stay outside it:
# a hold must land while a take is still waiting on the room.
_SEQ = threading.Lock()
_REC = threading.Lock()

_ID = r"([A-Za-z0-9_.:-]{1,128})"


class ConfigError(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status: int, code: str, reason: str, headers=None, **extra):
        super().__init__(reason)
        self.status, self.code, self.reason, self.extra = status, code, reason, extra
        self.headers = headers or {}


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _host_name(header: str) -> str:
    """The host part of a Host header, without port."""
    if header.startswith("["):
        return header.split("]")[0] + "]"
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


def check_bind(host: str, token) -> None:
    """Refuse a non-loopback bind unless a real token is set."""
    if _is_loopback(host):
        return
    if not token:
        raise ConfigError(f"refusing to bind {host!r}: set VOLT_TOKEN to serve beyond loopback")
    if len(token) < MIN_REMOTE_TOKEN:
        raise ConfigError(f"refusing to bind {host!r}: VOLT_TOKEN must be at least {MIN_REMOTE_TOKEN} characters")


def get_room():
    if ROOM is not None:
        return ROOM
    url = os.environ.get("VOLT_ROOM_URL")
    if not url:
        raise ApiError(503, "no_room", "no room is configured (set VOLT_ROOM_URL)")
    return CoreVideoProRoom(url, os.environ.get("VOLT_ROOM_TOKEN") or None)


# ---- state payloads -------------------------------------------------------

def rundown_payload(store: Store, show_id: str) -> dict:
    """The /rundown shape: {hold, items}. Raises KeyError for an unknown show."""
    held = hold_state(store, show_id)
    items = [
        {"id": row.id, "slug": row.slug, "status": row.status, "urls": row.urls,
         "asRunIn": row.as_run_in, "asRunOut": row.as_run_out,
         # Millisecond as-run (migration 11). Rows stamped before it, or by a test clock, have none: the second.
         "asRunInPrecise": row.as_run_in_precise or row.as_run_in,
         "asRunOutPrecise": row.as_run_out_precise or row.as_run_out}
        for row in rundown(store, show_id)
    ]
    return {"hold": None if held is None else {"reason": held.reason, "heldAt": held.held_at},
            "items": items}


def asset_payload(store: Store, asset_id: str) -> dict:
    a = store.get_asset(asset_id)
    return {"id": a.id, "owner": a.owner, "show": a.show_name, "camera": a.camera,
            "cleared": a.cleared, "legalHold": a.legal_hold}


# ---- helpers --------------------------------------------------------------

def _show_exists(store: Store, show_id: str) -> None:
    try:
        hold_state(store, show_id)
    except KeyError:
        raise ApiError(404, "not_found", f"no such show: {show_id}") from None


def _item_show(store: Store, item_id: str) -> tuple[str, str]:
    row = store.conn.execute("SELECT show_id, status FROM item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", f"no such item: {item_id}")
    return row["show_id"], row["status"]


def _asset_exists(store: Store, asset_id: str) -> None:
    try:
        store.get_asset(asset_id)
    except KeyError:
        raise ApiError(404, "not_found", f"no such asset: {asset_id}") from None


def _str(body: dict, key: str, required: bool = False, default=None, limit: int = 500):
    value = body.get(key, default)
    if value is None:
        if required:
            raise ApiError(422, "bad_input", f"{key} is required")
        return default
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        raise ApiError(422, "bad_input", f"{key} must be a string of at most {limit} characters")
    return value


def _bool(body: dict, key: str) -> bool:
    value = body.get(key)
    if not isinstance(value, bool):
        raise ApiError(422, "bad_input", f"{key} must be true or false")
    return value


def _unknown_keys(body: dict, allowed: set) -> None:
    extra = sorted(set(body) - allowed)
    if extra:
        raise ApiError(422, "bad_input", f"unknown field(s): {', '.join(extra)}")


def _advance_errors(fn):
    """Run an advance, mapping the playout refusals to API errors. Nothing has changed when these fire."""
    try:
        return fn()
    except HeldError as exc:
        raise ApiError(409, "held", str(exc)) from None
    except NotClearedError:
        raise ApiError(409, "not_cleared", "the next item's media is not cleared for air (or is on legal hold)") from None
    except KeyError:
        raise ApiError(409, "no_media", "the next item has no media attached") from None


# ---- actions: each takes (store, match groups, body) and returns a dict ----

def act_go(store, show_id, body):
    _unknown_keys(body, set())
    _show_exists(store, show_id)
    with _SEQ:
        adv = _advance_errors(lambda: advance(store, show_id))
        return {"ok": True, "target": adv.target, "previous": adv.previous, "passed": adv.passed,
                "over": adv.target is None, "rundown": rundown_payload(store, show_id)}


def act_hold(store, show_id, body):
    _unknown_keys(body, {"reason"})
    reason = _str(body, "reason", default="")
    _show_exists(store, show_id)
    already = hold_state(store, show_id) is not None
    hold(store, show_id, reason)
    return {"ok": True, "alreadyHeld": already, "rundown": rundown_payload(store, show_id)}


def act_release(store, show_id, body):
    _unknown_keys(body, set())
    _show_exists(store, show_id)
    was = release(store, show_id)
    return {"ok": True, "wasHeld": was, "rundown": rundown_payload(store, show_id)}


def act_take(store, show_id, body):
    _unknown_keys(body, {"owner"})
    owner = _str(body, "owner", default="desk", limit=100)
    _show_exists(store, show_id)
    room = get_room()
    with _SEQ:
        result = _advance_errors(lambda: take_item(store, room, show_id, owner=owner, **POLL))
        results = [{"cue": r.cue_id, "command": r.command, "status": r.status, "detail": r.detail}
                   for r in result.results]
        state = rundown_payload(store, show_id)
    if not result.advanced:
        bad = next(r for r in results if r["status"] != "fired")
        code = "cue_unsupported" if bad["status"] == "unsupported" else "cue_failed"
        raise ApiError(409, code,
                       f"cue {bad['command']} did not land ({bad['detail'] or bad['status']}); "
                       "the rundown is back where it was",
                       item=result.item_id, cue=bad, results=results, rundown=state)
    return {"ok": True, "advanced": True, "item": result.item_id, "results": results, "rundown": state}


def act_reorder(store, show_id, body):
    """Set the order. `version` (the `version` of the /rundown payload the page last saw) makes it conditional: a
    changed list is 409 `stale_order` and nothing moves. Without it the order is set unconditionally (older clients)."""
    _unknown_keys(body, {"items", "version"})
    _show_exists(store, show_id)
    ids = body.get("items")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ApiError(422, "bad_input", "items must be a list of item ids")
    version = _str(body, "version", limit=64)
    _build_errors(lambda: building.reorder(store, show_id, ids, version))
    return {"ok": True, "rundown": rundown_payload(store, show_id), "orderVersion": building.order_version(store, show_id)}


def act_skip(store, item_id, body):
    _unknown_keys(body, set())
    show_id, _ = _item_show(store, item_id)
    with _SEQ:
        try:
            skip(store, item_id)
        except PlayoutError as exc:
            raise ApiError(409, "not_skippable", str(exc)) from None
        return {"ok": True, "item": item_id, "rundown": rundown_payload(store, show_id)}


def act_kill(store, item_id, body):
    _unknown_keys(body, set())
    show_id, _ = _item_show(store, item_id)
    with _SEQ:
        # _SEQ only orders requests inside this process; kill() decides with one conditional write, so a go
        # or take from another process cannot slip between a read here and the write.
        if not kill(store, item_id):
            status = store.conn.execute("SELECT status FROM item WHERE id = ?", (item_id,)).fetchone()["status"]
            raise ApiError(409, "not_killable", f"cannot kill a {status} item")
        return {"ok": True, "item": item_id, "rundown": rundown_payload(store, show_id)}


def act_attach(store, item_id, body):
    _unknown_keys(body, {"span"})
    span_id = _str(body, "span", required=True, limit=128)
    show_id, status = _item_show(store, item_id)
    if store.conn.execute("SELECT 1 FROM span WHERE id = ?", (span_id,)).fetchone() is None:
        raise ApiError(404, "not_found", f"no such span: {span_id}")
    with _SEQ:
        if status != "ready":
            raise ApiError(409, "not_attachable", f"cannot attach media to a {status} item")
        attach(store, item_id, span_id)
        return {"ok": True, "item": item_id, "span": span_id, "rundown": rundown_payload(store, show_id)}


def act_record_start(store, _unused, body):
    _unknown_keys(body, {"owner", "camera", "show"})
    owner = _str(body, "owner", default="desk", limit=100)
    camera = _str(body, "camera", limit=100)
    show = _str(body, "show", limit=200)
    room = get_room()
    with _REC:
        try:
            rec = start_recording(store, room, owner, camera=camera, show_name=show, **POLL)
        except LiveError as exc:
            raise ApiError(502, "room_refused", str(exc)) from None
        return {"ok": True, "minted": rec.minted, "essence": rec.essence_id,
                "asset": asset_payload(store, rec.asset_id)}


def act_record_stop(store, _unused, body):
    _unknown_keys(body, {"asset"})
    asset_id = _str(body, "asset", required=True, limit=128)
    _asset_exists(store, asset_id)
    room = get_room()
    with _REC:
        try:
            checksum = finish_recording(store, room, asset_id, **POLL)
        except (LiveError, RecordError) as exc:
            raise ApiError(502, "not_wrapped", str(exc)) from None
        return {"ok": True, "checksum": checksum, "asset": asset_payload(store, asset_id)}


def act_cleared(store, asset_id, body):
    _unknown_keys(body, {"cleared"})
    value = _bool(body, "cleared")
    _asset_exists(store, asset_id)
    store.set_cleared(asset_id, value)
    return {"ok": True, "asset": asset_payload(store, asset_id)}


def act_legal_hold(store, asset_id, body):
    _unknown_keys(body, {"held"})
    value = _bool(body, "held")
    _asset_exists(store, asset_id)
    store.set_hold(asset_id, value)
    return {"ok": True, "asset": asset_payload(store, asset_id)}


def act_publish(store, span_id, body):
    _unknown_keys(body, set())
    span = store.conn.execute("SELECT asset_id FROM span WHERE id = ?", (span_id,)).fetchone()
    if span is None:
        raise ApiError(404, "not_found", f"no such span: {span_id}")
    try:
        store.assert_publishable(span["asset_id"])
    except NotClearedError:
        raise ApiError(409, "not_cleared", "the asset is not cleared, or is on legal hold") from None
    versions = [{"id": v.id, "kind": v.kind, "status": v.status, "url": v.url}
                for v in PUBLISH(store, span_id, OUT_DIR)]
    payload = {"ok": all(v["status"] == "done" for v in versions), "span": span_id, "versions": versions}
    if not payload["ok"] and any(v["status"] == "busy" for v in versions) \
            and not any(v["status"] == "failed" for v in versions):
        raise ApiError(409, "publish_busy", "another writer is publishing this span; try again shortly", **{
            "span": span_id, "versions": versions})
    if not payload["ok"]:
        raise ApiError(502, "publish_failed", "one or more versions failed to render", **{
            "span": span_id, "versions": versions})
    return payload


# ---- shot spans: marking (slice 4 of docs/specs/shot-spans.md) ------------------------------------
# Every rule (range, overlap, in-use) lives in volt/shots.py; these handlers only read the body, call it, and
# map its refusals to HTTP. Marking is allowed on any asset: spans carry no rights (the asset does), and an
# operator has to be able to log footage to decide what to clear. Publish and air keep their gate.

SHOT_STATUS = {"not_found": 404, "bad_input": 422, "bad_timecode": 422, "outside_source": 422, "engine_missing": 503}


def _shot_errors(fn):
    """Run a shots.* call, mapping its ShotError to an ApiError. Nothing has changed when this fires.
    404 and 422 are the request's fault; everything else (overlap, in use, not editable) is a conflict."""
    try:
        return fn()
    except ShotError as exc:
        raise ApiError(SHOT_STATUS.get(exc.code, 409), exc.code, exc.reason) from None


def _span_payload(store: Store, span: dict) -> dict:
    """shots.span_json plus `blockedBy`, in the same words as /api/state (the page shows it next to the span)."""
    row = store.conn.execute("SELECT cleared, legal_hold FROM asset WHERE id = ?", (span["assetId"],)).fetchone()
    blocked = operator_state._blocked_by(bool(row["cleared"]), bool(row["legal_hold"])) if row else None
    return {**span, "blockedBy": blocked}


def act_span_create(store, asset_id, body):
    """Either `tc_in` + `tc_out`, or `from_words` (the first and last word span IDs) with optional `handle_in`,
    `handle_out` (frames) and `snap` ("shot"): then the server computes the times from the stored words."""
    _unknown_keys(body, {"kind", "tc_in", "tc_out", "label", "from_words", "handle_in", "handle_out", "snap"})
    kind = _str(body, "kind", required=True, limit=20)
    label = _str(body, "label", limit=shots.LABEL_LIMIT)
    _asset_exists(store, asset_id)
    if "from_words" in body:
        stray = [k for k in ("tc_in", "tc_out") if k in body]
        if stray:
            raise ApiError(422, "bad_input", f"from_words and {stray[0]} cannot both be given: the server computes the times")
        words = body["from_words"]
        if (not isinstance(words, list) or len(words) != 2
                or not all(isinstance(w, str) and 0 < len(w) <= 128 for w in words)):
            raise ApiError(422, "bad_input", "from_words must be [first word span id, last word span id]")
        if kind != shots.CLIP:
            raise ApiError(422, "bad_input", "from_words makes a clip; kind must be 'clip'")
        snap = body.get("snap")
        span = _shot_errors(lambda: shots.create_from_words(
            store, asset_id, words[0], words[1], handle_in=body.get("handle_in"), handle_out=body.get("handle_out"),
            snap=snap, label=label))
        return {"ok": True, "span": _span_payload(store, span)}
    stray = [k for k in ("handle_in", "handle_out", "snap") if k in body]
    if stray:
        raise ApiError(422, "bad_input", f"{stray[0]} only applies with from_words")
    tc_in = _str(body, "tc_in", required=True, limit=32)
    tc_out = _str(body, "tc_out", required=True, limit=32)
    span = _shot_errors(lambda: shots.create(store, asset_id, kind, tc_in, tc_out, label=label))
    return {"ok": True, "span": _span_payload(store, span)}


def act_span_trim(store, span_id, body):
    _unknown_keys(body, {"tc_in", "tc_out"})
    tc_in = _str(body, "tc_in", limit=32)
    tc_out = _str(body, "tc_out", limit=32)
    span = _shot_errors(lambda: shots.trim(store, span_id, tc_in, tc_out))
    return {"ok": True, "span": _span_payload(store, span)}


def act_span_label(store, span_id, body):
    _unknown_keys(body, {"label"})
    if "label" not in body:
        raise ApiError(422, "bad_input", "label is required (null clears it)")
    label = _str(body, "label", limit=shots.LABEL_LIMIT)
    span = _shot_errors(lambda: shots.label(store, span_id, label))
    return {"ok": True, "span": _span_payload(store, span)}


def act_span_delete(store, span_id, body):
    _unknown_keys(body, set())
    _shot_errors(lambda: shots.delete(store, span_id))
    return {"ok": True, "deleted": span_id}


def act_span_split(store, span_id, body):
    _unknown_keys(body, {"at"})
    at = _str(body, "at", required=True, limit=32)
    left, right = _shot_errors(lambda: shots.split(store, span_id, at))
    return {"ok": True, "spans": [_span_payload(store, left), _span_payload(store, right)]}


def act_span_merge(store, span_id, body):
    _unknown_keys(body, {"with"})
    other = _str(body, "with", required=True, limit=128)
    span = _shot_errors(lambda: shots.merge(store, span_id, other))
    return {"ok": True, "span": _span_payload(store, span), "removed": other}


def act_detect(store, asset_id, body):
    """Queue a detection run (a runner does the work). Rights are not consulted. A repeat of a finished run is a
    no-op with `alreadyDone: true`; a repeat of a queued or running one is that same job."""
    _unknown_keys(body, {"threshold", "min_frames"})
    _asset_exists(store, asset_id)
    job, already_done = _shot_errors(lambda: shots.request_detect_checked(
        store, asset_id, body.get("threshold"), body.get("min_frames")))
    return {"ok": True, "alreadyDone": already_done, "job": job}


def act_span_frames(store, asset_id, body):
    """Queue the span-frames backfill if the asset has spans still missing their frame columns. Safe to repeat."""
    _unknown_keys(body, set())
    _asset_exists(store, asset_id)
    job = shots.request_span_frames(store, asset_id)
    return {"ok": True, "queued": job is not None, "pendingFrames": shots.pending_frames(store, asset_id),
            "job": shots.indexing_job(store, asset_id)}


def act_export_nle(store, _unused, body):
    """Stateless: builds the file from the span IDs sent and returns it. Writes nothing."""
    try:
        return nle.export(store, body, os.environ.get("VOLT_EXPORT_PATH_MAP"))
    except nle.NleError as exc:
        raise ApiError(exc.status, exc.code, exc.reason, **exc.extra) from None


# ---- show building: create, edit, cue and ingest (docs/specs/operator-api.md, "Show building") ------------------
# Rules live in volt/building.py; these handlers read the body, call it, and map BuildError to HTTP. They take no
# _SEQ: each edit is one transaction under the show's own lock, which go and take also take, in every process.

def _build_errors(fn):
    try:
        return fn()
    except building.BuildError as exc:
        raise ApiError(exc.status, exc.code, exc.reason, **exc.extra) from None


def _client_id(body: dict):
    """Optional caller-chosen id: a retry of the same create is then a 200 `existed`, not a second show or item."""
    value = _str(body, "id", limit=128)
    if value is not None and not re.fullmatch(_ID, value):
        raise ApiError(422, "bad_input", "id may use letters, digits and _ . : - only (1 to 128)")
    return value


def act_show_create(store, _unused, body):
    _unknown_keys(body, {"title", "id"})
    title = _str(body, "title", required=True, limit=200).strip()
    show_id, created = _build_errors(lambda: building.create_show(store, title, _client_id(body)))
    return {"ok": True, "show": {"id": show_id, "title": title}, "existed": not created,
            "rundown": rundown_payload(store, show_id),
            "orderVersion": building.order_version(store, show_id)}


def act_item_add(store, show_id, body):
    _unknown_keys(body, {"slug", "script", "id"})
    slug = _str(body, "slug", required=True, limit=200).strip()
    script = _str(body, "script", default="", limit=20000)
    item_id = _client_id(body)
    _show_exists(store, show_id)
    item, created = _build_errors(lambda: building.add_item(store, show_id, slug, script, item_id))
    return {"ok": True, "item": item, "existed": not created, "rundown": rundown_payload(store, show_id),
            "orderVersion": building.order_version(store, show_id)}


def act_item_rename(store, item_id, body):
    _unknown_keys(body, {"slug", "script"})
    slug = _str(body, "slug", limit=200)
    script = _str(body, "script", limit=20000)
    if slug is None and script is None:
        raise ApiError(422, "bad_input", "give slug, script or both")
    if slug is not None:
        slug = slug.strip()
        if not slug:
            raise ApiError(422, "bad_input", "slug cannot be empty")
    show_id, _ = _item_show(store, item_id)
    _build_errors(lambda: building.rename_item(store, item_id, slug, script))
    return {"ok": True, "item": item_id, "rundown": rundown_payload(store, show_id),
            "orderVersion": building.order_version(store, show_id)}


def act_item_remove(store, item_id, body):
    _unknown_keys(body, set())
    _item_show(store, item_id)
    show_id = _build_errors(lambda: building.remove_item(store, item_id))
    return {"ok": True, "removed": item_id, "rundown": rundown_payload(store, show_id),
            "orderVersion": building.order_version(store, show_id)}


def act_item_detach(store, item_id, body):
    _unknown_keys(body, set())
    _item_show(store, item_id)
    show_id, was = _build_errors(lambda: building.detach_span(store, item_id))
    return {"ok": True, "item": item_id, "wasAttached": was, "rundown": rundown_payload(store, show_id),
            "orderVersion": building.order_version(store, show_id)}


def _cue_json(store, cue_id: str) -> dict:
    row = store.conn.execute("SELECT item_id FROM cue WHERE id = ?", (cue_id,)).fetchone()
    c = next(c for c in cues_of(store, row["item_id"]) if c.id == cue_id)
    return {"id": c.id, "room": c.room, "command": c.command, "payload": c.payload, "status": c.status,
            "verified": c.command in building.HANDLERS,
            "failure": None if c.fail_status is None else {"status": c.fail_status, "detail": c.fail_detail}}


def act_cue_add(store, item_id, body):
    _unknown_keys(body, {"room", "command", "payload"})
    room = _str(body, "room", default=building.CUE_ROOM, limit=100)
    command = _str(body, "command", required=True, limit=100)
    payload = _str(body, "payload", default="", limit=2000)
    show_id, _ = _item_show(store, item_id)
    cue_id, _show, verified = _build_errors(lambda: building.add_cue(store, item_id, room, command, payload))
    out = {"ok": True, "cue": _cue_json(store, cue_id), "rundown": rundown_payload(store, show_id),
            "orderVersion": building.order_version(store, show_id)}
    if not verified:
        out["warning"] = (f"{command} has no verified adapter yet: the cue is stored, but a take stops at it "
                          "with cue_unsupported")
    return out


def act_cue_remove(store, cue_id, body):
    _unknown_keys(body, set())
    item_id, show_id = _build_errors(lambda: building.remove_cue(store, cue_id))
    return {"ok": True, "removed": cue_id, "item": item_id, "rundown": rundown_payload(store, show_id),
            "orderVersion": building.order_version(store, show_id)}


def act_ingest(store, _unused, body):
    """Ingest one file from an allowed folder (VOLT_INGEST_ROOTS). Runs in the request: probe and checksum of a
    large file take as long as they take. Replay is safe (same file, same asset)."""
    _unknown_keys(body, {"path", "owner", "show", "camera"})
    path = _str(body, "path", required=True, limit=4096)
    owner = _str(body, "owner", default="desk", limit=100)
    out = _build_errors(lambda: building.ingest_allowed(
        store, path, Path(os.environ.get("VOLT_QUARANTINE") or OUT_DIR / "quarantine"), owner,
        _str(body, "show", limit=200), _str(body, "camera", limit=100)))
    return {"ok": True, **out, "assetState": asset_payload(store, out["asset"])}


def show_building_get(handler, store, path: str, query: dict) -> bool:
    """GET /api/cue-commands and GET /api/items/{item}/cues. Returns True when it answered."""
    m = re.fullmatch(rf"/api/items/{_ID}/cues", path)
    if path != "/api/cue-commands" and not m:
        return False
    if query:
        raise ApiError(422, "bad_input", "this route takes no parameters")
    if m:
        _item_show(store, m.group(1))
        handler._json(200, {"ok": True, "item": m.group(1),
                            "cues": [_cue_json(store, c.id) for c in cues_of(store, m.group(1))],
                            "commands": building.cue_commands()})
    else:
        handler._json(200, {"ok": True, "commands": building.cue_commands()})
    return True


# Reads on an asset and on a span, served by do_GET. IDs are path segments, never file names.
READ_ROUTES = re.compile(rf"^/api/assets/{_ID}/(spans|transcript|proxy)$")
THUMB_ROUTE = re.compile(rf"^/api/spans/{_ID}/thumb$")

ROUTES = [
    (re.compile(rf"^/api/shows/{_ID}/go$"), act_go),
    (re.compile(rf"^/api/shows/{_ID}/hold$"), act_hold),
    (re.compile(rf"^/api/shows/{_ID}/release$"), act_release),
    (re.compile(rf"^/api/shows/{_ID}/take$"), act_take),
    (re.compile(rf"^/api/shows/{_ID}/reorder$"), act_reorder),
    (re.compile(rf"^/api/items/{_ID}/skip$"), act_skip),
    (re.compile(rf"^/api/items/{_ID}/kill$"), act_kill),
    (re.compile(rf"^/api/items/{_ID}/attach$"), act_attach),
    (re.compile(r"^/api/record/start$()"), act_record_start),
    (re.compile(r"^/api/record/stop$()"), act_record_stop),
    (re.compile(rf"^/api/assets/{_ID}/cleared$"), act_cleared),
    (re.compile(rf"^/api/assets/{_ID}/legal-hold$"), act_legal_hold),
    (re.compile(rf"^/api/spans/{_ID}/publish$"), act_publish),
    (re.compile(rf"^/api/assets/{_ID}/spans$"), act_span_create),
    (re.compile(rf"^/api/spans/{_ID}/trim$"), act_span_trim),
    (re.compile(rf"^/api/spans/{_ID}/label$"), act_span_label),
    (re.compile(rf"^/api/spans/{_ID}/delete$"), act_span_delete),
    (re.compile(rf"^/api/spans/{_ID}/split$"), act_span_split),
    (re.compile(rf"^/api/spans/{_ID}/merge$"), act_span_merge),
    (re.compile(rf"^/api/assets/{_ID}/shots/detect$"), act_detect),
    (re.compile(rf"^/api/assets/{_ID}/span-frames$"), act_span_frames),
    (re.compile(r"^/api/exports/nle$()"), act_export_nle),
]
ROUTES += [  # show building
    (re.compile(r"^/api/shows$()"), act_show_create),
    (re.compile(rf"^/api/shows/{_ID}/items$"), act_item_add),
    (re.compile(rf"^/api/items/{_ID}/rename$"), act_item_rename),
    (re.compile(rf"^/api/items/{_ID}/remove$"), act_item_remove),
    (re.compile(rf"^/api/items/{_ID}/detach$"), act_item_detach),
    (re.compile(rf"^/api/items/{_ID}/cues$"), act_cue_add),
    (re.compile(rf"^/api/cues/{_ID}/remove$"), act_cue_remove),
    (re.compile(r"^/api/ingest$()"), act_ingest),
]


# ---- HTTP -----------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _gate(self) -> bool:
        """Auth and host checks. Sends the refusal and returns False when the request must stop."""
        token = TOKEN
        try:
            if token:
                header = self.headers.get("Authorization", "")
                scheme, _, given = header.partition(" ")
                ok = scheme.lower() == "bearer" and hmac.compare_digest(
                    given.strip().encode("utf-8"), token.encode("utf-8"))
                if not ok:
                    raise ApiError(401, "unauthorized", "missing or wrong bearer token")
            else:
                # No token means loopback only. A rebinding page would arrive with a foreign Host.
                host = _host_name(self.headers.get("Host") or "")
                if not _is_loopback(host):
                    raise ApiError(403, "bad_host", "this server answers on loopback only")
        except ApiError as err:
            self._error(err)
            return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path in STATIC:
            self._static(path)
            return
        if path != "/" and not self._gate():
            return
        # One connection per request. Postgres schema is applied once in main().
        store = Store(DB_PATH, init=not is_dsn(DB_PATH))
        try:
            self._route(store)
        except ApiError as err:
            self._error(err)
        except media.MediaError as err:
            self._error(ApiError(err.status, err.code, err.reason, headers=err.headers))
        except Exception:  # same rule as POST: no leaked internals, and the client always gets a response
            import traceback
            traceback.print_exc()
            self._error(ApiError(500, "internal", "internal error"))
        finally:
            store.close()

    def do_POST(self):
        if not self._gate():
            return
        store = None
        try:
            path = urlparse(self.path).path
            for pattern, fn in ROUTES:
                m = pattern.match(path)
                if m:
                    break
            else:
                raise ApiError(404, "not_found", "no such route")
            body = self._read_json()
            store = Store(DB_PATH, init=not is_dsn(DB_PATH))
            self._json(200, fn(store, m.group(1), body))
        except ApiError as err:
            self._error(err)
        except Exception:  # never leak internals; the cause is in the server's stderr
            import traceback
            traceback.print_exc()
            self._error(ApiError(500, "internal", "internal error; nothing further was changed by this handler"))
        finally:
            if store is not None:
                store.close()

    def _read_json(self) -> dict:
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "length_required", "send Content-Length; chunked bodies are not accepted")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(422, "bad_input", "bad Content-Length") from None
        if length < 0:
            raise ApiError(422, "bad_input", "bad Content-Length")
        if length > MAX_BODY:
            self.close_connection = True
            raise ApiError(413, "too_large", f"body is over {MAX_BODY} bytes")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            # Also keeps cross-site forms out: only JSON, which a browser will not send without a preflight.
            raise ApiError(415, "unsupported_media_type", "Content-Type must be application/json")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(422, "bad_input", "body is not valid JSON") from None
        if not isinstance(body, dict):
            raise ApiError(422, "bad_input", "body must be a JSON object")
        return body

    def _route(self, STORE):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = (ROOT / "index.html").read_bytes()
            self._send(200, "text/html", body)
            return
        if parsed.path == "/api/state":
            self._json(200, self._operator_state(STORE, parse_qs(parsed.query).get("show", [""])[0]))
            return
        if parsed.path == "/assets":
            rows = [
                {"id": row.id, "show": row.show_name, "camera": row.camera, "open": row.open}
                for row in list_assets(STORE)
            ]
            self._send(200, "application/json", json.dumps(rows).encode())
            return
        if parsed.path == "/rundown":
            show_id = parse_qs(parsed.query).get("show", [""])[0]
            if not show_id:
                self._json(200, {"hold": None, "items": []})
                return
            try:
                payload = rundown_payload(STORE, show_id)
            except KeyError:
                raise ApiError(404, "not_found", f"no such show: {show_id}") from None
            self._json(200, payload)
            return
        if parsed.path == "/seek":
            query = parse_qs(parsed.query).get("q", [""])[0]
            hit = seek_hit(STORE, query)
            payload = {} if hit is None else {
                "asset_id": hit.asset_id,
                "span_id": hit.span_id,
                "tc_in": hit.tc_in,
                "text": hit.text,
                "proxy": hit.proxy,
            }
            self._send(200, "application/json", json.dumps(payload).encode())
            return
        if show_building_get(self, STORE, parsed.path, parse_qs(parsed.query, keep_blank_values=True)):
            return
        m = READ_ROUTES.match(parsed.path)
        if m:
            asset_id, what = m.group(1), m.group(2)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if what == "spans":
                self._json(200, media.list_spans(STORE, asset_id, query))
            elif what == "transcript":
                self._json(200, media.transcript(STORE, asset_id, query))
            else:
                if query:
                    raise ApiError(422, "bad_input", "the proxy route takes no parameters")
                self._proxy(media.proxy_for_asset(STORE, asset_id))
            return
        m = THUMB_ROUTE.match(parsed.path)
        if m:
            if parsed.query:
                raise ApiError(422, "bad_input", "the thumb route takes no parameters")
            path = media.ensure_thumb(STORE, m.group(1), OUT_DIR)
            self._send_headers(200, "image/jpeg", path.stat().st_size,
                               {"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})
            self.wfile.write(path.read_bytes())
            return
        if parsed.path == "/media":
            # The old shell's player. `path` must be a registered proxy's own location, compared as a string.
            query = parse_qs(parsed.query, keep_blank_values=True)
            given = query.get("path", [])
            if set(query) != {"path"} or len(given) != 1:
                raise ApiError(422, "bad_input", "give exactly one path parameter")
            self._proxy(media.proxy_by_location(STORE, given[0]))
            return
        self._send(404, "text/plain", b"not found")

    def _send_headers(self, status: int, content_type: str, length: int, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _proxy(self, proxy: media.Proxy):
        """Stream a registered proxy, honouring one byte range. The file is opened before any header is sent, so
        a missing file is a clean 404; the size comes from the open handle so range and length agree."""
        with media.open_proxy(proxy) as fh:
            size = os.fstat(fh.fileno()).st_size
            # An If-Range we cannot validate (no ETag, and the file may still be growing) means: send it all.
            rng = None if self.headers.get("If-Range") else media.parse_range(self.headers.get("Range"), size)
            first, last = (0, size - 1) if rng is None else rng
            length = max(0, last - first + 1)
            extra = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-cache",
                     "X-Content-Type-Options": "nosniff"}
            if rng is not None:
                extra["Content-Range"] = f"bytes {first}-{last}/{size}"
            self._send_headers(206 if rng is not None else 200, media.content_type(proxy.path), length, extra)
            fh.seek(first)
            left = length
            try:
                while left > 0:
                    chunk = fh.read(min(65536, left))
                    if not chunk:           # the file shrank under us: the length is now a lie, so drop the connection
                        self.close_connection = True
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True

    def _static(self, path: str):
        name, ctype = STATIC[path]
        # Host check only: with no token this server answers loopback only (DNS rebinding guard).
        if not TOKEN and not _is_loopback(_host_name(self.headers.get("Host") or "")):
            self._error(ApiError(403, "bad_host", "this server answers on loopback only"))
            return
        body = (ROOT / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # Same-origin only: nothing external may load, and the page may not be framed.
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; base-uri 'none'; "
                         "form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _operator_state(self, store, show_id: str) -> dict:
        """GET /api/state?show=<id>. No show means the newest. The /rundown shape stays in rundown_payload."""
        listing = operator_state.shows(store)
        if not show_id and listing:
            show_id = listing[-1]["id"]
        rundown = None
        if show_id:
            try:
                rundown = rundown_payload(store, show_id)
            except KeyError:  # a remembered show that no longer exists: fall back to the newest
                show_id = listing[-1]["id"] if listing else None
                rundown = rundown_payload(store, show_id) if show_id else None
        state = operator_state.build(store, rundown, show_id or None, operator_state.room_state(get_room))
        # What a reorder must quote to be conditional on the list the page is showing (building.order_version).
        state["orderVersion"] = building.order_version(store, show_id) if show_id and rundown is not None else None
        return state

    def _json(self, status: int, payload: dict):
        self._send(status, "application/json", json.dumps(payload).encode())

    def _error(self, err: ApiError):
        payload = {"ok": False, "error": {"code": err.code, "reason": err.reason}}
        payload.update(err.extra)
        self.send_response(err.status)
        if err.status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        for name, value in err.headers.items():
            self.send_header(name, value)
        body = json.dumps(payload).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if err.status == 413:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def main():
    host = os.environ.get("VOLT_HOST") or "127.0.0.1"
    port = int(os.environ.get("VOLT_PORT") or 8765)
    try:
        check_bind(host, TOKEN)
    except ConfigError as exc:
        raise SystemExit(f"volt: {exc}")
    # Optional Postgres pool (VOLT_PG_POOL_MAX > 0; see volt/pg.py). Opened before the bootstrap so it fails
    # at startup if the database is unreachable, and so requests borrow instead of connecting.
    try:
        pool = pg.configure_pool_from_env(DB_PATH)
    except Exception as exc:  # noqa: BLE001 - bad option, missing psycopg_pool, unreachable database
        raise SystemExit(f"volt: postgres pool: {exc}")
    try:
        Store(DB_PATH).close()  # bootstrap the schema once
        server = ThreadingHTTPServer((host, port), Handler)
        print(f"Volt session on http://{host}:{port}" + (" (token required)" if TOKEN else "")
              + (f" (postgres pool of up to {pool.max_size})" if pool else ""))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        pg.close_pools()


if __name__ == "__main__":
    main()
