"""Wire intake: feeds into an inbox, and a person turning an inbox entry into a draft story.

docs/superpowers/specs/2026-11-01-newsroom-scripts-design.md ("Wire intake"). Rules this module keeps:

* Feed locations come from server configuration (VOLT_WIRE_FEEDS), never from a request. Nothing configured means nothing fetched.
* Parsers are pure functions of bytes. XML with a DOCTYPE or an ENTITY is refused before the standard library sees it (its parser
  is not hardened against entity expansion). Bodies that carry HTML are reduced to text. Everything is size-capped.
* A fetch or parse failure is recorded on the feed and never raised: one bad feed does not stop the others, and nothing here blocks
  the server or the runner's other jobs (the fetch has a timeout and a size cap).
* Nothing from a wire reaches a rundown without a person calling take(); a taken story is a draft, revision 1, with no approval,
  and its text is never a cue.

Formats (fixtures are written from general knowledge of them, and are unvalidated against any real agency): RSS 2.0, Atom, a minimal
NewsML-G2-style subset (headline, byline, body text, version time) and JSON through a configured field mapping.
"""

from __future__ import annotations

import hashlib
import html.parser
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Optional

from volt.rundown import show_lock
from volt.store import Store, _id

KINDS = ("rss", "atom", "newsml", "json")
MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 200            # newest-first feeds are cut here; the rest of a very long feed is ignored
MAX_HEADLINE, MAX_BODY, MAX_BYLINE = 500, 20000, 200
FETCH_TIMEOUT = 10.0
OK_EVERY = timedelta(seconds=60)
ERROR_EVERY = timedelta(seconds=300)
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


class WireError(Exception):
    """A feed that cannot be used. `code` is stable; the reason never contains the feed's address."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code, self.reason = code, reason


class WireRefusal(Exception):
    """A request that cannot be done (take or dismiss). Maps to HTTP in serve.py."""

    def __init__(self, status: int, code: str, reason: str):
        super().__init__(reason)
        self.status, self.code, self.reason = status, code, reason


@dataclass(frozen=True)
class WireStory:
    external_id: str
    headline: str
    body: str
    byline: str
    published_at: Optional[str]      # UTC `YYYY-MM-DDTHH:MM:SSZ`, or None when the feed gave no usable time


@dataclass(frozen=True)
class Feed:
    name: str
    kind: str
    url: str
    mapping: Optional[dict] = None


# ---- text helpers -----------------------------------------------------------------------------------------

class _TextOut(html.parser.HTMLParser):
    _BREAKS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section", "article"}
    _SKIP = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BREAKS and tag != "br":     # a <br/> arrives as a start and an end: one line break, not two
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def to_text(value: Optional[str]) -> str:
    """Reduce a body that may carry HTML to plain text: tags dropped, script and style content dropped, entities decoded, block
    elements become line breaks. Text with no markup comes back with only its whitespace tidied."""
    raw = value or ""
    if "<" in raw or "&" in raw:
        out = _TextOut()
        try:
            out.feed(raw)
            out.close()
            raw = "".join(out.parts)
        except Exception:           # malformed beyond repair: keep the text and strip anything tag-like
            raw = re.sub(r"<[^>]*>", " ", raw)
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out_lines, blank = [], False
    for line in lines:
        if line:
            out_lines.append(line)
            blank = False
        elif out_lines and not blank:
            out_lines.append("")
            blank = True
    return "\n".join(out_lines).strip()


def _clip(value: Optional[str], limit: int) -> str:
    return (value or "").strip()[:limit]


def _utc(value) -> Optional[str]:
    """A feed time as UTC text, or None. Accepts RFC 822 (RSS) and ISO 8601 (Atom, NewsML, JSON); a time with no zone is read as UTC."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    moment = None
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _story(external_id, headline, body, byline, published) -> Optional[WireStory]:
    headline = _clip(to_text(headline).replace("\n", " "), MAX_HEADLINE)
    external = _clip(external_id, 300)
    if not headline:
        return None
    if not external:
        external = "h:" + hashlib.sha256((headline + "|" + str(published or "")).encode("utf-8")).hexdigest()[:24]
    return WireStory(external, headline, _clip(to_text(body), MAX_BODY), _clip(to_text(byline).replace("\n", " "), MAX_BYLINE), _utc(published))


