"""Asset deletion and retention: soft delete (tombstone), restore, and a separate purge. See docs/specs/retention.md.

Two steps, on purpose:

1. `tombstone` (the runner's `delete` job, or the operator route) sets `asset.deleted_at`. The asset disappears from
   the library, search and the operator lists; nothing on disk or in storage is touched and `restore` undoes it.
2. `purge_asset` / `purge_due` run later, after the retention window (VOLT_PURGE_AFTER_DAYS, default 30), and
   remove the files Volt owns and then the database rows. Every object it touches, removes or deliberately leaves
   is written to `purge_audit`.

Safeguards, checked at tombstone time and again at purge time (a hold or an attachment can arrive in between):
legal hold, retention class `permanent`, an open (still recording) essence, any rundown item using one of the
asset's spans, any publish version, and (purge only) a running job. A refusal names every reason and changes nothing.

What a purge may delete. Only files Volt itself made, found under a directory it was told it owns and named for the
asset: the proxy under VOLT_PROXY_DIR, the quarantine copy made by watch-folder ingest under VOLT_QUARANTINE_DIR,
the thumbnail cache directory under VOLT_OUT, and, only when asked for explicitly, published versions through the
storage backend that holds them. A hi-res essence registered from somewhere else (a recording the core wrote, a file
named by `record_start`) is never touched: it is audited as `left_in_place`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from volt.store import Store, _id, _now

log = logging.getLogger("volt.retention")

DEFAULT_PURGE_DAYS = 30.0
PERMANENT_CLASSES = frozenset({"permanent"})  # retention classes that can never be deleted


class DeleteRefused(Exception):
    """The asset cannot be deleted (or purged) now. `reasons` lists every cause; nothing was changed."""

    def __init__(self, asset_id: str, reasons: list):
        super().__init__(f"asset {asset_id} cannot be deleted: " + "; ".join(reasons))
        self.asset_id, self.reasons = asset_id, list(reasons)


# ---- policy ------------------------------------------------------------------------------------

def retention_days(retention_class: str, env=None) -> float:
    """Days a tombstone waits before purge: VOLT_PURGE_AFTER_DAYS_<CLASS>, else VOLT_PURGE_AFTER_DAYS, else 30."""
    env = os.environ if env is None else env
    suffix = "".join(c if c.isalnum() else "_" for c in (retention_class or "").upper())
    for name in (f"VOLT_PURGE_AFTER_DAYS_{suffix}", "VOLT_PURGE_AFTER_DAYS"):
        raw = env.get(name)
        if raw not in (None, ""):
            try:
                days = float(raw)
            except ValueError:
                raise ValueError(f"{name} must be a number of days") from None
            if days < 0:
                raise ValueError(f"{name} must not be negative")
            return days
    return DEFAULT_PURGE_DAYS


def blockers(store: Store, asset_id: str, purge: bool = False, allow_versions: bool = False) -> list:
    """Every reason this asset must not be deleted right now. Empty = deletable. KeyError for an unknown asset."""
    asset = store.get_asset(asset_id)
    why = []
    if asset.legal_hold:
        why.append("legal hold")
    if asset.retention_class in PERMANENT_CLASSES:
        why.append(f"retention class {asset.retention_class!r} is never deleted")
    for row in store.conn.execute("SELECT id, open FROM essence WHERE asset_id = ?", (asset_id,)).fetchall():
        if bool(row["open"]):
            why.append("still recording (an essence is open)")
            break
    attached = store.conn.execute(
        """SELECT COUNT(*) FROM item i JOIN span s ON s.id = i.span_id WHERE s.asset_id = ?""",
        (asset_id,)).fetchone()[0]
    if attached:
        why.append(f"attached to {attached} rundown item(s)")
    if not allow_versions:
        # A withdrawn (unpublished) version is history, not a publication: it does not block a delete.
        versions = store.conn.execute(
            "SELECT COUNT(*) FROM version WHERE asset_id = ? AND withdrawn_at IS NULL", (asset_id,)).fetchone()[0]
        if versions:
            why.append(f"has {versions} active publish version(s); unpublish them first")
    if purge and not allow_versions:
        # Purging removes the version rows. A withdrawn one whose stored object was never removed (the delete failed,
        # or the item was on air) would leave that object behind with nothing pointing at it: finish the unpublish first.
        pending = [r for r in store.conn.execute(
            "SELECT storage_key FROM version WHERE asset_id = ? AND withdrawn_at IS NOT NULL "
            "AND storage_key IS NOT NULL AND storage_deleted_at IS NULL", (asset_id,)).fetchall()
            if str(r["storage_key"]).startswith(f"{asset_id}/")]
        if pending:
            why.append(f"{len(pending)} withdrawn version(s) still have a stored object; call unpublish again to remove it")
    if purge:
        running = store.conn.execute(
            "SELECT COUNT(*) FROM job WHERE asset_id = ? AND status = 'running' AND type <> 'delete'",
            (asset_id,)).fetchone()[0]
        if running:
            why.append(f"{running} job(s) are running on it")
    return why


# ---- step 1: tombstone and restore --------------------------------------------------------------

def tombstone(store: Store, asset_id: str, now: Optional[str] = None) -> bool:
    """Soft-delete. True when this call tombstoned it, False when it already was. Raises DeleteRefused / KeyError.

    The safeguards are re-read inside the transaction that sets the mark."""
    with store.transaction(f"asset:{asset_id}"):
        asset = store.get_asset(asset_id)
        if asset.deleted_at:
            return False
        why = blockers(store, asset_id)
        if why:
            raise DeleteRefused(asset_id, why)
        cur = store.conn.execute(
            "UPDATE asset SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL", (now or _now(), asset_id))
        return cur.rowcount == 1


def restore(store: Store, asset_id: str) -> bool:
    """Undo a tombstone. True when it was tombstoned. KeyError for an unknown (or already purged) asset."""
    with store.transaction(f"asset:{asset_id}"):
        store.get_asset(asset_id)
        cur = store.conn.execute(
            "UPDATE asset SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL", (asset_id,))
        if cur.rowcount != 1:
            return False
        # a finished delete job would otherwise swallow the next delete (same idempotency key)
        store.conn.execute("DELETE FROM job WHERE asset_id = ? AND type = 'delete' AND status = 'done'", (asset_id,))
        return True


# ---- step 2: purge ---------------------------------------------------------------------------------

@dataclass
class OwnedRoots:
    """The only places a purge may delete from. A root left as None means 'not owned', so files there are kept."""
    proxy_dir: Optional[Path] = None
    quarantine_dir: Optional[Path] = None
    out_dir: Optional[Path] = None          # thumbnail cache: <out_dir>/thumbs/<asset>
    backend: Optional[object] = None        # a storage.Backend, used only for published versions (allow_versions)
    backend_factory: Optional[Callable[[], object]] = None

    def get_backend(self):
        if self.backend is None and self.backend_factory is not None:
            self.backend = self.backend_factory()
        return self.backend


@dataclass
class PurgeResult:
    asset_id: str
    status: str                              # purged | refused | partial | skipped
    reasons: list = field(default_factory=list)
    removed: int = 0
    left: int = 0
    failed: list = field(default_factory=list)


@dataclass
class PurgeReport:
    results: list = field(default_factory=list)

    @property
    def failed(self) -> list:
        return [f"{r.asset_id}: {'; '.join(r.failed or r.reasons)}" for r in self.results if r.status == "partial"]

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    def __str__(self) -> str:
        return " ".join(f"{s}={self.count(s)}" for s in ("purged", "partial", "refused", "skipped"))


def _audit(store: Store, asset_id: str, kind: str, outcome: str, ref=None, role=None, location=None, detail=None):
    store.conn.execute(
        """INSERT INTO purge_audit (id, purged_at, asset_id, kind, ref, role, location, outcome, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (_id(), _now(), asset_id, kind, ref, role, location, outcome, detail))
    store.conn.commit()


