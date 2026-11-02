"""Local session and operator API. python3 -m volt.serve from control/.

GET `/` and `/operator` serve the operator UI. POST /api/... routes are the operator's hands:
JSON in, JSON out, one store connection per request. See docs/specs/operator-api.md.
"""

from __future__ import annotations

import hmac
import ipaddress
import argparse
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from volt import archive, building, mos_send, health, logs, media, mos, nle, operator_state, retention, shots, tickets, users
from volt.clock import set_recurrence, set_relative, set_schedule, to_utc_text
from volt.paths import package_root
from volt.cues import cues as cues_of
from volt.migrations import SchemaTooNewError
from volt.live import finish_recording, start_recording
from volt import pg
from volt.pg import is_dsn
from volt.playout import HeldError, PlayoutError, TakingError, advance, hold, hold_state, release, skip
from volt import publish as publish_mod
from volt.publish import publish_files
from volt.record import RecordError
from volt.rooms import CoreVideoProRoom, LiveError
from volt.rundown import ItemNotReady, attach, kill, reorder
from volt.session import list_assets, rundown, seek_hit
from volt.shots import ShotError
from volt.store import ArchivedError, NotClearedError, Store
from volt.show_input import configured_actions
from volt.take import take_item

log = logging.getLogger("volt.serve")
access = logging.getLogger("volt.access")
EX_CONFIG = 78  # exit code for "the configuration is wrong; restarting will not help" (volt run stops on it)
ACCESS_LOG = False  # one line per request when on (VOLT_ACCESS_LOG=1 or --access-log); errors are always logged

ROOT = package_root() / "session"
# The operator UI: three same-origin static files, no build step. Open without a token
# (like "/"), so a page can ask for the token; every data route stays gated.
STATIC = {
    "/": ("operator/index.html", "text/html; charset=utf-8"),
    "/operator": ("operator/index.html", "text/html; charset=utf-8"),
    "/operator/operator.css": ("operator/operator.css", "text/css; charset=utf-8"),
    "/operator/operator.js": ("operator/operator.js", "text/javascript; charset=utf-8"),
    "/operator/theme.js": ("operator/theme.js", "text/javascript; charset=utf-8"),
}
# VOLT_DB is a SQLite path or a postgres:// DSN (needs psycopg). Default: local SQLite.
DB_PATH = os.environ.get("VOLT_DB") or str(package_root().parent / "volt.db")
OUT_DIR = Path(os.environ.get("VOLT_OUT") or package_root().parent / "out")

# Bearer token. When set, every non-static request must carry it. main() refuses to
# bind off loopback without one.
TOKEN = os.environ.get("VOLT_TOKEN") or None
MEDIA_KEY = os.environ.get("VOLT_MEDIA_KEY") or None
MIN_REMOTE_TOKEN = 16
MAX_BODY = 64 * 1024

# The room cues and recording talk to. Tests set a FakeRoom here; otherwise it comes
# from VOLT_ROOM_URL / VOLT_ROOM_TOKEN, and with neither, room-backed routes say no_room.
ROOM = None
# How hard take and record wait for the room to show evidence. Tests shrink these.
POLL = {"attempts": 40, "interval": 0.25, "sleep": time.sleep}
# Renders the three versions of a span. Tests swap in a renderer that needs no ffmpeg.
PUBLISH = publish_files
# The storage backend unpublish deletes through. None = what VOLT_STORAGE says (local files under VOLT_OUT by default). Tests set one.
UNPUBLISH_BACKEND = None

# Take, go and the other sequencing writes run one at a time in this process, so two
# operators cannot both advance the same rundown. Hold and release stay outside it:
# a hold must land while a take is still waiting on the room.
_SEQ = threading.Lock()
_REC = threading.Lock()

_ID = r"([A-Za-z0-9_.:-]{1,128})"
_ACTOR = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
_AUDIT_KIND = {
    "shows": "show",
    "items": "item",
    "assets": "asset",
    "spans": "span",
    "versions": "version",
    "cues": "cue",
    "record": "asset",
    "ingest": "asset",
    "exports": "export",
    "users": "user",
}


class Principal:
    """Who a request is: a per-user token (name, role), or the legacy VOLT_TOKEN / open loopback (desk, admin)."""

    def __init__(self, name: str, role: str, legacy: bool):
        self.name, self.role, self.legacy = name, role, legacy


LEGACY_ACTOR = "desk"


class ConfigError(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status: int, code: str, reason: str, headers=None, **extra):
        super().__init__(reason)
        self.status, self.code, self.reason, self.extra = status, code, reason, extra
        self.headers = headers or {}


class TicketDenied(Exception):
    """Wrong or expired media ticket: 401 with an empty body, so a <video> Range gets no bytes."""


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


def check_bind(host: str, token, has_admin_user: bool = False) -> None:
    """Refuse a non-loopback bind unless a real token is set, or (with no VOLT_TOKEN) an enabled admin user exists."""
    if _is_loopback(host):
        return
    if not token and has_admin_user:
        return
    if not token:
        raise ConfigError(f"refusing to bind {host!r}: set VOLT_TOKEN to serve beyond loopback "
                          "(or create an admin with `volt user add NAME --role admin`)")
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

