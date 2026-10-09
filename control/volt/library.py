"""Library search. A hit is a span, not a file."""

from __future__ import annotations

from dataclasses import dataclass

from volt.store import Store


@dataclass
class Hit:
    span_id: str
    asset_id: str
    tc_in: str
    tc_out: str
    text: str


def index_words(store: Store, asset_id: str, words: list[tuple[str, str, str]]) -> int:
    store.get_asset(asset_id)
    job = store.enqueue(asset_id, "asr", "asr-v1")
    existing = store.conn.execute(
        "SELECT COUNT(*) FROM span WHERE asset_id = ? AND kind = 'transcript'",
        (asset_id,),
    ).fetchone()[0]
    if existing:
        store.complete_job(job.id)
        return existing
    for text, tc_in, tc_out in words:
        store.add_span(asset_id, "transcript", tc_in, tc_out, text=text)
    store.complete_job(job.id)
    return len(words)


def search(store: Store, query: str) -> list[Hit]:
    needle = query.strip().lower()
    if not needle:
        return []
    rows = store.conn.execute(
        """SELECT id, asset_id, tc_in, tc_out, text
           FROM span
           WHERE kind = 'transcript' AND lower(text) LIKE ?
           ORDER BY tc_in""",
        (f"%{needle}%",),
    ).fetchall()
    return [
        Hit(row["id"], row["asset_id"], row["tc_in"], row["tc_out"], row["text"])
        for row in rows
    ]