# ---- XML --------------------------------------------------------------------------------------------------

def _xml_root(data: bytes) -> ET.Element:
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_FEED_BYTES:
        raise WireError("too_large", "the feed is larger than the limit")
    if b"\x00" in data:
        raise WireError("bad_encoding", "the feed is not UTF-8 text")
    low = bytes(data).lower()
    if b"<!doctype" in low or b"<!entity" in low:
        raise WireError("doctype_refused", "the feed carries a DOCTYPE or ENTITY declaration, which is refused")
    try:
        return ET.fromstring(bytes(data))
    except ET.ParseError:
        raise WireError("bad_xml", "the feed is not well-formed XML") from None


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(node: ET.Element, name: str) -> list:
    return [c for c in node if _local(c.tag) == name]


def _first(node: ET.Element, *names: str) -> Optional[ET.Element]:
    for name in names:
        found = _children(node, name)
        if found:
            return found[0]
    return None


def _text(node: Optional[ET.Element]) -> str:
    return "".join(node.itertext()) if node is not None else ""


def parse_rss(data: bytes) -> list:
    """RSS 2.0: channel/item with title, guid (else link), description or content:encoded, author or dc:creator, pubDate."""
    root = _xml_root(data)
    out = []
    for item in [e for e in root.iter() if _local(e.tag) == "item"][:MAX_ITEMS]:
        ident = _text(_first(item, "guid")) or _text(_first(item, "link"))
        body = _text(_first(item, "encoded")) or _text(_first(item, "description"))
        s = _story(ident, _text(_first(item, "title")), body, _text(_first(item, "creator", "author")), _text(_first(item, "pubDate", "date")))
        if s:
            out.append(s)
    return out


def parse_atom(data: bytes) -> list:
    """Atom: entry with id, title, content (else summary), author/name, published (else updated)."""
    root = _xml_root(data)
    out = []
    for entry in [e for e in root.iter() if _local(e.tag) == "entry"][:MAX_ITEMS]:
        author = _first(entry, "author")
        s = _story(_text(_first(entry, "id")), _text(_first(entry, "title")), _text(_first(entry, "content")) or _text(_first(entry, "summary")),
                   _text(_first(author, "name")) if author is not None else "", _text(_first(entry, "published", "updated")))
        if s:
            out.append(s)
    return out


def parse_newsml(data: bytes) -> list:
    """A minimal NewsML-G2-style subset: newsItem (guid attribute), contentMeta/headline, contentMeta/byline, the text under
    contentSet/inlineXML (else a body element), itemMeta/versionCreated. Anything else in the file is ignored."""
    root = _xml_root(data)
    out = []
    for item in [e for e in root.iter() if _local(e.tag) == "newsItem"][:MAX_ITEMS]:
        meta, content, imeta = _first(item, "contentMeta"), _first(item, "contentSet"), _first(item, "itemMeta")
        headline = _text(_first(meta, "headline")) if meta is not None else ""
        byline = _text(_first(meta, "byline")) if meta is not None else ""
        body = ""
        if content is not None:
            inline = _first(content, "inlineXML")
            node = inline if inline is not None else content
            bodies = [e for e in node.iter() if _local(e.tag) == "body"]
            body = "\n".join(_text(_paragraphs(b)) for b in bodies) if bodies else _text(node)
        created = _text(_first(imeta, "versionCreated")) if imeta is not None else ""
        s = _story(item.get("guid") or "", headline, body, byline, created)
        if s:
            out.append(s)
    return out


