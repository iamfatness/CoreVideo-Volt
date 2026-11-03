"""Tenancy: several organisations in one hosted deployment, isolated by Postgres schema.

A tenant is a `TenantRef(slug, dsn, schema)`. Almost every tenant shares the cluster DSN and has a schema of its own; one that must be
isolated further can point at its own database (`dsn`). The registry lives in a control schema (`volt_control` by default) with its
own numbered migrations. Tenancy is Postgres only and off by default: installed single-tenant use never touches this module.

The tenant is found from the request's Host header, before authentication (volt/serve.py); it is never taken from a body or a token.

Isolation here is application-level: one pool and one database role serve every tenant, and a connection is pointed at a tenant's
schema with `search_path` on checkout and reset on return (volt/pg.py). A missing table fails loudly instead of reading another
tenant's rows. It is not a defence against SQL injection that names another schema; per-tenant roles would be (not done).

Platform actions (create, adopt, suspend, resume, limit, domain) are command-line only, and each appends to `control_audit` with the
operating-system user and a reason. Nothing here is on the take / go / hold / release / skip / kill path."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from volt import migrations, pg

SLUG = re.compile(r"^[a-z][a-z0-9-]{1,38}$")
RESERVED = frozenset({"public", "volt-control", "admin", "administrator", "www", "api", "root", "system", "default", "template",
                      "pg-catalog", "pg-toast", "information-schema", "postgres", "volt", "platform", "control"})
LIMIT_NAMES = ("max_assets", "max_storage_bytes", "max_jobs_per_day", "max_running_jobs")
# Non-secret settings a tenant may carry in the registry. A secret is never stored there: the registry names an environment variable.
SETTING_NAMES = ("room_url", "room_token_env")
DEFAULT_CONTROL_SCHEMA = "volt_control"
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_HOST = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


class TenantError(Exception):
    """A platform action was refused. The message says why and is safe to print."""


@dataclass(frozen=True)
class TenantRef:
    slug: str
    dsn: str
    schema: str
    state: str = "active"

    def __repr__(self) -> str:      # a DSN carries a password: never let it reach a log through a repr
        return f"TenantRef(slug={self.slug!r}, schema={self.schema!r}, state={self.state!r})"


def check_slug(slug) -> str:
    if not isinstance(slug, str) or not SLUG.match(slug):
        raise TenantError("a slug is 2 to 39 characters: a lowercase letter, then lowercase letters, digits and hyphens")
    if slug in RESERVED:
        raise TenantError(f"{slug!r} is a reserved name")
    return slug


def schema_for(slug: str) -> str:
    return "t_" + slug.replace("-", "_")


def check_schema(name) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise TenantError("a schema name is lowercase letters, digits and underscores, starting with a letter or underscore")
    if name.startswith("pg_") or name == "information_schema":
        raise TenantError(f"{name!r} is a system schema")
    return name


def normalise_host(value) -> str:
    """Lowercase host without port or trailing dot. Raises TenantError for something that is not a host name."""
    host = (value or "").strip().lower()
    if host.startswith("["):        # an IPv6 literal is never a tenant domain
        raise TenantError("not a host name")
    host = host.rsplit(":", 1)[0] if ":" in host else host
    host = host.rstrip(".")
    if not _HOST.match(host):
        raise TenantError("not a host name")
    return host


# ---- control schema -------------------------------------------------------------------------------

@dataclass(frozen=True)
class ControlMigration:
    version: int
    name: str
    statements: tuple


CONTROL_MIGRATIONS = (
    ControlMigration(1, "registry tables", (
        "CREATE TABLE IF NOT EXISTS tenant (slug TEXT PRIMARY KEY, state TEXT NOT NULL, dsn TEXT NOT NULL, schema TEXT NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL, suspended_at TIMESTAMPTZ)",
        "CREATE TABLE IF NOT EXISTS tenant_domain (host TEXT PRIMARY KEY, slug TEXT NOT NULL REFERENCES tenant(slug))",
        "CREATE TABLE IF NOT EXISTS tenant_limit (slug TEXT NOT NULL REFERENCES tenant(slug), name TEXT NOT NULL, value BIGINT NOT NULL, "
        "PRIMARY KEY (slug, name))",
        "CREATE TABLE IF NOT EXISTS tenant_setting (slug TEXT NOT NULL REFERENCES tenant(slug), name TEXT NOT NULL, value TEXT NOT NULL, "
        "PRIMARY KEY (slug, name))",
        "CREATE TABLE IF NOT EXISTS registry_epoch (id INTEGER PRIMARY KEY, epoch BIGINT NOT NULL)",
        "INSERT INTO registry_epoch (id, epoch) VALUES (1, 0) ON CONFLICT (id) DO NOTHING",
        "CREATE TABLE IF NOT EXISTS control_audit (id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL, operator TEXT NOT NULL, "
        "action TEXT NOT NULL, slug TEXT, detail TEXT)",
    )),
)
CONTROL_LATEST = CONTROL_MIGRATIONS[-1].version


def _q(ident: str) -> str:
    check_schema(ident)
    return f'"{ident}"'


class Registry:
    """The control schema's tenants, domains, limits and epoch. Every method opens its own short connection."""

    def __init__(self, control_dsn: str, control_schema: str = DEFAULT_CONTROL_SCHEMA):
        if not pg.is_dsn(control_dsn or ""):
            raise TenantError("tenancy needs a Postgres DSN for the control schema (VOLT_CONTROL_DSN)")
        self.dsn = control_dsn
        self.schema = check_schema(control_schema)
        self._ready = False

    # -- connections
    @contextmanager
    def _cx(self, bootstrap: bool = True):
        if bootstrap and not self._ready:
            self._bootstrap()
        conn = pg.PgConnection(self.dsn, schema=self.schema)
        try:
            yield conn
        finally:
            conn.close()

    def _bootstrap(self) -> None:
        """Create the control schema and apply CONTROL_MIGRATIONS once, under an advisory lock. Refuses a newer control schema."""
        import psycopg
        with psycopg.connect(self.dsn) as c:
            with c.transaction():
                c.execute("SELECT pg_advisory_xact_lock(%s)", (0x566F6C75,))
                c.execute(f"CREATE SCHEMA IF NOT EXISTS {_q(self.schema)}")
                c.execute(f"SET LOCAL search_path TO {_q(self.schema)}")
                c.execute("CREATE TABLE IF NOT EXISTS control_schema_version "
                          "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL)")
                have = {r[0] for r in c.execute("SELECT version FROM control_schema_version").fetchall()}
                if have and max(have) > CONTROL_LATEST:
                    raise migrations.SchemaTooNewError(max(have), CONTROL_LATEST)
                for m in CONTROL_MIGRATIONS:
                    if m.version in have:
                        continue
                    for stmt in m.statements:
                        c.execute(stmt)
                    c.execute("INSERT INTO control_schema_version (version, name, applied_at) VALUES (%s, %s, now())", (m.version, m.name))
        self._ready = True

    def _audit(self, conn, operator: Optional[str], action: str, slug: Optional[str], detail: str = "") -> None:
        conn.execute("INSERT INTO control_audit (at, operator, action, slug, detail) VALUES (now(), ?, ?, ?, ?)",
                     (operator or _operator(), action, slug, detail or None))

    def _bump(self, conn) -> None:
        conn.execute("UPDATE registry_epoch SET epoch = epoch + 1 WHERE id = 1")

    @staticmethod
    def _ref(row) -> TenantRef:
        return TenantRef(row["slug"], row["dsn"], row["schema"], row["state"])

    # -- reading
    def get(self, slug: str) -> Optional[TenantRef]:
        with self._cx() as c:
            row = c.execute("SELECT slug, state, dsn, schema FROM tenant WHERE slug = ?", (slug,)).fetchone()
        return self._ref(row) if row else None

    def by_host(self, host) -> Optional[TenantRef]:
        """The tenant whose domain this Host header names (suspended ones included, so the caller can answer 403), or None."""
        try:
            name = normalise_host(host)
        except TenantError:
            return None
        with self._cx() as c:
            row = c.execute("SELECT t.slug, t.state, t.dsn, t.schema FROM tenant_domain d JOIN tenant t ON t.slug = d.slug "
                            "WHERE d.host = ?", (name,)).fetchone()
        return self._ref(row) if row else None

    def all(self) -> list:
        with self._cx() as c:
            rows = c.execute("SELECT slug, state, dsn, schema FROM tenant ORDER BY slug").fetchall()
        return [self._ref(r) for r in rows]

    def active(self) -> list:
        return [t for t in self.all() if t.state == "active"]

    def domains(self, slug: str) -> list:
        with self._cx() as c:
            return [r["host"] for r in c.execute("SELECT host FROM tenant_domain WHERE slug = ? ORDER BY host", (slug,)).fetchall()]

    def limits(self, slug: str) -> dict:
        with self._cx() as c:
            return {r["name"]: int(r["value"]) for r in c.execute("SELECT name, value FROM tenant_limit WHERE slug = ?", (slug,)).fetchall()}

    def settings(self, slug: str) -> dict:
        with self._cx() as c:
            return {r["name"]: r["value"] for r in c.execute("SELECT name, value FROM tenant_setting WHERE slug = ?", (slug,)).fetchall()}

    def epoch(self) -> int:
        with self._cx() as c:
            return int(c.execute("SELECT epoch FROM registry_epoch WHERE id = 1").fetchone()["epoch"])

    # -- platform actions
    def create(self, slug: str, domain: str, dsn: Optional[str] = None, operator: Optional[str] = None, reason: str = "") -> TenantRef:
        """Make a tenant: its schema, every numbered migration in it, a registry row and a domain. All or nothing as far as the registry
        goes: if the schema cannot be made or migrated the registry is left as it was (an empty schema may remain and is reused)."""
        check_slug(slug)
        host = normalise_host(domain)
        target = dsn or self.dsn
        if not pg.is_dsn(target):
            raise TenantError("a tenant database must be a Postgres DSN")
        schema = schema_for(slug)
        with self._cx() as c:
            if c.execute("SELECT 1 FROM tenant WHERE slug = ?", (slug,)).fetchone():
                raise TenantError(f"tenant {slug} already exists")
            if c.execute("SELECT 1 FROM tenant_domain WHERE host = ?", (host,)).fetchone():
                raise TenantError(f"domain {host} already belongs to a tenant")
        self._make_schema(target, schema)
        self._migrate_one(TenantRef(slug, target, schema))
        with self._cx() as c:
            with c.transaction():
                c.execute("INSERT INTO tenant (slug, state, dsn, schema, created_at) VALUES (?, 'active', ?, ?, now())", (slug, target, schema))
                c.execute("INSERT INTO tenant_domain (host, slug) VALUES (?, ?)", (host, slug))
                self._bump(c)
                self._audit(c, operator, "create", slug, f"domain {host}; schema {schema}; {reason}".strip("; "))
        return TenantRef(slug, target, schema)

    def adopt(self, slug: str, schema: str, domain: str, dsn: Optional[str] = None, operator: Optional[str] = None) -> TenantRef:
        """Register an existing schema (today's single-tenant data is `public`) as a tenant. No data moves, and the schema is only read
        to check that it exists and brought to the latest numbered migration, as any start would."""
        check_slug(slug)
        check_schema(schema)
        host = normalise_host(domain)
        target = dsn or self.dsn
        import psycopg
        with psycopg.connect(target) as c:
            if not c.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,)).fetchone():
                raise TenantError(f"schema {schema} does not exist; nothing was adopted")
        with self._cx() as c:
            if c.execute("SELECT 1 FROM tenant WHERE slug = ?", (slug,)).fetchone():
                raise TenantError(f"tenant {slug} already exists")
            if c.execute("SELECT 1 FROM tenant_domain WHERE host = ?", (host,)).fetchone():
                raise TenantError(f"domain {host} already belongs to a tenant")
        self._migrate_one(TenantRef(slug, target, schema))
        with self._cx() as c:
            with c.transaction():
                c.execute("INSERT INTO tenant (slug, state, dsn, schema, created_at) VALUES (?, 'active', ?, ?, now())", (slug, target, schema))
                c.execute("INSERT INTO tenant_domain (host, slug) VALUES (?, ?)", (host, slug))
                self._bump(c)
                self._audit(c, operator, "adopt", slug, f"domain {host}; schema {schema}")
        return TenantRef(slug, target, schema)

    def add_domain(self, slug: str, domain: str, operator: Optional[str] = None) -> None:
        host = normalise_host(domain)
        with self._cx() as c:
            self._need(c, slug)
            if c.execute("SELECT 1 FROM tenant_domain WHERE host = ?", (host,)).fetchone():
                raise TenantError(f"domain {host} already belongs to a tenant")
            with c.transaction():
                c.execute("INSERT INTO tenant_domain (host, slug) VALUES (?, ?)", (host, slug))
                self._bump(c)
                self._audit(c, operator, "domain", slug, f"add {host}")

    def suspend(self, slug: str, operator: Optional[str] = None, reason: str = "") -> None:
        self._set_state(slug, "suspended", "suspend", operator, reason)

    def resume(self, slug: str, operator: Optional[str] = None) -> None:
        self._set_state(slug, "active", "resume", operator, "")

    def _set_state(self, slug, state, action, operator, reason) -> None:
        with self._cx() as c:
            self._need(c, slug)
            with c.transaction():
                c.execute("UPDATE tenant SET state = ?, suspended_at = CASE WHEN ? = 'suspended' THEN now() ELSE NULL END WHERE slug = ?",
                          (state, state, slug))
                self._bump(c)
                self._audit(c, operator, action, slug, reason)

    def set_limit(self, slug: str, name: str, value, operator: Optional[str] = None) -> None:
        if name not in LIMIT_NAMES:
            raise TenantError(f"unknown limit {name!r}; the limits are {', '.join(LIMIT_NAMES)}")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TenantError("a limit is a whole number, 0 or more")
        with self._cx() as c:
            self._need(c, slug)
            with c.transaction():
                c.execute("INSERT INTO tenant_limit (slug, name, value) VALUES (?, ?, ?) "
                          "ON CONFLICT (slug, name) DO UPDATE SET value = excluded.value", (slug, name, value))
                self._bump(c)
                self._audit(c, operator, "limit", slug, f"{name}={value}")

    def set_setting(self, slug: str, name: str, value: str, operator: Optional[str] = None) -> None:
        if name not in SETTING_NAMES:
            raise TenantError(f"unknown setting {name!r}; the settings are {', '.join(SETTING_NAMES)} "
                              "(a secret is never stored: name the environment variable that holds it)")
        if not isinstance(value, str) or not value or len(value) > 500:
            raise TenantError("a setting is a non-empty string of at most 500 characters")
        if name == "room_token_env" and not re.match(r"^[A-Z][A-Z0-9_]{0,63}$", value):
            raise TenantError("room_token_env names an environment variable: capital letters, digits and underscores")
        with self._cx() as c:
            self._need(c, slug)
            with c.transaction():
                c.execute("INSERT INTO tenant_setting (slug, name, value) VALUES (?, ?, ?) "
                          "ON CONFLICT (slug, name) DO UPDATE SET value = excluded.value", (slug, name, value))
                self._bump(c)
                self._audit(c, operator, "setting", slug, f"{name} set")      # the value is not audited: it may be a URL with a key

    @staticmethod
    def _need(conn, slug: str) -> None:
        if not conn.execute("SELECT 1 FROM tenant WHERE slug = ?", (slug,)).fetchone():
            raise TenantError(f"no such tenant: {slug}")

    # -- tenant schemas
    @staticmethod
    def _make_schema(dsn: str, schema: str) -> None:
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA IF NOT EXISTS {_q(schema)}")

    @staticmethod
    def _migrate_one(ref: TenantRef) -> list:
        """Open the tenant's store (which applies the base tables and every numbered migration under the existing lock). Returns the
        versions this call applied."""
        from volt.store import Store
        store = Store(ref.dsn, schema=ref.schema)
        try:
            return list(store.last_migrations)
        finally:
            store.close()


