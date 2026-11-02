"""The derived search index (passages, metadata, label rows), the query grammar, facets, paging, and the two backends."""
import sqlite3
import unittest
from fractions import Fraction
from unittest import mock

from volt import library, media, metadata, retention, searchindex
from volt.searchindex import SearchError
from volt.store import Store

from tests.pgutil import PgCase


def words(text, start=0, step=1):
    """(text, tc_in, tc_out) for each word of `text`, one every `step` seconds from second `start`."""
    out = []
    for i, w in enumerate(text.split()):
        s = start + i * step
        out.append((w, f"00:00:{s:02d}:00", f"00:00:{s:02d}:12"))
    return out


class Model(unittest.TestCase):
    """Pure functions: passages, the text parser, the filter grammar, the cursor."""

    def w(self, i, sec, text="w"):
        return {"id": f"s{i}", "text": text, "tc_in": f"00:00:{sec:02d}:00", "tc_out": f"00:00:{sec:02d}:10",
                "frame_in": None, "frame_out": None}

    def test_passages_cut_rules(self):
        rate = Fraction(25)
        # a gap over 1.5 s cuts: word 1 ends 0.4 s after it starts, word 2 starts 3 s later
        got = searchindex.passages([self.w(0, 0), self.w(1, 1), self.w(2, 5), self.w(3, 6)], rate)
        self.assertEqual([p["text"] for p in got], ["w w", "w w"])
        self.assertEqual([(p["first_id"], p["last_id"]) for p in got], [("s0", "s1"), ("s2", "s3")])
        # a gap of exactly 1.5 s (end 00:00:00:10 -> start 00:00:01:... ) does not cut
        near = [self.w(0, 0), {**self.w(1, 1), "tc_in": "00:00:01:12"}]
        self.assertEqual(len(searchindex.passages(near, rate)), 1)
        # 25 words cut, even with no gap
        dense = [{"id": f"d{i}", "text": f"t{i}", "tc_in": f"00:00:00:{i:02d}", "tc_out": f"00:00:00:{i + 1:02d}",
                  "frame_in": None, "frame_out": None} for i in range(24)]
        dense += [{"id": f"e{i}", "text": f"u{i}", "tc_in": f"00:00:01:{i:02d}", "tc_out": f"00:00:01:{i + 1:02d}",
                   "frame_in": None, "frame_out": None} for i in range(0, 6)]
        sizes = [len(p["text"].split()) for p in searchindex.passages(dense, rate)]
        self.assertEqual(sizes, [25, 5])
        # 12 s cut: a word every second for 30 s, no gap
        slow = [{"id": f"x{i}", "text": f"t{i}", "tc_in": f"00:00:{i:02d}:00", "tc_out": f"00:00:{i:02d}:24",
                 "frame_in": None, "frame_out": None} for i in range(30)]
        got = searchindex.passages(slow, rate)
        self.assertTrue(all(len(p["text"].split()) <= 12 for p in got), [len(p["text"].split()) for p in got])
        self.assertEqual(sum(len(p["text"].split()) for p in got), 30)
        self.assertEqual(searchindex.passages([], rate), [])

    def test_text_parser(self):
        t = searchindex.parse_text('Okonkwo "steal the show" -tax bud')
        self.assertEqual((t.terms, t.phrases, t.negations, t.prefix), (["okonkwo", "bud"], [["steal", "the", "show"]], ["tax"], True))
        self.assertFalse(searchindex.parse_text("budget ").prefix)
        # diacritics and case fold; punctuation inside a term separates words
        self.assertEqual(searchindex.parse_text("Zoë co-op").terms, ["zoe", "co", "op"])
        # hostile input becomes terms or bad_input, never a backend error
        for hostile in ('" OR ) * NEAR(', "a AND b OR c", "NEAR/2 x", "col:value", "\"\"", "x" * 500, "'; DROP TABLE span;--"):
            try:
                searchindex.parse_text(hostile)
            except SearchError as exc:
                self.assertEqual(exc.code, "bad_input", hostile)
        for empty in ("", "   ", '""', "-tax", "* ( )"):
            if empty.strip() == "":
                self.assertEqual(searchindex.parse_text(empty).terms, [])
            else:
                with self.assertRaises(SearchError) as cm:
                    searchindex.parse_text(empty)
                self.assertEqual(cm.exception.code, "bad_input", empty)
        with self.assertRaises(SearchError):
            searchindex.parse_text(" ".join(f"t{i}" for i in range(40)))

    def test_filter_grammar_refusals(self):
        ok = searchindex.parse_filter({"all": [{"field": "show", "op": "eq", "value": "news"},
                                               {"field": "created", "op": "range", "from": "2026-01-01", "to": "2026-12-31"},
                                               {"field": "label.face", "op": "exists", "min_confidence": 0.5}]})
        self.assertEqual(len(ok.clauses), 3)
        self.assertEqual(searchindex.parse_filter(None).clauses, [])
        bad = [{"any": []}, {"all": "x"}, {"all": [{"field": "nope", "op": "eq", "value": 1}]},
               {"all": [{"field": "show", "op": "like", "value": "x"}]},
               {"all": [{"field": "show", "op": "range", "from": "a", "to": "b"}]},
               {"all": [{"field": "show", "op": "eq"}]},
               {"all": [{"field": "show", "op": "eq", "value": 5}]},
               {"all": [{"field": "cleared", "op": "eq", "value": "yes"}]},
               {"all": [{"field": "created", "op": "range", "from": "yesterday"}]},
               {"all": [{"field": "label.face", "op": "eq", "value": "x", "min_confidence": 7}]},
               {"all": [{"field": "label.face", "op": "eq", "value": "x", "status": "rejected"}]},
               {"all": [{"field": "show", "op": "eq", "value": "x", "extra": 1}]},
               {"all": [{"field": "show", "op": "in", "value": []}]},
               {"all": [{"field": "meta.Bad Key", "op": "eq", "value": "x"}]},
               {"all": [{"field": "show", "op": "eq", "value": "x"}] * 21}, "text", 5]
        for obj in bad:
            with self.assertRaises(SearchError, msg=str(obj)[:80]) as cm:
                searchindex.parse_filter(obj)
            self.assertEqual(cm.exception.code, "bad_input")

    def test_cursor_roundtrip_and_foreign_refused(self):
        raw = searchindex.encode_cursor(["abc", "recent", [[-1, -2], "id-1"]], "id-1")
        self.assertEqual(searchindex.decode_cursor(raw), (["abc", "recent", [[-1, -2], "id-1"]], "id-1"))
        for foreign in ("", "not-base64!!", "e30", "W10", "WyJhIl0", "Zm9v"):
            with self.assertRaises(SearchError) as cm:
                searchindex.decode_cursor(foreign)
            self.assertEqual(cm.exception.code, "bad_input", foreign)