def _sched_text(value):
    """A stored schedule time as UTC `YYYY-MM-DDTHH:MM:SSZ` (Postgres hands back a datetime, SQLite the text)."""
    return None if value is None else to_utc_text(value)


def rundown_payload(store: Store, show_id: str) -> dict:
    """The /rundown shape: {hold, items, slotSeconds}. Raises KeyError for an unknown show."""
    held = hold_state(store, show_id)
    slot = store.conn.execute("SELECT slot_seconds, recurrence, recurrence_of FROM show WHERE id = ?", (show_id,)).fetchone()
    sched = {r["id"]: r for r in store.conn.execute(
        "SELECT id, scheduled_at, schedule_state, schedule_note, schedule_mode, schedule_offset_seconds FROM item WHERE show_id = ?",
        (show_id,)).fetchall()}
    items = [
        {"id": row.id, "slug": row.slug, "status": row.status, "urls": row.urls,
         "asRunIn": row.as_run_in, "asRunOut": row.as_run_out,
         # Millisecond as-run (migration 13). Rows stamped before it, or by a test clock, have none: the second.
         "asRunInPrecise": row.as_run_in_precise or row.as_run_in,
         "asRunOutPrecise": row.as_run_out_precise or row.as_run_out,
         "scheduledAt": _sched_text(sched[row.id]["scheduled_at"]) if row.id in sched else None,
         "scheduleState": sched[row.id]["schedule_state"] if row.id in sched else None,
         "scheduleNote": sched[row.id]["schedule_note"] if row.id in sched else None,
         "scheduleMode": sched[row.id]["schedule_mode"] if row.id in sched else None,
         "scheduleOffsetSeconds": sched[row.id]["schedule_offset_seconds"] if row.id in sched else None}
        for row in rundown(store, show_id)
    ]
    return {"hold": None if held is None else {"reason": held.reason, "heldAt": held.held_at},
            "items": items, "slotSeconds": slot[0] if slot else None,
            "recurrence": json.loads(slot[1]) if slot and slot[1] else None, "recurrenceOf": slot[2] if slot else None}


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


def _actor(headers, body: dict, principal=None) -> str:
    """A per-user token's own name (the header is ignored). Otherwise X-Volt-Actor, else body owner, else desk.
    Invalid is 422 before the mutation runs."""
    if principal is not None and not principal.legacy:
        return principal.name
    given = headers.get("X-Volt-Actor")
    if given is None:
        given = body["owner"] if "owner" in body else "desk"
    if not isinstance(given, str) or not _ACTOR.fullmatch(given):
        raise ApiError(422, "bad_actor", "actor must be 1-100 characters matching [A-Za-z0-9._:-]")
    return given


def _audit_fields(path: str, captured: str, body: dict, out: dict) -> tuple[str, str, str]:
    parts = [p for p in path.split("/") if p]
    action = parts[-1].replace("-", "_") if parts else path
    if len(parts) >= 3 and parts[1] == "assets" and action == "delete":
        action = "delete_asset"
    if len(parts) >= 2 and parts[1] == "record":
        action = "record_" + parts[-1]
    if len(parts) >= 2 and parts[1] == "users":
        action = "user_" + (parts[-1] if len(parts) > 2 else "add")
        if not captured and isinstance(out.get("user"), dict):
            captured = out["user"].get("name") or ""
    kind = _AUDIT_KIND.get(parts[1] if len(parts) > 1 else "", "unknown")
    target = captured or ""
    if not target:
        asset = out.get("asset")
        if isinstance(asset, str):
            target = asset
        elif isinstance(out.get("show"), dict):
            target = out["show"].get("id") or ""
        elif isinstance(body.get("asset"), str):
            target = body["asset"]
    if not target:
        target = path
    return action, kind, target


def _advance_errors(fn):
    """Run an advance, mapping the playout refusals to API errors. Nothing has changed when these fire."""
    try:
        return fn()
    except HeldError as exc:
        raise ApiError(409, "held", str(exc)) from None
    except TakingError as exc:
        raise ApiError(409, "taking", str(exc), item=exc.item_id) from None
    except ArchivedError:
        raise ApiError(409, "archived", "the next item's media is archived; rehydrate the asset first") from None
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


def act_show_slot(store, show_id, body):
    """Set or clear the show's planned length. {seconds: 1..86399 | null}; nothing else is accepted."""
    _unknown_keys(body, {"seconds"})
    if "seconds" not in body:
        raise ApiError(422, "bad_input", "seconds is required (a whole number, or null to clear the slot)")
    seconds = body["seconds"]
    if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 86399):
        raise ApiError(422, "bad_input", "seconds must be a whole number from 1 to 86399, or null")
    _show_exists(store, show_id)
    store.conn.execute("UPDATE show SET slot_seconds = ? WHERE id = ?", (seconds, show_id))
    store.conn.commit()
    return {"ok": True, "rundown": rundown_payload(store, show_id)}


