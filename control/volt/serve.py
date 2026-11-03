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

from volt import collections as colls, searchindex, metadata, access as acl, auditview, archive, building, devices, discuss, leases, mos_send, oidc, safety, health, logs, media, mos, nle, operator_state, retention, sessions, shots, story, templates, tickets, users, wire
from volt.clock import set_recurrence, set_relative, set_schedule, to_utc_text
from volt.paths import package_root
from volt.cues import cues as cues_of
from volt.migrations import SchemaTooNewError
from volt.live import finish_recording, start_recording
from volt import pg
from volt.pg import is_dsn
from volt.playout import HeldError, PlayoutError, StoryNotApproved, TakingError, advance, hold, hold_state, release, skip
from volt import publish as publish_mod
from volt.publish import publish_files
from volt.record import RecordError
from volt.rooms import CoreVideoProRoom, LiveError
from volt.rundown import ItemNotReady, attach, kill, reorder, show_lock
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
    # The prompter page: open like the operator page, because it carries its own show-scoped ticket; the feed it reads is gated.
    "/prompter": ("operator/prompter.html", "text/html; charset=utf-8"),
    "/operator/prompter.js": ("operator/prompter.js", "text/javascript; charset=utf-8"),
    "/operator/prompter.css": ("operator/prompter.css", "text/css; charset=utf-8"),
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
# Who is making the POST being handled. The action handlers take (store, match, body) only, so the request's actor
# and principal ride here (one value per handler thread, set in do_POST and cleared after).
_REQUEST = threading.local()


def _who():
    """(actor name, principal) of the POST being handled; ("desk", None) outside a request (a direct call from a test)."""
    return getattr(_REQUEST, "actor", None) or LEGACY_ACTOR, getattr(_REQUEST, "principal", None)


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
    "groups": "group",
    "devices": "device",
    "session": "session",

    "schema": "schema",
    "searches": "search",
    "bins": "bin",
    "wire": "wire",
    "templates": "template",
    "presence": "presence",
    "comments": "comment",
    "notifications": "notification",
}

CLOCK = time.time  # what "now" is for sessions; a test replaces it with a frozen clock
leases.ON_TAKEN = discuss.on_lease_taken   # a taken-over lease pings the person who lost it (volt/discuss.py); a failure there never undoes the takeover
# The OIDC sign-in configuration (volt/oidc.py), loaded from VOLT_OIDC_CONFIG at start. None = sign-in is off and /auth/* answers 404.
OIDC_CONFIG = None


_CALLER = threading.local()


def _caller() -> tuple:
    """(actor, role) of the request being served on this thread. Set by do_POST just before the act runs."""
    return getattr(_CALLER, "actor", LEGACY_ACTOR), getattr(_CALLER, "role", "admin")


class Principal:
    """Who a request is: a per-user token (name, role), or the legacy VOLT_TOKEN / open loopback (desk, admin)."""

    def __init__(self, name: str, role: str, legacy: bool, groups: tuple = (), via: str = "", session_id: str = "", csrf: str = ""):
        self.name, self.role, self.legacy, self.groups = name, role, legacy, tuple(groups)
        # How the request proved who it is: the legacy VOLT_TOKEN or open loopback, a per-user token, or a cookie session.
        self.via = via or ("legacy" if legacy else "token")
        self.session_id, self.csrf = session_id, csrf


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
    """The /rundown shape: {hold, items, slotSeconds, readWpm}. Raises KeyError for an unknown show.

    Each item also carries its story summary (storyStatus, scriptRev, words, readSeconds, floated, lengthSource) but never
    the script text: this is polled every two seconds, and the text can hold private notes."""
    held = hold_state(store, show_id)
    slot = store.conn.execute("SELECT slot_seconds, recurrence, recurrence_of, read_wpm FROM show WHERE id = ?", (show_id,)).fetchone()
    wpm = (slot["read_wpm"] if slot else None) or story.DEFAULT_WPM
    sched = {r["id"]: r for r in store.conn.execute(
        "SELECT id, scheduled_at, schedule_state, schedule_note, schedule_mode, schedule_offset_seconds, script, story_status, "
        "script_rev, floated FROM item WHERE show_id = ?", (show_id,)).fetchall()}
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
         "scheduleOffsetSeconds": sched[row.id]["schedule_offset_seconds"] if row.id in sched else None,
         **_story_summary(sched.get(row.id), row.span_id, wpm)}
        for row in rundown(store, show_id)
    ]
    return {"hold": None if held is None else {"reason": held.reason, "heldAt": held.held_at},
            "items": items, "slotSeconds": slot[0] if slot else None,
            "recurrence": json.loads(slot[1]) if slot and slot[1] else None, "recurrenceOf": slot[2] if slot else None,
            "readWpm": wpm}


def _story_summary(row, span_id, wpm: int) -> dict:
    """The per-item story fields of /rundown. `lengthSource` says where the planned length comes from: the attached
    media, the script's read time, or nothing (no media and nothing to read)."""
    stats = story.script_stats(row["script"] if row is not None else "", wpm)
    return {"storyStatus": (row["story_status"] if row is not None else None) or "draft",
            "scriptRev": (row["script_rev"] if row is not None else None) or 1,
            "words": stats["words"], "readSeconds": stats["readSeconds"],
            "floated": bool(row["floated"]) if row is not None else False,
            "lengthSource": "media" if span_id else ("read" if stats["words"] else "none")}


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
    if len(parts) >= 2 and parts[1] == "groups":
        action = "group_" + (parts[-1] if len(parts) > 2 else "create")
        if not captured and isinstance(out.get("group"), dict):
            captured = out["group"].get("name") or ""
    if len(parts) >= 3 and parts[1] == "shows" and parts[-1] == "access":
        action = "show_access"
    if len(parts) >= 2 and parts[1] == "session":
        action = {"start": "login", "end": "logout"}.get(parts[-1], "session_" + parts[-1])
    if len(parts) >= 2 and parts[1] == "users":
        action = "user_" + (parts[-1] if len(parts) > 2 else "add")
        if action == "user_end-sessions":
            action = "session_end"
        if not captured and isinstance(out.get("user"), dict):
            captured = out["user"].get("name") or ""
    if len(parts) >= 2 and parts[1] == "schema":
        action = "schema_term_retire" if parts[-2:] == ["terms", "retire"] else "schema_" + {"fields": "field", "retire": "retire", "terms": "terms"}[parts[-1]]
        if not captured and isinstance(out.get("field"), dict):
            captured = out["field"].get("key") or ""
    if len(parts) >= 2 and parts[1] == "searches":
        action = "search_delete" if parts[-1] == "delete" else "search_save"
        if not captured and isinstance(out.get("search"), dict):
            captured = out["search"].get("id") or ""
    if len(parts) >= 2 and parts[1] == "wire":
        action = "wire_" + parts[-1]
    if len(parts) >= 2 and parts[1] == "templates":
        action = "template_" + (parts[-1] if len(parts) > 2 else "create")
        if not captured and isinstance(out.get("template"), dict):
            captured = out["template"].get("id") or ""
    if len(parts) >= 2 and parts[1] == "bins":
        action = "bin_" + (parts[-1] if len(parts) > 2 else "create")
        if not captured and isinstance(out.get("bin"), dict):
            captured = out["bin"].get("id") or ""
    if len(parts) >= 2 and parts[1] == "comments":
        action = "comment_" + (parts[-1] if len(parts) > 2 else "add")
        if not captured and isinstance(out.get("comment"), dict):
            captured = out["comment"].get("id") or ""
    if len(parts) >= 2 and parts[1] == "notifications":
        action = "notifications_read"
    if len(parts) >= 4 and parts[1] == "items" and parts[3] == "lease":
        action = "lease_" + {"lease": "acquire"}.get(parts[-1], parts[-1])
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


def _meta_error(fn, missing_is_404: bool = False):
    """Run a metadata call, mapping MetaError to ApiError. For routes that name the field in the path an unknown one is 404."""
    try:
        return fn()
    except metadata.MetaError as exc:
        if missing_is_404 and exc.code == "unknown_field":
            raise ApiError(404, "not_found", exc.reason) from None
        raise ApiError(exc.status, exc.code, exc.reason) from None


def _advance_errors(fn):
    """Run an advance, mapping the playout refusals to API errors. Nothing has changed when these fire."""
    try:
        return fn()
    except HeldError as exc:
        raise ApiError(409, "held", str(exc)) from None
    except TakingError as exc:
        raise ApiError(409, "taking", str(exc), item=exc.item_id) from None
    except StoryNotApproved as exc:
        raise ApiError(409, "story_not_approved", str(exc), item=exc.item_id) from None
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
    except (building.BuildError, story.StoryError) as exc:
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


def _client_tab(body: dict):
    """The optional `clientId` a browser tab sends so the server can tell its own lease from another tab's."""
    value = body.get("clientId")
    if value is None:
        return None
    try:
        return leases.check_client_id(value)
    except leases.LeaseError as exc:
        raise ApiError(exc.status, exc.code, exc.reason) from None


