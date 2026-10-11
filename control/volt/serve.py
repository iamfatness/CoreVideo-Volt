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

from volt import operator_state
from volt.live import finish_recording, start_recording
from volt import pg
from volt.pg import is_dsn
from volt.playout import HeldError, PlayoutError, advance, hold, hold_state, release, skip
from volt.publish import publish_files
from volt.record import RecordError
from volt.rooms import CoreVideoProRoom, LiveError
from volt.rundown import attach, kill, reorder
from volt.session import list_assets, rundown, seek_hit
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
    def __init__(self, status: int, code: str, reason: str, **extra):
        super().__init__(reason)
        self.status, self.code, self.reason, self.extra = status, code, reason, extra


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
         "asRunIn": row.as_run_in, "asRunOut": row.as_run_out}
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
    _unknown_keys(body, {"items"})
    _show_exists(store, show_id)
    ids = body.get("items")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ApiError(422, "bad_input", "items must be a list of item ids")
    with _SEQ:
        have = {r["id"] for r in store.conn.execute("SELECT id FROM item WHERE show_id = ?", (show_id,))}
        if len(set(ids)) != len(ids) or set(ids) != have:
            raise ApiError(422, "bad_input", "items must list every item of the show exactly once")
        reorder(store, show_id, ids)
        return {"ok": True, "rundown": rundown_payload(store, show_id)}


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
        self._send(404, "text/plain", b"not found")

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
                         "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
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
        return operator_state.build(store, rundown, show_id or None, operator_state.room_state(get_room))

    def _json(self, status: int, payload: dict):
        self._send(status, "application/json", json.dumps(payload).encode())

    def _error(self, err: ApiError):
        payload = {"ok": False, "error": {"code": err.code, "reason": err.reason}}
        payload.update(err.extra)
        self.send_response(err.status)
        if err.status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
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