class SearchCases:
    """Behaviour over a fixture; a subclass supplies self.store."""

    def make(self, text, show="news", camera=None, start=0, step=1, owner="desk"):
        a = self.store.mint_asset(owner=owner, show_name=show, camera=camera)
        library.index_words(self.store, a.id, words(text, start, step))
        return a.id

    def run_search(self, **req):
        return searchindex.search(self.store, req)

    def ids(self, out, key="assets"):
        return [x["assetId"] for x in out[key]]

    # ---- the index ----

    def test_transcript_hit_returns_first_word_span_and_in_point(self):
        a = self.make("we will steal the show tonight")
        first = self.store.conn.execute(
            "SELECT id, tc_in FROM span WHERE asset_id = ? AND kind = 'transcript' ORDER BY tc_in", (a,)).fetchone()
        out = self.run_search(q="steal")
        self.assertEqual(self.ids(out), [a])
        hit = out["assets"][0]["hits"][0]
        self.assertEqual((hit["kind"], hit["spanId"], hit["tcIn"]), ("transcript", first["id"], first["tc_in"]))
        self.assertIn("steal", hit["snippet"])

    def test_reindex_does_not_duplicate(self):
        a = self.make("steal the show")
        library.index_words(self.store, a, words("steal the show"))   # the library's own acceptance line
        before = self.store.conn.execute("SELECT COUNT(*) FROM search_doc WHERE asset_id = ?", (a,)).fetchone()[0]
        searchindex.rebuild_asset(self.store, a)
        searchindex.rebuild_asset(self.store, a)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM search_doc WHERE asset_id = ?", (a,)).fetchone()[0], before)
        self.assertEqual(len(self.run_search(q="steal")["assets"][0]["hits"]), 1)

    def test_rebuild_from_sources_matches_live(self):
        a = self.make("alpha budget meeting", show="news")
        b = self.make("budget vote tomorrow", show="sport")
        metadata.define_field(self.store, "guest", "Guest", "text")
        metadata.set_values(self.store, a, "guest", ["Okonkwo"], "desk")
        live = [self.ids(self.run_search(q=q)) for q in ("budget", "okonkwo", "vote")]
        live_docs = self.store.conn.execute("SELECT COUNT(*) FROM search_doc").fetchone()[0]
        self.store.conn.execute("DELETE FROM search_doc")
        self.store.conn.commit()
        self.assertEqual(self.run_search(q="budget")["assets"], [])
        self.assertEqual(searchindex.rebuild_all(self.store), 2)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM search_doc").fetchone()[0], live_docs)
        self.assertEqual([self.ids(self.run_search(q=q)) for q in ("budget", "okonkwo", "vote")], live)
        self.assertEqual(sorted(live[0]), sorted([a, b]))

    def test_metadata_text_is_searchable_and_follows_edits_and_clears(self):
        a = self.make("nothing relevant here")
        metadata.define_field(self.store, "guest", "Guest", "text")
        metadata.define_field(self.store, "mood", "Mood", "enum")
        metadata.add_terms(self.store, "mood", ["jubilant"])
        metadata.set_values(self.store, a, "guest", ["Chidi Okonkwo"], "desk")
        metadata.set_values(self.store, a, "mood", ["jubilant"], "desk")
        out = self.run_search(q="okonkwo")
        self.assertEqual((self.ids(out), out["assets"][0]["hits"][0]["kind"]), ([a], "meta"))
        self.assertEqual(self.ids(self.run_search(q="jubilant")), [a])
        metadata.set_values(self.store, a, "guest", ["Someone Else"], "desk")
        self.assertEqual(self.run_search(q="okonkwo")["assets"], [])
        metadata.set_values(self.store, a, "mood", [], "desk")
        self.assertEqual(self.run_search(q="jubilant")["assets"], [])
        self.assertEqual(len(self.run_search(q="someone")["assets"]), 1)

    def test_tombstoned_asset_excluded_archived_included_flagged(self):
        gone = self.make("steal the show")
        cold = self.make("steal the budget")
        self.store.add_essence(cold, "hi-res", "x.mxf", frame_rate="25", timecode_start="00:00:00:00")
        self.store.conn.execute("UPDATE essence SET tier = 'archived' WHERE asset_id = ?", (cold,))
        self.store.conn.commit()
        retention.tombstone(self.store, gone)
        out = self.run_search(q="steal")
        self.assertEqual(self.ids(out), [cold])
        self.assertTrue(out["assets"][0]["archived"])
        self.assertNotIn("location", str(out))
        retention.restore(self.store, gone)
        self.assertEqual(sorted(self.ids(self.run_search(q="steal"))), sorted([gone, cold]))

    def test_blocked_by_shape_matches_spans_route(self):
        a = self.make("steal the show")
        self.store.add_span(a, "clip", "00:00:00:00", "00:00:02:00", label="c", frame_in=0, frame_out=50)
        for cleared, hold, expect in ((False, False, "not cleared"), (True, False, None), (True, True, "legal hold")):
            self.store.conn.execute("UPDATE asset SET cleared = ?, legal_hold = ? WHERE id = ?", (cleared, hold, a))
            self.store.conn.commit()
            row = self.run_search(q="steal")["assets"][0]
            spans = media.list_spans(self.store, a, {"kind": ["clip"]})["spans"]
            self.assertEqual(len(spans), 1)
            self.assertEqual((row["blockedBy"], row["cleared"], row["legalHold"]), (spans[0]["blockedBy"], cleared, hold))
            self.assertEqual(row["blockedBy"], expect)

    # ---- text semantics ----

    def test_phrase_negation_prefix_and_diacritics(self):
        a = self.make("the budget vote passed")
        b = self.make("the vote budget failed")
        c = self.make("zoë reports the tax budget")
        self.assertEqual(self.ids(self.run_search(q='"budget vote"')), [a])
        self.assertEqual(sorted(self.ids(self.run_search(q="budget -tax"))), sorted([a, b]))
        self.assertEqual(sorted(self.ids(self.run_search(q="budg"))), sorted([a, b, c]))      # last term is a prefix
        self.assertEqual(self.ids(self.run_search(q="zoe")), [c])
        self.assertEqual(self.ids(self.run_search(q="ZOË")), [c])
        self.assertEqual(self.run_search(q="budget vote")["assets"][0]["assetId"] in (a, b), True)

    def test_hostile_text_is_inert(self):
        self.make("the budget vote passed")
        for q in ('" OR ) * NEAR(', "budget AND OR", "x:y", "NOT", "-", "'--", "a" * 200):
            try:
                self.run_search(q=q)
            except SearchError as exc:
                self.assertEqual(exc.code, "bad_input")

    def test_scope_limits_the_kinds(self):
        a = self.make("steal the show")
        metadata.define_field(self.store, "guest", "Guest", "text")
        metadata.set_values(self.store, a, "guest", ["steal"], "desk")
        kinds = lambda scope: sorted({h["kind"] for h in self.run_search(q="steal", scope=scope, group="hit")["hits"]})  # noqa: E731
        self.assertEqual(kinds("transcript"), ["transcript"])
        self.assertEqual(kinds("metadata"), ["meta"])
        self.assertEqual(kinds("all"), ["meta", "transcript"])
        self.assertEqual(kinds("labels"), [])

    # ---- labels in the index ----

    def label_doc(self, asset, kind, value, status, person_id=None, confidence=0.9, ref="l1"):
        searchindex.put_doc(self.store, asset, kind, ref, value, status=status, person_id=person_id, confidence=confidence)
        self.store.conn.commit()

    def test_face_proposed_hidden_by_default(self):
        a = self.make("quiet clip")
        self.label_doc(a, "face", "Amy Adeyemi", "proposed", person_id="p1")
        self.label_doc(a, "object", "microphone", "proposed", ref="l2")
        self.assertEqual(self.run_search(q="amy")["assets"], [])
        self.assertEqual(self.ids(self.run_search(q="microphone")), [a])               # other kinds include proposed
        self.assertEqual(self.ids(self.run_search(q="amy", include="proposed")), [a])
        self.store.conn.execute("UPDATE search_doc SET status = 'confirmed' WHERE kind = 'face'")
        self.store.conn.commit()
        self.assertEqual(self.ids(self.run_search(q="amy")), [a])
        self.assertEqual(self.run_search(q="amy", filter={"all": [{"field": "label.face", "op": "exists"}]})["total"], 1)

    def test_face_label_filter_hides_unconfirmed_too(self):
        a = self.make("quiet clip")
        self.label_doc(a, "face", "Amy Adeyemi", "proposed", person_id="p1")
        flt = {"all": [{"field": "label.person", "op": "eq", "value": "p1"}]}
        self.assertEqual(self.run_search(filter=flt)["assets"], [])
        self.assertEqual(self.ids(self.run_search(filter=flt, include="proposed")), [a])

    def test_rejected_never_matches(self):
        a = self.make("quiet clip")
        self.label_doc(a, "object", "microphone", "rejected")
        self.assertEqual(self.run_search(q="microphone")["assets"], [])
        self.assertEqual(self.run_search(q="microphone", include="proposed")["assets"], [])
        self.assertEqual(self.run_search(filter={"all": [{"field": "label.object", "op": "exists"}]})["assets"], [])
        self.assertEqual(self.run_search(filter={"all": [{"field": "label.object", "op": "missing"}]})["total"], 1)

    def test_label_confidence_and_status_clauses(self):
        a = self.make("one")
        b = self.make("two")
        self.label_doc(a, "object", "car", "confirmed", confidence=0.4, ref="l1")
        self.label_doc(b, "object", "Car", "proposed", confidence=0.95, ref="l2")
        eq = {"field": "label.object", "op": "eq", "value": "car"}
        self.assertEqual(sorted(self.ids(self.run_search(filter={"all": [eq]}))), sorted([a, b]))
        self.assertEqual(self.ids(self.run_search(filter={"all": [{**eq, "min_confidence": 0.9}]})), [b])
        self.assertEqual(self.ids(self.run_search(filter={"all": [{**eq, "status": "confirmed"}]})), [a])

    # ---- filters, facets, paging ----

    def test_filters_builtin_and_meta(self):
        a = self.make("alpha", show="news", camera="c1")
        b = self.make("beta", show="sport", camera="c2")
        c = self.make("gamma", show="news", camera=None)
        metadata.define_field(self.store, "mood", "Mood", "enum")
        metadata.add_terms(self.store, "mood", ["calm", "tense"])
        metadata.define_field(self.store, "rating", "Rating", "number")
        metadata.define_field(self.store, "aired", "Aired", "date")
        metadata.define_field(self.store, "live", "Live", "bool")
        metadata.define_field(self.store, "slug", "Slug", "text", required=True)
        metadata.set_values(self.store, a, "mood", ["calm"], "desk")
        metadata.set_values(self.store, b, "mood", ["tense"], "desk")
        metadata.set_values(self.store, a, "rating", [3], "desk")
        metadata.set_values(self.store, b, "rating", [8.5], "desk")
        metadata.set_values(self.store, a, "aired", ["2026-03-01"], "desk")
        metadata.set_values(self.store, b, "aired", ["2026-09-01"], "desk")
        metadata.set_values(self.store, a, "live", [True], "desk")
        metadata.set_values(self.store, b, "live", [False], "desk")
        metadata.set_values(self.store, a, "slug", ["x"], "desk")

        def f(*clauses):
            return sorted(self.ids(self.run_search(filter={"all": list(clauses)})))

        self.assertEqual(f({"field": "show", "op": "eq", "value": "news"}), sorted([a, c]))
        self.assertEqual(f({"field": "show", "op": "ne", "value": "news"}), [b])
        self.assertEqual(f({"field": "show", "op": "in", "value": ["sport", "other"]}), [b])
        self.assertEqual(f({"field": "camera", "op": "missing"}), [c])
        self.assertEqual(f({"field": "camera", "op": "exists"}), sorted([a, b]))
        self.assertEqual(f({"field": "meta.mood", "op": "eq", "value": "calm"}), [a])
        self.assertEqual(f({"field": "meta.mood", "op": "ne", "value": "calm"}), sorted([b, c]))
        self.assertEqual(f({"field": "meta.mood", "op": "in", "value": ["calm", "tense"]}), sorted([a, b]))
        self.assertEqual(f({"field": "meta.rating", "op": "range", "from": 5}), [b])
        self.assertEqual(f({"field": "meta.rating", "op": "range", "from": 1, "to": 3}), [a])
        self.assertEqual(f({"field": "meta.aired", "op": "range", "from": "2026-06-01", "to": "2026-12-31"}), [b])
        self.assertEqual(f({"field": "meta.live", "op": "eq", "value": True}), [a])
        self.assertEqual(f({"field": "meta.mood", "op": "missing"}), [c])
        self.assertEqual(f({"field": "incomplete", "op": "eq", "value": True}), sorted([b, c]))
        self.assertEqual(f({"field": "incomplete", "op": "eq", "value": False}), [a])
        self.assertEqual(f({"field": "show", "op": "eq", "value": "news"}, {"field": "meta.mood", "op": "eq", "value": "calm"}), [a])
        self.assertEqual(f({"field": "cleared", "op": "eq", "value": False}), sorted([a, b, c]))
        self.assertEqual(f({"field": "created", "op": "range", "from": "2000-01-01", "to": "2999-01-01"}), sorted([a, b, c]))
        self.assertEqual(f({"field": "created", "op": "range", "from": "2999-01-01"}), [])
        for bad in ({"field": "meta.ghost", "op": "eq", "value": "x"}, {"field": "meta.rating", "op": "eq", "value": "x"},
                    {"field": "meta.mood", "op": "range", "from": "a"}):
            with self.assertRaises(SearchError, msg=str(bad)):
                self.run_search(filter={"all": [bad]})

    def test_retired_field_not_searchable(self):
        metadata.define_field(self.store, "mood", "Mood", "text")
        metadata.retire_field(self.store, "mood")
        with self.assertRaises(SearchError):
            self.run_search(filter={"all": [{"field": "meta.mood", "op": "exists"}]})

    def test_facets_count_over_filtered_set_with_other(self):
        for i in range(25):
            self.make(f"clip{i}", show=f"show{i:02d}", camera="c1" if i % 2 else "c2")
        self.make("odd", show="show00")
        out = self.run_search(facets="show,camera,cleared,created")
        shows = out["facets"]["show"]
        self.assertEqual(len(shows["values"]), 20)
        self.assertEqual(shows["values"][0], {"value": "show00", "count": 2})
        self.assertEqual(shows["other"], 5)
        cams = {v["value"]: v["count"] for v in out["facets"]["camera"]["values"]}
        self.assertEqual(cams, {"c1": 12, "c2": 13})
        self.assertEqual(out["facets"]["cleared"]["values"], [{"value": False, "count": 26}])
        self.assertEqual(len(out["facets"]["created"]["values"]), 1)
        # counted over the filtered set, not the whole library
        narrowed = self.run_search(facets="camera", filter={"all": [{"field": "camera", "op": "eq", "value": "c1"}]})
        self.assertEqual(narrowed["facets"]["camera"]["values"], [{"value": "c1", "count": 12}])
        with self.assertRaises(SearchError):
            self.run_search(facets="nonsense")

    def test_facets_meta_and_label_kind_and_person(self):
        a = self.make("one")
        b = self.make("two")
        metadata.define_field(self.store, "mood", "Mood", "enum")
        metadata.add_terms(self.store, "mood", ["calm", "tense"])
        metadata.set_values(self.store, a, "mood", ["calm"], "desk")
        metadata.set_values(self.store, b, "mood", ["calm"], "desk")
        self.label_doc(a, "face", "Amy", "confirmed", person_id="p1", ref="l1")
        self.label_doc(b, "face", "Amy", "proposed", person_id="p1", ref="l2")
        self.label_doc(a, "object", "car", "proposed", ref="l3")
        out = self.run_search(facets="meta.mood,label.kind,label.person")
        self.assertEqual(out["facets"]["meta.mood"]["values"], [{"value": "calm", "count": 2}])
        kinds = {v["value"]: v["count"] for v in out["facets"]["label.kind"]["values"]}
        self.assertEqual(kinds, {"face": 1, "object": 1})                  # the proposed face is not counted
        self.assertEqual(out["facets"]["label.person"]["values"], [{"value": "p1", "count": 1}])
        out = self.run_search(facets="label.person", include="proposed")
        self.assertEqual(out["facets"]["label.person"]["values"], [{"value": "p1", "count": 2}])

    def test_keyset_paging_no_gap_no_repeat(self):
        made = [self.make(f"budget item{i}", show="news" if i % 3 else "sport") for i in range(60)]
        for sort, q in (("recent", None), ("show", None), ("relevance", "budget")):
            seen, after, pages = [], None, 0
            while True:
                req = {"limit": 7, "sort": sort}
                if q:
                    req["q"] = q
                if after:
                    req["after"] = after
                out = self.run_search(**req)
                seen += self.ids(out)
                pages += 1
                after = out["next"]
                if not after:
                    break
                self.assertLess(pages, 20)
            self.assertEqual(sorted(seen), sorted(made), sort)
            self.assertEqual(len(seen), len(set(seen)), sort)
            self.assertEqual(pages, 9, sort)
        by_hit, after, seen = [], None, []
        while True:
            out = self.run_search(q="budget", group="hit", limit=11, **({"after": after} if after else {}))
            seen += [h["spanId"] for h in out["hits"]]
            after = out["next"]
            if not after:
                break
        self.assertEqual(len(seen), 60)
        self.assertEqual(len(set(seen)), 60)

    def test_limits_and_option_validation(self):
        self.make("budget")
        for bad in ({"limit": 101}, {"limit": 0}, {"limit": "x"}, {"sort": "zigzag"}, {"scope": "everything"}, {"group": "both"},
                    {"include": "everything"}, {"sort": "relevance"}, {"group": "hit"}, {"after": "garbage"}, {"unknown": 1}):
            with self.assertRaises(SearchError, msg=str(bad)) as cm:
                self.run_search(**bad)
            self.assertEqual(cm.exception.code, "bad_input")
        self.assertEqual(len(self.run_search(limit=100)["assets"]), 1)

    def test_foreign_cursor_refused(self):
        for i in range(5):
            self.make(f"budget {i}")
        page = self.run_search(q="budget", limit=2)
        with self.assertRaises(SearchError) as cm:
            self.run_search(q="vote", limit=2, after=page["next"])           # issued for a different query
        self.assertEqual(cm.exception.code, "bad_input")
        with self.assertRaises(SearchError):
            self.run_search(q="budget", limit=2, after="AAAA")
        self.assertEqual(len(self.run_search(q="budget", limit=2, after=page["next"])["assets"]), 2)

    def test_person_clause_writes_one_audit_row_without_a_name(self):
        a = self.make("one")
        self.label_doc(a, "face", "Amy Adeyemi", "confirmed", person_id="p1")

        class P:
            name = "olive"

        flt = {"all": [{"field": "label.person", "op": "eq", "value": "p1"}, {"field": "label.person", "op": "in", "value": ["p1", "p2"]}]}
        out = searchindex.search(self.store, {"filter": flt}, principal=P())
        self.assertEqual(self.ids(out), [a])
        rows = [r for r in self.store.conn.execute("SELECT * FROM audit WHERE action = 'search_person' ORDER BY target_id")]
        self.assertEqual([(r["actor"], r["target_kind"], r["target_id"], r["detail"]) for r in rows],
                         [("olive", "person", "p1", None), ("olive", "person", "p2", None)])
        self.assertNotIn("Amy", str([tuple(r) for r in self.store.conn.execute("SELECT * FROM audit")]))
        searchindex.search(self.store, {"q": "one"}, principal=P())          # no person clause: no audit row
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'search_person'").fetchone()[0], 2)

    def test_purge_removes_search_docs(self):
        a = self.make("steal the show")
        self.label_doc(a, "object", "car", "proposed")
        self.assertGreater(self.store.conn.execute("SELECT COUNT(*) FROM search_doc").fetchone()[0], 1)
        retention.tombstone(self.store, a)
        self.assertEqual(retention.purge_asset(self.store, a, retention.OwnedRoots()).status, "purged")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM search_doc").fetchone()[0], 0)
        self.assertEqual(self.run_search(q="steal")["assets"], [])

    def test_transcribe_replacement_refreshes_the_index(self):
        a = self.make("steal the show")
        self.assertEqual(self.ids(self.run_search(q="steal")), [a])
        self.store.conn.execute("DELETE FROM span WHERE asset_id = ? AND kind = 'transcript'", (a,))
        self.store.conn.commit()
        searchindex.rebuild_asset(self.store, a)
        self.assertEqual(self.run_search(q="steal")["assets"], [])