def _inside(root: Path, path: Path) -> bool:
    """path's directory, with symlinks resolved, is root or below it. The final component is not followed."""
    try:
        parent = Path(os.path.realpath(path.parent))
        parent.relative_to(os.path.realpath(root))
    except ValueError:
        return False
    return True


def _owned_file(roots: OwnedRoots, asset_id: str, role: str, location: str) -> tuple:
    """(Path or None, why not). A file is owned when it sits under an owned root and is named for this asset."""
    path = Path(location)
    if not path.name.startswith(asset_id):
        return None, "not named for this asset"
    candidates = []
    if role == "proxy" and roots.proxy_dir:
        candidates.append(roots.proxy_dir)
    if roots.quarantine_dir:
        candidates.append(roots.quarantine_dir)
    for root in candidates:
        if _inside(root, path):
            return path, ""
    if not candidates:
        return None, "no directory is configured as Volt-owned for this file"
    return None, "outside the directories Volt owns (a registered external file)"


def _unlink(path: Path) -> str:
    """deleted | already_missing. Raises OSError. A directory is never removed here."""
    if path.is_dir() and not path.is_symlink():
        raise OSError("is a directory")
    try:
        path.unlink()
    except FileNotFoundError:
        return "already_missing"
    return "deleted"


def purge_asset(store: Store, asset_id: str, roots: OwnedRoots, allow_versions: bool = False,
                require_tombstone: bool = True) -> PurgeResult:
    """Remove one tombstoned asset: owned files first, database rows last. Idempotent and resumable.

    If any file could not be removed the rows are kept (the asset stays tombstoned) and the next call retries;
    files already gone count as removed. Returns a PurgeResult; it raises only for an unknown asset."""
    asset = store.get_asset(asset_id)
    if require_tombstone and not asset.deleted_at:
        return PurgeResult(asset_id, "skipped", ["not tombstoned"])
    why = blockers(store, asset_id, purge=True, allow_versions=allow_versions)
    if why:
        return PurgeResult(asset_id, "refused", why)

    result = PurgeResult(asset_id, "purged")
    essences = store.conn.execute(
        "SELECT id, role, location FROM essence WHERE asset_id = ? ORDER BY created_at, id", (asset_id,)).fetchall()
    for row in essences:
        path, why_not = _owned_file(roots, asset_id, row["role"], row["location"])
        if path is not None:
            shared = store.conn.execute(
                "SELECT COUNT(*) FROM essence WHERE location = ? AND asset_id <> ?",
                (row["location"], asset_id)).fetchone()[0]
            if shared:
                path, why_not = None, "another asset's essence points at the same file"
        if path is None:
            _audit(store, asset_id, "essence", "left_in_place", row["id"], row["role"], row["location"], why_not)
            result.left += 1
            continue
        try:
            outcome = _unlink(path)
        except OSError as exc:
            _audit(store, asset_id, "essence", "failed", row["id"], row["role"], row["location"], str(exc)[:300])
            result.failed.append(f"{row['role']} {row['location']}: {exc}")
            continue
        _audit(store, asset_id, "essence", outcome, row["id"], row["role"], row["location"])
        result.removed += 1

    if roots.out_dir:
        thumbs = Path(roots.out_dir) / "thumbs" / asset_id
        if thumbs.is_dir() and not thumbs.is_symlink() and _inside(Path(roots.out_dir), thumbs):
            try:
                shutil.rmtree(thumbs)
                _audit(store, asset_id, "thumbs", "deleted", None, None, str(thumbs))
            except OSError as exc:
                _audit(store, asset_id, "thumbs", "failed", None, None, str(thumbs), str(exc)[:300])
                result.failed.append(f"thumbnails {thumbs}: {exc}")

    if allow_versions:
        versions = store.conn.execute(
            "SELECT id, storage_backend, storage_key FROM version WHERE asset_id = ? AND storage_deleted_at IS NULL ORDER BY id",
            (asset_id,)).fetchall()
        for row in versions:
            if not row["storage_key"]:
                continue  # nothing was stored
            location = f"{row['storage_backend']}:{row['storage_key']}"
            try:
                backend = roots.get_backend()
                if backend is None or backend.id != row["storage_backend"]:
                    raise RuntimeError(f"stored in {row['storage_backend']!r}, which is not the configured storage "
                                       f"({getattr(backend, 'id', None)!r})")
                backend.delete(row["storage_key"])
            except Exception as exc:  # noqa: BLE001 - StorageError text is already secret-free
                _audit(store, asset_id, "version", "failed", row["id"], None, location, str(exc)[:300])
                result.failed.append(f"version {location}: {exc}")
                continue
            _audit(store, asset_id, "version", "deleted", row["id"], None, location)
            result.removed += 1

    if result.failed:
        result.status = "partial"
        log.warning("purge of %s incomplete, will retry: %s", asset_id, result.failed[0])
        return result

    with store.transaction(f"asset:{asset_id}"):
        again = blockers(store, asset_id, purge=True, allow_versions=allow_versions)
        if again:  # a hold or an attachment arrived while files were being removed: keep the rows
            return PurgeResult(asset_id, "refused", again, result.removed, result.left)
        c = store.conn
        counts = {}
        for table, sql in (
            ("asr_chunk", "DELETE FROM asr_chunk WHERE job_id IN (SELECT id FROM job WHERE asset_id = ?)"),
            ("asr_run", "DELETE FROM asr_run WHERE asset_id = ?"),
            ("shot_run", "DELETE FROM shot_run WHERE asset_id = ?"),
            ("job", "DELETE FROM job WHERE asset_id = ?"),
            ("version", "DELETE FROM version WHERE asset_id = ?"),
            ("span", "DELETE FROM span WHERE asset_id = ?"),
            ("essence", "DELETE FROM essence WHERE asset_id = ?"),
        ):
            counts[table] = c.execute(sql, (asset_id,)).rowcount
        c.execute("DELETE FROM asset WHERE id = ?", (asset_id,))
        summary = {"owner": asset.owner, "show": asset.show_name, "source_key": asset.source_key,
                   "retention_class": asset.retention_class, "tombstoned_at": asset.deleted_at,
                   "rows_deleted": counts, "files_removed": result.removed, "files_left": result.left}
        c.execute(
            """INSERT INTO purge_audit (id, purged_at, asset_id, kind, ref, role, location, outcome, detail)
               VALUES (?, ?, ?, 'asset', ?, NULL, NULL, 'purged', ?)""",
            (_id(), _now(), asset_id, asset_id, json.dumps(summary, sort_keys=True)))
    log.info("purged asset %s (%d file(s) removed, %d left in place)", asset_id, result.removed, result.left)
    return result


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def due(store: Store, now: Optional[datetime] = None, env=None, window_days: Optional[float] = None) -> list:
    """Ids of tombstoned assets whose retention window has passed. `window_days` overrides the policy."""
    now = now or datetime.now(timezone.utc)
    rows = store.conn.execute(
        "SELECT id, retention_class, deleted_at FROM asset WHERE deleted_at IS NOT NULL ORDER BY deleted_at, id"
    ).fetchall()
    out = []
    for row in rows:
        days = window_days if window_days is not None else retention_days(row["retention_class"], env)
        if _parse(row["deleted_at"]) + timedelta(days=days) <= now:
            out.append(row["id"])
    return out