def act_item_schedule(store, item_id, body):
    """Set or clear when the clock takes an item. {at: ISO time with a UTC offset | null}. Only a ready item can be scheduled."""
    _unknown_keys(body, {"at", "mode", "offsetSeconds"})
    mode = body.get("mode", "absolute")
    if mode not in ("absolute", "after_previous", "none"):
        raise ApiError(422, "bad_input", "mode must be absolute, after_previous or none")
    offset = body.get("offsetSeconds")
    if mode == "after_previous":
        if "at" in body:
            raise ApiError(422, "bad_input", "after_previous takes an offset, not a time")
        offset = 0 if offset is None else offset
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 86399:
            raise ApiError(422, "bad_input", "offsetSeconds must be a whole number from 0 to 86399")
    elif mode == "none":
        if "at" in body or offset is not None:
            raise ApiError(422, "bad_input", "mode none takes no time and no offset")
        body = {"at": None}
    elif offset is not None:
        raise ApiError(422, "bad_input", "offsetSeconds only applies to after_previous")
    if mode != "after_previous" and "at" not in body:
        raise ApiError(422, "bad_input", "at is required (a time with a UTC offset, or null to clear the schedule)")
    at = body.get("at")
    if at is not None and (not isinstance(at, str) or not at.strip()):
        raise ApiError(422, "bad_input", "at must be a time such as 2026-11-01T20:00:00Z, or null")
    show_id, status = _item_show(store, item_id)
    if status != "ready":
        raise ApiError(409, "not_schedulable", f"an item that is {status} cannot be scheduled; only a ready item can")
    try:
        if mode == "after_previous":
            set_relative(store, item_id, offset)
        else:
            set_schedule(store, item_id, at)
    except ValueError as exc:
        raise ApiError(422, "bad_input", str(exc) if mode == "after_previous" else "at must be a time with a UTC offset, such as 2026-11-01T20:00:00Z") from None
    return {"ok": True, "item": item_id, "rundown": rundown_payload(store, show_id)}


def act_show_recurrence(store, show_id, body):
    """Set or clear how a show repeats. {rule: {freq, days?, at, tz} | null}. The clock makes each next copy 24 hours ahead."""
    _unknown_keys(body, {"rule"})
    if "rule" not in body:
        raise ApiError(422, "bad_input", "rule is required (an object, or null to stop repeating)")
    _show_exists(store, show_id)
    try:
        set_recurrence(store, show_id, body["rule"])
    except PermissionError as exc:
        raise ApiError(409, "not_a_template", str(exc)) from None
    except ValueError as exc:
        raise ApiError(422, "bad_input", str(exc)) from None
    return {"ok": True, "rundown": rundown_payload(store, show_id)}


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
    # Dispatch waits on the room with no _SEQ; the taking row is the contract across processes.
    result = _advance_errors(
        lambda: take_item(store, room, show_id, owner=owner, show_input_actions=configured_actions(), **POLL))
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
        except TakingError as exc:
            raise ApiError(409, "taking", str(exc), item=exc.item_id) from None
        except PlayoutError as exc:
            raise ApiError(409, "not_skippable", str(exc)) from None
        return {"ok": True, "item": item_id, "rundown": rundown_payload(store, show_id)}


def act_kill(store, item_id, body):
    _unknown_keys(body, set())
    show_id, _ = _item_show(store, item_id)
    with _SEQ:
        # _SEQ only orders requests inside this process; kill() decides with one conditional write, so a go
        # or take from another process cannot slip between a read here and the write.
        try:
            killed = kill(store, item_id)
        except TakingError as exc:
            raise ApiError(409, "taking", str(exc), item=exc.item_id) from None
        if not killed:
            status = store.conn.execute("SELECT status FROM item WHERE id = ?", (item_id,)).fetchone()["status"]
            raise ApiError(409, "not_killable", f"cannot kill a {status} item")
        return {"ok": True, "item": item_id, "rundown": rundown_payload(store, show_id)}


def act_attach(store, item_id, body):
    _unknown_keys(body, {"span"})
    span_id = _str(body, "span", required=True, limit=128)
    show_id, _ = _item_show(store, item_id)
    if store.conn.execute("SELECT 1 FROM span WHERE id = ?", (span_id,)).fetchone() is None:
        raise ApiError(404, "not_found", f"no such span: {span_id}")
    # The decision is made inside attach(): one transaction under the show's lock, a conditional write. No _SEQ,
    # like the other building edits, so attaching stays possible while a take waits on the room.
    try:
        attach(store, item_id, span_id)
    except ItemNotReady as exc:
        raise ApiError(409, "not_attachable", f"cannot attach media to a {exc.status} item") from None
    except KeyError:
        raise ApiError(404, "not_found", "the item or span no longer exists") from None
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


def act_delete(store, asset_id, body):
    """Soft delete (tombstone). Hides the asset; keeps every file; undone by /restore; purged later by the runner."""
    _unknown_keys(body, set())
    _asset_exists(store, asset_id)
    try:
        newly = retention.tombstone(store, asset_id)
    except retention.DeleteRefused as exc:
        raise ApiError(409, "delete_refused", str(exc), reasons=exc.reasons) from None
    return {"ok": True, "alreadyDeleted": not newly, "deletedAt": store.get_asset(asset_id).deleted_at,
            "asset": asset_payload(store, asset_id)}