class SqliteSearch(SearchCases, unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)


@unittest.skipUnless(searchindex.sqlite_has_fts5(), "this Python's sqlite3 was built without FTS5")
class SqlitePlans(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        for i in range(300):
            a = self.store.mint_asset(owner="desk", show_name="news")
            library.index_words(self.store, a.id, words(f"alpha budget{i % 7} vote"))
        self.store.conn.commit()

    def test_sqlite_plan_uses_fts(self):
        sql, args = searchindex.text_candidates_sql(self.store, searchindex.parse_text("budget3"), ("transcript", "meta"), False, "1 = 1", [])
        plan = " ".join(str(tuple(r)) for r in self.store.conn.execute("EXPLAIN QUERY PLAN " + sql, tuple(args)).fetchall())
        self.assertIn("search_fts", plan)
        self.assertNotIn("SCAN span", plan)
        self.assertNotIn("SCAN d", plan)

    def test_fts_is_present_and_synced_by_triggers(self):
        self.assertTrue(searchindex.fts_available(self.store))
        n = self.store.conn.execute("SELECT COUNT(*) FROM search_fts WHERE search_fts MATCH 'budget3'").fetchone()[0]
        self.assertGreater(n, 0)
        a = self.store.mint_asset(owner="desk")
        library.index_words(self.store, a.id, words("zebra"))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM search_fts WHERE search_fts MATCH 'zebra'").fetchone()[0], 1)
        self.store.conn.execute("DELETE FROM search_doc WHERE asset_id = ?", (a.id,))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM search_fts WHERE search_fts MATCH 'zebra'").fetchone()[0], 0)


class SqliteFallback(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_fallback_like_when_fts_missing(self):
        a = self.store.mint_asset(owner="desk")
        library.index_words(self.store, a.id, words("steal the show tonight"))
        b = self.store.mint_asset(owner="desk")
        library.index_words(self.store, b.id, words("steal the budget"))
        with mock.patch.object(searchindex, "fts_available", return_value=False):
            out = searchindex.search(self.store, {"q": "steal show"})
            self.assertTrue(out["degraded"])
            self.assertEqual([x["assetId"] for x in out["assets"]], [a.id])
            self.assertEqual([x["assetId"] for x in searchindex.search(self.store, {"q": '"the budget"'})["assets"]], [b.id])
            self.assertEqual([x["assetId"] for x in searchindex.search(self.store, {"q": "steal -show"})["assets"]], [b.id])
            self.assertEqual(len(searchindex.search(self.store, {"q": "ste"})["assets"]), 2)       # prefix
            self.assertEqual(searchindex.search(self.store, {"q": "100%_"})["assets"], [])        # LIKE wildcards are inert
            with self.assertRaises(SearchError):
                searchindex.search(self.store, {"q": "steal", "limit": 50})                      # the lower page cap
        self.assertFalse(searchindex.search(self.store, {"q": "steal"})["degraded"])

    def test_no_fts_module_does_not_break_open(self):
        def no_fts(db):
            raise sqlite3.OperationalError("no such module: fts5")

        # the migration step tolerates a SQLite without FTS5
        from volt import migrations
        db = migrations.SqliteBackend(sqlite3.connect(":memory:"))
        with mock.patch.object(db, "execute", side_effect=sqlite3.OperationalError("no such module: fts5")):
            migrations._sqlite_fts(db)   # does not raise


class PostgresSearch(SearchCases, PgCase):
    def setUp(self):
        super().setUp()
        self.store = PgCase.store(self)

    def test_pg_plan_uses_gin(self):
        for i in range(300):
            a = self.store.mint_asset(owner="desk", show_name="news")
            library.index_words(self.store, a.id, words(f"alpha budget{i % 7} vote"))
        self.store.conn.commit()
        self.store.conn.execute("ANALYZE search_doc")
        self.store.conn.commit()
        admin = self.admin()
        admin.execute("SET enable_seqscan = off")
        sql, args = searchindex.text_candidates_sql(self.store, searchindex.parse_text("budget3"), ("transcript", "meta"), False, "TRUE", [])
        plan = " ".join(str(tuple(r)) for r in admin.execute("EXPLAIN " + sql.replace("?", "%s"), tuple(args)).fetchall())
        self.assertNotIn("on span", plan)                      # a text query never reads the word spans
        # On 300 documents the planner may prefer another index for the whole query, so also prove the GIN index is the one a
        # tsvector match can use: the same predicate on its own, with sequential scans off, has no other way in.
        bare = admin.execute("EXPLAIN SELECT 1 FROM search_doc d, to_tsquery('simple', %s) q WHERE d.tsv @@ q", ("'budget3'",)).fetchall()
        self.assertIn("search_doc_tsv", " ".join(str(tuple(r)) for r in bare))


class PostgresParity(PgCase):
    def test_backends_return_same_asset_and_hit_sets(self):
        lite = Store(":memory:")
        self.addCleanup(lite.close)
        pg_store = self.store()
        scenarios = []
        for store in (lite, pg_store):
            ids = {}
            for name, text, show in (("a", "the budget vote passed", "news"), ("b", "the vote budget failed", "sport"),
                                     ("c", "zoë reports the tax budget", "news"), ("d", "co-op housing plan", "news")):
                asset = store.mint_asset(owner="desk", show_name=show)
                library.index_words(store, asset.id, words(text))
                ids[name] = asset.id
            metadata.define_field(store, "guest", "Guest", "text")
            metadata.set_values(store, ids["d"], "guest", ["Chidi Okonkwo"], "desk")
            scenarios.append((store, ids))
        queries = [{"q": "budget"}, {"q": "budg"}, {"q": '"budget vote"'}, {"q": "budget -tax"}, {"q": "zoe"}, {"q": "ZOË"},
                   {"q": "co op"}, {"q": "okonkwo"}, {"q": "vote", "scope": "transcript"},
                   {"q": "budget", "filter": {"all": [{"field": "show", "op": "eq", "value": "news"}]}},
                   {"filter": {"all": [{"field": "show", "op": "ne", "value": "news"}]}},
                   {"q": "budget", "group": "hit"}, {"q": "the", "group": "hit"}]
        for req in queries:
            results = []
            for store, ids in scenarios:
                inv = {v: k for k, v in ids.items()}
                out = searchindex.search(store, dict(req))
                if req.get("group") == "hit":
                    results.append(sorted((inv[h["assetId"]], h["kind"], h["snippet"]) for h in out["hits"]))
                else:
                    results.append(sorted((inv[x["assetId"]], tuple(sorted(h["kind"] for h in x["hits"]))) for x in out["assets"]))
            self.assertEqual(results[0], results[1], req)


class Preflight(unittest.TestCase):
    def test_check_warns_without_fts5_and_is_quiet_with_it(self):
        from volt import preflight
        with mock.patch.object(searchindex, "sqlite_has_fts5", return_value=False):
            r = preflight._search_index("volt.db")
        self.assertEqual((r.name, r.status), ("search index", preflight.WARN))
        self.assertIn("FTS5", r.detail)
        with mock.patch.object(searchindex, "sqlite_has_fts5", return_value=True):
            self.assertEqual(preflight._search_index("volt.db").status, preflight.OK)
        self.assertEqual(preflight._search_index("postgresql://u@h/d").status, preflight.OK)


if __name__ == "__main__":
    unittest.main()
