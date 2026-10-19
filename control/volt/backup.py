"""Backup, restore and storage verification. `python -m volt.backup --help`. See docs/specs/backup.md.

A backup is a directory: the data file (`volt.db` for SQLite, `volt.dump` for Postgres) and `manifest.json`, written
last. A directory without a manifest is not a backup. The manifest holds the format, the kind, the schema version,
the time, the row count of every table and the SHA-256 and size of the data file; `verify_backup` and `restore`
check all of it, so a damaged or truncated backup is refused before anything is touched.

SQLite uses the online backup API (sqlite3.Connection.backup), so a backup taken while serve and the runner are
writing is a consistent snapshot, WAL included. Postgres goes through `pg_dump` / `pg_restore` (custom format) and
refuses, with a message, when those tools are not installed. The password never appears on a command line: it is
passed in PGPASSWORD.

This module exposes plain functions (`backup`, `verify_backup`, `restore`, `verify_storage`) and a small `main()` so
a `volt` umbrella command can call them. Every backup writes `media.json` naming closed essences and done versions.
`--with-media` copies local files under the proxy, quarantine and publish directories into `<dest>/media/`; object
storage is listed, not copied. `verify_storage` tells you whether those bytes are still where the database points.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from volt import migrations, pg

FORMAT = 1
MANIFEST = "manifest.json"
MEDIA = "media.json"
SQLITE_FILE = "volt.db"
PG_FILE = "volt.dump"


class BackupError(Exception):
    """A backup, restore or verification was refused or failed. The message says why and is safe to print."""


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> tuple:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as fh:
        while block := fh.read(1 << 20):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _redact(target: str) -> str:
    if pg.is_dsn(target):
        u = urlparse(target)
        host = u.hostname or ""
        return f"{u.scheme}://{(u.username + '@') if u.username else ''}{host}{(':' + str(u.port)) if u.port else ''}{u.path}"
    return str(Path(target).resolve())


# ---- SQLite helpers -------------------------------------------------------------------------------

def _sqlite_tables(conn) -> list:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _sqlite_counts(conn) -> dict:
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in _sqlite_tables(conn)}


def _sqlite_version(conn) -> int:
    if "schema_version" not in _sqlite_tables(conn):
        return 0
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]


def _sqlite_check(conn) -> None:
    rows = [r[0] for r in conn.execute("PRAGMA integrity_check")]
    if rows != ["ok"]:
        raise BackupError("integrity_check failed: " + "; ".join(rows[:5]))


# ---- Postgres helpers -----------------------------------------------------------------------------

_PG_DIRS = ("/usr/lib/postgresql/*/bin", "/usr/local/pgsql/bin", "/opt/homebrew/opt/postgresql*/bin",
            r"C:\Program Files\PostgreSQL\*\bin")


def find_pg_tool(name: str) -> str:
    """Path to pg_dump / pg_restore: $VOLT_PG_DUMP / $VOLT_PG_RESTORE, else PATH, else the usual install folders."""
    override = os.environ.get("VOLT_" + name.upper())
    if override:
        return override
    found = shutil.which(name)
    if found:
        return found
    exe = name + (".exe" if os.name == "nt" else "")
    for pattern in _PG_DIRS:
        for folder in sorted(glob.glob(pattern), reverse=True):
            candidate = Path(folder) / exe
            if candidate.is_file():
                return str(candidate)
    raise BackupError(f"{name} was not found on PATH (or in the usual PostgreSQL install folders). Install the "
                      f"PostgreSQL client tools matching your server, or set VOLT_{name.upper()} to the program. "
                      "Refusing to back up or restore Postgres without it.")


def _pg_args_env(dsn: str) -> tuple:
    u = urlparse(dsn)
    args = []
    if u.hostname:
        args += ["--host", u.hostname]
    if u.port:
        args += ["--port", str(u.port)]
    if u.username:
        args += ["--username", unquote(u.username)]
    dbname = unquote(u.path.lstrip("/")) or "postgres"
    args += ["--dbname", dbname]
    env = dict(os.environ)
    if u.password:
        env["PGPASSWORD"] = unquote(u.password)
    q = parse_qs(u.query)
    for key, var in (("sslmode", "PGSSLMODE"), ("sslrootcert", "PGSSLROOTCERT"), ("connect_timeout", "PGCONNECT_TIMEOUT")):
        if key in q:
            env[var] = q[key][0]
    return args, env, (unquote(u.password) if u.password else "")


def _run_pg(cmd: list, env: dict, secret: str, stdin=None, stdout=None) -> str:
    try:
        done = subprocess.run(cmd, env=env, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError(f"{Path(cmd[0]).name} could not run: {exc}") from exc
    err = (done.stderr or b"").decode("utf-8", "replace").strip()
    if secret:
        err = err.replace(secret, "***")
    if done.returncode != 0:
        raise BackupError(f"{Path(cmd[0]).name} failed (exit {done.returncode}): {err[-600:]}")
    return err


def _pg_state(dsn: str) -> dict:
    """{'version': int, 'counts': {table: rows}} for the connected database's current schema."""
    from volt.store import Store
    store = Store(dsn, init=False)
    try:
        tables = [r[0] for r in store.conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
            "AND table_type = 'BASE TABLE' ORDER BY table_name").fetchall()]
        counts = {}
        for t in tables:
            if not re.fullmatch(r"\w+", t):
                continue
            counts[t] = store.conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        version = 0
        if "schema_version" in tables:
            version = store.conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
        return {"version": version, "counts": counts}
    finally:
        store.close()