def act_restore(store, asset_id, body):
    """Bring a tombstoned asset back. 404 once it has been purged."""
    _unknown_keys(body, set())
    _asset_exists(store, asset_id)
    if not retention.restore(store, asset_id):
        raise ApiError(409, "not_deleted", "this asset is not deleted")
    return {"ok": True, "asset": asset_payload(store, asset_id)}


def _archive_call(fn, store, asset_id):
    _asset_exists(store, asset_id)
    try:
        return fn(store, asset_id)
    except archive.ArchiveRefused as exc:
        raise ApiError(409, "archive_refused", str(exc), reasons=exc.reasons) from None


def act_asset_archive(store, asset_id, body):
    """Queue moving the hi-res to the archive store. The runner uploads, verifies, records, then deletes the local file."""
    _unknown_keys(body, set())
    job = _archive_call(archive.request_archive, store, asset_id)
    return {"ok": True, "job": job, "asset": asset_payload(store, asset_id)}


def act_asset_rehydrate(store, asset_id, body):
    """Queue bringing an archived hi-res back to its own path. (/restore already means un-delete.)"""
    _unknown_keys(body, set())
    job = _archive_call(archive.request_restore, store, asset_id)
    return {"ok": True, "job": job, "asset": asset_payload(store, asset_id)}


def act_mos_resolve(store, ident, body):
    """The desk's answer for a show whose roCreate got no acknowledgement: {action: created | retry}. `ident` is device:show."""
    _unknown_keys(body, {"action"})
    action = body.get("action")
    device, sep, show_id = ident.partition(":")
    if not sep or not device or not show_id:
        raise ApiError(422, "bad_input", "the id is device:show")
    if action not in ("created", "retry"):
        raise ApiError(422, "bad_input", "action must be created or retry")
    try:
        mos_send.resolve(store, device, show_id, action)
    except KeyError:
        raise ApiError(404, "not_found", f"nothing is waiting on {ident}") from None
    return {"ok": True, "mos": mos_send.status(store)}


def act_unpublish(store, version_id, body):
    """Withdraw one publish version and remove the object Volt stored for it. Idempotent; a failed storage delete is
    a 502 `storage_failed` with the version already withdrawn, and calling again retries just the delete."""
    _unknown_keys(body, set())
    try:
        out = publish_mod.unpublish(store, version_id, backend=UNPUBLISH_BACKEND, local_root=OUT_DIR)
    except KeyError:
        raise ApiError(404, "not_found", f"no such version: {version_id}") from None
    except publish_mod.UnpublishError as exc:
        raise ApiError(exc.status, exc.code, exc.reason, **exc.extra) from None
    return {"ok": True, "version": {"id": out.id, "kind": out.kind, "span": out.span_id, "status": "withdrawn",
                                    "withdrawnAt": out.withdrawn_at},
            "alreadyWithdrawn": out.already, "storage": out.storage, "location": out.location}


def act_publish(store, span_id, body):
    _unknown_keys(body, set())
    span = store.conn.execute("SELECT asset_id FROM span WHERE id = ?", (span_id,)).fetchone()
    if span is None:
        raise ApiError(404, "not_found", f"no such span: {span_id}")
    try:
        store.assert_publishable(span["asset_id"])
    except ArchivedError:
        raise ApiError(409, "archived", "the asset's hi-res is archived; rehydrate it first") from None
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


def act_show_delete(store, show_id, body):
    """Delete a show that has not played: its ready/killed/skipped items and cues, then the show. Spans and
    assets stay. Refused when any item is on air or has aired."""
    _unknown_keys(body, set())
    removed = _build_errors(lambda: building.delete_show(store, show_id))
    return {"ok": True, "deleted": show_id, "removedItems": removed["items"], "removedCues": removed["cues"]}


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
            "adapter": c.command in building.HANDLERS,
            "verified": c.command in building.LIVE_PROVEN,
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
    if command not in building.HANDLERS:
        out["warning"] = (f"{command} has no verified adapter yet: the cue is stored, but a take stops at it "
                          "with cue_unsupported")
    elif not verified:
        out["warning"] = (f"{command} has an adapter but is not proven against a live core yet: "
                          "the cue is stored and a take will run it")
    return out


def act_cue_remove(store, cue_id, body):
    _unknown_keys(body, set())
    item_id, show_id = _build_errors(lambda: building.remove_cue(store, cue_id))
    return {"ok": True, "removed": cue_id, "item": item_id, "rundown": rundown_payload(store, show_id),
            "orderVersion": building.order_version(store, show_id)}


