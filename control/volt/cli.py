"""The `volt` command.

    volt serve    the operator API and session shell        (same as python -m volt.serve)
    volt runner   the job runner                            (same as python -m volt.runner)
    volt clock    the schedule clock                        (same as python -m volt.clock)
    volt backup   back up the database into a new directory     (volt.backup; the same module does the next three)
    volt restore  restore a backup into a database; volt verify checks one; volt verify-storage checks media
    volt run      all three, supervised: restart on crash, clean stop on Ctrl-C or SIGTERM
    volt check    preflight: is this machine ready?
    volt watch    enqueue ingest for files sitting still in VOLT_INGEST_ROOTS
    volt version  the build

`serve`, `runner` and `clock` pass every argument through, so `volt runner --once` and
`volt serve --port 9000` behave exactly like the module commands.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

import volt
from volt import envfile

USAGE = __doc__
BACKUP_COMMANDS = {"backup": "backup", "restore": "restore", "verify": "verify", "verify-storage": "verify-storage"}


def _bridge_break_signal() -> None:
    """Windows sends Ctrl-Break (SIGBREAK) to ask a process in its own group to stop. Treat it as Ctrl-C so
    the runner's and the clock's own SIGINT handlers finish the job in flight and exit cleanly."""
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: signal.raise_signal(signal.SIGINT))


def _version_text() -> str:
    return f"volt {volt.__version__} (python {sys.version.split()[0]})"


def _environment(env_file) -> dict:
    """The process environment plus the settings file (which never overrides a variable that is already set)."""
    env = dict(os.environ)
    if env_file:
        env.update(envfile.load(env_file, env))
    return env


def _check(argv: list) -> int:
    from volt import preflight

    ap = argparse.ArgumentParser(prog="volt check", description="Preflight: python, ffmpeg, database, storage, ports.")
    ap.add_argument("--db", default=None, help="SQLite path or DSN (default: VOLT_DB)")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--env-file", default=None, help="KEY=VALUE file of settings, as `volt run --env-file`")
    args = ap.parse_args(argv)
    try:
        env = _environment(args.env_file)
        results = preflight.run_checks(env=env, db=args.db, host=args.host, port=args.port)
    except (ValueError, envfile.EnvFileError) as exc:  # a malformed number in the environment
        print(f"volt: {exc}", file=sys.stderr)
        return 1
    if args.json:
        import json
        print(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        print(preflight.render(results))
        failed = sum(1 for r in results if r.status == preflight.FAIL)
        warned = sum(1 for r in results if r.status == preflight.WARN)
        print(f"\n{failed} failed, {warned} warning(s)")
    return preflight.exit_code(results)


def _run(argv: list) -> int:
    from volt import logs, supervisor

    ap = argparse.ArgumentParser(prog="volt run", description="Run serve, runner and clock together, supervised.")
    ap.add_argument("--db", default=None, help="SQLite path or DSN for every process (default: VOLT_DB)")
    ap.add_argument("--host", default=None, help="serve: address to bind (or VOLT_HOST)")
    ap.add_argument("--port", type=int, default=None, help="serve: port (or VOLT_PORT)")
    ap.add_argument("--log-dir", default=None, help="each process writes <name>.log here (or VOLT_LOG_DIR)")
    ap.add_argument("--access-log", action="store_true", help="serve: log every request")
    ap.add_argument("--only", default=None, help="comma list of serve,runner,clock (default: all that are configured)")
    ap.add_argument("--backoff-max", type=float, default=60.0, help="longest wait between restarts, seconds")
    ap.add_argument("--stop-grace", type=float, default=20.0, help="seconds to let children finish before killing them")
    ap.add_argument("--env-file", default=None, help="KEY=VALUE file of settings; variables already set win")
    ap.add_argument("--no-preflight", action="store_true", help="skip the quick configuration check before starting")
    args = ap.parse_args(argv)

    try:
        env = _environment(args.env_file)
    except envfile.EnvFileError as exc:
        print(f"volt: {exc}", file=sys.stderr)
        return supervisor.EX_CONFIG
    if args.db:
        env["VOLT_DB"] = args.db
    if args.log_dir:
        env["VOLT_LOG_DIR"] = args.log_dir
    if not env.get("VOLT_DB"):
        from volt import preflight
        env["VOLT_DB"] = preflight.default_db()
    try:
        logs.setup("run", log_dir=args.log_dir, env=env)
    except (ValueError, OSError) as exc:
        print(f"volt: {exc}", file=sys.stderr)
        return supervisor.EX_CONFIG
    log = logging.getLogger("volt.run")

    if not args.no_preflight:
        from volt import preflight
        results = [r for r in preflight.run_checks(env=env, db=env["VOLT_DB"], host=args.host, port=args.port)
                   if r.name in ("python", "database", "install layout", "access", "disk floor")
                   or r.name.startswith("storage")]
        for r in results:
            if r.status != preflight.OK:
                log.log(logging.ERROR if r.status == preflight.FAIL else logging.WARNING,
                        "preflight %s %s: %s", r.status, r.name, logs.redact(r.detail))
        if preflight.exit_code(results):
            log.error("not starting: fix the failed check(s) above (volt check shows all of them)")
            return supervisor.EX_CONFIG

    children = supervisor.build_children(args, env)
    if not children:
        log.error("nothing to run")
        return supervisor.EX_CONFIG
    sup = supervisor.Supervisor(children, backoff_max=args.backoff_max, grace=args.stop_grace)
    sup.install_signal_handlers()
    log.info("volt %s running: %s", volt.__version__, ", ".join(c.name for c in children))
    code = sup.run()
    log.info("volt run ended code=%d", code)
    return code


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE.strip())
        return 0
    command, rest = argv[0], argv[1:]
    if command in ("version", "--version", "-V"):
        print(_version_text())
        return 0
    if command == "check":
        return _check(rest)
    if command == "run":
        return _run(rest)
    if command in BACKUP_COMMANDS:  # volt backup <db> <dir>, volt restore <dir> [db], volt verify <dir>, volt verify-storage
        from volt import backup
        return backup.main([BACKUP_COMMANDS[command], *rest])
    if command == "serve":
        from volt import serve
        _bridge_break_signal()
        return serve.main(rest) or 0
    if command == "runner":
        from volt import runner
        _bridge_break_signal()
        return runner.main(rest)
    if command == "clock":
        from volt import clock
        _bridge_break_signal()
        return clock.main(rest)
    if command == "watch":
        from volt import watch
        _bridge_break_signal()
        return watch.main(rest)
    print(f"volt: unknown command {command!r}\n\n{USAGE.strip()}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