def _paragraphs(node: ET.Element) -> ET.Element:
    """Keep paragraph breaks when a body's text is flattened: a copy with a newline after each p, div and br."""
    for e in node.iter():
        if _local(e.tag) in ("p", "div", "br", "li"):
            e.tail = "\n" + (e.tail or "")
    return node


# ---- JSON through a mapping ---------------------------------------------------------------------------------

def _path(value, path: str):
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def check_mapping(mapping) -> dict:
    """{items, externalId, headline, body, byline, publishedAt}: dotted paths into the feed's JSON. `items` names the list ('' means
    the document is the list); headline and externalId are required."""
    if not isinstance(mapping, dict):
        raise ValueError("a JSON feed needs a mapping object")
    allowed = {"items", "externalId", "headline", "body", "byline", "publishedAt"}
    extra = sorted(set(mapping) - allowed)
    if extra:
        raise ValueError(f"unknown mapping key(s): {', '.join(extra)}")
    for key in ("externalId", "headline"):
        if not isinstance(mapping.get(key), str) or not mapping[key]:
            raise ValueError(f"mapping needs {key}")
    if any(not isinstance(v, str) for v in mapping.values()):
        raise ValueError("mapping values must be strings")
    return mapping


def parse_json(data: bytes, mapping: dict) -> list:
    check_mapping(mapping)
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_FEED_BYTES:
        raise WireError("too_large", "the feed is larger than the limit")
    try:
        doc = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WireError("bad_json", "the feed is not valid JSON") from None
    rows = _path(doc, mapping.get("items", ""))
    if not isinstance(rows, list):
        raise WireError("bad_json", "the mapping's items path does not lead to a list")
    out = []
    for row in rows[:MAX_ITEMS]:
        def get(key):
            v = _path(row, mapping.get(key, "")) if mapping.get(key) else None
            return v if isinstance(v, (str, int, float)) and not isinstance(v, bool) else None
        ident = get("externalId")
        s = _story("" if ident is None else str(ident), "" if get("headline") is None else str(get("headline")),
                   "" if get("body") is None else str(get("body")), "" if get("byline") is None else str(get("byline")), get("publishedAt"))
        if s:
            out.append(s)
    return out


def parse(feed: Feed, data: bytes) -> list:
    if feed.kind == "rss":
        return parse_rss(data)
    if feed.kind == "atom":
        return parse_atom(data)
    if feed.kind == "newsml":
        return parse_newsml(data)
    return parse_json(data, feed.mapping or {})


# ---- configuration ----------------------------------------------------------------------------------------

def feeds_from_env(environ) -> list:
    """VOLT_WIRE_FEEDS is a list of `name=kind@url` (kinds rss, atom, newsml, json) separated by whitespace or semicolons; a JSON feed's
    mapping comes from the file VOLT_WIRE_MAPPINGS names ({feed name: mapping}). Nothing set means no feeds. A bad entry raises
    ValueError naming the entry's number and never printing its address."""
    raw = (environ.get("VOLT_WIRE_FEEDS") or "").strip()
    if not raw:
        return []
    mappings = {}
    path = environ.get("VOLT_WIRE_MAPPINGS")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                mappings = json.load(fh)
        except (OSError, ValueError):
            raise ValueError("VOLT_WIRE_MAPPINGS cannot be read as JSON") from None
        if not isinstance(mappings, dict):
            raise ValueError("VOLT_WIRE_MAPPINGS must hold an object of feed name to mapping")
    feeds, seen = [], set()
    for n, entry in enumerate([e for e in re.split(r"[\s;]+", raw) if e], 1):
        m = re.fullmatch(r"([^=@]+)=([a-z]+)@(.+)", entry)
        if not m or not _NAME.match(m.group(1)) or m.group(2) not in KINDS:
            raise ValueError(f"VOLT_WIRE_FEEDS entry {n} is not name=kind@url with kind one of {', '.join(KINDS)}")
        name, kind, url = m.groups()
        if urllib.parse.urlparse(url).scheme not in ("http", "https") or name in seen:
            raise ValueError(f"VOLT_WIRE_FEEDS entry {n} needs an http or https address and a name used once")
        mapping = None
        if kind == "json":
            try:
                mapping = check_mapping(mappings.get(name))
            except ValueError as exc:
                raise ValueError(f"VOLT_WIRE_FEEDS entry {n} ({name}): {exc}") from None
        seen.add(name)
        feeds.append(Feed(name, kind, url, mapping))
    return feeds