def act_ingest(store, _unused, body):
    """Enqueue ingest of one file from an allowed folder (VOLT_INGEST_ROOTS). The runner copies, probes, and
    checksums. Replay is safe (same file, same asset)."""
    _unknown_keys(body, {"path", "owner", "show", "camera"})
    path = _str(body, "path", required=True, limit=4096)
    owner = _str(body, "owner", default="desk", limit=100)
    out = _build_errors(lambda: building.ingest_allowed(
        store, path, Path(os.environ.get("VOLT_QUARANTINE") or OUT_DIR / "quarantine"), owner,
        _str(body, "show", limit=200), _str(body, "camera", limit=100)))
    return {"ok": True, **out, "assetState": asset_payload(store, out["asset"]), "httpStatus": 202}


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
MOS_ROUTE = re.compile(rf"^/api/shows/{_ID}/mos$")

def _user_error(fn):
    try:
        return fn()
    except users.UserError as exc:
        raise ApiError(exc.status, exc.code, exc.reason) from None


def act_user_add(store, _unused, body):
    _unknown_keys(body, {"name", "role"})
    user, token = _user_error(lambda: users.add(store, body.get("name"), body.get("role")))
    # The only time the plaintext exists. It is not written to the audit row or the log.
    return {"ok": True, "user": user, "token": token}


def act_user_disable(store, name, body):
    _unknown_keys(body, set())
    return {"ok": True, "user": _user_error(lambda: users.disable(store, name, legacy_admin=bool(TOKEN)))}


def act_user_enable(store, name, body):
    _unknown_keys(body, set())
    return {"ok": True, "user": _user_error(lambda: users.enable(store, name))}


def act_user_rotate(store, name, body):
    _unknown_keys(body, set())
    token = _user_error(lambda: users.rotate(store, name))
    return {"ok": True, "user": _public_user(store, name), "token": token}


def _public_user(store, name):
    return next(u for u in users.list_users(store) if u["name"] == name)


ROUTES = [
    (re.compile(rf"^/api/shows/{_ID}/go$"), act_go),
    (re.compile(rf"^/api/shows/{_ID}/hold$"), act_hold),
    (re.compile(rf"^/api/shows/{_ID}/slot$"), act_show_slot),
    (re.compile(rf"^/api/shows/{_ID}/recurrence$"), act_show_recurrence),
    (re.compile(rf"^/api/shows/{_ID}/release$"), act_release),
    (re.compile(rf"^/api/shows/{_ID}/take$"), act_take),
    (re.compile(rf"^/api/shows/{_ID}/reorder$"), act_reorder),
    (re.compile(rf"^/api/items/{_ID}/skip$"), act_skip),
    (re.compile(rf"^/api/items/{_ID}/schedule$"), act_item_schedule),
    (re.compile(rf"^/api/items/{_ID}/kill$"), act_kill),
    (re.compile(rf"^/api/items/{_ID}/attach$"), act_attach),
    (re.compile(r"^/api/record/start$()"), act_record_start),
    (re.compile(r"^/api/record/stop$()"), act_record_stop),
    (re.compile(rf"^/api/assets/{_ID}/cleared$"), act_cleared),
    (re.compile(rf"^/api/assets/{_ID}/legal-hold$"), act_legal_hold),
    (re.compile(rf"^/api/assets/{_ID}/delete$"), act_delete),
    (re.compile(rf"^/api/assets/{_ID}/restore$"), act_restore),
    (re.compile(rf"^/api/assets/{_ID}/archive$"), act_asset_archive),
    (re.compile(rf"^/api/mos/{_ID}/resolve$"), act_mos_resolve),
    (re.compile(rf"^/api/assets/{_ID}/rehydrate$"), act_asset_rehydrate),
    (re.compile(rf"^/api/spans/{_ID}/publish$"), act_publish),
    (re.compile(rf"^/api/versions/{_ID}/unpublish$"), act_unpublish),
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
    (re.compile(rf"^/api/shows/{_ID}/delete$"), act_show_delete),
    (re.compile(rf"^/api/items/{_ID}/rename$"), act_item_rename),
    (re.compile(rf"^/api/items/{_ID}/remove$"), act_item_remove),
    (re.compile(rf"^/api/items/{_ID}/detach$"), act_item_detach),
    (re.compile(rf"^/api/items/{_ID}/cues$"), act_cue_add),
    (re.compile(rf"^/api/cues/{_ID}/remove$"), act_cue_remove),
    (re.compile(r"^/api/ingest$()"), act_ingest),
]
ROUTES += [  # user management: admin only
    (re.compile(r"^/api/users$()"), act_user_add),
    (re.compile(rf"^/api/users/{_ID}/disable$"), act_user_disable),
    (re.compile(rf"^/api/users/{_ID}/enable$"), act_user_enable),
    (re.compile(rf"^/api/users/{_ID}/rotate$"), act_user_rotate),
]