# ---- backup ---------------------------------------------------------------------------------------

def backup(source: str, dest: str, with_media: bool = False, proxy_dir=None, quarantine_dir=None,
           publish_dir=None) -> dict:
    """Back up the database at `source` (SQLite path or postgres:// DSN) into the new or empty directory `dest`.

    Always writes `media.json` (closed essences and done versions). With `with_media`, copies local files whose
    paths sit under the proxy, quarantine or publish directories into `<dest>/media/`; a missing source is listed
    in `media.json` `missing` and is not a failure. Returns the manifest. Raises BackupError (nothing usable is
    left behind on failure: no manifest is written)."""
    dest = Path(dest)
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise BackupError(f"{dest} already exists and is not an empty directory; a backup never overwrites anything")
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if pg.is_dsn(source):
            manifest = _backup_pg(source, dest)
            plan = _media_plan_from_dsn(source)
        else:
            manifest = _backup_sqlite(source, dest)
            plan = _media_plan_from_sqlite(dest / SQLITE_FILE)
        media_doc = {"essences": plan["essences"], "versions": plan["versions"]}
        if with_media:
            media_doc["missing"] = _copy_owned_media(
                dest, plan,
                proxy_dir if proxy_dir is not None else os.environ.get("VOLT_PROXY_DIR"),
                quarantine_dir if quarantine_dir is not None else os.environ.get("VOLT_QUARANTINE_DIR"),
                publish_dir if publish_dir is not None else os.environ.get("VOLT_PUBLISH_DIR"),
            )
        (dest / MEDIA).write_text(json.dumps(media_doc, indent=2, sort_keys=True), encoding="utf-8")
    except BaseException:
        _wipe_backup_dir(dest)  # a failed backup must not look like a partial one
        raise
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def _wipe_backup_dir(dest: Path) -> None:
    for leftover in dest.iterdir():
        try:
            if leftover.is_dir():
                shutil.rmtree(leftover)
            else:
                leftover.unlink()
        except OSError:
            pass


def _under(path: Path, root: Path) -> bool:
    p = os.path.normcase(os.path.abspath(path))
    r = os.path.normcase(os.path.abspath(root))
    if p == r:
        return True
    prefix = r if r.endswith(os.sep) else r + os.sep
    return p.startswith(prefix)


