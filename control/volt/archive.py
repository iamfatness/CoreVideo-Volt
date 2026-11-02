"""The archive tier (docs/specs/remaining-build, workstream F): move an asset's hi-res essence to a cold object store and back.

Archive is upload, verify, record, then delete the local file, in that order, so there is no moment with zero copies. A crash
at any step is finished by running the job again. Restore asks the store to rehydrate the object, waits, downloads it back to
the essence's own path and checks it against the digest recorded when it went cold.

`essence.tier`: NULL or 'online' (the file is local), 'archiving', 'archived' (only the stored object exists), 'restoring'.
While archived or restoring, the hi-res cannot air, publish, be proxied or be exported (`store.assert_publishable` raises
ArchivedError); the proxy and thumbnails stay local on purpose, so an editor can still browse.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from volt.storage import sha256_file
from volt.store import ArchivedError, Store, _now

COLD = ("archived", "restoring")


class ArchiveRefused(Exception):
    """The request cannot be accepted. `reasons` lists every cause found."""

    def __init__(self, reasons: list):
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


class ArchiveFailed(Exception):
    """The work failed in a way a retry will not fix (missing file, digest mismatch)."""


class Wait(Exception):
    """Not ready yet: the job goes back to the queue without using an attempt."""

    def __init__(self, reason: str, delay: float = 300.0):
        super().__init__(reason)
        self.delay = delay


def hi_res(store: Store, asset_id: str):
    return store.conn.execute("SELECT * FROM essence WHERE asset_id = ? AND role = 'hi-res'", (asset_id,)).fetchone()


def is_cold(store: Store, asset_id: str) -> bool:
    row = hi_res(store, asset_id)
    return row is not None and row["tier"] in COLD


def require_online(store: Store, asset_id: str) -> None:
    """Raise ArchivedError when the asset's hi-res is in the archive: the one check every hi-res reader makes."""
    if is_cold(store, asset_id):
        raise ArchivedError(asset_id)


def request_archive(store: Store, asset_id: str) -> str:
    """Queue the archive job. Returns the job id. Raises KeyError (no such asset) or ArchiveRefused with every reason."""
    asset = store.get_asset(asset_id)
    why = []
    if asset.deleted_at:
        why.append("the asset is deleted")
    if asset.legal_hold:
        why.append("the asset is under legal hold")
    row = hi_res(store, asset_id)
    if row is None:
        why.append("the asset has no hi-res essence to archive")
    else:
        if row["open"]:
            why.append("the asset is still recording")
        if row["tier"] in COLD:
            why.append(f"the hi-res is already {row['tier']}")
    if why:
        raise ArchiveRefused(why)
    return store.enqueue(asset_id, "archive", f"archive:{asset_id}").id


def request_restore(store: Store, asset_id: str) -> str:
    store.get_asset(asset_id)
    row = hi_res(store, asset_id)
    if row is None or row["tier"] not in COLD:
        raise ArchiveRefused(["the hi-res is not archived"])
    return store.enqueue(asset_id, "restore", f"restore:{asset_id}").id


def _key(asset_id: str, row) -> str:
    return f"archive/{asset_id}/{row['id']}{Path(row['location']).suffix}"


def run_archive(store: Store, asset_id: str, backend, cold_class: str) -> None:
    asset = store.get_asset(asset_id)
    if asset.legal_hold:                       # a hold placed after the request still wins
        raise ArchiveRefused(["the asset is under legal hold"])
    row = hi_res(store, asset_id)
    if row is None:
        raise ArchiveFailed("the asset has no hi-res essence")
    path = Path(row["location"])
    if row["tier"] == "archived":              # resume: the record is made; finish the delete if the object is really there
        info = backend.stat(row["archive_key"]) if hasattr(backend, "stat") else None
        if path.is_file() and (info is not None or backend.exists(row["archive_key"])):
            os.remove(path)
        return
    if row["tier"] == "restoring":
        raise Wait("a restore is in progress")
    if row["open"]:
        raise ArchiveFailed("the asset is still recording")
    if not path.is_file():
        raise ArchiveFailed(f"the hi-res file is missing: {path}")
    digest, size = sha256_file(path)
    key = row["archive_key"] or _key(asset_id, row)
    store.conn.execute("UPDATE essence SET tier = 'archiving' WHERE id = ?", (row["id"],))
    store.conn.commit()
    obj = backend.put_file(path, key)          # raises StorageError: the file is untouched, a rerun retries
    if (obj.sha256, obj.size) != (digest, size):
        raise ArchiveFailed("the stored object does not match the file; nothing was removed")
    backend.set_storage_class(key, cold_class)
    with store.transaction():
        store.conn.execute("UPDATE essence SET tier = 'archived', archive_key = ?, archive_sha256 = ?, checksum = COALESCE(checksum, ?) WHERE id = ?",
                           (key, digest, digest, row["id"]))
        store.conn.execute("UPDATE asset SET archived_at = ? WHERE id = ?", (_now(), asset_id))
    os.remove(path)                            # only after the record: a crash here leaves two copies, never none


def run_restore(store: Store, asset_id: str, backend, days: int = 7) -> None:
    row = hi_res(store, asset_id)
    if row is None or row["tier"] not in COLD:
        return                                 # already online (or never archived): idempotent
    key = row["archive_key"]
    state = backend.restore_state(key)
    if state == "archived":
        backend.restore(key, days)
        store.conn.execute("UPDATE essence SET tier = 'restoring' WHERE id = ?", (row["id"],))
        store.conn.commit()
        state = backend.restore_state(key)      # a store with immediate restores is already online
        if state != "online":
            raise Wait("restore requested; waiting for the store")
    elif state == "restoring":
        store.conn.execute("UPDATE essence SET tier = 'restoring' WHERE id = ?", (row["id"],))
        store.conn.commit()
        raise Wait("the store is still restoring the object")
    dest = Path(row["location"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".restore")
    try:
        backend.get_file(key, part)
        digest, _size = sha256_file(part)
        if row["archive_sha256"] and digest != row["archive_sha256"]:
            raise ArchiveFailed("the downloaded file does not match the archived digest; it was not put in place")
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)
    with store.transaction():
        store.conn.execute("UPDATE essence SET tier = 'online' WHERE id = ?", (row["id"],))
        store.conn.execute("UPDATE asset SET archived_at = NULL WHERE id = ?", (asset_id,))


def pick_backend(config_storage, archive_dir: Optional[Path], publish_dir: Optional[Path], env=None):
    """The store archives go to: a dedicated folder (VOLT_ARCHIVE_DIR), else the configured object store (VOLT_STORAGE), else a
    folder under the publish directory. Returns (backend, cold class)."""
    from volt import storage
    env = os.environ if env is None else env
    if archive_dir is not None:
        backend = storage.LocalBackend(archive_dir)
    else:
        backend = config_storage or storage.from_env(local_root=publish_dir)
    return backend, env.get("VOLT_ARCHIVE_CLASS") or getattr(backend, "cold_class", "cold")