# Every POST route's role, by action function. operator runs the show; admin is everything an operator can do plus
# users and the destructive or retention actions. A route missing here is refused as admin-only, and a test fails
# until it is classified on purpose.
ROUTE_ROLES = {
    # show control
    "act_mos_resolve": "operator", "act_asset_archive": "operator", "act_asset_rehydrate": "operator", "act_show_recurrence": "operator", "act_item_schedule": "operator", "act_show_slot": "operator", "act_go": "operator", "act_hold": "operator", "act_release": "operator", "act_take": "operator",
    "act_reorder": "operator", "act_skip": "operator", "act_kill": "operator", "act_attach": "operator",
    "act_record_start": "operator", "act_record_stop": "operator",
    # rights and publishing
    "act_cleared": "operator", "act_legal_hold": "operator", "act_restore": "operator",
    "act_publish": "operator", "act_export_nle": "operator",
    # clip (span) editing
    "act_span_create": "operator", "act_span_trim": "operator", "act_span_label": "operator",
    "act_span_split": "operator", "act_span_merge": "operator", "act_span_frames": "operator",
    # rundown and cue writes
    "act_show_create": "operator", "act_item_add": "operator", "act_item_rename": "operator",
    "act_item_remove": "operator", "act_item_detach": "operator", "act_cue_add": "operator",
    "act_cue_remove": "operator", "act_ingest": "operator",
    # destructive or retention
    "act_delete": "admin", "act_unpublish": "admin", "act_span_delete": "admin", "act_detect": "admin",
    "act_show_delete": "admin",
    # users and tokens
    "act_user_add": "admin", "act_user_disable": "admin", "act_user_enable": "admin", "act_user_rotate": "admin",
}