def derive_media_key(master: str, slug: str) -> str:
    """The signing key for one tenant's media tickets: HMAC(master, slug). A ticket signed for tenant A never verifies on tenant B, even
    for the same asset id, and no tenant's key reveals the master."""
    return hmac.new(master.encode("utf-8"), f"volt-media:{slug}".encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class TenantSettings:
    """Everything `serve` used to hold in a module global that is really one tenant's."""
    out_dir: Path
    proxy_dir: Path
    publish_dir: Path
    quarantine_dir: Path
    storage_prefix: str
    media_key: Optional[str]
    room_url: Optional[str]
    room_token: Optional[str]


@dataclass(frozen=True)
class TenantContext:
    ref: TenantRef
    settings: TenantSettings
    limits: dict = field(default_factory=dict)

    def open_store(self, init: bool = False):
        """The only way a request gets a Store in tenant mode. The schema was migrated when the tenant was made (or by `volt tenant migrate`)."""
        from volt.store import Store
        return Store(self.ref.dsn, init=init, schema=self.ref.schema)


def build_context(ref: TenantRef, stored: dict, limits: dict, out_root, master_key: Optional[str], env=None) -> TenantContext:
    env = os.environ if env is None else env
    root = Path(out_root)
    token_env = stored.get("room_token_env")
    return TenantContext(ref, TenantSettings(
        out_dir=root / ref.slug,
        proxy_dir=Path(env.get("VOLT_PROXY_DIR") or root / "proxy") / ref.slug,
        publish_dir=Path(env.get("VOLT_PUBLISH_DIR") or root / "publish") / ref.slug,
        quarantine_dir=Path(env.get("VOLT_QUARANTINE") or root / "quarantine") / ref.slug,
        storage_prefix=f"tenants/{ref.slug}/",
        media_key=derive_media_key(master_key, ref.slug) if master_key else None,
        room_url=stored.get("room_url"),
        room_token=(env.get(token_env) or None) if token_env else None), dict(limits))


class TenantResolver:
    """Host header to TenantContext, cached. A cached answer lives `ttl` seconds, but the registry epoch is re-read every `epoch_every`
    seconds and a change drops the whole cache, so a suspend takes effect within `epoch_every`, not `ttl`. Unknown hosts are cached
    briefly (so a scanner does not make one registry query per request) and the cache is bounded."""

    MAX_ENTRIES = 1000
    NEGATIVE_TTL = 5.0

    def __init__(self, registry: Registry, out_root, master_key: Optional[str] = None, env=None, now=time.monotonic,
                 ttl: float = 60.0, epoch_every: float = 5.0):
        self.registry, self.out_root, self.master_key, self._env = registry, out_root, master_key, env
        self._now, self.ttl, self.epoch_every = now, ttl, epoch_every
        self._lock = threading.Lock()
        self._cache: dict = {}
        self._epoch: Optional[int] = None
        self._epoch_at = float("-inf")

    def size(self) -> int:
        with self._lock:
            return len(self._cache)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._epoch = None
            self._epoch_at = float("-inf")

    def resolve(self, host) -> Optional[TenantContext]:
        try:
            name = normalise_host(host)
        except TenantError:
            return None
        now = self._now()
        with self._lock:
            if now - self._epoch_at >= self.epoch_every:
                epoch = self.registry.epoch()
                if epoch != self._epoch:
                    self._cache.clear()
                    self._epoch = epoch
                self._epoch_at = now
            hit = self._cache.get(name)
            if hit is not None and now - hit[1] < (self.ttl if hit[0] is not None else self.NEGATIVE_TTL):
                return hit[0]
        ref = self.registry.by_host(name)
        ctx = None
        if ref is not None:
            ctx = build_context(ref, self.registry.settings(ref.slug), self.registry.limits(ref.slug), self.out_root,
                                self.master_key, self._env)
        with self._lock:
            if len(self._cache) >= self.MAX_ENTRIES:
                self._cache.clear()
            self._cache[name] = (ctx, now)
        return ctx


def health_env(ctx: TenantContext) -> dict:
    """The environment `health.collect` sees on a tenant host: the thresholds, and this tenant's own proxy folder. Nothing that names
    another tenant's directory, the database, or the node's shared paths."""
    keep = ("VOLT_MIN_FREE_MB", "VOLT_THUMB_CACHE_MB", "VOLT_RUNNER_STALE_SECONDS", "VOLT_STUCK_JOB_SECONDS")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["VOLT_PROXY_DIR"] = str(ctx.settings.proxy_dir)
    return env


def _operator() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"


# ---- command line -----------------------------------------------------------------------------------

EX_CONFIG = 78


def _registry_from_env() -> Registry:
    dsn = os.environ.get("VOLT_CONTROL_DSN") or (os.environ.get("VOLT_DB") if pg.is_dsn(os.environ.get("VOLT_DB") or "") else None)
    if not dsn:
        raise TenantError("set VOLT_CONTROL_DSN (a Postgres DSN) for the tenant registry")
    return Registry(dsn, os.environ.get("VOLT_CONTROL_SCHEMA") or DEFAULT_CONTROL_SCHEMA)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="volt tenant", description="Platform operator commands for a multi-tenant deployment (Postgres).")
    sub = ap.add_subparsers(dest="action", required=True)
    c = sub.add_parser("create", help="make a tenant: schema, migrations, registry row, first domain")
    c.add_argument("--slug", required=True)
    c.add_argument("--domain", required=True)
    c.add_argument("--dsn", default=None, help="the tenant's own database (default: the cluster)")
    c.add_argument("--reason", default="")
    a = sub.add_parser("adopt", help="register an existing schema (today's data is `public`) as a tenant; no data moves")
    a.add_argument("--slug", required=True)
    a.add_argument("--schema", required=True)
    a.add_argument("--domain", required=True)
    a.add_argument("--dsn", default=None)
    sub.add_parser("list", help="tenants, their state and domains")
    for name in ("suspend", "resume"):
        s = sub.add_parser(name)
        s.add_argument("slug")
        if name == "suspend":
            s.add_argument("--reason", default="")
    lm = sub.add_parser("limit", help="set a limit: " + ", ".join(LIMIT_NAMES))
    lm.add_argument("slug")
    lm.add_argument("name")
    lm.add_argument("value", type=int)
    st = sub.add_parser("setting", help="set a non-secret tenant setting: " + ", ".join(SETTING_NAMES))
    st.add_argument("slug")
    st.add_argument("name")
    st.add_argument("value")
    us = sub.add_parser("user", help="volt tenant user <slug> add <name> <role>: make a user in a tenant (its token is printed once)")
    us.add_argument("slug")
    us.add_argument("op", choices=["add"])
    us.add_argument("name")
    us.add_argument("role", choices=["viewer", "operator", "admin"])
    d = sub.add_parser("domain", help="volt tenant domain add <slug> <host>")
    d.add_argument("op", choices=["add"])
    d.add_argument("slug")
    d.add_argument("host")
    m = sub.add_parser("migrate", help="bring tenant schemas to the latest migration (idempotent)")
    m.add_argument("--tenants", default="all", help="all, or slug,slug")
    args = ap.parse_args(argv)
    try:
        reg = _registry_from_env()
    except TenantError as exc:
        print(f"volt: {exc}", file=sys.stderr)
        return EX_CONFIG
    try:
        if args.action == "create":
            ref = reg.create(args.slug, args.domain, dsn=args.dsn, reason=args.reason)
            print(f"created {ref.slug} (schema {ref.schema}) at {normalise_host(args.domain)}")
        elif args.action == "adopt":
            ref = reg.adopt(args.slug, args.schema, args.domain, dsn=args.dsn)
            print(f"adopted {ref.slug} (schema {ref.schema}) at {normalise_host(args.domain)}")
        elif args.action == "list":
            for t in reg.all():
                print(f"{t.slug}\t{t.state}\t{t.schema}\t{','.join(reg.domains(t.slug))}")
        elif args.action == "suspend":
            reg.suspend(args.slug, reason=args.reason)
            print(f"suspended {args.slug}")
        elif args.action == "resume":
            reg.resume(args.slug)
            print(f"resumed {args.slug}")
        elif args.action == "limit":
            reg.set_limit(args.slug, args.name, args.value)
            print(f"{args.slug}: {args.name} = {args.value}")
        elif args.action == "setting":
            reg.set_setting(args.slug, args.name, args.value)
            print(f"{args.slug}: {args.name} set")
        elif args.action == "user":
            ref = reg.get(args.slug)
            if ref is None:
                raise TenantError(f"no such tenant: {args.slug}")
            from volt import users
            from volt.store import Store
            store = Store(ref.dsn, init=False, schema=ref.schema)
            try:
                row, token = users.add(store, args.name, args.role)
            except users.UserError as exc:
                raise TenantError(str(exc)) from None
            finally:
                store.close()
            with reg._cx() as c:
                reg._audit(c, None, "user", args.slug, f"added {row['name']} as {args.role}")
                c.commit()
            print(f"{args.slug}: {row['name']} ({args.role}); token (shown once): {token}")
        elif args.action == "domain":
            reg.add_domain(args.slug, args.host)
            print(f"added {normalise_host(args.host)} to {args.slug}")
        elif args.action == "migrate":
            slugs = None if args.tenants == "all" else [s for s in args.tenants.split(",") if s]
            report = migrations.run_all_tenants(reg, slugs=slugs)
            bad = 0
            for slug, res in report.items():
                if res["ok"]:
                    print(f"{slug}\tok\tapplied {res['applied'] or 'nothing'}")
                else:
                    bad += 1
                    print(f"{slug}\tFAILED\t{res['error']}")
            return 1 if bad else 0
    except TenantError as exc:
        print(f"volt: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
