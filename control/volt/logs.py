"""Logging for every Volt process: one line per event, to stderr and (when asked) a rotating file.

    from volt import logs
    logs.setup("serve")          # reads VOLT_LOG_* from the environment

Lines are logfmt-style text by default (`ts=... level=INFO logger=volt.serve msg="..."`) or JSON
(VOLT_LOG_FORMAT=json). Every line passes through `redact` on its way out, so a bearer token, a database
password, a cloud signature or an access key that reaches a message or a traceback is blanked. Redaction is a
safety net, not a licence to log secrets: call sites still avoid passing them in.

Each process writes its own file (`<log dir>/<component>.log`), never a shared one: rotating a file another
process holds open fails on Windows.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

COMPONENT_NAMES = ("serve", "runner", "clock", "run")
MASK = "***"

# Secret-looking pairs. The name must END in a secret word, so `token_type=x` is left alone and `VOLT_TOKEN=x`,
# `api_key=x`, `AccountKey=x`, `sig=x`, `X-Amz-Signature=x` are caught. `name=value` is always a pair; `name: value`
# only counts for a quoted name (JSON or a dict repr: `"token": "x"`) or the Authorization header, because plain
# prose such as "no token: loopback only" is not a secret.
_NAME = r"(?:[\w.\-]*(?:token|passw(?:or)?d|secret|key|credential|signature|authorization))|\bsig"
_VALUE = r"([^\s&;,\"']+)"
_PAIR = re.compile(rf"(?i)({_NAME})(\s*=\s*[\"']?)(?:bearer\s+|basic\s+)?{_VALUE}")
_QUOTED_PAIR = re.compile(rf"(?i)((?:{_NAME})[\"']\s*:\s*[\"']?)(?:bearer\s+|basic\s+)?{_VALUE}")
_AUTH_HEADER = re.compile(rf"(?i)(\bauthorization\s*:\s*)(?:bearer\s+|basic\s+)?{_VALUE}")
_BEARER = re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=\-]{6,}")
# user:password@host in any URL or DSN (postgres://, https://, ...).
_URL_PASSWORD = re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://[^/\s:@]*:)[^@\s/]+(@)")
# AWS access key ids.
_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")


def _known_secrets(env=None) -> list:
    """Literal secret values the process was given, so they are blanked even in unexpected places."""
    env = os.environ if env is None else env
    found = []
    for name in ("VOLT_TOKEN", "VOLT_ROOM_TOKEN"):
        value = env.get(name)
        if value and len(value) >= 6:
            found.append(value)
    match = re.match(r"(?i)[a-z][a-z0-9+.\-]*://[^/\s:@]*:([^@\s/]+)@", env.get("VOLT_DB") or "")
    if match and len(match.group(1)) >= 4:
        found.append(match.group(1))
    return found


def redact(text: str, env=None) -> str:
    """Blank anything in `text` that looks like a credential. Idempotent."""
    if not text:
        return text
    for secret in _known_secrets(env):
        text = text.replace(secret, MASK)
    text = _URL_PASSWORD.sub(lambda m: m.group(1) + MASK + m.group(2), text)
    text = _PAIR.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
    text = _QUOTED_PAIR.sub(lambda m: m.group(1) + MASK, text)
    text = _AUTH_HEADER.sub(lambda m: m.group(1) + MASK, text)
    text = _BEARER.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
    return _AWS_KEY.sub(MASK, text)


def redact_dsn(dsn: str) -> str:
    """A database target that is safe to show: the password of a postgres:// DSN is blanked."""
    return redact(dsn)


def _iso(created: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(created)) + f".{int(created * 1000) % 1000:03d}Z"


def _quote(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


class Formatter(logging.Formatter):
    def __init__(self, fmt: str = "text", component: str = ""):
        super().__init__()
        self.kind = fmt
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        message = redact(record.getMessage())
        exc = ""
        if record.exc_info:
            exc = redact("".join(traceback.format_exception(*record.exc_info)).rstrip())
        if self.kind == "json":
            payload = {"ts": _iso(record.created), "level": record.levelname, "logger": record.name,
                       "component": self.component, "pid": record.process, "msg": message}
            if exc:
                payload["exc"] = exc
            return json.dumps(payload, ensure_ascii=False)
        line = f"ts={_iso(record.created)} level={record.levelname} logger={record.name} msg={_quote(message)}"
        return line + ("\n" + exc if exc else "")


_MARK = "_volt_handler"


def _file_name(component: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", component) + ".log"


def setup(component: str, log_dir=None, level: Optional[str] = None, fmt: Optional[str] = None,
          env=None, stream=None) -> Optional[Path]:
    """Install the stderr handler and, with a log directory, the rotating file handler on the `volt` logger.

    Settings fall back to VOLT_LOG_DIR, VOLT_LOG_LEVEL (default INFO), VOLT_LOG_FORMAT (text|json),
    VOLT_LOG_MAX_MB (default 10) and VOLT_LOG_BACKUPS (default 5). Safe to call twice: the first
    handlers are replaced. Returns the log file path, or None when only stderr is used."""
    env = os.environ if env is None else env
    log_dir = log_dir or env.get("VOLT_LOG_DIR") or None
    level_name = (level or env.get("VOLT_LOG_LEVEL") or "INFO").upper()
    fmt = (fmt or env.get("VOLT_LOG_FORMAT") or "text").lower()
    if fmt not in ("text", "json"):
        raise ValueError(f"VOLT_LOG_FORMAT must be text or json, not {fmt!r}")
    numeric = logging.getLevelName(level_name)
    if not isinstance(numeric, int):
        raise ValueError(f"VOLT_LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR, not {level_name!r}")
    root = logging.getLogger("volt")
    for handler in list(root.handlers):
        if getattr(handler, _MARK, False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(numeric)
    root.propagate = False
    formatter = Formatter(fmt, component)
    handlers = [logging.StreamHandler(stream or sys.stderr)]
    path = None
    if log_dir:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / _file_name(component)
        max_mb = float(env.get("VOLT_LOG_MAX_MB") or 10)
        backups = int(env.get("VOLT_LOG_BACKUPS") or 5)
        handlers.append(logging.handlers.RotatingFileHandler(
            path, maxBytes=int(max_mb * 1024 * 1024), backupCount=backups, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _MARK, True)
        root.addHandler(handler)
    _install_excepthook()
    return path


def _install_excepthook() -> None:
    if getattr(sys.excepthook, "_volt", False):
        return
    previous = sys.excepthook

    def hook(kind, value, tb):
        if issubclass(kind, KeyboardInterrupt):
            return previous(kind, value, tb)
        logging.getLogger("volt").critical("uncaught exception", exc_info=(kind, value, tb))

    hook._volt = True  # type: ignore[attr-defined]
    sys.excepthook = hook


def access_log_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get("VOLT_ACCESS_LOG") or "").strip().lower() in ("1", "true", "yes", "on")