def _media_from_conn(conn) -> dict:
    essences, essence_files = [], []
    for row in conn.execute(
            "SELECT id, asset_id, role, location, checksum FROM essence WHERE NOT open ORDER BY id"):
        eid, asset_id, role, location, checksum = row[0], row[1], row[2], row[3], row[4]
        essences.append({"id": eid, "role": role, "location": location, "checksum": checksum})
        essence_files.append((asset_id, location))
    versions, version_files = [], []
    for row in conn.execute(
            "SELECT id, asset_id, storage_backend, storage_key, storage_sha256 FROM version "
            "WHERE status = 'done' ORDER BY id"):
        vid, asset_id, backend, key, digest = row[0], row[1], row[2], row[3], row[4]
        versions.append({"id": vid, "backend": backend, "key": key, "sha256": digest})
        version_files.append((asset_id, backend, key))
    return {"essences": essences, "versions": versions,
            "essence_files": essence_files, "version_files": version_files}


def _media_plan_from_sqlite(path: Path) -> dict:
    conn = sqlite3.connect(str(path))
    try:
        return _media_from_conn(conn)
    finally:
        conn.close()


def _media_plan_from_dsn(dsn: str) -> dict:
    from volt.store import Store
    store = Store(dsn, init=False)
    try:
        return _media_from_conn(store.conn)
    finally:
        store.close()


def _copy_owned_media(dest: Path, plan: dict, proxy_dir, quarantine_dir, publish_dir) -> list:
    roots = [Path(p) for p in (proxy_dir, quarantine_dir, publish_dir) if p]
    missing = []
    media_root = dest / "media"
    for asset_id, location in plan["essence_files"]:
        src = Path(location)
        if not any(_under(src, r) for r in roots):
            continue
        if not src.is_file():
            missing.append(location)
            continue
        out = media_root / asset_id / src.name
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out)
    publish = Path(publish_dir) if publish_dir else None
    for asset_id, backend, key in plan["version_files"]:
        if backend != "local" or publish is None or not key:
            continue
        src = publish / key
        if not _under(src, publish):
            continue
        if not src.is_file():
            missing.append(f"{backend}:{key}")
            continue
        out = media_root / asset_id / Path(key).name
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out)
    return missing


def _peek_media_json(directory) -> Optional[dict]:
    """Read media.json when present. Old or unreadable backups are ignored."""
    path = Path(directory) / MEDIA
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _backup_sqlite(source: str, dest: Path) -> dict:
    src_path = Path(source)
    if source == ":memory:" or not src_path.is_file():
        raise BackupError(f"no SQLite database at {source}")
    out = dest / SQLITE_FILE
    src = sqlite3.connect(str(src_path), timeout=30)
    dst = sqlite3.connect(str(out))
    try:
        src.backup(dst)  # the online backup API: a consistent snapshot while writers are active
        _sqlite_check(dst)
        version = _sqlite_version(dst)
        counts = _sqlite_counts(dst)
        dst.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal needed to read it
        dst.commit()
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite backup failed: {exc}") from exc
    finally:
        dst.close()
        src.close()
    digest, size = _sha256(out)
    return {"format": FORMAT, "kind": "sqlite", "file": SQLITE_FILE, "created_at": _stamp(),
            "source": _redact(source), "schema_version": version, "code_schema_version": migrations.known_version(),
            "row_counts": counts, "sha256": digest, "size": size, "integrity_check": "ok"}


def _backup_pg(dsn: str, dest: Path) -> dict:
    tool = find_pg_tool("pg_dump")
    args, env, secret = _pg_args_env(dsn)
    state = _pg_state(dsn)
    out = dest / PG_FILE
    with open(out, "wb") as fh:
        _run_pg([tool, "--format=custom", "--no-owner", *args], env, secret, stdout=fh)
    if out.stat().st_size == 0:
        raise BackupError("pg_dump produced an empty file")
    digest, size = _sha256(out)
    return {"format": FORMAT, "kind": "postgres", "file": PG_FILE, "created_at": _stamp(),
            "source": _redact(dsn), "schema_version": state["version"], "code_schema_version": migrations.known_version(),
            "row_counts": state["counts"], "sha256": digest, "size": size,
            "tool": Path(tool).name}


# ---- verify ---------------------------------------------------------------------------------------