def act_item_rename(store, item_id, body):
    """Rename an item / set its text. Optional `version` (the item's content version: 409 `stale_item` with the current text when it moved on)
    and `clientId` (this tab; a live lease held by anyone else is 409 `leased`). Without either it behaves as it always did."""
    _unknown_keys(body, {"slug", "script", "version", "clientId"})
    slug = _str(body, "slug", limit=200)
    script = _str(body, "script", limit=20000)
    version = _str(body, "version", limit=64)
    client = _client_tab(body)
    if slug is None and script is None:
        raise ApiError(422, "bad_input", "give slug, script or both")
    if slug is not None:
        slug = slug.strip()
        if not slug:
            raise ApiError(422, "bad_input", "slug cannot be empty")
    show_id, _ = _item_show(store, item_id)
    _build_errors(lambda: building.rename_item(store, item_id, slug, script, _who()[0], version=version, client_id=client, now=CLOCK()))
    return {"ok": True, "item": item_id, "rundown": rundown_payload(store, show_id),
            "orderVersion": building.order_version(store, show_id)}


# ---- edit leases and presence (volt/leases.py) ------------------------------------------------------------------------
# Advisory UX plus a server check. Nothing on the on-air path calls these, and a lease table that cannot be read is a 503 here and
# "collaboration unavailable" in the page: never a reason for take, go, hold, release, skip or kill to wait.

# The one POST function whose success is not audited: a presence heartbeat is ephemeral state, not a record. A lease renew is the other
# named exception; act_item_lease marks it `auditSkip` itself, because only it knows whether the call began a lease or extended one.
HEARTBEAT_ROUTES = frozenset({"act_presence"})


def _collab(fn):
    try:
        return fn()
    except leases.LeaseError as exc:
        raise ApiError(exc.status, exc.code, exc.reason, **exc.extra) from None
    except ApiError:
        raise
    except Exception:  # noqa: BLE001 - a missing or locked collaboration table: say so, change nothing
        log.exception("collaboration tables unavailable")
        raise ApiError(503, "collab_unavailable", "editing leases are unavailable right now; the show itself is unaffected") from None


def _lease_client(body: dict) -> str:
    if "clientId" not in body:
        raise ApiError(422, "bad_input", "clientId is required: a random id for this browser tab")
    return _client_tab(body)


def act_item_lease(store, item_id, body):
    """Acquire the edit lease on an item, or renew your own (the same call; `renew: true` says you believe you hold it, so a lost lease is
    409 `lease_lost`). 409 `leased` names the holder. Audited when it begins a lease, not when it extends one."""
    _unknown_keys(body, {"clientId", "renew"})
    client = _lease_client(body)
    renew = body.get("renew", False)
    if not isinstance(renew, bool):
        raise ApiError(422, "bad_input", "renew must be true or false")
    actor, now = _who()[0], CLOCK()
    held = _collab(lambda: leases.acquire(store, "item", item_id, actor, client, now, renew))
    out = {"ok": True, "lease": held.public(now)}
    if not held.fresh:
        out["auditSkip"] = True
    return out


def act_item_lease_release(store, item_id, body):
    """Give up your lease (closing the editor). Idempotent; only your own tab's lease is freed."""
    _unknown_keys(body, {"clientId"})
    client = _lease_client(body)
    freed = _collab(lambda: leases.release(store, "item", item_id, _who()[0], client))
    out = {"ok": True, "released": freed}
    if not freed:
        out["auditSkip"] = True
    return out


def act_item_lease_takeover(store, item_id, body):
    """Take a live lease from its holder (a producer fixing a script whose editor walked away). 409 `not_leased` when nobody holds it.
    The previous holder's next renew is `lease_lost`; the takeover is audited with who lost it."""
    _unknown_keys(body, {"clientId"})
    client = _lease_client(body)
    actor, now = _who()[0], CLOCK()
    held = _collab(lambda: leases.takeover(store, "item", item_id, actor, client, now))
    return {"ok": True, "lease": held.public(now), "auditDetail": json.dumps({"previous": held.previous}, sort_keys=True)}


def act_presence(store, _unused, body):
    """A heartbeat from one browser tab: {clientId, show?, view}. Sent every 10 s, live for 30, never audited."""
    _unknown_keys(body, {"clientId", "show", "view"})
    client = _lease_client(body)
    show = _str(body, "show", limit=128)
    view = body.get("view")
    actor, principal = _who()
    if show is not None:
        _show_exists(store, show)
        _show_gate(store, principal, "show", show, "view")     # presence names a show: only someone who can see it may say they are looking
    _collab(lambda: leases.beat(store, actor, client, show, view, CLOCK()))
    return {"ok": True}


# ---- comments, mentions and notifications (volt/discuss.py) -----------------------------------------------------------
# A comment is readable, writable and notifiable only by someone who can view its show. A body travels in the POST body and the JSON answer and
# is stored in the comment table: never in the audit table, the log or a URL (the audit detail carries ids and the names that were pinged).

def _discuss(fn):
    try:
        return fn()
    except discuss.DiscussError as exc:
        raise ApiError(exc.status, exc.code, exc.reason, **exc.extra) from None
    except ApiError:
        raise
    except Exception:  # noqa: BLE001 - a missing or locked comment table: say so, change nothing
        log.exception("comment tables unavailable")
        raise ApiError(503, "collab_unavailable", "comments are unavailable right now; the show itself is unaffected") from None


def act_comment_add(store, _unused, body):
    """Post a comment or a one-level reply: {kind: item|show|asset, target, body, parent?}. The author must be able to view the show."""
    _unknown_keys(body, {"kind", "target", "body", "parent"})
    kind, target = body.get("kind"), body.get("target")
    text, parent = body.get("body"), body.get("parent")
    for name, value in (("kind", kind), ("target", target), ("body", text)):
        if not isinstance(value, str):
            raise ApiError(422, "bad_input", f"{name} is required")
    if parent is not None and not isinstance(parent, str):
        raise ApiError(422, "bad_input", "parent must be a comment id")
    actor, principal = _who()
    show_id = _discuss(lambda: discuss.show_of(store, kind, target))
    if show_id is not None:
        _show_gate(store, principal, "show", show_id, "view")      # whoever cannot view the show cannot talk on it
    made = _discuss(lambda: discuss.add_comment(store, actor, kind, target, text, parent, now=CLOCK()))
    detail = {"kind": kind, "target": target, "show": show_id, "parent": parent, "mentions": made["mentions"]}
    return {"ok": True, "comment": made, "auditDetail": json.dumps(detail, sort_keys=True)}


def _comment_ref(store, comment_id: str):
    row = store.conn.execute("SELECT target_kind, target_id, show_id FROM comment WHERE id = ?", (comment_id,)).fetchone()
    return {"kind": row["target_kind"], "target": row["target_id"], "show": row["show_id"]} if row else {}


def act_comment_resolve(store, comment_id, body):
    """Resolve a thread or reopen it: {resolved: true|false}. Needs edit on the show (the route table checked it)."""
    _unknown_keys(body, {"resolved"})
    resolved = body.get("resolved")
    if not isinstance(resolved, bool):
        raise ApiError(422, "bad_input", "resolved must be true or false")
    actor = _who()[0]
    made = _discuss(lambda: discuss.resolve(store, comment_id, actor, resolved, now=CLOCK()))
    return {"ok": True, "comment": made, "auditDetail": json.dumps(dict(_comment_ref(store, comment_id), resolved=resolved), sort_keys=True)}


def act_comment_delete(store, comment_id, body):
    """Delete a comment (soft; the body is blanked): its author, or an admin."""
    _unknown_keys(body, set())
    actor, principal = _who()
    made = _discuss(lambda: discuss.delete(store, comment_id, actor, acl.bypasses(principal), now=CLOCK()))
    return {"ok": True, "comment": made, "auditDetail": json.dumps(_comment_ref(store, comment_id), sort_keys=True)}


def act_notifications_read(store, _unused, body):
    """Mark some of your own notifications read ({ids: [...]}) or all of them ({all: true}); anyone else's ids change nothing."""
    _unknown_keys(body, {"ids", "all"})
    ids, everything = body.get("ids"), body.get("all")
    if (ids is None) == (everything is None):
        raise ApiError(422, "bad_input", "give ids (a list) or all: true, not both and not neither")
    if everything is not None and everything is not True:
        raise ApiError(422, "bad_input", "all must be true")
    if ids is not None and (not isinstance(ids, list) or len(ids) > 200 or not all(isinstance(i, str) and 0 < len(i) <= 128 for i in ids)):
        raise ApiError(422, "bad_input", "ids must be a list of at most 200 notification ids")
    actor = _who()[0]
    marked = _discuss(lambda: discuss.mark_read(store, actor, None if everything else ids, now=CLOCK()))
    left = _discuss(lambda: discuss.unread_count(store, actor))
    return {"ok": True, "marked": marked, "unread": left, "auditDetail": json.dumps({"all": bool(everything), "marked": marked}, sort_keys=True)}


def discuss_get(handler, store, path: str, query: dict) -> bool:
    """GET /api/comments?kind=&target= and GET /api/notifications[?unread=1]."""
    if path == "/api/comments":
        if sorted(query) != ["kind", "target"] or any(len(v) != 1 or not v[0] for v in query.values()):
            raise ApiError(422, "bad_input", "this route takes kind and target, each once")
        kind, target = query["kind"][0], query["target"][0]
        show_id = _discuss(lambda: discuss.show_of(store, kind, target))
        if show_id is not None:
            _show_gate(store, handler.principal, "show", show_id, "view")
        me = handler.principal.name if handler.principal else LEGACY_ACTOR
        got = _discuss(lambda: {"comments": discuss.list_comments(store, kind, target, me), "people": discuss.roster(store, show_id)})
        handler._json(200, {"ok": True, "kind": kind, "target": target, "show": show_id, **got})
        return True
    if path == "/api/notifications":
        extra = sorted(set(query) - {"unread"})
        if extra or any(len(v) != 1 for v in query.values()):
            raise ApiError(422, "bad_input", "this route takes unread (0 or 1), once")
        if query.get("unread", ["0"])[0] not in ("0", "1"):
            raise ApiError(422, "bad_input", "unread must be 0 or 1")
        me = handler.principal.name if handler.principal else LEGACY_ACTOR
        got = _discuss(lambda: {"notifications": discuss.notifications(store, me, unread_only=query.get("unread") == ["1"]),
                                "unread": discuss.unread_count(store, me)})
        handler._json(200, {"ok": True, **got})
        return True
    return False


