"""Saved searches and bins.

A saved search is a named request body (the JSON `searchindex.search` takes) kept for its owner, optionally shared with every
operator. Running one is the ordinary search call with that body, so a saved search can never return more than the caller
could already see. A bin is a static list of assets or clips (an asset plus an optional span) with the same owner and
sharing rules. `bin_member.span_id` has no foreign key: transcription and detection delete and rewrite spans, and a bin must
never block them, so a member whose span has gone is reported with `gone: true`.

Only the owner or an admin changes or deletes one. What someone else has not shared is invisible to them (not_found), not
merely read-only.
"""

from __future__ import annotations

import json
from typing import Optional

from volt import searchindex
from volt.store import Store, _id, _now

MAX_SEARCHES = 200       # per owner
MAX_BINS = 200           # per owner
MAX_MEMBERS = 5000       # per bin
MAX_NAME = 100
MAX_QUERY = 8192         # characters of stored JSON


class CollectionError(Exception):
    """A refusal. `code` is stable, `reason` is for people, `status` is the HTTP status the API uses."""

    _STATUS = {"not_found": 404, "forbidden_role": 403, "bin_full": 409}

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code, self.reason = code, reason
        self.status = self._STATUS.get(code, 422)


def _bad(reason: str) -> CollectionError:
    return CollectionError("bad_input", reason)


def _name(name) -> str:
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME or "\x00" in name:
        raise _bad(f"name must be 1 to {MAX_NAME} characters")
    return name.strip()


def _shared(shared) -> bool:
    if not isinstance(shared, bool):
        raise _bad("shared must be true or false")
    return shared


# ---- saved searches --------------------------------------------------------------------------------------

def _search_json(row) -> dict:
    return {"id": row["id"], "name": row["name"], "owner": row["owner"], "shared": bool(row["shared"]),
            "query": json.loads(row["query"]), "createdAt": row["created_at"], "updatedAt": row["updated_at"]}