def purge_due(store: Store, roots: OwnedRoots, now: Optional[datetime] = None, env=None,
              window_days: Optional[float] = None, limit: int = 100, allow_versions: bool = False) -> PurgeReport:
    """Purge every tombstone past its window, up to `limit` per call. One asset failing does not stop the rest."""
    report = PurgeReport()
    for asset_id in due(store, now, env, window_days)[:limit]:
        try:
            report.results.append(purge_asset(store, asset_id, roots, allow_versions=allow_versions))
        except Exception as exc:  # noqa: BLE001 - keep going; the next pass retries
            store.conn.rollback()
            report.results.append(PurgeResult(asset_id, "partial", failed=[str(exc) or exc.__class__.__name__]))
    return report


def tombstones(store: Store) -> list:
    rows = store.conn.execute(
        "SELECT id, owner, show_name, retention_class, deleted_at FROM asset WHERE deleted_at IS NOT NULL "
        "ORDER BY deleted_at, id").fetchall()
    return [{"id": r["id"], "owner": r["owner"], "show": r["show_name"], "retentionClass": r["retention_class"],
             "deletedAt": r["deleted_at"]} for r in rows]


# ---- command line ------------------------------------------------------------------------------

def main(argv: list) -> int:
    import argparse
    from volt import storage
    from volt.store import Store as _Store
    ap = argparse.ArgumentParser(prog="python -m volt.retention",
                                 description="List, restore and purge soft-deleted assets.")
    ap.add_argument("db", nargs="?", default=os.environ.get("VOLT_DB"), help="SQLite path or postgres:// DSN (or VOLT_DB)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show tombstoned assets")
    r = sub.add_parser("restore", help="undo a tombstone")
    r.add_argument("asset")
    p = sub.add_parser("purge", help="purge tombstones past their retention window")
    p.add_argument("--older-than-days", type=float, default=None, help="override the retention window for this run")
    p.add_argument("--asset", default=None, help="purge only this tombstoned asset (still honours the safeguards)")
    p.add_argument("--include-published", action="store_true",
                   help="also delete published versions through the storage backend (VOLT_STORAGE...)")
    p.add_argument("--proxy-dir", type=Path, default=Path(os.environ["VOLT_PROXY_DIR"]) if os.environ.get("VOLT_PROXY_DIR") else None)
    p.add_argument("--quarantine-dir", type=Path, default=Path(os.environ["VOLT_QUARANTINE_DIR"]) if os.environ.get("VOLT_QUARANTINE_DIR") else None)
    p.add_argument("--out-dir", type=Path, default=Path(os.environ["VOLT_OUT"]) if os.environ.get("VOLT_OUT") else None)
    p.add_argument("--publish-dir", type=Path, default=Path(os.environ["VOLT_PUBLISH_DIR"]) if os.environ.get("VOLT_PUBLISH_DIR") else None)
    args = ap.parse_args(argv)
    if not args.db:
        ap.error("a database path or DSN is required")
    store = _Store(args.db)
    try:
        if args.cmd == "list":
            for t in tombstones(store):
                print(f"{t['id']}  deleted {t['deletedAt']}  {t['retentionClass']}  {t['owner']}  {t['show'] or ''}")
            return 0
        if args.cmd == "restore":
            print("restored" if restore(store, args.asset) else "was not deleted")
            return 0
        roots = OwnedRoots(args.proxy_dir, args.quarantine_dir, args.out_dir,
                           backend_factory=lambda: storage.from_env(local_root=args.publish_dir))
        if args.asset:
            report = PurgeReport([purge_asset(store, args.asset, roots, allow_versions=args.include_published)])
        else:
            report = purge_due(store, roots, window_days=args.older_than_days, allow_versions=args.include_published)
        for res in report.results:
            print(f"{res.asset_id} {res.status} {'; '.join(res.reasons + res.failed)}".rstrip())
        print(report)
        return 1 if report.count("partial") else 0
    finally:
        store.close()


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