# ---- HTTP -----------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    principal = None  # set by _gate: who this request is

    def _bearer(self) -> str:
        scheme, _, given = self.headers.get("Authorization", "").partition(" ")
        return given.strip() if scheme.lower() == "bearer" else ""

    def _open_store(self) -> Store:
        return Store(DB_PATH, init=not is_dsn(DB_PATH))

    def _bearer_principal(self):
        """The principal a valid Authorization header names (legacy VOLT_TOKEN, else a user token), or None."""
        given = self._bearer()
        if not given:
            return None
        if TOKEN and hmac.compare_digest(given.encode("utf-8"), TOKEN.encode("utf-8")):
            return Principal(LEGACY_ACTOR, "admin", True)
        try:
            store = self._open_store()
            try:
                user = users.authenticate(store, given)
            finally:
                store.close()
        except Exception:  # noqa: BLE001 - fail closed; the cause goes to the log
            log.exception("token lookup failed")
            raise ApiError(503, "auth_unavailable", "cannot check the token right now") from None
        return Principal(user.name, user.role, False) if user else None

    def _auth_configured(self) -> bool:
        """Is a token required? Yes with VOLT_TOKEN or any user row; otherwise this is the open loopback mode."""
        if TOKEN:
            return True
        try:
            store = self._open_store()
            try:
                return users.any_users(store)
            finally:
                store.close()
        except Exception:  # noqa: BLE001 - cannot tell: require a token rather than open up
            log.exception("user table check failed")
            return True

    def _authenticate(self) -> Principal:
        found = self._bearer_principal()
        if found is not None:
            return found
        if self._auth_configured():
            raise ApiError(401, "unauthorized", "missing or wrong bearer token")
        # No token and no users means loopback only. A rebinding page would arrive with a foreign Host.
        if not _is_loopback(_host_name(self.headers.get("Host") or "")):
            raise ApiError(403, "bad_host", "this server answers on loopback only")
        return Principal(LEGACY_ACTOR, "admin", True)

    def _gate(self) -> bool:
        """Auth and host checks. Sends the refusal and returns False when the request must stop."""
        try:
            self.principal = self._authenticate()
        except ApiError as err:
            self._error(err)
            return False
        return True

    def _require_admin(self) -> None:
        if self.principal is None or self.principal.role != "admin":
            raise ApiError(403, "forbidden_role", "this needs the admin role")

    def _ticket_on_media_get(self) -> bool:
        parsed = urlparse(self.path)
        m = READ_ROUTES.match(parsed.path)
        if m and m.group(2) == "proxy":
            media_get = True
        else:
            media_get = bool(THUMB_ROUTE.match(parsed.path))
        return media_get and "ticket" in parse_qs(parsed.query, keep_blank_values=True)

    def _gate_or_ticket(self) -> bool:
        """Bearer (or loopback host) as usual, except a media GET may present `ticket` instead of Authorization."""
        if self._ticket_on_media_get() and self._auth_configured():
            return True
        if self._ticket_on_media_get():
            host = _host_name(self.headers.get("Host") or "")
            if not _is_loopback(host):
                self._error(ApiError(403, "bad_host", "this server answers on loopback only"))
                return False
            return True
        return self._gate()

    def _empty_401(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _accept_media_ticket(self, asset_id: str, kind: str, query: dict) -> None:
        extra = sorted(set(query) - {"ticket"})
        if extra:
            raise ApiError(422, "bad_input",
                           "the proxy route takes no parameters" if kind == "proxy"
                           else "the thumb route takes no parameters")
        given = query.get("ticket")
        if not given:
            return
        if len(given) != 1:
            raise ApiError(422, "bad_input", "ticket may be given once")
        if self._bearer_principal() is not None:
            return
        secret = MEDIA_KEY or TOKEN
        try:
            if not secret:
                raise tickets.TicketError("ticket_invalid")
            tickets.verify(secret, asset_id, kind, given[0], int(time.time()))
        except tickets.TicketError:
            raise TicketDenied() from None

    def _issue_media_ticket(self, query: dict) -> None:
        secret = MEDIA_KEY or TOKEN
        if not secret:
            raise ApiError(503, "no_media_key",
                           "no media signing key is configured (set VOLT_MEDIA_KEY or VOLT_TOKEN)")
        extra = sorted(set(query) - {"asset", "kind", "ttl"})
        if extra:
            raise ApiError(422, "bad_input", f"unknown parameter(s): {', '.join(extra)}")
        single = {}
        for key, values in query.items():
            if len(values) != 1:
                raise ApiError(422, "bad_input", f"{key} may be given once")
            single[key] = values[0]
        asset_id = single.get("asset") or ""
        kind = single.get("kind") or ""
        if not asset_id or kind not in tickets.KINDS:
            raise ApiError(422, "bad_input", "asset and kind=proxy|thumb are required")
        ttl = 120
        if "ttl" in single:
            raw = single["ttl"]
            if not raw.isdigit() or int(raw) < 1:
                raise ApiError(422, "bad_input", "ttl must be a whole number from 1 to 3600")
            ttl = min(int(raw), 3600)
        token, exp = tickets.issue(secret, asset_id, kind, ttl, int(time.time()))
        if kind == "proxy":
            url = f"/api/assets/{asset_id}/proxy?ticket={token}"
        else:
            url = f"?ticket={token}"
        self._json(200, {"url": url, "expires": exp})

    def do_GET(self):
        path = urlparse(self.path).path
        if path in STATIC:
            self._static(path)
            return
        if path == "/health":  # liveness for a supervisor or load balancer: no auth, no data
            self._json(200, health.liveness())
            return
        if path != "/" and not self._gate_or_ticket():
            return
        # One connection per request. Postgres schema is applied once in main().
        store = Store(DB_PATH, init=not is_dsn(DB_PATH))
        try:
            self._route(store)
        except TicketDenied:
            self._empty_401()
        except ApiError as err:
            self._error(err)
        except media.MediaError as err:
            self._error(ApiError(err.status, err.code, err.reason, headers=err.headers))
        except Exception:  # same rule as POST: no leaked internals, and the client always gets a response
            log.exception("unhandled error on GET %s", urlparse(self.path).path)
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
            need = ROUTE_ROLES.get(fn.__name__, "admin")
            if need == "admin" and self.principal.role != "admin":
                raise ApiError(403, "forbidden_role",
                               f"{need} role required; {self.principal.name} is {self.principal.role}")
            body = self._read_json()
            actor = _actor(self.headers, body, self.principal)
            store = Store(DB_PATH, init=not is_dsn(DB_PATH))
            out = fn(store, m.group(1), body)
            status = 200
            if isinstance(out, dict) and out.get("httpStatus") == 202:
                out = dict(out)
                out.pop("httpStatus")
                status = 202
            if isinstance(out, dict) and out.get("ok") is True and 200 <= status < 300:
                action, kind, target = _audit_fields(path, m.group(1), body, out)
                store.append_audit(actor, action, kind, target)
            self._json(status, out)
        except ApiError as err:
            self._error(err)
        except Exception:  # never leak internals; the cause is in the server's log
            log.exception("unhandled error on POST %s", urlparse(self.path).path)
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
        if parsed.path in ("/api/health", "/metrics"):  # behind the same gate as every data route
            report = health.collect(STORE, OUT_DIR, db_target=DB_PATH)
            if parsed.path == "/metrics":
                self._send(200 if report["status"] != "down" else 503, "text/plain; version=0.0.4; charset=utf-8",
                           health.prometheus(report).encode())
            else:
                self._json(200 if report["status"] != "down" else 503, report)
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
        if parsed.path == "/api/audit":
            query = parse_qs(parsed.query, keep_blank_values=True)
            extra = sorted(set(query) - {"target"})
            if extra:
                raise ApiError(422, "bad_input", f"unknown parameter(s): {', '.join(extra)}")
            given = query.get("target")
            if not given or len(given) != 1 or not given[0]:
                raise ApiError(422, "bad_input", "target is required")
            self._json(200, {"ok": True, "rows": STORE.list_audit(given[0])})
            return
        if parsed.path == "/api/whoami":
            self._json(200, {"ok": True, "name": self.principal.name if self.principal else LEGACY_ACTOR,
                             "role": self.principal.role if self.principal else "admin",
                             "legacy": self.principal is None or self.principal.legacy})
            return
        if parsed.path == "/api/users":
            self._require_admin()
            self._json(200, {"ok": True, "users": users.list_users(STORE)})
            return
        if parsed.path == "/api/media-ticket":
            self._issue_media_ticket(parse_qs(parsed.query, keep_blank_values=True))
            return
        if show_building_get(self, STORE, parsed.path, parse_qs(parsed.query, keep_blank_values=True)):
            return
        m = MOS_ROUTE.match(parsed.path)
        if m:  # read-only MOS roCreate export (docs/specs/mos.md)
            try:
                self._json(200, mos.export(STORE, m.group(1), **mos.parse_query(parse_qs(parsed.query, keep_blank_values=True))))
            except mos.MosError as exc:
                raise ApiError(exc.status, exc.code, exc.reason, **exc.extra) from None
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
                self._accept_media_ticket(asset_id, "proxy", query)
                self._proxy(media.proxy_for_asset(STORE, asset_id))
            return
        m = THUMB_ROUTE.match(parsed.path)
        if m:
            query = parse_qs(parsed.query, keep_blank_values=True)
            span_id = m.group(1)
            row = STORE.conn.execute("SELECT asset_id FROM span WHERE id = ?", (span_id,)).fetchone()
            self._accept_media_ticket(row["asset_id"] if row else "", "thumb", query)
            path = media.ensure_thumb(STORE, span_id, OUT_DIR)
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
        if not self._auth_configured() and not _is_loopback(_host_name(self.headers.get("Host") or "")):
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
        if err.status >= 500:
            log.error("request failed status=%d code=%s path=%s", err.status, err.code, urlparse(self.path).path)
        elif err.status in (401, 403):
            log.warning("request refused status=%d code=%s path=%s client=%s", err.status, err.code,
                        urlparse(self.path).path, self.client_address[0])
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
        if ACCESS_LOG:  # opt in: one line per request, redacted like every log line
            access.info("request client=%s %s", self.client_address[0], fmt % args)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="volt serve", description="The Volt operator API and session shell.")
    ap.add_argument("--host", default=os.environ.get("VOLT_HOST") or "127.0.0.1",
                    help="address to bind (default 127.0.0.1; or VOLT_HOST). Beyond loopback needs VOLT_TOKEN.")
    ap.add_argument("--port", type=int, default=int(os.environ.get("VOLT_PORT") or 8765),
                    help="port (default 8765; or VOLT_PORT; 0 picks a free one)")
    ap.add_argument("--db", default=None, help="SQLite path or postgres:// DSN (default: VOLT_DB, else control/volt.db)")
    ap.add_argument("--out-dir", default=None, help="thumbnail cache and publish output root (or VOLT_OUT)")
    ap.add_argument("--log-dir", default=None, help="write a rotating serve.log here (or VOLT_LOG_DIR)")
    ap.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING or ERROR (or VOLT_LOG_LEVEL)")
    ap.add_argument("--access-log", action="store_true", help="log every request (or VOLT_ACCESS_LOG=1)")
    return ap


