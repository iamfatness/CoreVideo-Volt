"""The `volt` command.

    volt serve    the operator API and session shell        (same as python -m volt.serve)
    volt runner   the job runner                            (same as python -m volt.runner)
    volt clock    the schedule clock                        (same as python -m volt.clock)
    volt backup   back up the database into a new directory     (volt.backup; the same module does the next three)
    volt restore  restore a backup into a database; volt verify checks one; volt verify-storage checks media
    volt run      all three, supervised: restart on crash, clean stop on Ctrl-C or SIGTERM
    volt check    preflight: is this machine ready?
    volt user     per-user tokens: add, list, disable, enable, rotate (operator and admin roles)
    volt migrate  bring the database (or every tenant) to the latest schema as its own step; idempotent
    volt upgrade  volt upgrade plan [--verify]: what an upgrade would do, and whether it can roll one node at a time
    volt tenant   platform commands for a hosted multi-tenant deployment: create, adopt, list, suspend, resume, limit, domain, migrate
    volt watch    enqueue ingest for files sitting still in VOLT_INGEST_ROOTS
    volt mos      volt mos export <show> [--out file]: the rundown as a MOS roCreate message (read-only)
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
    ap.add_argument("--stop-grace", type=float, default=30.0, help="seconds to let children finish before killing them")
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


def _mos(argv: list) -> int:
    from volt import mos
    from volt.store import Store

    ap = argparse.ArgumentParser(prog="volt mos", description="MOS export of a show's rundown (roCreate XML).")
    sub = ap.add_subparsers(dest="action", required=True)
    ex = sub.add_parser("export", help="write the show's roCreate message to stdout or --out")
    ex.add_argument("show", help="show id, or an exact show title")
    ex.add_argument("--db", default=os.environ.get("VOLT_DB"), help="SQLite path or DSN (default: VOLT_DB)")
    ex.add_argument("--out", default=None, help="write the XML to this file instead of stdout")
    ex.add_argument("--cues", action="store_true", help="include the item's cues as namespaced metadata")
    ex.add_argument("--mos-id", default=mos.DEFAULT_MOS_ID, help="mosID of this media object server")
    ex.add_argument("--ncs-id", default=mos.DEFAULT_NCS_ID, help="ncsID of the receiving newsroom system")
    args = ap.parse_args(argv)
    if not args.db:
        ap.error("a database path or DSN is required (--db or VOLT_DB)")
    store = Store(args.db)
    try:
        out = mos.export(store, args.show, include_cues=args.cues, mos_id=args.mos_id, ncs_id=args.ncs_id)
    except mos.MosError as exc:
        print(f"volt mos: {exc.reason}", file=sys.stderr)
        return 1
    finally:
        store.close()
    for warning in out["warnings"]:
        print(f"volt mos: warning: {warning}", file=sys.stderr)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(out["content"])
    else:
        sys.stdout.write(out["content"])
    return 0


def _db_target(args_db):
    from volt import preflight
    target = args_db if args_db is not None else os.environ.get("VOLT_DB")
    return target


def _tenant_registry():
    from volt import tenancy
    return tenancy._registry_from_env()


def _migrate(argv: list) -> int:
    """Run the numbered migrations as a step of their own, before nodes roll. Safe to re-run."""
    from volt import migrations, tenancy
    from volt.store import Store

    ap = argparse.ArgumentParser(prog="volt migrate", description="Bring the database (or every tenant's schema) to the latest migration.")
    ap.add_argument("--db", default=None, help="SQLite path or DSN (default: VOLT_DB)")
    ap.add_argument("--tenants", default=None, help="tenant mode (VOLT_TENANCY=on): all, or slug,slug")
    args = ap.parse_args(argv)
    if os.environ.get("VOLT_TENANCY", "").lower() == "on" or args.tenants:
        try:
            registry = _tenant_registry()
            slugs = None if args.tenants in (None, "all") else [s for s in args.tenants.split(",") if s]
            report = migrations.run_all_tenants(registry, slugs=slugs)
        except tenancy.TenantError as exc:
            print(f"volt: {exc}", file=sys.stderr)
            return 78
        bad = 0
        for slug, res in report.items():
            if res["ok"]:
                print(f"{slug}\tok\tapplied {res['applied'] or 'nothing'}")
            else:
                bad += 1
                print(f"{slug}\tFAILED\t{res['error']}")
        return 1 if bad else 0
    target = _db_target(args.db)
    if not target:
        print("volt: set VOLT_DB or pass --db", file=sys.stderr)
        return 78
    try:
        store = Store(target)
    except migrations.SchemaTooNewError as exc:
        print(f"volt: {exc}", file=sys.stderr)
        return 78
    try:
        applied = list(store.last_migrations) if store.postgres else None
        if applied is None:     # SQLite: compare before and after, since the bootstrap does not return what it applied
            applied = []
    finally:
        store.close()
    print(f"ok\tschema {migrations.known_version()}\tapplied {applied or 'nothing (or already current)'}")
    return 0


def _upgrade(argv: list) -> int:
    """volt upgrade plan: pending migrations, expand or breaking, and whether the upgrade can roll one node at a time."""
    from volt import migrations
    from volt.store import Store

    ap = argparse.ArgumentParser(prog="volt upgrade")
    ap.add_argument("what", choices=["plan"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--verify", action="store_true", help="check that the database is at this build's schema (after the upgrade)")
    args = ap.parse_args(argv)
    target = _db_target(args.db)
    if not target:
        print("volt: set VOLT_DB or pass --db", file=sys.stderr)
        return 78
    from volt import pg
    try:
        if pg.is_dsn(target):
            import psycopg
            with psycopg.connect(target) as c:
                db = migrations.PostgresBackend(c)
                db.ensure_version_table()
                applied, required = db.applied(), db.required_code()
                c.commit()
        else:
            import sqlite3
            if not os.path.exists(target):
                print(f"volt: no database at {target}", file=sys.stderr)
                return 78
            conn = sqlite3.connect(target)
            db = migrations.SqliteBackend(conn)
            db.ensure_version_table()
            applied, required = db.applied(), db.required_code()
            conn.commit()
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"volt: cannot read the database: {exc}", file=sys.stderr)
        return 78
    behind = [m.version for m in migrations.MIGRATIONS if m.version not in applied]
    if args.verify:
        if behind:
            print(f"behind: this build knows schema {migrations.known_version()}; the database lacks {behind}")
            return 1
        print(f"ok: the database is at schema {migrations.known_version()}")
        return 0
    result = migrations.plan(applied, migrations.MIGRATIONS, required=required)
    print(f"database at schema {max(applied) if applied else 0}; this build knows {migrations.known_version()}; required code version {required}")
    for version, breaking in result.pending:
        print(f"  pending {version}: {'BREAKING' if breaking else 'expand (additive)'}")
    if not result.pending:
        print("  nothing pending")
    for reason in result.reasons:
        print(f"  refused: {reason}")
    print("rolling upgrade: " + ("ok" if result.rolling_ok else "NOT ok"))
    return 0 if result.rolling_ok else 2


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
    if command == "user":
        from volt import users
        return users.main(rest)
    if command == "mos":
        return _mos(rest)
    if command == "tenant":
        from volt import tenancy
        return tenancy.main(rest)
    if command in ("migrate", "upgrade"):
        return _migrate(rest) if command == "migrate" else _upgrade(rest)
    if command == "watch":
        from volt import watch
        _bridge_break_signal()
        return watch.main(rest)
    print(f"volt: unknown command {command!r}\n\n{USAGE.strip()}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