# ---- stories: the script save and the story read (docs/superpowers/specs/2026-11-01-newsroom-scripts-design.md) -----------
# Rules live in volt/story.py; these handlers read the body, call it, and map StoryError to HTTP.

def _base_rev(body: dict) -> int:
    value = body.get("baseRev")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ApiError(422, "bad_input", "baseRev is required: the revision this edit is based on (a whole number, 1 or more)")
    return value


def act_story_script(store, item_id, body):
    """Save an item's slug and/or script text, based on revision `baseRev`. A stale base is 409 with what is current."""
    _unknown_keys(body, {"script", "slug", "baseRev", "clientId"})
    script = _str(body, "script", limit=story.MAX_SCRIPT)
    slug = _str(body, "slug", limit=200)
    if script is None and slug is None:
        raise ApiError(422, "bad_input", "give slug, script or both")
    if slug is not None and not slug.strip():
        raise ApiError(422, "bad_input", "slug cannot be empty")
    base = _base_rev(body)
    client = _client_tab(body)
    show, _ = _item_show(store, item_id)
    actor = _who()[0]

    def save():
        with store.transaction(show_lock(show)):        # the lease check and the write are one step, under the lock go and take hold
            building.guard_edit(store, item_id, actor, client, CLOCK())
            return story.set_script(store, item_id, script=script, slug=slug, base_rev=base, actor=actor)
    out = _build_errors(save)
    row = store.conn.execute("SELECT show_id FROM item WHERE id = ?", (item_id,)).fetchone()
    text = store.conn.execute("SELECT script FROM item WHERE id = ?", (item_id,)).fetchone()["script"]
    return {"ok": True, "item": item_id, **out, "stats": story.script_stats(text, story.wpm_of(store, row["show_id"]))}


def act_item_float(store, item_id, body):
    """Mark an item as a backup story, or put it back. {floated: bool}. Only a ready item can be floated."""
    _unknown_keys(body, {"floated"})
    floated = _bool(body, "floated")
    show_id, _ = _item_show(store, item_id)
    _build_errors(lambda: story.set_floated(store, item_id, floated, _who()[0]))
    return {"ok": True, "item": item_id, "floated": floated, "rundown": rundown_payload(store, show_id)}


def act_show_pace(store, show_id, body):
    """Set or clear the show's reading pace. {wpm: 60..400 | null}; nothing else is accepted."""
    _unknown_keys(body, {"wpm"})
    if "wpm" not in body:
        raise ApiError(422, "bad_input", "wpm is required (a whole number from 60 to 400, or null for the default pace)")
    _show_exists(store, show_id)
    _build_errors(lambda: story.set_pace(store, show_id, body["wpm"], _who()[0]))
    return {"ok": True, "rundown": rundown_payload(store, show_id)}


def _wire_errors(fn):
    try:
        return fn()
    except (wire.WireRefusal, building.BuildError, story.StoryError) as exc:
        raise ApiError(exc.status, exc.code, exc.reason, **getattr(exc, "extra", {})) from None


def act_wire_take(store, wire_id, body):
    """A person turns an inbox entry into a draft story in a show. {show, slug?, position?}. Nothing from a wire airs without this."""
    _unknown_keys(body, {"show", "slug", "position"})
    show_id = _str(body, "show", required=True, limit=128)
    slug = _str(body, "slug", limit=200)
    _show_gate(store, _who()[1], "show", show_id, "edit")      # the show is named in the body, so the path gate cannot see it
    out = _wire_errors(lambda: wire.take(store, wire_id, show_id, slug, body.get("position"), _who()[0]))
    return {"ok": True, "wire": wire_id, "show": {"id": show_id}, **out, "rundown": rundown_payload(store, show_id)}


def act_wire_dismiss(store, wire_id, body):
    _unknown_keys(body, set())
    return {"ok": True, "wire": wire_id, **_wire_errors(lambda: wire.dismiss(store, wire_id, _who()[0]))}


def _template_errors(fn):
    try:
        return fn()
    except (templates.TemplateError, building.BuildError, story.StoryError) as exc:
        raise ApiError(exc.status, exc.code, exc.reason, **getattr(exc, "extra", {})) from None


def act_template_create(store, _unused, body):
    """Make a template: {kind, name, fromShow | fromItem | body}. Exactly one source."""
    _unknown_keys(body, {"kind", "name", "fromShow", "fromItem", "body"})
    kind = _str(body, "kind", required=True, limit=20)
    name = _str(body, "name", required=True, limit=templates.MAX_NAME)
    sources = [k for k in ("fromShow", "fromItem", "body") if body.get(k) is not None]
    if len(sources) != 1:
        raise ApiError(422, "bad_input", "give exactly one of fromShow, fromItem or body")
    actor = _who()[0]
    principal = _who()[1]
    if sources[0] == "fromShow":
        if kind != "show":
            raise ApiError(422, "bad_input", "fromShow makes a show template")
        _show_gate(store, principal, "show", str(body["fromShow"])[:128], "view")
        tid = _template_errors(lambda: templates.from_show(store, _str(body, "fromShow", required=True, limit=128), name, actor))
    elif sources[0] == "fromItem":
        if kind != "story":
            raise ApiError(422, "bad_input", "fromItem makes a story template")
        _show_gate(store, principal, "item", str(body["fromItem"])[:128], "view")
        tid = _template_errors(lambda: templates.from_item(store, _str(body, "fromItem", required=True, limit=128), name, actor))
    else:
        tid = _template_errors(lambda: templates.create(store, kind, name, body["body"], actor))
    return {"ok": True, "template": {"id": tid, "kind": kind, "name": name.strip()}}