def save_search(store: Store, owner: str, name, query, shared) -> dict:
    name, shared = _name(name), _shared(shared)
    if not isinstance(query, dict):
        raise _bad("query must be a search request object")
    if "after" in query:
        raise _bad("a saved search is a question, not a page: leave out 'after'")
    try:
        searchindex.validate_request(store, query)
    except searchindex.SearchError as exc:
        raise _bad(f"query: {exc.reason}") from None
    text = json.dumps(query, sort_keys=True)
    if len(text) > MAX_QUERY:
        raise _bad(f"query is over {MAX_QUERY} characters")
    with store.transaction(f"searches:{owner}"):
        if store.conn.execute("SELECT COUNT(*) FROM saved_search WHERE owner = ?", (owner,)).fetchone()[0] >= MAX_SEARCHES:
            raise _bad(f"at most {MAX_SEARCHES} saved searches each; delete one first")
        sid, now = _id(), _now()
        store.conn.execute(
            "INSERT INTO saved_search (id, name, owner, shared, query, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, name, owner, shared, text, now, now))
    return _search_json(store.conn.execute("SELECT * FROM saved_search WHERE id = ?", (sid,)).fetchone())


def list_searches(store: Store, actor: str) -> list:
    """The caller's own saved searches and every shared one, newest first."""
    rows = store.conn.execute(
        "SELECT * FROM saved_search WHERE owner = ? OR shared = ? ORDER BY created_at DESC, id", (actor, True)).fetchall()
    return [_search_json(r) for r in rows]


def delete_search(store: Store, search_id: str, actor: str, is_admin: bool) -> None:
    with store.transaction(f"search:{search_id}"):
        row = store.conn.execute("SELECT owner, shared FROM saved_search WHERE id = ?", (search_id,)).fetchone()
        if row is None or not (row["owner"] == actor or row["shared"]):
            raise CollectionError("not_found", "no such saved search")
        if row["owner"] != actor and not is_admin:
            raise CollectionError("forbidden_role", "only its owner or an admin can delete a shared search")
        store.conn.execute("DELETE FROM saved_search WHERE id = ?", (search_id,))


# ---- bins ------------------------------------------------------------------------------------------------

def _count(store: Store, bin_id: str) -> int:
    return store.conn.execute("SELECT COUNT(*) FROM bin_member WHERE bin_id = ?", (bin_id,)).fetchone()[0]


def _bin_json(store: Store, row) -> dict:
    return {"id": row["id"], "name": row["name"], "owner": row["owner"], "shared": bool(row["shared"]),
            "createdAt": row["created_at"], "count": _count(store, row["id"])}


def _visible(store: Store, bin_id, actor: str):
    row = store.conn.execute("SELECT * FROM bin WHERE id = ?", (bin_id,)).fetchone() if isinstance(bin_id, str) else None
    if row is None or not (row["owner"] == actor or row["shared"]):
        raise CollectionError("not_found", "no such bin")
    return row


def _editable(store: Store, bin_id, actor: str, is_admin: bool):
    row = _visible(store, bin_id, actor)
    if row["owner"] != actor and not is_admin:
        raise CollectionError("forbidden_role", "only its owner or an admin can change this bin")
    return row


def create_bin(store: Store, owner: str, name, shared) -> dict:
    name, shared = _name(name), _shared(shared)
    with store.transaction(f"bins:{owner}"):
        if store.conn.execute("SELECT COUNT(*) FROM bin WHERE owner = ?", (owner,)).fetchone()[0] >= MAX_BINS:
            raise _bad(f"at most {MAX_BINS} bins each; delete one first")
        bin_id = _id()
        store.conn.execute("INSERT INTO bin (id, name, owner, shared, created_at) VALUES (?, ?, ?, ?, ?)",
                           (bin_id, name, owner, shared, _now()))
    return _bin_json(store, store.conn.execute("SELECT * FROM bin WHERE id = ?", (bin_id,)).fetchone())


def list_bins(store: Store, actor: str) -> list:
    rows = store.conn.execute("SELECT * FROM bin WHERE owner = ? OR shared = ? ORDER BY created_at DESC, id", (actor, True)).fetchall()
    return [_bin_json(store, r) for r in rows]


def get_bin(store: Store, bin_id, actor: str, is_admin: bool = False) -> dict:
    return _bin_json(store, _visible(store, bin_id, actor))


def members(store: Store, bin_id: str) -> list:
    rows = store.conn.execute(
        """SELECT m.asset_id, m.span_id, m.position, m.added_by, m.added_at, s.id AS sid, s.tc_in, s.tc_out, s.label, s.kind,
                  a.deleted_at
           FROM bin_member m LEFT JOIN span s ON s.id = m.span_id LEFT JOIN asset a ON a.id = m.asset_id
           WHERE m.bin_id = ? ORDER BY m.position""", (bin_id,)).fetchall()
    return [{"assetId": r["asset_id"], "spanId": r["span_id"], "position": r["position"], "addedBy": r["added_by"],
             "addedAt": r["added_at"], "gone": r["span_id"] is not None and r["sid"] is None, "kind": r["kind"],
             "label": r["label"], "tcIn": r["tc_in"], "tcOut": r["tc_out"], "assetDeleted": r["deleted_at"] is not None}
            for r in rows]


def _member_row(store: Store, bin_id: str, asset_id: str, span_id: Optional[str]):
    sql = "SELECT position FROM bin_member WHERE bin_id = ? AND asset_id = ? AND span_id "
    if span_id is None:
        return store.conn.execute(sql + "IS NULL", (bin_id, asset_id)).fetchone()
    return store.conn.execute(sql + "= ?", (bin_id, asset_id, span_id)).fetchone()


def add_member(store: Store, bin_id, asset_id, span_id, actor: str, is_admin: bool = False) -> dict:
    """Put an asset (or one of its spans) in a bin. Adding what is already there is not an error: `added` says which happened."""
    if not isinstance(asset_id, str) or not asset_id:
        raise _bad("assetId is required")
    if span_id is not None and not isinstance(span_id, str):
        raise _bad("spanId must be a span id or left out")
    with store.transaction(f"bin:{bin_id}"):
        _editable(store, bin_id, actor, is_admin)
        asset = store.conn.execute("SELECT deleted_at FROM asset WHERE id = ?", (asset_id,)).fetchone()
        if asset is None or asset["deleted_at"] is not None:
            raise CollectionError("not_found", f"no such asset: {asset_id}")
        if span_id is not None and store.conn.execute(
                "SELECT 1 FROM span WHERE id = ? AND asset_id = ?", (span_id, asset_id)).fetchone() is None:
            raise _bad("that span is not on that asset")
        have = _member_row(store, bin_id, asset_id, span_id)
        if have is not None:
            return {"added": False, "member": _one(store, bin_id, asset_id, span_id)}
        if _count(store, bin_id) >= MAX_MEMBERS:
            raise CollectionError("bin_full", f"a bin holds at most {MAX_MEMBERS} items")
        position = store.conn.execute("SELECT COALESCE(MAX(position), 0) FROM bin_member WHERE bin_id = ?", (bin_id,)).fetchone()[0] + 1
        store.conn.execute(
            "INSERT INTO bin_member (bin_id, asset_id, span_id, position, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?)",
            (bin_id, asset_id, span_id, position, actor, _now()))
    return {"added": True, "member": _one(store, bin_id, asset_id, span_id)}


def _one(store: Store, bin_id: str, asset_id: str, span_id: Optional[str]) -> dict:
    return next(m for m in members(store, bin_id) if m["assetId"] == asset_id and m["spanId"] == span_id)


def remove_member(store: Store, bin_id, asset_id, span_id, actor: str, is_admin: bool = False) -> bool:
    """Take an item out of a bin. The asset is untouched. False when it was not there."""
    if not isinstance(asset_id, str) or not asset_id:
        raise _bad("assetId is required")
    if span_id is not None and not isinstance(span_id, str):
        raise _bad("spanId must be a span id or left out")
    with store.transaction(f"bin:{bin_id}"):
        _editable(store, bin_id, actor, is_admin)
        if span_id is None:
            cur = store.conn.execute("DELETE FROM bin_member WHERE bin_id = ? AND asset_id = ? AND span_id IS NULL", (bin_id, asset_id))
        else:
            cur = store.conn.execute("DELETE FROM bin_member WHERE bin_id = ? AND asset_id = ? AND span_id = ?", (bin_id, asset_id, span_id))
        return cur.rowcount > 0


def delete_bin(store: Store, bin_id, actor: str, is_admin: bool = False) -> None:
    with store.transaction(f"bin:{bin_id}"):
        _editable(store, bin_id, actor, is_admin)
        store.conn.execute("DELETE FROM bin_member WHERE bin_id = ?", (bin_id,))
        store.conn.execute("DELETE FROM bin WHERE id = ?", (bin_id,))
