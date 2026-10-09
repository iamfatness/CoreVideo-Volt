"""Session shell. One list, one search, a hit that seeks the proxy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from volt.library import search
from volt.store import Store


@dataclass
class AssetRow:
    id: str
    show_name: Optional[str]
    camera: Optional[str]
    open: bool


@dataclass
class Seek:
    asset_id: str
    span_id: str
    tc_in: str
    text: str
    proxy: str


def list_assets(store: Store) -> list[AssetRow]:
    rows = store.conn.execute(
        """SELECT a.id, a.show_name, a.camera,
                  COALESCE(MAX(e.open), 0) AS open
           FROM asset a
           LEFT JOIN essence e ON e.asset_id = a.id AND e.role = 'hi-res'
           GROUP BY a.id
           ORDER BY a.created_at"""
    ).fetchall()
    return [AssetRow(row["id"], row["show_name"], row["camera"], bool(row["open"])) for row in rows]


def seek_hit(store: Store, query: str) -> Optional[Seek]:
    hits = search(store, query)
    if not hits:
        return None
    hit = hits[0]
    proxy = store.conn.execute(
        "SELECT location FROM essence WHERE asset_id = ? AND role = 'proxy'",
        (hit.asset_id,),
    ).fetchone()
    if proxy is None:
        return None
    return Seek(hit.asset_id, hit.span_id, hit.tc_in, hit.text, proxy["location"])