# ---- fetching ---------------------------------------------------------------------------------------------

class _HttpOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme not in ("http", "https"):
            raise urllib.error.URLError("redirect to a non-http address refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, timeout: float = FETCH_TIMEOUT, max_bytes: int = MAX_FEED_BYTES) -> bytes:
    """GET `url` (http or https only, redirects stay on http or https) with a timeout and a size cap."""
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise WireError("bad_url", "only http and https feeds are fetched")
    opener = urllib.request.build_opener(_HttpOnly)
    req = urllib.request.Request(url, headers={"User-Agent": "volt-wire/1", "Accept": "*/*"})
    with opener.open(req, timeout=timeout) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise WireError("too_large", "the feed is larger than the limit")
    return data


# ---- polling ----------------------------------------------------------------------------------------------

def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scrub(message: str, feed: Feed) -> str:
    text = message.replace(feed.url, "<feed address>")
    return re.sub(r"https?://\S+", "<address>", text)[:300]


def upsert(store: Store, feed: Feed, stories: list, received: datetime) -> int:
    """Insert new stories; update a story still `new`; leave a `taken` or `dismissed` one alone (a taken story was copied at take time).
    Returns how many rows were inserted or updated."""
    changed = 0
    with store.transaction():
        for s in stories:
            row = store.conn.execute("SELECT id, state FROM wire_story WHERE feed = ? AND external_id = ?", (feed.name, s.external_id)).fetchone()
            if row is None:
                store.conn.execute(
                    "INSERT INTO wire_story (id, feed, external_id, headline, body, byline, published_at, received_at, state, item_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', NULL)",
                    (_id(), feed.name, s.external_id, s.headline, s.body, s.byline, s.published_at, _stamp(received)))
                changed += 1
            elif row["state"] == "new":
                store.conn.execute(
                    "UPDATE wire_story SET headline = ?, body = ?, byline = ?, published_at = ? WHERE id = ? AND state = 'new'",
                    (s.headline, s.body, s.byline, s.published_at, row["id"]))
                changed += 1
    return changed


def _record(store: Store, feed: Feed, now: datetime, ok: bool, error: Optional[str]) -> None:
    nxt = _stamp(now + (OK_EVERY if ok else ERROR_EVERY))
    with store.transaction():
        if store.conn.execute("SELECT 1 FROM wire_feed WHERE feed = ?", (feed.name,)).fetchone() is None:
            store.conn.execute("INSERT INTO wire_feed (feed, kind, last_attempt_at, last_ok_at, last_error, next_attempt_at) VALUES (?, ?, ?, ?, ?, ?)",
                               (feed.name, feed.kind, _stamp(now), _stamp(now) if ok else None, error, nxt))
        elif ok:
            store.conn.execute("UPDATE wire_feed SET kind = ?, last_attempt_at = ?, last_ok_at = ?, last_error = NULL, next_attempt_at = ? WHERE feed = ?",
                               (feed.kind, _stamp(now), _stamp(now), nxt, feed.name))
        else:
            store.conn.execute("UPDATE wire_feed SET kind = ?, last_attempt_at = ?, last_error = ?, next_attempt_at = ? WHERE feed = ?",
                               (feed.kind, _stamp(now), error, nxt, feed.name))


def poll(store: Store, feeds: list, fetch_fn: Callable[[str, float, int], bytes], now: datetime) -> dict:
    """Fetch every feed that is due, parse it, upsert its stories. `fetch_fn(url, timeout, max_bytes) -> bytes` is injected. Never raises
    for a feed's failure: it is recorded on the feed (last_error, next attempt in 5 minutes) and the next feed goes on. Returns
    {feed name: new or updated count, or the error text}."""
    report = {}
    for feed in feeds:
        row = store.conn.execute("SELECT next_attempt_at FROM wire_feed WHERE feed = ?", (feed.name,)).fetchone()
        if row is not None and row["next_attempt_at"] is not None:
            due = row["next_attempt_at"]
            due = _stamp(due) if isinstance(due, datetime) else str(due)
            if due > _stamp(now):
                continue
        try:
            data = fetch_fn(feed.url, FETCH_TIMEOUT, MAX_FEED_BYTES)
            stories = parse(feed, data)
            report[feed.name] = upsert(store, feed, stories, now)
            _record(store, feed, now, True, None)
        except WireError as exc:
            report[feed.name] = exc.code
            _record(store, feed, now, False, exc.code + ": " + exc.reason)
        except Exception as exc:        # a network error, a timeout, a bug in one parser: this feed only
            msg = f"{type(exc).__name__}: {_scrub(str(exc), feed)}"
            report[feed.name] = "error"
            _record(store, feed, now, False, msg)
    return report


# ---- the inbox and the human step -------------------------------------------------------------------------

def _iso(value) -> Optional[str]:
    if value is None:
        return None
    return _stamp(value) if isinstance(value, datetime) else str(value)


def list_inbox(store: Store, state: str = "new", limit: int = 50, cursor: Optional[str] = None) -> dict:
    """Newest first, keyset-paged. `cursor` is the `next` of the previous page. Bodies are not listed, only a snippet."""
    if state not in ("new", "taken", "dismissed", "all"):
        raise WireRefusal(422, "bad_input", "state must be new, taken, dismissed or all")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise WireRefusal(422, "bad_input", "limit must be a whole number from 1 to 200")
    sql, params = ("SELECT id, feed, headline, body, byline, published_at, received_at, state, item_id FROM wire_story WHERE 1 = 1", [])
    if state != "all":
        sql += " AND state = ?"
        params.append(state)
    if cursor:
        at, _, ident = cursor.partition("|")
        if not at or not ident:
            raise WireRefusal(422, "bad_input", "cursor is not one this server gave")
        sql += " AND (received_at < ? OR (received_at = ? AND id < ?))"
        params += [at, at, ident]
    rows = store.conn.execute(sql + " ORDER BY received_at DESC, id DESC LIMIT ?", (*params, limit + 1)).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    stories = [{"id": r["id"], "feed": r["feed"], "headline": r["headline"], "snippet": (r["body"] or "")[:280], "byline": r["byline"] or "",
                "publishedAt": _iso(r["published_at"]), "receivedAt": _iso(r["received_at"]), "state": r["state"], "itemId": r["item_id"]} for r in rows]
    feeds = [{"feed": f["feed"], "kind": f["kind"], "lastOkAt": _iso(f["last_ok_at"]), "lastError": f["last_error"], "nextAttemptAt": _iso(f["next_attempt_at"])}
             for f in store.conn.execute("SELECT feed, kind, last_ok_at, last_error, next_attempt_at FROM wire_feed ORDER BY feed").fetchall()]
    return {"ok": True, "stories": stories, "feeds": feeds, "next": f"{_iso(rows[-1]['received_at'])}|{rows[-1]['id']}" if more else None}


def _source_line(feed: str, byline: str, published: Optional[str]) -> str:
    """The first line of a taken story: an instruction (display only) naming where the copy came from."""
    bits = [re.sub(r"[\[\]\r\n]+", " ", b).strip() for b in (f"WIRE {feed}", byline, published or "") if b]
    return "[" + " - ".join(b for b in bits if b) + "]"


def _body_for_script(body: str) -> str:
    """Wire text becomes the script's read text. A line that would be read as a private note (starting //) is kept readable."""
    return "\n".join(("/ /" + line.strip()[2:]) if line.strip().startswith("//") else line for line in (body or "").split("\n"))


def take(store: Store, wire_id: str, show_id: str, slug: Optional[str], position: Optional[int], actor: str) -> dict:
    """A person turns an inbox entry into a draft story at the end of (or at `position` in) a show. One transaction: the wire row moves
    from `new` to `taken` by a conditional update, so two people taking at once make one item and the second gets already_taken.
    Returns {item, placed}."""
    from volt import building, story
    if position is not None and (isinstance(position, bool) or not isinstance(position, int) or position < 1):
        raise WireRefusal(422, "bad_input", "position must be a whole number from 1")
    if slug is not None and (not isinstance(slug, str) or not slug.strip() or len(slug.strip()) > story.MAX_SLUG):
        raise WireRefusal(422, "bad_input", f"slug must be a non-empty string of at most {story.MAX_SLUG} characters")
    with store.transaction(show_lock(show_id)):
        wire = store.conn.execute("SELECT id, feed, headline, body, byline, published_at, state FROM wire_story WHERE id = ?", (wire_id,)).fetchone()
        if wire is None:
            raise WireRefusal(404, "not_found", f"no such wire story: {wire_id}")
        if store.conn.execute("SELECT 1 FROM show WHERE id = ?", (show_id,)).fetchone() is None:
            raise WireRefusal(404, "not_found", f"no such show: {show_id}")
        if wire["state"] != "new":
            raise WireRefusal(409, "already_taken", f"this wire story is already {wire['state']}")
        text = _source_line(wire["feed"], wire["byline"] or "", _iso(wire["published_at"])) + "\n" + _body_for_script(wire["body"] or "")
        item_id, _ = building.add_item(store, show_id, (slug or wire["headline"]).strip()[:story.MAX_SLUG], text[:story.MAX_SCRIPT])
        cur = store.conn.execute("UPDATE wire_story SET state = 'taken', item_id = ? WHERE id = ? AND state = 'new'", (item_id, wire_id))
        if cur.rowcount != 1:
            raise WireRefusal(409, "already_taken", "this wire story was taken a moment ago")
        store.conn.execute("UPDATE item SET story_status = 'draft', script_rev = 1, source_wire_id = ? WHERE id = ?", (wire_id, item_id))
        story._event(store, item_id, actor, "import", None, wire["feed"], 1, "from wire " + wire_id)
        placed = True
        if position is not None:
            ids = [r["id"] for r in store.conn.execute("SELECT id FROM item WHERE show_id = ? ORDER BY position, id", (show_id,)).fetchall()]
            ids.remove(item_id)
            ids.insert(min(position - 1, len(ids)), item_id)
            try:
                building.reorder(store, show_id, ids)
            except building.BuildError:
                placed = False          # before something already on air: it stays at the end, and the answer says so
    return {"item": item_id, "placed": placed}


def dismiss(store: Store, wire_id: str, actor: str) -> dict:
    """Mark an inbox entry as not wanted. Refused once it has been taken; dismissing twice is a no-op."""
    with store.transaction():
        wire = store.conn.execute("SELECT state FROM wire_story WHERE id = ?", (wire_id,)).fetchone()
        if wire is None:
            raise WireRefusal(404, "not_found", f"no such wire story: {wire_id}")
        if wire["state"] == "taken":
            raise WireRefusal(409, "already_taken", "this wire story has been taken into a show")
        store.conn.execute("UPDATE wire_story SET state = 'dismissed' WHERE id = ? AND state IN ('new', 'dismissed')", (wire_id,))
    return {"state": "dismissed"}