def _config_exit(message: str):
    print(f"volt: {message}", file=sys.stderr)
    raise SystemExit(EX_CONFIG)


def _on_stop(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    global DB_PATH, OUT_DIR, ACCESS_LOG
    args = build_parser().parse_args(argv)
    if args.db:
        DB_PATH = args.db
    if args.out_dir:
        OUT_DIR = Path(args.out_dir)
    ACCESS_LOG = args.access_log or logs.access_log_enabled()
    try:
        logs.setup("serve", log_dir=args.log_dir, level=args.log_level)
    except (ValueError, OSError) as exc:
        _config_exit(str(exc))
    host, port = args.host, args.port
    try:
        check_bind(host, TOKEN, has_admin_user=(not TOKEN and not _is_loopback(host)
                                                and users.db_has_enabled_admin(DB_PATH)))
        configured_actions()
    except ConfigError as exc:
        _config_exit(str(exc))
    except ValueError as exc:
        _config_exit(str(exc))
    for name in ("SIGTERM", "SIGBREAK"):  # a supervisor's stop request ends the loop like Ctrl-C does
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _on_stop)
    # Optional Postgres pool (VOLT_PG_POOL_MAX > 0; see volt/pg.py). Opened before the bootstrap so it fails
    # at startup if the database is unreachable, and so requests borrow instead of connecting.
    try:
        pool = pg.configure_pool_from_env(DB_PATH)
    except Exception as exc:  # noqa: BLE001 - bad option, missing psycopg_pool, unreachable database
        _config_exit(f"postgres pool: {logs.redact(str(exc))}")
    try:
        try:
            Store(DB_PATH).close()  # bootstrap the schema once
            server = ThreadingHTTPServer((host, port), Handler)
        except SchemaTooNewError as exc:  # version-skew guard: never serve a database newer than this code
            _config_exit(str(exc))
        except OSError as exc:
            _config_exit(f"cannot start on {host}:{port}: {exc.strerror or exc}")
        port = server.server_address[1]
        print(f"Volt session on http://{host}:{port}" + (" (token required)" if TOKEN or users.db_has_enabled_admin(DB_PATH) else "")
              + (f" (postgres pool of up to {pool.max_size})" if pool else ""), flush=True)
        log.info("serve started version=%s host=%s port=%d db=%s auth=%s access_log=%s", health.__version__, host,
                 port, logs.redact_dsn(DB_PATH), "required" if TOKEN else "users" if users.db_has_enabled_admin(DB_PATH) else "none", ACCESS_LOG)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            log.info("serve stopped")
    finally:
        pg.close_pools()


if __name__ == "__main__":
    main()
