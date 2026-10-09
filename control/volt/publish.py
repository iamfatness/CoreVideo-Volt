"""Publish. One span, three versions, no second asset."""

from __future__ import annotations

from dataclasses import dataclass

from volt.store import Store, _id, _now

KINDS = ("house", "ott", "social")


@dataclass
class Version:
    id: str
    kind: str
    status: str
    url: str | None


def publish(store: Store, span_id: str, fail: str | None = None) -> list[Version]:
    span = store.conn.execute(
        "SELECT asset_id FROM span WHERE id = ?", (span_id,)
    ).fetchone()
    if span is None:
        raise KeyError(span_id)
    store.assert_publishable(span["asset_id"])
    versions = []
    for kind in KINDS:
        job = store.enqueue(span["asset_id"], "publish", f"publish:{span_id}:{kind}")
        existing = store.conn.execute(
            "SELECT id FROM version WHERE asset_id = ? AND span_id = ? AND kind = ?",
            (span["asset_id"], span_id, kind),
        ).fetchone()
        if existing is None:
            version_id = _id()
            store.conn.execute(
                """INSERT INTO version (id, asset_id, span_id, kind, status, url, created_at)
                   VALUES (?, ?, ?, ?, 'queued', NULL, ?)""",
                (version_id, span["asset_id"], span_id, kind, _now()),
            )
        else:
            version_id = existing["id"]
        if fail == kind:
            store.fail_job(job.id, "worker failed")
            store.conn.execute(
                "UPDATE version SET status = 'failed' WHERE id = ?", (version_id,)
            )
            url = None
            status = "failed"
        else:
            url = f"volt://{kind}/{span['asset_id']}/{span_id}"
            store.complete_job(job.id)
            store.conn.execute(
                "UPDATE version SET status = 'done', url = ? WHERE id = ?",
                (url, version_id),
            )
            status = "done"
        versions.append(Version(version_id, kind, status, url))
    store.conn.commit()
    return versions