def read_manifest(directory) -> dict:
    path = Path(directory) / MANIFEST
    if not path.is_file():
        raise BackupError(f"{directory} has no {MANIFEST}: not a complete backup")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"{MANIFEST} is unreadable: {exc}") from exc
    for key in ("format", "kind", "file", "sha256", "size", "schema_version", "row_counts"):
        if key not in manifest:
            raise BackupError(f"{MANIFEST} is missing {key!r}")
    if manifest["format"] != FORMAT:
        raise BackupError(f"backup format {manifest['format']} is not supported by this volt (knows {FORMAT})")
    if manifest["kind"] not in ("sqlite", "postgres"):
        raise BackupError(f"unknown backup kind {manifest['kind']!r}")
    if manifest["file"] != Path(manifest["file"]).name:
        raise BackupError("manifest names a file outside the backup directory")
    return manifest


def verify_backup(directory) -> dict:
    """Check a backup end to end without restoring it. Returns the manifest or raises BackupError."""
    directory = Path(directory)
    manifest = read_manifest(directory)
    data = directory / manifest["file"]
    if not data.is_file():
        raise BackupError(f"the data file {manifest['file']} is missing")
    digest, size = _sha256(data)
    if size != manifest["size"]:
        raise BackupError(f"{manifest['file']} is {size} bytes, the manifest says {manifest['size']} (truncated?)")
    if digest != manifest["sha256"]:
        raise BackupError(f"{manifest['file']} does not match its SHA-256: the backup is corrupt")
    if manifest["kind"] == "sqlite":
        try:
            conn = sqlite3.connect(f"{data.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        except sqlite3.Error as exc:
            raise BackupError(f"cannot open the backup: {exc}") from exc
        try:
            _sqlite_check(conn)
            if _sqlite_version(conn) != manifest["schema_version"]:
                raise BackupError("schema version inside the backup differs from the manifest")
            if _sqlite_counts(conn) != manifest["row_counts"]:
                raise BackupError("row counts inside the backup differ from the manifest")
        except sqlite3.Error as exc:
            raise BackupError(f"the backup is not a readable SQLite database: {exc}") from exc
        finally:
            conn.close()
    else:
        try:
            tool = find_pg_tool("pg_restore")
        except BackupError:
            return manifest  # checksum verified; the archive listing needs pg_restore
        with open(data, "rb") as fh:
            _run_pg([tool, "--list"], dict(os.environ), "", stdin=fh, stdout=subprocess.DEVNULL)
    return manifest


# ---- restore --------------------------------------------------------------------------------------

def _sqlite_rows(path: Path) -> Optional[int]:
    """Total rows in a SQLite file, 0 for none/empty, None when it is not a readable database."""
    if not path.exists() or path.stat().st_size == 0:
        return 0
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            return sum(_sqlite_counts(conn).values())
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def restore(backup_dir, target: str, overwrite: bool = False) -> dict:
    """Restore a verified backup into `target` (SQLite path or postgres:// DSN), then migrate it forward.

    Refuses a corrupt backup, a backup newer than this code, and a target that already holds data unless
    `overwrite` is true (SQLite: the old file is moved aside, never deleted). Returns a report."""
    manifest = verify_backup(backup_dir)
    _peek_media_json(backup_dir)  # optional; old backups have none
    if manifest["schema_version"] > migrations.known_version():
        raise migrations.SchemaTooNewError(manifest["schema_version"], migrations.known_version())
    if (manifest["kind"] == "postgres") != pg.is_dsn(target):
        raise BackupError(f"this is a {manifest['kind']} backup; the target {_redact(target)} is "
                          f"{'Postgres' if pg.is_dsn(target) else 'a SQLite path'}")
    if manifest["kind"] == "postgres":
        return _restore_pg(Path(backup_dir), manifest, target, overwrite)
    return _restore_sqlite(Path(backup_dir), manifest, Path(target), overwrite)


def _restore_sqlite(backup_dir: Path, manifest: dict, target: Path, overwrite: bool) -> dict:
    from volt.store import Store
    rows = _sqlite_rows(target)
    if rows != 0 and not overwrite:
        what = "is not a readable SQLite database" if rows is None else f"already holds {rows} row(s)"
        raise BackupError(f"{target} {what}; refusing to overwrite it (pass overwrite=True / --overwrite to replace it; "
                          "the old file is kept beside it)")
    target.parent.mkdir(parents=True, exist_ok=True)
    work = target.with_name(f"{target.name}.restoring-{uuid.uuid4().hex[:8]}")
    try:
        shutil.copyfile(backup_dir / manifest["file"], work)
        store = Store(str(work))  # migrates an older schema forward; refuses a newer one
        try:
            ran = _applied_after(store, manifest["schema_version"])
        finally:
            store.close()
        check = sqlite3.connect(str(work))
        try:
            _sqlite_check(check)
            counts = _sqlite_counts(check)
        finally:
            check.close()
        for table, n in manifest["row_counts"].items():
            if table != "schema_version" and counts.get(table) != n:  # the ledger gains rows when it migrates
                raise BackupError(f"restored table {table} has {counts.get(table)} rows, the backup had {n}")
        aside = None
        if target.exists() and target.stat().st_size > 0:
            aside = target.with_name(f"{target.name}.pre-restore-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
            os.replace(target, aside)
        for suffix in ("-wal", "-shm"):  # a stale journal beside a replaced file would be replayed into it
            side = Path(str(target) + suffix)
            if side.exists():
                if aside is not None:
                    os.replace(side, Path(str(aside) + suffix))
                else:
                    side.unlink()
        os.replace(work, target)
    finally:
        for p in (work, Path(str(work) + "-wal"), Path(str(work) + "-shm")):
            try:
                p.unlink()
            except OSError:
                pass
    return {"target": str(target), "kind": "sqlite", "from_schema_version": manifest["schema_version"],
            "migrated": ran, "moved_aside": str(aside) if aside else None, "row_counts": manifest["row_counts"]}


def _applied_after(store, old_version: int) -> list:
    return [v for v in sorted(migrations.SqliteBackend(store.conn).applied()) if v > old_version]


def _restore_pg(backup_dir: Path, manifest: dict, dsn: str, overwrite: bool) -> dict:
    from volt.store import Store
    tool = find_pg_tool("pg_restore")
    args, env, secret = _pg_args_env(dsn)
    state = _pg_state(dsn)
    rows = sum(state["counts"].values())
    if rows and not overwrite:
        raise BackupError(f"the target database already holds {rows} row(s); refusing to overwrite it "
                          "(pass overwrite=True / --overwrite to replace the Volt tables)")
    cmd = [tool, "--no-owner", "--exit-on-error", "--single-transaction"]
    if state["counts"]:
        cmd += ["--clean", "--if-exists"]
    with open(backup_dir / manifest["file"], "rb") as fh:
        _run_pg([*cmd, *args], env, secret, stdin=fh)
    store = Store(dsn)  # applies the migrations an older backup lacks
    try:
        after = {t: store.conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in manifest["row_counts"]}
        ran = [v for v in sorted(migrations.PostgresBackend(store.conn._conn).applied()) if v > manifest["schema_version"]]
    finally:
        store.close()
    for table, n in manifest["row_counts"].items():
        if table != "schema_version" and after.get(table) != n:
            raise BackupError(f"restored table {table} has {after.get(table)} rows, the backup had {n}")
    return {"target": _redact(dsn), "kind": "postgres", "from_schema_version": manifest["schema_version"],
            "migrated": ran, "moved_aside": None, "row_counts": manifest["row_counts"]}


# ---- storage verification (read only) ------------------------------------------------------------------

@dataclass
class StorageReport:
    checked: int = 0
    missing: list = field(default_factory=list)      # (what, where)
    mismatched: list = field(default_factory=list)   # (what, where, why)
    empty: list = field(default_factory=list)        # zero-byte files the database says exist
    orphaned: list = field(default_factory=list)     # (where) present, referenced by nothing
    errors: list = field(default_factory=list)       # (what, where, why) could not be checked
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.mismatched or self.empty or self.errors)

    def lines(self) -> list:
        out = [f"checked {self.checked} object(s): {len(self.missing)} missing, {len(self.mismatched)} mismatched, "
               f"{len(self.empty)} empty, {len(self.errors)} unreadable, {len(self.orphaned)} orphaned"]
        out += [f"MISSING   {w}  {where}" for w, where in self.missing]
        out += [f"MISMATCH  {w}  {where}  {why}" for w, where, why in self.mismatched]
        out += [f"EMPTY     {where}" for where in self.empty]
        out += [f"ERROR     {w}  {where}  {why}" for w, where, why in self.errors]
        out += [f"ORPHAN    {where}" for where in self.orphaned]
        out += [f"note: {n}" for n in self.notes]
        return out


def verify_storage(store, backend=None, proxy_dir=None, quarantine_dir=None, deep: bool = False) -> StorageReport:
    """Check every essence location and every published version the database references. Read only.

    Local essence paths: exists, non-empty; with `deep`, a closed essence with a stored checksum is re-hashed.
    Published versions: stat through the storage `backend` (existence, size, and the SHA-256 where the backend
    exposes one, compared with `version.storage_sha256`). Orphans are objects the backend can list (local, S3) or
    files in the proxy/quarantine folders that no row references. Nothing is written or deleted."""
    from volt import storage
    rep = StorageReport()
    referenced_local = set()
    for row in store.conn.execute(
            "SELECT id, asset_id, role, location, checksum, open FROM essence ORDER BY asset_id, id").fetchall():
        path = Path(row["location"])
        what = f"essence {row['role']} of asset {row['asset_id']}"
        rep.checked += 1
        referenced_local.add(os.path.normcase(os.path.abspath(row["location"])))
        try:
            if not path.is_file():
                rep.missing.append((what, row["location"]))
                continue
            size = path.stat().st_size
            if size == 0 and not bool(row["open"]):
                rep.empty.append(row["location"])
                continue
            if deep and row["checksum"] and not bool(row["open"]):
                digest, _ = storage.sha256_file(path)
                if digest != row["checksum"]:
                    rep.mismatched.append((what, row["location"], "SHA-256 differs from the stored checksum"))
        except OSError as exc:
            rep.errors.append((what, row["location"], str(exc)))

    versions = store.conn.execute(
        "SELECT id, asset_id, status, storage_backend, storage_key, storage_sha256 FROM version "
        "WHERE storage_key IS NOT NULL ORDER BY id").fetchall()
    keys_by_backend: dict = {}
    for row in versions:
        keys_by_backend.setdefault(row["storage_backend"], set()).add(row["storage_key"])
        if row["status"] != "done":
            continue
        what = f"version {row['id']} of asset {row['asset_id']}"
        where = f"{row['storage_backend']}:{row['storage_key']}"
        if backend is None or backend.id != row["storage_backend"]:
            rep.notes.append(f"{where} is in {row['storage_backend']}, which is not the configured storage; not checked")
            continue
        rep.checked += 1
        try:
            info = storage.stat_object(backend, row["storage_key"])
        except Exception as exc:  # noqa: BLE001 - StorageError text is secret-free; anything else is reported as is
            rep.errors.append((what, where, str(exc)[:300]))
            continue
        if info is None:
            rep.missing.append((what, where))
        elif info.size == 0:
            rep.empty.append(where)
        elif info.sha256 and row["storage_sha256"] and info.sha256 != row["storage_sha256"]:
            rep.mismatched.append((what, where, "SHA-256 differs from the one recorded at publish"))
        elif not info.sha256:
            note = f"{backend.id}: existence and size only for published versions (no SHA-256 exposed)"
            if note not in rep.notes:
                rep.notes.append(note)

    if backend is not None:
        lister = getattr(backend, "list_keys", None)
        if lister is None:
            rep.notes.append(f"{backend.id} cannot be listed, so orphaned objects are not reported for it")
        else:
            known = keys_by_backend.get(backend.id, set())
            try:
                for key in lister():
                    if key not in known and not key.startswith("."):
                        rep.orphaned.append(f"{backend.id}:{key}")
            except Exception as exc:  # noqa: BLE001
                rep.errors.append(("listing", backend.id, str(exc)[:300]))
    for folder in (proxy_dir, quarantine_dir):
        if folder and Path(folder).is_dir():
            for path in sorted(Path(folder).iterdir()):
                if path.is_file() and not path.name.endswith((".lock", ".part", ".part.mp4")) \
                        and os.path.normcase(os.path.abspath(path)) not in referenced_local:
                    rep.orphaned.append(str(path))
    return rep


# ---- command line ---------------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m volt.backup",
                                 description="Back up, restore and verify the Volt database and its storage.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup", help="back up a database into a new directory")
    b.add_argument("db", nargs="?", default=os.environ.get("VOLT_DB"), help="SQLite path or postgres:// DSN (or VOLT_DB)")
    b.add_argument("dest")
    b.add_argument("--with-media", action="store_true",
                   help="copy local files under VOLT_PROXY_DIR, VOLT_QUARANTINE_DIR and VOLT_PUBLISH_DIR into dest/media/")
    v = sub.add_parser("verify", help="check a backup directory without restoring it")
    v.add_argument("dir")
    r = sub.add_parser("restore", help="restore a backup into a database (stop serve and the runner first)")
    r.add_argument("dir")
    r.add_argument("db", nargs="?", default=os.environ.get("VOLT_DB"))
    r.add_argument("--overwrite", action="store_true", help="replace a database that already holds data")
    s = sub.add_parser("verify-storage", help="check every file and object the database references (read only)")
    s.add_argument("db", nargs="?", default=os.environ.get("VOLT_DB"))
    s.add_argument("--deep", action="store_true", help="re-hash closed local essences against their stored checksum")
    s.add_argument("--proxy-dir", type=Path, default=Path(os.environ["VOLT_PROXY_DIR"]) if os.environ.get("VOLT_PROXY_DIR") else None)
    s.add_argument("--quarantine-dir", type=Path, default=Path(os.environ["VOLT_QUARANTINE_DIR"]) if os.environ.get("VOLT_QUARANTINE_DIR") else None)
    s.add_argument("--publish-dir", type=Path, default=Path(os.environ["VOLT_PUBLISH_DIR"]) if os.environ.get("VOLT_PUBLISH_DIR") else None)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "backup":
            if not args.db:
                ap.error("a database path or DSN is required")
            m = backup(args.db, args.dest, with_media=args.with_media,
                       proxy_dir=os.environ.get("VOLT_PROXY_DIR"),
                       quarantine_dir=os.environ.get("VOLT_QUARANTINE_DIR"),
                       publish_dir=os.environ.get("VOLT_PUBLISH_DIR"))
            print(f"backed up {m['kind']} schema v{m['schema_version']}, {sum(m['row_counts'].values())} rows, "
                  f"{m['size']} bytes, sha256 {m['sha256'][:16]}... to {args.dest}")
        elif args.cmd == "verify":
            m = verify_backup(args.dir)
            print(f"ok: {m['kind']} backup of {m['created_at']}, schema v{m['schema_version']}, "
                  f"{sum(m['row_counts'].values())} rows")
        elif args.cmd == "restore":
            if not args.db:
                ap.error("a database path or DSN is required")
            rep = restore(args.dir, args.db, overwrite=args.overwrite)
            print(f"restored to {rep['target']} (schema v{rep['from_schema_version']}"
                  + (f", migrated forward through v{max(rep['migrated'])}" if rep["migrated"] else "") + ")"
                  + (f"; previous database kept at {rep['moved_aside']}" if rep["moved_aside"] else ""))
        else:
            from volt import storage
            from volt.store import Store
            if not args.db:
                ap.error("a database path or DSN is required")
            store = Store(args.db)
            try:
                try:
                    backend = storage.from_env(local_root=args.publish_dir)
                except storage.StorageError as exc:
                    backend = None
                    print(f"note: no storage backend ({exc}); published versions are not checked")
                rep = verify_storage(store, backend, args.proxy_dir, args.quarantine_dir, deep=args.deep)
            finally:
                store.close()
            print("\n".join(rep.lines()))
            return 0 if rep.ok else 1
    except (BackupError, migrations.SchemaTooNewError) as exc:
        print(f"volt backup: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
