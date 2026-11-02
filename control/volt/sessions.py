"""Server-side sessions behind an HttpOnly cookie (docs/superpowers/specs/2026-11-01-collaboration-identity-design.md).

A session is a row, not a role: the cookie value is 32 random bytes, stored only as a sha256 hash (a database read does not yield a usable
cookie), and the role, groups and enabled state of the user are read from their own tables on every request, so disabling a user or
changing a role takes effect on the next request. Time is always passed in (`now`, epoch seconds); nothing here reads the clock.

Two limits apply, both on the server: an idle limit (`VOLT_SESSION_IDLE`, default 8 h) and an absolute limit (`VOLT_SESSION_MAX`, default
14 h, one shift). `last_seen_at` is written at most once per minute, so a 2-second poll is not a write per request.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

log = logging.getLogger("volt.sessions")

COOKIE = "volt_session"
HOST_COOKIE = "__Host-volt_session"
DEFAULT_IDLE = 8 * 3600
DEFAULT_MAX = 14 * 3600
TOUCH_EVERY = 60
_MAX_COOKIE = 256          # a real value is 43 characters; anything longer is not looked up
_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class SessionInfo:
    id_hash: str
    user_name: str
    provider: str
    csrf: str
    created_at: float
    last_seen_at: float
    expires_at: float


def _positive_int(env, name: str, default: int) -> int:
    raw = (env if env is not None else os.environ).get(name, "")
    return int(raw) if isinstance(raw, str) and raw.isdigit() and int(raw) > 0 else default


def idle_seconds(env=None) -> int:
    return _positive_int(env, "VOLT_SESSION_IDLE", DEFAULT_IDLE)


def max_seconds(env=None) -> int:
    return _positive_int(env, "VOLT_SESSION_MAX", DEFAULT_MAX)


def hash_cookie(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _epoch(value) -> float:
    """A stored stamp back to epoch seconds: SQLite keeps the text, Postgres hands back a datetime."""
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


def create(store, user_name: str, provider: str, now: float, env=None) -> tuple[str, str]:
    """Mint a session for `user_name`. Returns (cookie value, csrf value). The id is always new; a client never supplies one."""
    _prune(store, now, env)
    value, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    store.conn.execute(
        "INSERT INTO session (id_hash, user_name, provider, csrf, created_at, last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (hash_cookie(value), user_name, provider, csrf, _stamp(now), _stamp(now), _stamp(now + max_seconds(env))))
    store.conn.commit()
    return value, csrf


def _prune(store, now: float, env=None) -> None:
    """Drop rows past either limit, so a table of old logins does not grow without end."""
    idle = idle_seconds(env)
    dead = [r["id_hash"] for r in store.conn.execute("SELECT id_hash, last_seen_at, expires_at FROM session").fetchall()
            if now >= _epoch(r["expires_at"]) or now - _epoch(r["last_seen_at"]) >= idle]
    for id_hash in dead:
        store.conn.execute("DELETE FROM session WHERE id_hash = ?", (id_hash,))
    if dead:
        store.conn.commit()


def resolve(store, cookie_value, now: float, env=None) -> Optional[SessionInfo]:
    """The live session this cookie value names, else None. Applies the idle and absolute limits (and removes an expired row)."""
    if not isinstance(cookie_value, str) or not cookie_value or len(cookie_value) > _MAX_COOKIE:
        return None
    id_hash = hash_cookie(cookie_value)
    row = store.conn.execute(
        "SELECT id_hash, user_name, provider, csrf, created_at, last_seen_at, expires_at FROM session WHERE id_hash = ?",
        (id_hash,)).fetchone()
    if row is None:
        return None
    last, expires = _epoch(row["last_seen_at"]), _epoch(row["expires_at"])
    if now >= expires or now - last >= idle_seconds(env):
        end(store, id_hash)
        return None
    if now - last >= TOUCH_EVERY:
        try:
            store.conn.execute("UPDATE session SET last_seen_at = ? WHERE id_hash = ?", (_stamp(now), id_hash))
            store.conn.commit()
            last = now
        except Exception:  # noqa: BLE001 - a failed touch must not fail the request; the next one tries again
            log.warning("could not record session activity")
            try:
                store.conn.rollback()
            except Exception:  # noqa: BLE001
                pass
    return SessionInfo(id_hash, row["user_name"], row["provider"], row["csrf"], _epoch(row["created_at"]), last, expires)


def end(store, id_hash: str) -> None:
    store.conn.execute("DELETE FROM session WHERE id_hash = ?", (id_hash,))
    store.conn.commit()


def end_for_user(store, name: str) -> int:
    """End every session of one user. Returns how many there were."""
    count = store.conn.execute("SELECT COUNT(*) AS n FROM session WHERE user_name = ?", (name,)).fetchone()["n"]
    if count:
        store.conn.execute("DELETE FROM session WHERE user_name = ?", (name,))
        store.conn.commit()
    return count


# ---- the cookie ----------------------------------------------------------

def cookie_name(host_prefix: bool) -> str:
    return HOST_COOKIE if host_prefix else COOKIE


def cookie_header(value: str, secure: bool, host_prefix: bool) -> str:
    """The Set-Cookie text. HttpOnly, SameSite=Lax (Strict breaks the provider's redirect back), Path=/, never a Domain. The `__Host-`
    prefix is only valid with Secure, so it forces it."""
    parts = [f"{cookie_name(host_prefix)}={value}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if secure or host_prefix:
        parts.append("Secure")
    return "; ".join(parts)


def clear_header(secure: bool, host_prefix: bool) -> str:
    return cookie_header("", secure, host_prefix) + "; Max-Age=0"


_COOKIE_VALUE = re.compile(r"^[A-Za-z0-9_-]+$")


def cookie_value(header, host_prefix: bool) -> str:
    """The session cookie value out of a Cookie header, or ''. Under https only the `__Host-` name is trusted, and under http only the plain one."""
    if not header:
        return ""
    want = cookie_name(host_prefix)
    for piece in header.split(";"):
        name, sep, value = piece.strip().partition("=")
        if sep and name == want:
            return value if _COOKIE_VALUE.fullmatch(value) else ""
    return ""


# ---- the public URL and Origin ---------------------------------------------

def origin_of(value) -> Optional[str]:
    """scheme://host[:port] in lower case with a default port left out, or None for anything that is not an http(s) origin."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parts = urlsplit(value.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS or not host:
        return None
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != _DEFAULT_PORTS[parts.scheme] else "")


def public_origin(env=None) -> Optional[str]:
    """The origin of VOLT_PUBLIC_URL, or None when it is unset or not a usable http(s) URL."""
    url = (env if env is not None else os.environ).get("VOLT_PUBLIC_URL", "")
    origin = origin_of(url)
    if url and origin is None:
        log.warning("VOLT_PUBLIC_URL is not an http(s) URL; cookie sessions are limited to loopback")
    return origin


def is_https(env=None) -> bool:
    origin = public_origin(env)
    return bool(origin and origin.startswith("https://"))