def act_template_instantiate(store, template_id, body):
    """Use a template. A story template: {show, position?} adds a draft story. A show template: {title} makes a new show."""
    _unknown_keys(body, {"show", "position", "title"})
    actor = _who()[0]
    row = store.conn.execute("SELECT kind FROM story_template WHERE id = ?", (template_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", f"no such template: {template_id}")
    if row["kind"] == "story":
        show_id = _str(body, "show", required=True, limit=128)
        _show_gate(store, _who()[1], "show", show_id, "edit")
        out = _template_errors(lambda: templates.instantiate_story(store, template_id, show_id, body.get("position"), actor))
        return {"ok": True, "template": {"id": template_id}, "show": {"id": show_id}, **out, "rundown": rundown_payload(store, show_id)}
    title = _str(body, "title", required=True, limit=200)
    out = _template_errors(lambda: templates.instantiate_show(store, template_id, title, actor))
    return {"ok": True, "template": {"id": template_id}, **out, "show": {"id": out["show"], "title": title.strip()},
            "rundown": rundown_payload(store, out["show"])}


def act_template_delete(store, template_id, body):
    _unknown_keys(body, set())
    return {"ok": True, "template": {"id": template_id}, **_template_errors(lambda: templates.delete(store, template_id, _who()[0]))}


def act_item_copy(store, source_id, body):
    """Copy a story (or `archive:<id>`) into a show as a new draft. {show, position?}. Not a take: it works on a held show."""
    _unknown_keys(body, {"show", "position"})
    show_id = _str(body, "show", required=True, limit=128)
    _show_gate(store, _who()[1], "show", show_id, "edit")
    out = _build_errors(lambda: story.copy_story(store, source_id, show_id, body.get("position"), _who()[0]))
    return {"ok": True, "source": source_id, "show": {"id": show_id}, **out, "rundown": rundown_payload(store, show_id)}


def stories_get(handler, store, path: str, query: dict) -> bool:
    """GET /api/stories and GET /api/templates."""
    if path == "/api/templates":
        if query:
            raise ApiError(422, "bad_input", "this route takes no parameters")
        handler._json(200, templates.listing(store))
        return True
    if path != "/api/stories":
        return False
    extra = sorted(set(query) - {"q", "status", "show", "from", "to", "limit", "cursor"})
    if extra or any(len(v) != 1 for v in query.values()):
        raise ApiError(422, "bad_input", "this route takes q, status, show, from, to, limit and cursor, each once")
    one = lambda k: query.get(k, [""])[0]  # noqa: E731
    limit = one("limit") or "50"
    if not limit.isdigit():
        raise ApiError(422, "bad_input", "limit must be a whole number from 1 to 200")
    visible = _view_filter(store, getattr(handler, "principal", None))    # a restricted show's stories are not listed to people who are not on it
    handler._json(200, _build_errors(lambda: story.search(store, q=one("q"), status=one("status"), show=one("show"), from_=one("from"), to=one("to"),
                                                          limit=int(limit), cursor=one("cursor") or None, show_ok=visible)))
    return True


def wire_get(handler, store, path: str, query: dict) -> bool:
    """GET /api/wire?state=new&limit=&cursor=: the inbox, newest first, with each feed's last success and last error."""
    if path != "/api/wire":
        return False
    extra = sorted(set(query) - {"state", "limit", "cursor"})
    if extra or any(len(v) != 1 for v in query.values()):
        raise ApiError(422, "bad_input", "this route takes state, limit and cursor, each once")
    limit = query.get("limit", ["50"])[0]
    if not limit.isdigit():
        raise ApiError(422, "bad_input", "limit must be a whole number from 1 to 200")
    handler._json(200, _wire_errors(lambda: wire.list_inbox(store, query.get("state", ["new"])[0], int(limit), query.get("cursor", [None])[0])))
    return True


PROMPTER_TTL_DEFAULT, PROMPTER_TTL_MAX = 4 * 3600, 12 * 3600


def act_prompter_ticket(store, show_id, body):
    """Issue a link a prompter computer can open without an operator token: one show, read-only, 4 hours (at most 12).
    The ticket rides in the URL like a media ticket, so it can reach logs; it is short-lived and reads one feed only."""
    _unknown_keys(body, {"ttlSeconds"})
    ttl = body.get("ttlSeconds", PROMPTER_TTL_DEFAULT)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 60 <= ttl <= PROMPTER_TTL_MAX:
        raise ApiError(422, "bad_input", f"ttlSeconds must be a whole number from 60 to {PROMPTER_TTL_MAX}")
    _show_exists(store, show_id)
    secret = MEDIA_KEY or TOKEN
    if not secret:
        raise ApiError(503, "no_media_key", "no signing key is configured (set VOLT_MEDIA_KEY or VOLT_TOKEN)")
    token, exp = tickets.issue(secret, show_id, "prompter", ttl, int(time.time()))
    return {"ok": True, "show": {"id": show_id}, "url": f"/prompter?show={show_id}&ticket={token}", "expiresAt": exp}


def act_story_status(store, item_id, body):
    """Move a story between draft, ready, approved and killed, quoting `baseRev`. Never touches the item's playout status."""
    _unknown_keys(body, {"status", "baseRev", "note"})
    status = _str(body, "status", required=True, limit=20)
    if status not in story.STATUSES:
        raise ApiError(422, "bad_input", f"status must be one of {', '.join(story.STATUSES)}")
    base = _base_rev(body)
    note = _str(body, "note", limit=500)
    _item_show(store, item_id)
    actor, principal = _who()
    out = _build_errors(lambda: story.set_status(store, item_id, status, base_rev=base, actor=actor, principal=principal, note=note))
    return {"ok": True, "item": item_id, **out}


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


def act_device_safe_mode(store, _unused, body):
    """Turn safe mode on or off: {on: bool, reason?}. While on, Volt sends nothing to any device other than CoreVideo Pro."""
    _unknown_keys(body, {"on", "reason", "owner"})
    on = _bool(body, "on")
    reason = _str(body, "reason", default="", limit=300)
    state = safety.set(store, on, reason, getattr(_REQUEST, "actor", LEGACY_ACTOR))
    return {"ok": True, "safeMode": state, "devices": devices.status(store)}


def devices_get(handler, store, path: str, query: dict) -> bool:
    """GET /api/devices: the configured devices, what the poller last saw, and safe mode."""
    if path != "/api/devices":
        return False
    if query:
        raise ApiError(422, "bad_input", "this route takes no parameters")
    handler._json(200, {"ok": True, "devices": devices.status(store), "safeMode": safety.state(store)})
    return True


def show_building_get(handler, store, path: str, query: dict) -> bool:
    """GET /api/cue-commands, GET /api/items/{item}/cues and GET /api/items/{item}/story. Returns True when it answered."""
    p = PROMPTER_ROUTE.match(path)
    if p:     # the feed a prompter page polls: a bearer, or a ticket for exactly this show
        handler._accept_prompter_ticket(p.group(1), query)
        handler._json(200, _build_errors(lambda: story.prompter_feed(store, p.group(1))))
        return True
    t = re.fullmatch(rf"/api/shows/{_ID}/script", path)
    if t:
        if query != {"format": ["text"]}:
            raise ApiError(422, "bad_input", "this route needs format=text and nothing else")
        text = _build_errors(lambda: story.script_text(store, t.group(1)))
        handler._send(200, "text/plain; charset=utf-8", text.encode("utf-8"), {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        return True
    s = re.fullmatch(rf"/api/items/{_ID}/story", path)
    if s:
        if query:
            raise ApiError(422, "bad_input", "this route takes no parameters")
        who = getattr(handler, "principal", None)
        _show_gate(store, who, "item", s.group(1), "view")
        handler._json(200, _build_errors(lambda: story.get_story(store, s.group(1), include_notes=who is None or who.role != "viewer")))
        return True
    m = re.fullmatch(rf"/api/items/{_ID}/cues", path)
    if path != "/api/cue-commands" and not m:
        return False
    if query:
        raise ApiError(422, "bad_input", "this route takes no parameters")
    if m:
        _item_show(store, m.group(1))
        _show_gate(store, getattr(handler, "principal", None), "item", m.group(1), "view")
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
PROMPTER_ROUTE = re.compile(rf"^/api/shows/{_ID}/prompter$")

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


def _access_error(fn):
    try:
        return fn()
    except acl.AccessError as exc:
        raise ApiError(exc.status, exc.code, exc.reason) from None
    except users.UserError as exc:
        raise ApiError(exc.status, exc.code, exc.reason) from None


def act_group_create(store, _unused, body):
    _unknown_keys(body, {"name", "role"})
    return {"ok": True, "group": _access_error(lambda: acl.create_group(store, body.get("name"), body.get("role")))}


def act_group_members(store, name, body):
    _unknown_keys(body, {"add", "remove"})
    for key in ("add", "remove"):
        if key in body and not (isinstance(body[key], list) and all(isinstance(x, str) for x in body[key])):
            raise ApiError(422, "bad_input", f"{key} must be a list of user names")
    try:
        group = _access_error(lambda: acl.set_members(store, name, add=body.get("add") or (), remove=body.get("remove") or ()))
    except ApiError as err:
        if err.code == "no_such_group":
            raise ApiError(404, "not_found", err.reason) from None
        raise
    return {"ok": True, "group": group}


def act_group_delete(store, name, body):
    _unknown_keys(body, set())
    try:
        _access_error(lambda: acl.delete_group(store, name))
    except ApiError as err:
        if err.code == "no_such_group":
            raise ApiError(404, "not_found", err.reason) from None
        raise
    return {"ok": True, "deleted": name}


def act_show_access(store, show_id, body):
    """Give a user or group a level on a show (view, edit or run), or take it away (level null). {subject_kind, subject, level}.
    Adding the first row restricts the show to the people listed (and every admin); removing the last opens it again."""
    _unknown_keys(body, {"subject_kind", "subject", "level"})
    for key in ("subject_kind", "subject", "level"):
        if key not in body:
            raise ApiError(422, "bad_input", f"{key} is required (level null removes the row)")
    rows = _access_error(lambda: acl.set_acl(store, show_id, body["subject_kind"], body["subject"], body["level"]))
    return {"ok": True, "access": rows,
            "auditDetail": json.dumps({"subject_kind": body["subject_kind"], "subject": body["subject"], "level": body["level"]}, sort_keys=True)}


def act_user_role(store, name, body):
    _unknown_keys(body, {"role"})
    return {"ok": True, "user": _access_error(lambda: acl.set_user_role(store, name, body.get("role"), legacy_admin=bool(TOKEN)))}


def act_group_claims(store, name, body):
    _unknown_keys(body, {"provider", "value", "remove"})
    for key in ("provider", "value"):
        if key not in body:
            raise ApiError(422, "bad_input", f"{key} is required")
    remove = body.get("remove", False)
    if not isinstance(remove, bool):
        raise ApiError(422, "bad_input", "remove must be true or false")
    try:
        group = _access_error(lambda: acl.set_claim_map(store, name, body["provider"], body["value"], remove))
    except ApiError as err:
        if err.code == "no_such_group":
            raise ApiError(404, "not_found", err.reason) from None
        raise
    return {"ok": True, "group": group}


def act_user_identity(store, name, body):
    _unknown_keys(body, {"email"})
    if "email" not in body:
        raise ApiError(422, "bad_input", "email is required (null clears it)")
    return {"ok": True, "user": _access_error(lambda: acl.set_user_email(store, name, body["email"]))}


def act_user_end_sessions(store, name, body):
    _unknown_keys(body, set())
    _user_error(lambda: users._row(store, name))
    return {"ok": True, "ended": sessions.end_for_user(store, name)}


def _cookie_flags():
    return sessions.is_https(), sessions.is_https()   # (Secure, __Host- name): both follow VOLT_PUBLIC_URL, never a request header


def act_session_start(store, _unused, body, handler):
    """Turn a valid per-user bearer token into a cookie session. A cookie, the legacy VOLT_TOKEN and the open loopback mode cannot."""
    _unknown_keys(body, set())
    principal = handler.principal
    if principal.via == "session" or not handler._bearer():
        raise ApiError(401, "bearer_required", "starting a session needs a user token in the Authorization header")
    if principal.legacy:
        raise ApiError(409, "not_a_user", "the legacy VOLT_TOKEN names no user, so it cannot own a session; it keeps working as a bearer")
    if sessions.public_origin() is None and not _is_loopback(_host_name(handler.headers.get("Host") or "")):
        raise ApiError(409, "public_url_required",
                       "set VOLT_PUBLIC_URL to this server's https address before signing in with a cookie off loopback")
    value, csrf = sessions.create(store, principal.name, "token", CLOCK())
    secure, prefix = _cookie_flags()
    return {"ok": True, "csrf": csrf, "_headers": {"Set-Cookie": sessions.cookie_header(value, secure, prefix), "Cache-Control": "no-store"}}


def act_session_end(store, _unused, body, handler):
    _unknown_keys(body, set())
    principal = handler.principal
    if principal.via != "session":
        return {"ok": True, "ended": False}
    sessions.end(store, principal.session_id)
    secure, prefix = _cookie_flags()
    return {"ok": True, "ended": True, "_headers": {"Set-Cookie": sessions.clear_header(secure, prefix), "Cache-Control": "no-store"}}


# These two need the request itself (who it is, its Host), so do_POST passes the handler as a fourth argument and sets the
# response headers they name under `_headers`.
REQUEST_AWARE = {act_session_start, act_session_end}

def _field_of(store, key):
    for f in metadata.list_schema(store):
        if f["key"] == key:
            return f
    return None


def act_schema_field(store, _unused, body):
    """Create a field, or edit an existing one (label, type, required, multi). The show of a field never changes."""
    _unknown_keys(body, {"key", "label", "type", "show", "required", "multi"})
    key = _str(body, "key", required=True, limit=64)
    actor, _role = _caller()
    existing = _field_of(store, key)
    if existing is None:
        fn = lambda: metadata.define_field(  # noqa: E731
            store, key, body.get("label"), body.get("type"), show_name=body.get("show"),
            required=body.get("required", False), multi=body.get("multi", False), actor=actor)
        field = _meta_error(fn)
        return {"ok": True, "created": True, "field": field}
    if "show" in body and body["show"] != existing["showName"]:
        raise ApiError(422, "bad_input", "a field's show cannot change; retire it and define a new one")
    edits = {k: body[k] for k in ("label", "type", "required", "multi") if k in body}
    field = _meta_error(lambda: metadata.update_field(store, key, **edits))
    return {"ok": True, "created": False, "field": field}


def act_schema_retire(store, key, body):
    _unknown_keys(body, set())
    _meta_error(lambda: metadata.retire_field(store, key), missing_is_404=True)
    return {"ok": True, "retired": key}


def act_schema_terms(store, key, body):
    _unknown_keys(body, {"terms"})
    added = _meta_error(lambda: metadata.add_terms(store, key, body.get("terms")), missing_is_404=True)
    return {"ok": True, "added": added, "field": _field_of(store, key)}


def act_schema_term_retire(store, key, body):
    _unknown_keys(body, {"term"})
    _meta_error(lambda: metadata.retire_term(store, key, body.get("term")), missing_is_404=True)
    return {"ok": True, "field": _field_of(store, key)}


def act_asset_metadata(store, asset_id, body):
    """Set one field's values on one asset. The audit row's detail is the old and new values as JSON."""
    _unknown_keys(body, {"key", "values"})
    key = _str(body, "key", required=True, limit=64)
    if not isinstance(body.get("values"), list):
        raise ApiError(422, "bad_input", "values must be a list (an empty list clears the field)")
    actor, _role = _caller()
    try:
        change = _meta_error(lambda: metadata.set_values(store, asset_id, key, body["values"], actor))
    except KeyError:
        raise ApiError(404, "not_found", f"no such asset: {asset_id}") from None
    state = metadata.get_values(store, asset_id)
    return {"ok": True, "asset": asset_id, "key": key, "values": change["new"], "missing": state["missing"],
            "auditDetail": json.dumps({"key": key, "old": change["old"], "new": change["new"]}, sort_keys=True)}


def _collection_error(fn):
    try:
        return fn()
    except colls.CollectionError as exc:
        raise ApiError(exc.status, exc.code, exc.reason) from None


def _optional_span(body: dict):
    span = body.get("spanId")
    if span is not None and not isinstance(span, str):
        raise ApiError(422, "bad_input", "spanId must be a span id or left out")
    return span


def _flag(body: dict, key: str) -> bool:
    return body[key] if key in body else False


def act_search_save(store, _unused, body):
    _unknown_keys(body, {"name", "query", "shared"})
    actor, _role = _caller()
    saved = _collection_error(lambda: colls.save_search(store, actor, body.get("name"), body.get("query"), _flag(body, "shared")))
    return {"ok": True, "search": saved}


def act_search_delete(store, search_id, body):
    _unknown_keys(body, set())
    actor, role = _caller()
    _collection_error(lambda: colls.delete_search(store, search_id, actor, role == "admin"))
    return {"ok": True, "deleted": search_id}


def act_bin_create(store, _unused, body):
    _unknown_keys(body, {"name", "shared"})
    actor, _role = _caller()
    return {"ok": True, "bin": _collection_error(lambda: colls.create_bin(store, actor, body.get("name"), _flag(body, "shared")))}


def act_bin_add(store, bin_id, body):
    _unknown_keys(body, {"assetId", "spanId"})
    actor, role = _caller()
    out = _collection_error(lambda: colls.add_member(store, bin_id, body.get("assetId"), _optional_span(body), actor, role == "admin"))
    return {"ok": True, "bin": colls.get_bin(store, bin_id, actor), **out}


def act_bin_remove(store, bin_id, body):
    _unknown_keys(body, {"assetId", "spanId"})
    actor, role = _caller()
    removed = _collection_error(lambda: colls.remove_member(store, bin_id, body.get("assetId"), _optional_span(body), actor, role == "admin"))
    return {"ok": True, "removed": removed, "bin": colls.get_bin(store, bin_id, actor)}


def act_bin_delete(store, bin_id, body):
    _unknown_keys(body, set())
    actor, role = _caller()
    _collection_error(lambda: colls.delete_bin(store, bin_id, actor, role == "admin"))
    return {"ok": True, "deleted": bin_id}


SEARCH_PARAMS = {"q", "scope", "filter", "facets", "sort", "group", "include", "limit", "after"}
MAX_FILTER_PARAM = 4096


def _search_request(query: dict) -> dict:
    """The query string of GET /api/search as a search request. Every parameter at most once; `filter` is URL-encoded JSON."""
    extra = sorted(set(query) - SEARCH_PARAMS)
    if extra:
        raise ApiError(422, "bad_input", f"unknown parameter(s): {', '.join(extra)}")
    req = {}
    for key, values in query.items():
        if len(values) != 1:
            raise ApiError(422, "bad_input", f"{key} may be given once")
        req[key] = values[0]
    if "filter" in req:
        if len(req["filter"].encode("utf-8")) > MAX_FILTER_PARAM:
            raise ApiError(422, "bad_input", f"filter is over {MAX_FILTER_PARAM} bytes")
        try:
            req["filter"] = json.loads(req["filter"])
        except ValueError:
            raise ApiError(422, "bad_input", "filter must be URL-encoded JSON") from None
    if "limit" in req:
        if not req["limit"].isdigit():
            raise ApiError(422, "bad_input", "limit must be a whole number")
        req["limit"] = int(req["limit"])
    return req


def search_get(handler, store, path: str, query: dict) -> bool:
    """GET /api/search, /api/searches, /api/bins and /api/bins/{id}. Returns True when it answered."""
    one = re.fullmatch(rf"/api/bins/{_ID}", path)
    if path not in ("/api/search", "/api/searches", "/api/bins") and not one:
        return False
    principal = handler.principal
    actor = _actor(handler.headers, {}, principal)
    is_admin = principal is None or principal.role == "admin"
    if path == "/api/search":
        try:
            result = searchindex.search(store, _search_request(query), principal=Principal(actor, "operator", False))
        except searchindex.SearchError as exc:
            raise ApiError(exc.status, exc.code, exc.reason) from None
        handler._json(200, {"ok": True, **result})
        return True
    if query:
        raise ApiError(422, "bad_input", "this route takes no parameters")
    if path == "/api/searches":
        handler._json(200, {"ok": True, "searches": colls.list_searches(store, actor)})
    elif path == "/api/bins":
        handler._json(200, {"ok": True, "bins": colls.list_bins(store, actor)})
    else:
        found = _collection_error(lambda: colls.get_bin(store, one.group(1), actor, is_admin))
        handler._json(200, {"ok": True, "bin": found, "members": colls.members(store, one.group(1))})
    return True


def schema_get(handler, store, path: str, query: dict) -> bool:
    """GET /api/schema and GET /api/assets/{id}/metadata. Returns True when it answered."""
    m = re.fullmatch(rf"/api/assets/{_ID}/metadata", path)
    if path != "/api/schema" and not m:
        return False
    allowed = {"show"} if path == "/api/schema" else set()
    extra = sorted(set(query) - allowed)
    if extra:
        raise ApiError(422, "bad_input", f"unknown parameter(s): {', '.join(extra)}")
    if m:
        _asset_exists(store, m.group(1))
        handler._json(200, {"ok": True, "assetId": m.group(1), **metadata.get_values(store, m.group(1))})
    else:
        given = query.get("show") or []
        if len(given) > 1:
            raise ApiError(422, "bad_input", "show may be given once")
        handler._json(200, {"ok": True, "fields": metadata.list_schema(store, given[0] if given else None)})
    return True


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
    (re.compile(r"^/api/groups()$"), act_group_create),
    (re.compile(rf"^/api/groups/{_ID}/members$"), act_group_members),
    (re.compile(rf"^/api/groups/{_ID}/delete$"), act_group_delete),
    (re.compile(rf"^/api/groups/{_ID}/claims$"), act_group_claims),
    (re.compile(rf"^/api/users/{_ID}/role$"), act_user_role),
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
    (re.compile(rf"^/api/items/{_ID}/script$"), act_story_script),
    (re.compile(rf"^/api/items/{_ID}/lease$"), act_item_lease),
    (re.compile(rf"^/api/items/{_ID}/lease/release$"), act_item_lease_release),
    (re.compile(rf"^/api/items/{_ID}/lease/takeover$"), act_item_lease_takeover),
    (re.compile(r"^/api/presence$()"), act_presence),
    (re.compile(r"^/api/comments$()"), act_comment_add),
    (re.compile(rf"^/api/comments/{_ID}/resolve$"), act_comment_resolve),
    (re.compile(rf"^/api/comments/{_ID}/delete$"), act_comment_delete),
    (re.compile(r"^/api/notifications/read$()"), act_notifications_read),
    (re.compile(rf"^/api/items/{_ID}/story-status$"), act_story_status),
    (re.compile(rf"^/api/items/{_ID}/float$"), act_item_float),
    (re.compile(rf"^/api/shows/{_ID}/reading-pace$"), act_show_pace),
    (re.compile(rf"^/api/shows/{_ID}/prompter-ticket$"), act_prompter_ticket),
    (re.compile(rf"^/api/wire/{_ID}/take$"), act_wire_take),
    (re.compile(rf"^/api/wire/{_ID}/dismiss$"), act_wire_dismiss),
    (re.compile(rf"^/api/shows/{_ID}/access$"), act_show_access),
    (re.compile(r"^/api/templates$()"), act_template_create),
    (re.compile(rf"^/api/templates/{_ID}/instantiate$"), act_template_instantiate),
    (re.compile(rf"^/api/templates/{_ID}/delete$"), act_template_delete),
    (re.compile(rf"^/api/items/{_ID}/copy$"), act_item_copy),
    (re.compile(rf"^/api/items/{_ID}/remove$"), act_item_remove),
    (re.compile(rf"^/api/items/{_ID}/detach$"), act_item_detach),
    (re.compile(rf"^/api/items/{_ID}/cues$"), act_cue_add),
    (re.compile(rf"^/api/cues/{_ID}/remove$"), act_cue_remove),
    (re.compile(r"^/api/ingest$()"), act_ingest),
]
ROUTES += [  # device control
    (re.compile(r"^/api/devices/safe-mode$()"), act_device_safe_mode),
]
ROUTES += [  # typed metadata: the schema (admin, except adding terms) and per-asset values
    (re.compile(r"^/api/schema/fields$()"), act_schema_field),
    (re.compile(rf"^/api/schema/fields/{_ID}/retire$"), act_schema_retire),
    (re.compile(rf"^/api/schema/fields/{_ID}/terms$"), act_schema_terms),
    (re.compile(rf"^/api/schema/fields/{_ID}/terms/retire$"), act_schema_term_retire),
    (re.compile(rf"^/api/assets/{_ID}/metadata$"), act_asset_metadata),
]
ROUTES += [  # saved searches and bins (operator)
    (re.compile(r"^/api/searches$()"), act_search_save),
    (re.compile(rf"^/api/searches/{_ID}/delete$"), act_search_delete),
    (re.compile(r"^/api/bins$()"), act_bin_create),
    (re.compile(rf"^/api/bins/{_ID}/add$"), act_bin_add),
    (re.compile(rf"^/api/bins/{_ID}/remove$"), act_bin_remove),
    (re.compile(rf"^/api/bins/{_ID}/delete$"), act_bin_delete),
]
ROUTES += [  # user management: admin only
    (re.compile(r"^/api/users$()"), act_user_add),
    (re.compile(rf"^/api/users/{_ID}/disable$"), act_user_disable),
    (re.compile(rf"^/api/users/{_ID}/enable$"), act_user_enable),
    (re.compile(rf"^/api/users/{_ID}/rotate$"), act_user_rotate),
    (re.compile(rf"^/api/users/{_ID}/end-sessions$"), act_user_end_sessions),
    (re.compile(rf"^/api/users/{_ID}/identity$"), act_user_identity),
]
ROUTES += [  # sessions: any signed-in role
    (re.compile(r"^/api/session/start$()"), act_session_start),
    (re.compile(r"^/api/session/end$()"), act_session_end),
]

# Every POST route's role, by action function. operator runs the show; admin is everything an operator can do plus
# users and the destructive or retention actions. A route missing here is refused as admin-only, and a test fails
# until it is classified on purpose.
ROUTE_ROLES = {
    # show control
    "act_group_create": "admin", "act_group_members": "admin", "act_group_delete": "admin", "act_user_role": "admin",
    "act_group_claims": "admin", "act_user_identity": "admin", "act_show_access": "admin",
    "act_device_safe_mode": "operator", "act_mos_resolve": "operator", "act_asset_archive": "operator", "act_asset_rehydrate": "operator", "act_show_recurrence": "operator", "act_item_schedule": "operator", "act_show_slot": "operator", "act_go": "operator", "act_hold": "operator", "act_release": "operator", "act_take": "operator",
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
    "act_story_script": "operator", "act_prompter_ticket": "operator", "act_wire_take": "operator", "act_wire_dismiss": "operator", "act_template_create": "operator", "act_template_instantiate": "operator", "act_item_copy": "operator", "act_template_delete": "admin", "act_story_status": "operator", "act_item_float": "operator", "act_show_pace": "operator", "act_item_remove": "operator", "act_item_detach": "operator", "act_cue_add": "operator",
    "act_cue_remove": "operator", "act_ingest": "operator",
    # destructive or retention
    "act_delete": "admin", "act_unpublish": "admin", "act_span_delete": "admin", "act_detect": "admin",
    "act_show_delete": "admin",
    # typed metadata
    "act_schema_field": "admin", "act_schema_retire": "admin", "act_schema_terms": "operator",
    "act_schema_term_retire": "admin", "act_asset_metadata": "operator",
    # saved searches and bins
    "act_search_save": "operator", "act_search_delete": "operator", "act_bin_create": "operator", "act_bin_add": "operator",
    "act_bin_remove": "operator", "act_bin_delete": "operator",
    # users and tokens
    "act_user_add": "admin", "act_user_disable": "admin", "act_user_enable": "admin", "act_user_rotate": "admin",
    "act_user_end_sessions": "admin",
    # sessions: a viewer may sign in with a token and sign out
    "act_session_start": "viewer", "act_session_end": "viewer",
    # collaboration: a lease is an edit, so it needs an operator; presence is only "I am looking", so a viewer may send it
    "act_item_lease": "operator", "act_item_lease_release": "operator", "act_item_lease_takeover": "operator", "act_presence": "viewer",
    # comments: a viewer may read, comment and delete their own comment, and mark their own notifications read; closing a thread is an edit
    "act_comment_add": "viewer", "act_comment_delete": "viewer", "act_notifications_read": "viewer", "act_comment_resolve": "operator",
}


# ---- per-show access ---------------------------------------------------------
# Every POST function is classified once, here: either it acts on one show (ROUTE_SHOW_LEVEL gives the least level it needs, SHOW_RESOLVER
# says how to get from the id in its path to that show) or it does not (NOT_SHOW_SCOPED). A test walks serve.ROUTES and fails for a
# function in neither, in both, or with no resolver, and for any route under /api/shows/{id}/, /api/items/ or /api/cues/ that is exempt.
# do_POST checks the level right after the role check and before it reads the body or opens the handler's work. A function in neither map
# is refused (fail closed) for everyone but an admin and the desk token. The clock and the runner never come through here.
# view reads and comments, edit builds the rundown, run is edit plus go, take, hold, release, skip and kill.
ROUTE_SHOW_LEVEL = {
    # run the show
    "act_go": "run", "act_take": "run", "act_hold": "run", "act_release": "run", "act_skip": "run", "act_kill": "run",
    "act_mos_resolve": "run", "act_show_access": "run",
    # build the rundown
    "act_show_slot": "edit", "act_show_recurrence": "edit", "act_reorder": "edit", "act_show_pace": "edit", "act_show_delete": "edit",
    "act_prompter_ticket": "edit", "act_item_copy": "edit", "act_item_add": "edit", "act_item_schedule": "edit", "act_attach": "edit", "act_item_rename": "edit", "act_story_script": "edit",
    "act_story_status": "edit", "act_item_float": "edit", "act_item_remove": "edit", "act_item_detach": "edit", "act_cue_add": "edit",
    "act_cue_remove": "edit",
    "act_item_lease": "edit", "act_item_lease_release": "edit", "act_item_lease_takeover": "edit",
    "act_comment_resolve": "edit", "act_comment_delete": "view",
}
# How to find the show from the id the path captured: the show itself, an item, a cue, or a MOS "device:show" id.
SHOW_RESOLVER = {
    "act_go": "show", "act_take": "show", "act_hold": "show", "act_release": "show", "act_show_access": "show",
    "act_show_slot": "show", "act_show_recurrence": "show", "act_reorder": "show", "act_show_pace": "show", "act_show_delete": "show",
    "act_item_add": "show", "act_mos_resolve": "mos",
    "act_skip": "item", "act_kill": "item", "act_item_schedule": "item", "act_attach": "item", "act_item_rename": "item",
    "act_prompter_ticket": "show", "act_item_copy": "item", "act_story_script": "item", "act_story_status": "item", "act_item_float": "item", "act_item_remove": "item", "act_item_detach": "item",
    "act_cue_add": "item", "act_cue_remove": "cue",
    "act_item_lease": "item", "act_item_lease_release": "item", "act_item_lease_takeover": "item",
    "act_comment_resolve": "comment", "act_comment_delete": "comment",
}
NOT_SHOW_SCOPED = frozenset({
    "act_show_create",                                                       # there is no show yet
    "act_record_start", "act_record_stop", "act_ingest",                     # assets: they name a show only as free text
    "act_cleared", "act_legal_hold", "act_delete", "act_restore", "act_asset_archive", "act_asset_rehydrate", "act_asset_metadata",
    "act_publish", "act_unpublish", "act_export_nle", "act_detect", "act_span_frames",
    "act_span_create", "act_span_trim", "act_span_label", "act_span_delete", "act_span_split", "act_span_merge",
    "act_device_safe_mode",
    "act_schema_field", "act_schema_retire", "act_schema_terms", "act_schema_term_retire",
    "act_search_save", "act_search_delete", "act_bin_create", "act_bin_add", "act_bin_remove", "act_bin_delete",
    "act_user_add", "act_user_disable", "act_user_enable", "act_user_rotate", "act_user_end_sessions", "act_user_identity", "act_user_role",
    "act_group_create", "act_group_members", "act_group_delete", "act_group_claims",
    "act_session_start", "act_session_end",
    "act_presence",                                                          # names a show in the body and checks view access on it itself
    "act_comment_add",                                                       # names its target in the body and checks view access on that show itself
    "act_notifications_read",                                                # your own notifications; nothing show-scoped to check
    "act_template_create", "act_template_instantiate", "act_template_delete",   # the show is in the body: act_template_instantiate and _create check it themselves
    "act_wire_take", "act_wire_dismiss",                                     # the show is in the body: act_wire_take checks it itself
})


def _show_gate(store, principal, kind: str, ident: str, need: str) -> None:
    """Raise 403 forbidden_show unless `principal` has at least level `need` on the show that (kind, ident) belongs to.

    An id that names nothing passes (the handler answers 404). Admins and the desk token pass without the table being read. If the
    table cannot be read the answer is no (503), never a pass."""
    if acl.bypasses(principal):
        return
    try:
        if kind == "mos":
            kind, ident = "show", ident.partition(":")[2]
        show_id = acl.show_of(store, kind, ident)
        have = None if show_id is None else acl.show_level(store, principal, show_id)
    except Exception:  # noqa: BLE001 - fail closed; the cause goes to the log
        log.exception("show access check failed")
        raise ApiError(503, "access_unavailable", "cannot check show access right now") from None
    if show_id is None:
        return
    if have is None or acl.LEVELS[have] < acl.LEVELS[need]:
        raise ApiError(403, "forbidden_show", f"{need} access to this show is required")


def _view_filter(store, principal):
    try:
        return acl.view_filter(store, principal)
    except Exception:  # noqa: BLE001 - fail closed
        log.exception("show access check failed")
        raise ApiError(503, "access_unavailable", "cannot check show access right now") from None


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
        return Principal(user.name, user.role, False, user.groups) if user else None

    def _session_principal(self):
        """The principal a valid session cookie names, or None. The role is read from the tables now, never from the session row."""
        raw = sessions.cookie_value(self.headers.get("Cookie"), sessions.is_https())
        if not raw:
            return None
        try:
            store = self._open_store()
            try:
                info = sessions.resolve(store, raw, CLOCK())
                if info is None:
                    return None
                row = store.conn.execute("SELECT name, role, disabled_at FROM volt_user WHERE name = ?", (info.user_name,)).fetchone()
                if row is None or row["disabled_at"] is not None:
                    sessions.end(store, info.id_hash)
                    return None
                return Principal(row["name"], acl.effective_role(store, row["name"], row["role"]), False,
                                 acl.groups_of(store, row["name"]), via="session", session_id=info.id_hash, csrf=info.csrf)
            finally:
                store.close()
        except Exception:  # noqa: BLE001 - fail closed; the cause goes to the log
            log.exception("session lookup failed")
            raise ApiError(503, "auth_unavailable", "cannot check the session right now") from None

    def _check_cookie_write(self) -> None:
        """A cookie is attached by the browser on its own, so a cookie POST must also prove it came from this site's page:
        an Origin equal to the public URL's origin, and the per-session CSRF value in a header a foreign page cannot add."""
        expected = sessions.public_origin()
        if expected is None:  # no VOLT_PUBLIC_URL: only a loopback Host, whose http origin is its own
            host = self.headers.get("Host") or ""
            if _is_loopback(_host_name(host)):
                expected = sessions.origin_of("http://" + host)
        origin = sessions.origin_of(self.headers.get("Origin"))
        if expected is None or origin is None or origin != expected:
            raise ApiError(403, "bad_origin", "this request did not come from the page this server serves")
        given = self.headers.get("X-Volt-Csrf") or ""
        if not given or not hmac.compare_digest(given.encode("utf-8"), self.principal.csrf.encode("utf-8")):
            raise ApiError(403, "bad_csrf", "missing or wrong X-Volt-Csrf; read it from /api/whoami")

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
        if not self._bearer():  # a bearer header, even a wrong one, never falls back to a cookie
            found = self._session_principal()
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
            media_get = bool(THUMB_ROUTE.match(parsed.path)) or bool(PROMPTER_ROUTE.match(parsed.path))
        return media_get and "ticket" in parse_qs(parsed.query, keep_blank_values=True)

    def _accept_prompter_ticket(self, show_id: str, query: dict) -> None:
        """The prompter feed takes `ticket` instead of a bearer: one show, one purpose, short-lived. Nothing else is accepted."""
        extra = sorted(set(query) - {"ticket"})
        if extra:
            raise ApiError(422, "bad_input", "the prompter feed takes no parameters")
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
            tickets.verify(secret, show_id, "prompter", given[0], int(time.time()))
        except tickets.TicketError:
            raise TicketDenied() from None

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
        if path == "/auth" or path.startswith("/auth/"):   # sign-in: before the gate, because the browser has no credential yet
            self._auth(path)
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

    # ---- OIDC sign-in (unvalidated against a real provider; volt/oidc.py) --------------------------
    def _redirect(self, location: str, cookies=()):
        self.send_response(302)
        self.send_header("Location", location)
        for value in cookies:
            self.send_header("Set-Cookie", value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _login_error_page(self, err, secure: bool):
        """A fixed-text page: nothing from the request, the provider or the exception is put in it."""
        body = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
                '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign-in problem - Volt</title>'
                '<link rel="stylesheet" href="/operator/operator.css"></head><body><main class="auth-error">'
                '<h1>Sign-in could not be completed</h1><p>' + err.public_message + '</p>'
                '<p><a href="/">Back to Volt</a></p></main></body></html>').encode("utf-8")
        self.send_response(err.status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", oidc.clear_flow_cookie(secure))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _auth(self, path: str):
        cfg = OIDC_CONFIG
        if cfg is None or path not in ("/auth/providers", "/auth/login", "/auth/callback"):
            self._error(ApiError(404, "not_found", "no such route"))
            return
        parsed = urlparse(self.path)
        secure = cfg.public_url.startswith("https://")
        query = parse_qs(parsed.query, keep_blank_values=True) if len(parsed.query) <= 4096 else {}
        if path == "/auth/providers":
            self._send(200, "application/json", json.dumps(
                {"ok": True, "providers": [{"id": p.id, "label": p.label} for p in cfg.providers]}).encode(),
                {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
            return
        store = Store(DB_PATH, init=not is_dsn(DB_PATH))
        try:
            if path == "/auth/login":
                given = query.get("provider", [])
                if len(given) != 1:
                    raise ApiError(422, "bad_provider", "give exactly one provider")
                if set(query) - {"provider", "next"} or len(query.get("next", [""])) != 1:
                    raise ApiError(422, "bad_input", "only provider and next are accepted, once each")
                try:
                    url, binder = oidc.begin(store, cfg, given[0], query.get("next", ["/"])[0], CLOCK())
                except oidc.LoginError as err:
                    raise ApiError(err.status, err.code, err.public_message) from None
                self._redirect(url, [oidc.flow_cookie_header(binder, secure)])
                return
            flat = {k: (v[0] if len(v) == 1 else v) for k, v in query.items()}   # a repeated parameter stays a list, which complete() refuses
            try:
                res = oidc.complete(store, cfg, flat, oidc.flow_cookie_value(self.headers.get("Cookie")), CLOCK())
            except oidc.LoginError as err:
                self._login_error_page(err, secure)
                return
            sec, prefix = _cookie_flags()
            self._redirect(res.next, [sessions.cookie_header(res.session_cookie, sec, prefix), oidc.clear_flow_cookie(secure)])
        except ApiError as err:
            self._error(err)
        except Exception:  # noqa: BLE001 - no leaked internals; the cause is in the log
            log.exception("unhandled error on GET %s", parsed.path)
            self._error(ApiError(500, "internal", "internal error"))
        finally:
            store.close()

    def do_POST(self):
        if not self._gate():
            return
        store = None
        try:
            if self.principal.via == "session":
                self._check_cookie_write()
            path = urlparse(self.path).path
            for pattern, fn in ROUTES:
                m = pattern.match(path)
                if m:
                    break
            else:
                raise ApiError(404, "not_found", "no such route")
            need = ROUTE_ROLES.get(fn.__name__, "admin")
            if acl.RANK[need] > acl.RANK.get(self.principal.role, -1):
                raise ApiError(403, "forbidden_role",
                               f"{need} role required; {self.principal.name} is {self.principal.role}")
            if not acl.bypasses(self.principal):   # which show, and may this caller touch it: decided before any of the handler's work
                level = ROUTE_SHOW_LEVEL.get(fn.__name__)
                if level is not None:
                    store = Store(DB_PATH, init=not is_dsn(DB_PATH))
                    _show_gate(store, self.principal, SHOW_RESOLVER[fn.__name__], m.group(1), level)
                elif fn.__name__ not in NOT_SHOW_SCOPED:
                    raise ApiError(403, "forbidden_show", "this route is not classified for show access")
            body = self._read_json()
            actor = _actor(self.headers, body, self.principal)
            _REQUEST.actor, _REQUEST.principal = actor, self.principal   # for the actions that record who (safe mode, stories)
            _CALLER.actor, _CALLER.role = actor, self.principal.role
            if store is None:
                store = Store(DB_PATH, init=not is_dsn(DB_PATH))
            out = fn(store, m.group(1), body, self) if fn in REQUEST_AWARE else fn(store, m.group(1), body)
            headers = out.pop("_headers", None) if isinstance(out, dict) else None
            detail = out.pop("auditDetail", None) if isinstance(out, dict) else None
            status = 200
            if isinstance(out, dict) and out.get("httpStatus") == 202:
                out = dict(out)
                out.pop("httpStatus")
                status = 202
            skip_audit = bool(isinstance(out, dict) and out.pop("auditSkip", False)) or fn.__name__ in HEARTBEAT_ROUTES
            if isinstance(out, dict) and out.get("ok") is True and 200 <= status < 300 and not skip_audit:
                action, kind, target = _audit_fields(path, m.group(1), body, out)
                if kind in ("session", "notification"):
                    target = actor
                store.append_audit(actor, action, kind, target, detail)
            self._json(status, out, headers)
        except ApiError as err:
            self._error(err)
        except Exception:  # never leak internals; the cause is in the server's log
            log.exception("unhandled error on POST %s", urlparse(self.path).path)
            self._error(ApiError(500, "internal", "internal error; nothing further was changed by this handler"))
        finally:
            _REQUEST.actor = _REQUEST.principal = None
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
            _show_gate(STORE, self.principal, "show", show_id, "view")
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
            extra = sorted(set(query) - {"target", "actor", "action", "kind", "from", "to", "before", "limit", "format"})
            if extra:
                raise ApiError(422, "bad_input", f"unknown parameter(s): {', '.join(extra)}")

            def one(name):
                got = query.get(name)
                if got is None:
                    return None
                if len(got) != 1 or not got[0]:
                    raise ApiError(422, "bad_input", f"{name} must be given once and not empty")
                return got[0]
            target = one("target")
            if target is not None and not any(one(n) for n in ("actor", "action", "kind", "from", "to", "before", "limit", "format") if n in query):
                self._json(200, {"ok": True, "rows": STORE.list_audit(target)})   # the original call, for any operator
                return
            self._require_admin()
            raw_limit = one("limit")
            try:
                limit = int(raw_limit) if raw_limit is not None else 100
                rows, nxt = STORE.list_audit_page(actor=one("actor"), action=one("action"), kind=one("kind"), target=target,
                                                  since=one("from"), until=one("to"), before=one("before"), limit=limit)
            except ValueError as exc:
                raise ApiError(422, "bad_input", str(exc)) from None
            fmt = one("format")
            if fmt not in (None, "json", "csv"):
                raise ApiError(422, "bad_input", "format must be json or csv")
            if fmt == "csv":
                self._send(200, "text/csv; charset=utf-8", auditview.render_csv(rows).encode("utf-8"))
                return
            self._json(200, {"ok": True, "rows": rows, "next": nxt})
            return
        if parsed.path == "/api/whoami":
            p = self.principal
            name = p.name if p else LEGACY_ACTOR
            row = STORE.conn.execute("SELECT display_name FROM volt_user WHERE name = ?", (name,)).fetchone() if p and not p.legacy else None
            who = {"ok": True, "name": name, "role": p.role if p else "admin",
                   "legacy": p is None or p.legacy,
                   "groups": list(p.groups) if p else [],
                   "effectiveRole": p.role if p else "admin",
                   "via": p.via if p else "legacy",
                   "displayName": (row["display_name"] if row and row["display_name"] else name)}
            if p is not None and p.via == "session":
                who["csrf"] = p.csrf   # the page keeps it in memory and sends it back as X-Volt-Csrf
            self._json(200, who)
            return
        m = re.fullmatch(rf"/api/shows/{_ID}/access", parsed.path)
        if m:   # who may see, edit or run a show: an operator who can view it may read the list; nobody else learns who is on it
            if acl.RANK.get(self.principal.role, -1) < acl.RANK["operator"]:
                raise ApiError(403, "forbidden_role", "operator role required")
            _show_gate(STORE, self.principal, "show", m.group(1), "view")
            if acl.show_of(STORE, "show", m.group(1)) is None:
                raise ApiError(404, "not_found", f"no such show: {m.group(1)}")
            self._json(200, {"ok": True, "show": m.group(1), "access": acl.list_acl(STORE, m.group(1))})
            return
        if parsed.path == "/api/groups":
            self._require_admin()
            self._json(200, {"ok": True, "groups": [dict(g, claims=acl.claims_of(STORE, g["name"])) for g in acl.list_groups(STORE)]})
            return
        if parsed.path == "/api/users":
            self._require_admin()
            self._json(200, {"ok": True, "users": users.list_users(STORE)})
            return
        if parsed.path == "/api/media-ticket":
            self._issue_media_ticket(parse_qs(parsed.query, keep_blank_values=True))
            return
        if devices_get(self, STORE, parsed.path, parse_qs(parsed.query, keep_blank_values=True)):
            return
        if show_building_get(self, STORE, parsed.path, parse_qs(parsed.query, keep_blank_values=True)):
            return
        if wire_get(self, STORE, parsed.path, parse_qs(parsed.query, keep_blank_values=True)):
            return
        if discuss_get(self, STORE, parsed.path, parse_qs(parsed.query, keep_blank_values=True)):
            return
        if stories_get(self, STORE, parsed.path, parse_qs(parsed.query, keep_blank_values=True)):
            return
        if schema_get(self, STORE, parsed.path, parse_qs(parsed.query, keep_blank_values=True)):
            return
        if search_get(self, STORE, parsed.path, parse_qs(parsed.query, keep_blank_values=True)):
            return
        m = MOS_ROUTE.match(parsed.path)
        if m:  # read-only MOS roCreate export (docs/specs/mos.md)
            _show_gate(STORE, self.principal, "show", m.group(1), "view")
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
        visible = _view_filter(store, self.principal)
        listing = operator_state.shows(store, visible)
        if show_id and not visible(show_id):   # a remembered show this caller can no longer see: the same as one that is gone
            show_id = ""
        if not show_id and listing:
            show_id = listing[-1]["id"]
        rundown = None
        if show_id:
            try:
                rundown = rundown_payload(store, show_id)
            except KeyError:  # a remembered show that no longer exists: fall back to the newest
                show_id = listing[-1]["id"] if listing else None
                rundown = rundown_payload(store, show_id) if show_id else None
        state = operator_state.build(store, rundown, show_id or None, operator_state.room_state(get_room), visible)
        # What a reorder must quote to be conditional on the list the page is showing (building.order_version).
        state["orderVersion"] = building.order_version(store, show_id) if show_id and rundown is not None else None
        # Who is here and who is editing what, for the show on screen (which the caller may view: the filter above already decided that).
        # If these tables cannot be read the page still gets everything else, plus a flag to say so; nothing on the on-air path waits on them.
        state["presence"], state["leases"], state["collab"] = [], [], {"available": True}
        if show_id and rundown is not None:
            try:
                now = CLOCK()
                state["leases"] = leases.live(store, show_id, now)
                state["presence"] = leases.who(store, show_id, now)
            except Exception:  # noqa: BLE001 - a missing or locked table: the poll carries on without it
                log.warning("collaboration state unavailable for the poll", exc_info=True)
                state["presence"], state["leases"], state["collab"] = [], [], {"available": False}
                try:
                    store.conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
        # Unread pings and open threads per row: the bell and the chip. Same rule as above: unreadable tables cost the page only these.
        state["notifications"], state["commentCounts"] = {"unread": 0}, {}
        try:
            if discuss.available(store):
                state["notifications"] = {"unread": discuss.unread_count(store, self.principal.name if self.principal else LEGACY_ACTOR)}
                if show_id and rundown is not None:
                    state["commentCounts"] = discuss.open_counts(store, show_id)
            else:
                state["collab"] = {"available": False}
        except Exception:  # noqa: BLE001
            log.warning("comment state unavailable for the poll", exc_info=True)
            state["notifications"], state["commentCounts"], state["collab"] = {"unread": 0}, {}, {"available": False}
            try:
                store.conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        return state

    def _json(self, status: int, payload: dict, headers=None):
        self._send(status, "application/json", json.dumps(payload).encode(), headers)

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

    def _send(self, status, content_type, body, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
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
    global OIDC_CONFIG
    try:
        OIDC_CONFIG = oidc.load_config(os.environ.get("VOLT_OIDC_CONFIG"))
    except oidc.ConfigError as exc:
        _config_exit(str(exc))
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
