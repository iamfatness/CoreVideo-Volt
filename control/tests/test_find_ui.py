"""Library: Find and Details (docs/superpowers/plans/2026-11-01-search-indexing.md, Task 9, the parts that need no model).

The page's pure functions run under node; the markup rules are read from the three static files; the `find` harness scenario is read
back through the real routes the page calls."""

import json
import re
import unittest
from pathlib import Path
from urllib.parse import urlencode

from tests.operator_harness import seed
from tests.test_operator_api import Api
from tests.test_operator_ui import FILES, NODE, _node
from volt import serve


@unittest.skipUnless(NODE, "node is needed to run the page's functions")
class FindPure(unittest.TestCase):
    def test_build_search_request_encodes_filter_and_include_off_by_default(self):
        (req,) = _node(["buildSearchRequest", {
            "q": "  okonkwo ", "scope": "transcript", "sort": "auto",
            "sel": {"show": ["Morning Brief", "Final Four Watch"], "camera": ["studio-a"], "cleared": [True], "created": ["2026-02"], "meta.topic": ["Sports"]},
            "incomplete": True, "extra": [{"field": "owner", "op": "exists"}], "limit": 25}])
        self.assertEqual(req["q"], "okonkwo")
        self.assertEqual(req["scope"], "transcript")
        self.assertNotIn("include", req)                     # unreviewed faces are never asked for by this page
        self.assertNotIn("sort", req)                        # auto leaves the choice to the server
        clauses = {c["field"]: c for c in req["filter"]["all"]}
        self.assertEqual(clauses["show"], {"field": "show", "op": "in", "value": ["Morning Brief", "Final Four Watch"]})
        self.assertEqual(clauses["camera"], {"field": "camera", "op": "eq", "value": "studio-a"})
        self.assertEqual(clauses["cleared"], {"field": "cleared", "op": "eq", "value": True})
        self.assertEqual(clauses["created"], {"field": "created", "op": "range", "from": "2026-02-01", "to": "2026-02-28"})
        self.assertEqual(clauses["meta.topic"]["value"], "Sports")
        self.assertEqual(clauses["incomplete"], {"field": "incomplete", "op": "eq", "value": True})
        self.assertEqual(clauses["owner"], {"field": "owner", "op": "exists"})
        (leap,) = _node(["monthRange", "2028-02"])
        self.assertEqual(leap["to"], "2028-02-29")

    def test_nothing_chosen_is_an_empty_request_and_relevance_needs_a_query(self):
        empty, rel, recent = _node(["buildSearchRequest", {"sel": {"show": []}, "sort": "relevance"}],
                                   ["buildSearchRequest", {"q": "a", "sort": "relevance"}],
                                   ["buildSearchRequest", {"sort": "recent", "after": "c1", "limit": 25}])
        self.assertEqual(empty, {})
        self.assertEqual(rel["sort"], "relevance")
        self.assertEqual(recent, {"sort": "recent", "limit": 25, "after": "c1"})

    def test_the_request_goes_out_as_strings_and_a_saved_one_is_the_question_only(self):
        params, saved = _node(
            ["searchParams", {"q": "x", "filter": {"all": [{"field": "show", "op": "eq", "value": "A"}]}, "limit": 25, "after": "c"}, ["show", "meta.topic"]],
            ["savedQueryOf", {"q": "x", "after": "c", "limit": 25, "facets": "show", "scope": "metadata"}])
        self.assertEqual(params, {"q": "x", "after": "c", "filter": '{"all":[{"field":"show","op":"eq","value":"A"}]}', "limit": "25", "facets": "show,meta.topic"})
        self.assertEqual(saved, {"q": "x", "scope": "metadata"})

    def test_a_saved_search_comes_back_into_the_controls_and_keeps_what_it_cannot_show(self):
        (ui,) = _node(["uiFromRequest", {"q": "a", "scope": "metadata", "filter": {"all": [
            {"field": "show", "op": "in", "value": ["A", "B"]}, {"field": "incomplete", "op": "eq", "value": True},
            {"field": "label.face", "op": "exists"}, {"field": "created", "op": "range", "from": "2026-01-01"}]}}])
        self.assertEqual(ui["sel"], {"show": ["A", "B"]})
        self.assertTrue(ui["incomplete"])
        # kept untouched, so the saved question still means what it meant
        self.assertEqual([c["field"] for c in ui["extra"]], ["label.face", "created"])

    def test_parse_search_response_is_defensive(self):
        bad, ok = _node(["parseSearchResponse", {"ok": False}], ["parseSearchResponse", {
            "ok": True, "total": 3, "next": "c",
            "assets": [{"assetId": "a1", "hits": [{"snippet": 5}, None], "metadata": None}, {"nope": 1}, None],
            "facets": {"show": {"values": [{"value": "A", "count": 2}, {"value": "B"}], "other": 1}}}])
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["assets"], [])
        self.assertEqual((ok["total"], ok["next"], len(ok["assets"])), (3, "c", 1))
        self.assertEqual(ok["assets"][0]["hits"], [{"snippet": ""}])
        self.assertEqual(ok["assets"][0]["metadata"], {})
        self.assertEqual(ok["facets"]["show"], {"values": [{"value": "A", "count": 2}], "other": 1})

    def test_highlight_marks_words_and_phrases_not_excluded_ones_and_never_makes_markup(self):
        terms, a, b = _node(["queryTerms", 'Okonkwo "stadium vote" -rain'],
                            ["splitHighlight", "Amara Okonkwo spoke on the stadium vote.", 'okonkwo "stadium vote"'],
                            ["splitHighlight", "<img src=x onerror=1> a.b", "a.b <img"])
        self.assertEqual(terms, ["okonkwo", "stadium vote"])
        self.assertEqual([(p["text"], p["hit"]) for p in a],
                         [("Amara ", False), ("Okonkwo", True), (" spoke on the ", False), ("stadium vote", True), (".", False)])
        self.assertEqual("".join(p["text"] for p in b), "<img src=x onerror=1> a.b")   # pieces are text; the page sets them as text nodes
        self.assertTrue(any(p["hit"] for p in b))

    def test_slash_focuses_search_only_on_the_library_screen_and_never_in_a_field(self):
        got = _node(["findKeyAction", "/", {"inField": False, "screen": "library"}], ["findKeyAction", "/", {"inField": True, "screen": "library"}],
                    ["findKeyAction", "/", {"inField": False, "screen": "show"}], ["findKeyAction", "c", {"inField": False, "screen": "library"}])
        self.assertEqual(got, ["focus", None, None, None])

    def test_facet_words_are_for_people(self):
        got = _node(["facetValueText", "cleared", True], ["facetValueText", "cleared", False], ["facetValueText", "created", "2026-10"],
                    ["facetValueText", "meta.approved", True])
        self.assertEqual(got, ["Cleared", "Not cleared", "October 2026", "Yes"])

    def test_metadata_drafts_become_the_values_the_server_takes(self):
        num_bad, num, date_bad, lines, none, boolean, multi = _node(
            ["metaValues", {"type": "number", "label": "Runs", "multi": False}, "12x"],
            ["metaValues", {"type": "number", "label": "Runs", "multi": False}, " 4.5 "],
            ["metaValues", {"type": "date", "label": "Air date", "multi": False}, "02/03/2026"],
            ["metaValues", {"type": "text", "label": "Guests", "multi": True}, "A\n\n B \n"],
            ["metaValues", {"type": "text", "label": "Producer", "multi": False}, "  "],
            ["metaValues", {"type": "bool", "label": "OK", "multi": False}, "false"],
            ["metaValues", {"type": "enum", "label": "Topic", "multi": True}, ["Sports", "Weather"]])
        self.assertEqual((num_bad["ok"], num_bad["code"]), (False, "type_mismatch"))
        self.assertEqual(num, {"ok": True, "values": [4.5]})
        self.assertFalse(date_bad["ok"])
        self.assertEqual(lines["values"], ["A", "B"])
        self.assertEqual(none["values"], [])                  # clearing is an empty list; the server decides whether the field may be cleared
        self.assertEqual(boolean["values"], [False])
        self.assertEqual(multi["values"], ["Sports", "Weather"])


