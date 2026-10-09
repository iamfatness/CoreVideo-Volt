"""Session shell. One list, one search, a hit that seeks the proxy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from volt.library import search
from volt.publish import publish
from volt.rundown import attach
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


@dataclass
class ItemRow:
    id: str
    slug: str
    status: str
    span_id: Optional[str]
    urls: list[str]


def rundown(store: Store, show_id: str) -> list[ItemRow]:
    rows = store.conn.execute(
        """SELECT i.id, i.slug, i.status, i.span_id
           FROM item i WHERE i.show_id = ? ORDER BY i.position""",
        (show_id,),
    ).fetchall()
    items = []
    for row in rows:
        urls = []
        if row["span_id"]:
            urls = [
                hit["url"]
                for hit in store.conn.execute(
                    "SELECT url FROM version WHERE span_id = ? AND url IS NOT NULL ORDER BY kind",
                    (row["span_id"],),
                ).fetchall()
            ]
        items.append(ItemRow(row["id"], row["slug"], row["status"], row["span_id"], urls))
    return items


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


def attach_hit(store: Store, item_id: str, query: str) -> str:
    hit = search(store, query)
    if not hit:
        raise KeyError(query)
    attach(store, item_id, hit[0].span_id)
    return hit[0].span_id


def publish_item(store: Store, item_id: str) -> list[str]:
    row = store.conn.execute("SELECT span_id FROM item WHERE id = ?", (item_id,)).fetchone()
    if row is None or row["span_id"] is None:
        raise KeyError(item_id)
    return [version.url for version in publish(store, row["span_id"]) if version.url]
