"""Publish. One span, three versions, no second asset."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from volt import deliverables
from volt.deliverables import DeliverableError
from volt.store import Store, _id, _now

KINDS = ("house", "ott", "social")


@dataclass
class Version:
    id: str
    kind: str
    status: str
    url: str | None


def publish(
    store: Store,
    span_id: str,
    fail: str | None = None,
    renderer: Optional[Callable[[Store, object, str], str]] = None,
) -> list[Version]:
    span = store.conn.execute(
        "SELECT id, asset_id, tc_in, tc_out FROM span WHERE id = ?", (span_id,)
    ).fetchone()
    if span is None:
        raise KeyError(span_id)
    store.assert_publishable(span["asset_id"])
    versions = []
    for kind in KINDS:
        existing = store.conn.execute(
            "SELECT id, status, url FROM version WHERE asset_id = ? AND span_id = ? AND kind = ?",
            (span["asset_id"], span_id, kind),
        ).fetchone()
        if renderer is not None and existing is not None and existing["status"] == "done" and existing["url"]:
            versions.append(Version(existing["id"], kind, "done", existing["url"]))
            continue
        job = store.enqueue(span["asset_id"], "publish", f"publish:{span_id}:{kind}")
        if existing is None:
            version_id = _id()
            store.conn.execute(
                """INSERT INTO version (id, asset_id, span_id, kind, status, url, created_at)
                   VALUES (?, ?, ?, ?, 'queued', NULL, ?)""",
                (version_id, span["asset_id"], span_id, kind, _now()),
            )
        else:
            version_id = existing["id"]
        error = "worker failed" if fail == kind else None
        url = None
        if error is None and renderer is not None:
            try:
                url = renderer(store, span, kind)
            except DeliverableError as exc:
                error = str(exc) or "render failed"
        if error is not None:
            store.fail_job(job.id, error)
            store.conn.execute(
                "UPDATE version SET status = 'failed' WHERE id = ?", (version_id,)
            )
            url = None
            status = "failed"
        else:
            url = url or f"volt://{kind}/{span['asset_id']}/{span_id}"
            store.complete_job(job.id)
            store.conn.execute(
                "UPDATE version SET status = 'done', url = ? WHERE id = ?",
                (url, version_id),
            )
            status = "done"
        versions.append(Version(version_id, kind, status, url))
    store.conn.commit()
    return versions


def publish_files(store: Store, span_id: str, out_dir: Path) -> list[Version]:
    """Publish with real cut files written under out_dir. Done versions are not re-rendered."""
    return publish(
        store, span_id,
        renderer=lambda st, span, kind: deliverables.render(st, span, kind, out_dir),
    )