class FindMarkup(unittest.TestCase):
    HTML, JS, CSS = FILES["index.html"], FILES["operator.js"], FILES["operator.css"]

    def piece(self, start, end):
        i = self.HTML.index(start)
        return self.HTML[i:self.HTML.index(end, i)]

    def between(self, start, end):
        i = self.JS.index(start)
        return self.JS[i:self.JS.index(end, i)]

    def test_the_tabs_and_panels_are_wired_and_find_comes_first(self):
        tabs = self.piece('id="lib-tabs"', "</div>")
        self.assertEqual(re.findall(r'id="lib-tab-(\w+)"', tabs), ["find", "shots", "words", "export", "details"])
        for tid, ctl, icon_name in (("lib-tab-find", "lib-find", "search"), ("lib-tab-details", "lib-details", "tag")):
            tab = self.piece('id="%s"' % tid, "</button>")
            self.assertIn('aria-controls="%s"' % ctl, tab)
            self.assertIn('href="#i-%s"' % icon_name, tab)
            self.assertIn('tabindex="-1"', tab)
        for panel in ("lib-find", "lib-details"):
            self.assertRegex(self.HTML, r'<div id="%s" class="cols lib" role="tabpanel" aria-labelledby="lib-tab-%s" hidden>' % (panel, panel[4:]))
        for hid, icon_name in (("h-find", "search"), ("h-finds", "library"), ("h-facets", "filter"), ("h-saved", "search"), ("h-bins", "bin"),
                               ("h-det", "tag"), ("h-define", "plus")):
            self.assertIn('href="#i-%s"' % icon_name, self.piece('id="%s"' % hid, "</h2>"), hid)

    def test_the_new_keys_are_listed_and_slash_is_handled_before_the_library_keys(self):
        for needle in ("Library, Find tab", "<kbd>/</kbd>", "<kbd>Esc</kbd>"):
            self.assertIn(needle, self.HTML)
        self.assertLess(self.JS.index("=== 'focus') { e.preventDefault(); findFocus(); return; }"), self.JS.index("if (S.screen === 'library') { libKey(e); return; }"))
        body = self.between("function libKey", "// ---- rendering")
        for show_only in ("takeNow", "toggleHold", "skipItem", "killItem", "location.hash"):
            self.assertNotIn(show_only, body)
        self.assertIn("W.tab === 'find' || W.tab === 'details'", body)

    def test_no_innerhtml_and_no_markup_from_data_in_the_new_code(self):
        block = self.between("// ---- Library: Find and Details", "// ---- Activity: who did what")
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "setAttribute('style", "http://", "https://", "style:"):
            self.assertNotIn(forbidden, block)
        self.assertEqual(self.JS.count("innerHTML"), 3)       # the three that were already there: the icon, the glyph and the template helper
        self.assertNotIn("innerHTML", self.between("// ---- Find: the search request", "if (typeof document === 'undefined')"))
        for src in (self.HTML,):
            self.assertNotRegex(src, r"<script(?![^>]*\bsrc=)")
            self.assertNotIn("style=", src)

    def test_snippets_reach_the_page_only_as_text(self):
        block = self.between("function hitNode", "function paintFindResults")
        self.assertIn("el('mark', { text: p.text })", block)
        self.assertIn("splitHighlight(h.snippet, q)", block)

    def test_every_state_is_an_icon_a_word_and_a_colour(self):
        for kind in ("incomplete", "shared", "required"):
            self.assertRegex(self.JS, r"%s:\s+\{ icon: '\w+',\s+word: '\w+',\s+cls: 'st \w+' \}" % kind)
        for name in ("bin", "tag", "search"):
            self.assertIn('<symbol id="i-%s"' % name, self.HTML)

    def test_a_required_field_says_required_in_words_and_incomplete_is_a_chip(self):
        self.assertIn("stateChip('required')", self.between("function fieldBlock", "function renderDetails"))
        self.assertIn("stateChip('incomplete')", self.between("function renderDetails", "// ---- Activity"))
        self.assertIn("stateChip('incomplete')", self.between("function paintFindResults", "function paintFacets"))

    def test_there_is_no_control_that_includes_unreviewed_faces_and_no_confirm_all(self):
        block = self.between("// ---- Library: Find and Details", "// ---- Activity: who did what")
        self.assertNotIn("includeProposed", block)
        self.assertNotIn("Confirm all", block)

    def test_the_two_press_actions_say_what_the_second_press_does(self):
        self.assertIn("Press again to delete", self.between("function paintSaved", "function memberName"))
        self.assertIn("Press again to delete", self.between("function paintBins", "// ---- Details"))
        self.assertIn("Press again to retire", self.between("function fieldBlock", "function renderDetails"))

    def test_the_new_endpoints_are_the_servers(self):
        for method, tpl in (("GET", "/api/search"), ("GET", "/api/searches"), ("POST", "/api/searches"), ("POST", "/api/searches/{id}/delete"),
                            ("GET", "/api/bins"), ("GET", "/api/bins/{id}"), ("POST", "/api/bins"), ("POST", "/api/bins/{id}/add"),
                            ("POST", "/api/bins/{id}/remove"), ("POST", "/api/bins/{id}/delete"), ("GET", "/api/schema"),
                            ("POST", "/api/schema/fields"), ("POST", "/api/schema/fields/{id}/retire"), ("POST", "/api/schema/fields/{id}/terms"),
                            ("GET", "/api/assets/{id}/metadata"), ("POST", "/api/assets/{id}/metadata")):
            self.assertRegex(self.JS, r"\['%s',\s*'%s'\]" % (method, re.escape(tpl)))

    def test_the_new_css_uses_tokens_and_no_raw_colours(self):
        css = self.CSS[self.CSS.index("/* library: Find and Details"):]
        self.assertNotRegex(css, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(css, r"rgba?\(")


class FindScenario(Api):
    """The harness's `find` scenario, read back through the routes the page calls."""

    def setUp(self):
        super().setUp()
        serve.OUT_DIR = Path(self.tmp.name) / "out"
        serve.ROOM, self.show, self.spans = seed(self.store, "find", media_dir=Path(self.tmp.name))
        self.ids = {k[6:]: v for k, v in self.spans.items() if k.startswith("asset:")}

    def search(self, **params):
        status, payload, _ = self.call("GET", "/api/search?" + urlencode(params))
        self.assertEqual(status, 200, payload)
        return payload

    def test_the_landing_search_lists_assets_with_the_facets_the_page_asks_for(self):
        out = self.search(facets="show,camera,owner,retention,cleared,created,meta.topic,meta.approved")
        self.assertGreaterEqual(out["total"], 11)
        shows = {v["value"] for v in out["facets"]["show"]["values"]}
        self.assertEqual(shows, {"Final Four Watch", "Morning Brief"})
        self.assertEqual({v["value"] for v in out["facets"]["meta.topic"]["values"]}, {"Sports", "Politics", "Weather"})
        self.assertEqual({v["value"] for v in out["facets"]["cleared"]["values"]}, {True, False})

    def test_a_word_finds_speech_with_a_frame_to_seek_to_and_a_filter_narrows_it(self):
        out = self.search(q="Okonkwo", facets="show")
        by = {a["camera"]: a for a in out["assets"]}
        self.assertEqual({"iso-4", "studio-a", "studio-b"}, set(by))
        hit = by["studio-a"]["hits"][0]
        self.assertEqual(hit["kind"], "transcript")
        self.assertIsInstance(hit["frameIn"], int)
        narrowed = self.search(q="Okonkwo", filter=json.dumps({"all": [{"field": "show", "op": "eq", "value": "Morning Brief"}]}))
        self.assertEqual({a["camera"] for a in narrowed["assets"]}, {"studio-a", "studio-b"})

    def test_incomplete_assets_are_those_missing_the_required_producer(self):
        out = self.search(filter=json.dumps({"all": [{"field": "incomplete", "op": "eq", "value": True}]}))
        self.assertIn("studio-b", {a["camera"] for a in out["assets"]})
        self.assertTrue(all("producer" in a["missing"] for a in out["assets"]))

    def test_the_seeded_saved_search_and_bin_are_listed(self):
        _, searches, _ = self.call("GET", "/api/searches")
        _, bins, _ = self.call("GET", "/api/bins")
        self.assertEqual([s["name"] for s in searches["searches"]], ["Okonkwo, anywhere"])
        self.assertEqual([(b["name"], b["count"]) for b in bins["bins"]], [("Tomorrow's rundown", 2)])
