"""Run several processes and keep them running: `volt run`.

Each child is restarted when it exits for any reason except a stop request or exit code 78 (EX_CONFIG: the
configuration is wrong, restarting cannot help). Restarts back off exponentially (1 s, 2 s, 4 s ... up to
60 s) and the backoff resets once a child has stayed up for `stable_after` seconds. A stop (Ctrl-C,
SIGTERM, Ctrl-Break on Windows) asks every child to end cleanly, waits `grace` seconds, then kills what is left.

Children are plain `python -m volt <command>` processes, so the supervisor adds nothing the single commands
do not already do; it only starts, watches and stops them.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from volt import roomreg

log = logging.getLogger("volt.run")

EX_CONFIG = 78
IS_WINDOWS = os.name == "nt"


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill a child we started and anything it started. On Windows a virtualenv's python.exe is a launcher that
    runs the real interpreter as its own child, so killing only the launcher is not enough."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
    if proc.poll() is None:
        proc.kill()


@dataclass
class Child:
    name: str
    argv: list
    env: Optional[dict] = None
    proc: Optional[subprocess.Popen] = None
    started_at: float = 0.0
    restarts: int = 0
    next_start: float = 0.0
    backoff: float = 0.0
    fatal: bool = False   # exited with EX_CONFIG: never restarted


@dataclass
class Supervisor:
    children: list
    backoff_initial: float = 1.0
    backoff_max: float = 60.0
    stable_after: float = 60.0
    grace: float = 20.0
    tick: float = 0.2
    clock: Callable[[], float] = time.monotonic
    _stop: threading.Event = field(default_factory=threading.Event)
    exit_code: int = 0

    def stop(self) -> None:
        self._stop.set()

    # -- children
    def _spawn(self, child: Child) -> None:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
        child.proc = subprocess.Popen(child.argv, env=child.env, creationflags=flags)
        child.started_at = self.clock()
        log.info("started child=%s pid=%d restarts=%d", child.name, child.proc.pid, child.restarts)

    def _reap(self, child: Child) -> None:
        code = child.proc.poll()
        if code is None:
            return
        child.proc = None
        ran = self.clock() - child.started_at
        if code == EX_CONFIG:
            child.fatal = True
            log.error("child=%s exited with code %d: configuration error, not restarting", child.name, code)
            return
        if ran >= self.stable_after:
            child.backoff = 0.0
        child.backoff = self.backoff_initial if child.backoff == 0 else min(child.backoff * 2, self.backoff_max)
        child.restarts += 1
        child.next_start = self.clock() + child.backoff
        log.warning("child=%s exited code=%s after %.1fs; restarting in %.1fs", child.name, code, ran, child.backoff)

    # -- main loop
    def run(self) -> int:
        for child in self.children:
            self._spawn(child)
        try:
            while not self._stop.is_set():
                for child in self.children:
                    if child.proc is not None:
                        self._reap(child)
                    elif not child.fatal and self.clock() >= child.next_start:
                        self._spawn(child)
                if any(c.fatal for c in self.children):
                    self.exit_code = EX_CONFIG
                    log.error("stopping: a child has a configuration error")
                    break
                self._stop.wait(self.tick)
        finally:
            self.shutdown()
        return self.exit_code

    def shutdown(self) -> None:
        live = [c for c in self.children if c.proc is not None and c.proc.poll() is None]
        for child in live:
            self._ask_to_stop(child)
        deadline = time.monotonic() + self.grace
        for child in live:
            try:
                child.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
                log.info("stopped child=%s code=%s", child.name, child.proc.returncode)
            except subprocess.TimeoutExpired:
                log.warning("child=%s did not stop in %.0fs; killing it", child.name, self.grace)
                kill_tree(child.proc)
                child.proc.wait(timeout=10)

    @staticmethod
    def _ask_to_stop(child: Child) -> None:
        try:
            if IS_WINDOWS:
                child.proc.send_signal(signal.CTRL_BREAK_EVENT)  # the child is its own process group
            else:
                child.proc.send_signal(signal.SIGTERM)
        except (OSError, ValueError):
            child.proc.terminate()

    # -- signals
    def install_signal_handlers(self) -> None:
        names = ["SIGINT", "SIGTERM"] + (["SIGBREAK"] if IS_WINDOWS else [])
        for name in names:
            signal.signal(getattr(signal, name), lambda *_: self.stop())


def build_children(args, env: dict) -> list:
    """The serve, runner and clock child processes for `volt run`, sharing one environment.

    The database target travels in VOLT_DB, never on a command line, so a DSN password does not show in a
    process listing."""
    base = [sys.executable, "-m", "volt"]
    children = []
    serve = base + ["serve"]
    if args.host:
        serve += ["--host", args.host]
    if args.port is not None:
        serve += ["--port", str(args.port)]
    if args.access_log:
        serve.append("--access-log")
    wanted = set(args.only.split(",")) if args.only else {"serve", "runner", "clock"}
    if "serve" in wanted:
        children.append(Child("serve", serve, env))
    if "runner" in wanted:
        children.append(Child("runner", base + ["runner"], env))
    if "clock" in wanted:
        if roomreg.configured(env):
            children.append(Child("clock", base + ["clock"], env))
        else:
            log.warning("clock not started: no room is configured (set VOLT_ROOM_URL)")
    return children
