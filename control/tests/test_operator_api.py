import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tests.test_playout import Clock, _show
from tests.test_record import _clip
from tests.test_rooms import obs
from tests.test_take import _show as _cue_show
from volt import serve
from volt.library import index_words
from volt.playout import hold, on_air
from volt.publish import publish
from volt.rooms import FakeRoom
from volt.rundown import add_item, open_show
from volt.store import Store

TOKEN = "t" * 24


class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "volt.db")
        self.store = Store(self.path)
        self._saved = (serve.DB_PATH, serve.TOKEN, serve.ROOM, dict(serve.POLL), serve.PUBLISH, serve.MAX_BODY)
        serve.DB_PATH, serve.TOKEN, serve.ROOM = self.path, None, None
        serve.POLL.update(attempts=2, interval=0, sleep=lambda _: None)
        serve.PUBLISH = lambda store, span, out: publish(store, span)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        serve.DB_PATH, serve.TOKEN, serve.ROOM, poll, serve.PUBLISH, serve.MAX_BODY = self._saved
        serve.POLL.update(poll)
        self.store.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, token=None, headers=None, raw=None, ctype="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=15)
        h = dict(headers or {})
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        if data is not None:
            h["Content-Type"] = ctype
        if token:
            h["Authorization"] = f"Bearer {token}"
        try:
            conn.request(method, path, body=data, headers=h)
            resp = conn.getresponse()
            payload = resp.read()
            is_json = resp.getheader("Content-Type") == "application/json"
            return resp.status, (json.loads(payload) if payload and is_json else None), resp
        finally:
            conn.close()

    def post(self, path, body=None, **kw):
        status, payload, _ = self.call("POST", path, {} if body is None else body, **kw)
        return status, payload

    def refused(self, status, code, path, body=None, **kw):
        s, p = self.post(path, body, **kw)
        self.assertEqual(s, status, p)
        self.assertFalse(p["ok"])
        self.assertEqual(p["error"]["code"], code)
        self.assertTrue(p["error"]["reason"])
        return p

    def statuses(self, show):
        return [(r["id"], r["status"]) for r in self.store.conn.execute(
            "SELECT id, status FROM item WHERE show_id = ? ORDER BY position", (show,))]


class Go(Api):
    def test_go_returns_changed_state(self):
        show, (a, b, c) = _show(self.store)
        s, p = self.post(f"/api/shows/{show}/go")
        self.assertEqual((s, p["target"], p["previous"], p["over"]), (200, a, None, False))
        self.assertEqual(p["rundown"]["items"][0]["status"], "on air")
        self.assertIsNone(p["rundown"]["hold"])
        s, p = self.post(f"/api/shows/{show}/go")
        self.assertEqual((p["target"], p["previous"]), (b, a))

    def test_go_past_the_end_is_over(self):
        show, ids = _show(self.store, count=1)
        self.post(f"/api/shows/{show}/go")
        s, p = self.post(f"/api/shows/{show}/go")
        self.assertEqual((s, p["target"], p["over"]), (200, None, True))

    def test_held_refuses_and_changes_nothing(self):
        show, ids = _show(self.store)
        hold(self.store, show, "tech", Clock())
        before = self.statuses(show)
        self.refused(409, "held", f"/api/shows/{show}/go")
        self.assertEqual(self.statuses(show), before)

    def test_not_cleared_refuses_and_changes_nothing(self):
        show, ids = _show(self.store, cleared=False)
        before = self.statuses(show)
        self.refused(409, "not_cleared", f"/api/shows/{show}/go")
        self.assertEqual(self.statuses(show), before)

    def test_unknown_show_and_bad_input(self):
        self.refused(404, "not_found", "/api/shows/nope/go")
        show, _ = _show(self.store)
        self.refused(422, "bad_input", f"/api/shows/{show}/go", {"surprise": 1})
        self.refused(404, "not_found", "/api/shows/x/unknown-verb")

    def test_item_without_media_is_a_stable_refusal(self):
        show = open_show(self.store, "Empty")
        add_item(self.store, show, "no-media")
        self.refused(409, "no_media", f"/api/shows/{show}/go")


class HoldRelease(Api):
    def test_hold_release_cycle(self):
        show, _ = _show(self.store)
        s, p = self.post(f"/api/shows/{show}/hold", {"reason": "tech"})
        self.assertEqual((s, p["alreadyHeld"], p["rundown"]["hold"]["reason"]), (200, False, "tech"))
        s, p = self.post(f"/api/shows/{show}/hold", {"reason": "second"})
        self.assertEqual((p["alreadyHeld"], p["rundown"]["hold"]["reason"]), (True, "tech"))
        s, p = self.post(f"/api/shows/{show}/release")
        self.assertEqual((p["wasHeld"], p["rundown"]["hold"]), (True, None))
        s, p = self.post(f"/api/shows/{show}/release")
        self.assertEqual((s, p["wasHeld"]), (200, False))

    def test_refusals(self):
        show, _ = _show(self.store)
        self.refused(404, "not_found", "/api/shows/nope/hold")
        self.refused(404, "not_found", "/api/shows/nope/release")
        self.refused(422, "bad_input", f"/api/shows/{show}/hold", {"reason": 5})
        self.refused(422, "bad_input", f"/api/shows/{show}/hold", {"reason": "x" * 501})
        self.assertIsNone(self.post(f"/api/shows/{show}/release")[1]["rundown"]["hold"])


class Take(Api):
    def test_take_fires_cues_and_reports(self):
        show, (a,) = _cue_show(self.store, [[("overlay.take", "on")]])
        serve.ROOM = FakeRoom([obs("idle")], states=[{"lowerThirdOnAir": True}])
        s, p = self.post(f"/api/shows/{show}/take")
        self.assertEqual((s, p["advanced"], p["item"]), (200, True, a))
        self.assertEqual([r["status"] for r in p["results"]], ["fired"])
        self.assertEqual(p["rundown"]["items"][0]["status"], "on air")
        self.assertEqual(on_air(self.store, show), a)
        self.assertEqual(self.store.list_audit(show)[0]["action"], "take")
        self.assertEqual(self.store.list_audit(show)[0]["action"], "take")

    def test_failing_cue_rewinds_and_names_the_cue(self):
        show, (a, b) = _cue_show(self.store, [[], [("overlay.take", "on"), ("stream.start", "")]])
        serve.ROOM = FakeRoom([obs("idle")], senders=[["requested"]], states=[{"lowerThirdOnAir": True}])
        self.assertEqual(self.post(f"/api/shows/{show}/take")[0], 200)
        p = self.refused(409, "cue_failed", f"/api/shows/{show}/take")
        self.assertEqual(p["item"], b)
        self.assertEqual(p["cue"]["command"], "stream.start")
        self.assertEqual(p["cue"]["status"], "failed")
        self.assertEqual([r["status"] for r in p["results"]], ["fired", "failed"])
        self.assertEqual(on_air(self.store, show), a)
        self.assertEqual([i["status"] for i in p["rundown"]["items"]], ["on air", "ready"])
        self.assertEqual(self.statuses(show), [(a, "on air"), (b, "ready")])

    def test_failed_cue_is_remembered_until_it_fires(self):
        show, (a, b) = _cue_show(self.store, [[], [("overlay.take", "on"), ("stream.start", "")]])
        serve.ROOM = FakeRoom([obs("idle")], senders=[["requested"]], states=[{"lowerThirdOnAir": True}])
        self.assertEqual(self.post(f"/api/shows/{show}/take")[0], 200)
        self.refused(409, "cue_failed", f"/api/shows/{show}/take")

        def cue_rows():
            s, state, _ = self.call("GET", f"/api/state?show={show}")
            self.assertEqual(s, 200)
            return state["cues"][b]

        fired, failed = cue_rows()
        self.assertEqual((fired["status"], fired["failure"]), ("fired", None))
        self.assertEqual((failed["status"], failed["failure"]["status"]), ("armed", "failed"))
        self.assertIn("no sender observed producing", failed["failure"]["detail"])
        # a later, unrelated read still reports it
        self.assertEqual(cue_rows()[1]["failure"]["status"], "failed")
        # retried and fired: the failure is gone and the cue is fired
        serve.ROOM = FakeRoom([obs("idle")], senders=[["producing"]])
        self.assertEqual(self.post(f"/api/shows/{show}/take")[0], 200)
        self.assertEqual([(c["status"], c["failure"]) for c in cue_rows()], [("fired", None), ("fired", None)])

    def test_unsupported_cue_is_remembered_too(self):
        show, (a,) = _cue_show(self.store, [[("audio.snapshot", "walk-in")]])
        serve.ROOM = FakeRoom([obs("idle")])
        self.refused(409, "cue_unsupported", f"/api/shows/{show}/take")
        cue = self.call("GET", f"/api/state?show={show}")[1]["cues"][a][0]
        self.assertEqual((cue["status"], cue["failure"]["status"]), ("armed", "unsupported"))

    def test_unsupported_cue_has_its_own_code(self):
        show, (a,) = _cue_show(self.store, [[("audio.snapshot", "walk-in")]])
        serve.ROOM = FakeRoom([obs("idle")])
        p = self.refused(409, "cue_unsupported", f"/api/shows/{show}/take")
        self.assertEqual(p["cue"]["command"], "audio.snapshot")
        self.assertEqual(self.statuses(show), [(a, "ready")])

    def test_refusals(self):
        show, (a,) = _cue_show(self.store, [[]])
        self.refused(503, "no_room", f"/api/shows/{show}/take")
        serve.ROOM = FakeRoom([obs("idle")])
        self.refused(404, "not_found", "/api/shows/nope/take")
        self.refused(422, "bad_actor", f"/api/shows/{show}/take", {"owner": 3})
        hold(self.store, show, "tech", Clock())
        self.refused(409, "held", f"/api/shows/{show}/take")
        self.assertEqual(self.statuses(show), [(a, "ready")])
        self.assertEqual(serve.ROOM.calls, [])

    def test_not_cleared_refuses(self):
        show, (a,) = _cue_show(self.store, [[]])
        self.store.conn.execute("UPDATE asset SET cleared = FALSE")
        self.store.conn.commit()
        serve.ROOM = FakeRoom([obs("idle")])
        self.refused(409, "not_cleared", f"/api/shows/{show}/take")
        self.assertEqual(self.statuses(show), [(a, "ready")])

    def test_go_and_state_see_taking_while_dispatch_waits(self):
        show, (a, b) = _cue_show(self.store, [[("overlay.take", "on")], []])
        entered, gate = threading.Event(), threading.Event()

        class SlowRoom(FakeRoom):
            def invoke(self, action, args):
                entered.set()
                gate.wait(10)
                super().invoke(action, args)

        serve.ROOM = SlowRoom([obs("idle")], states=[{"lowerThirdOnAir": True}])
        with ThreadPoolExecutor(2) as pool:
            take = pool.submit(self.post, f"/api/shows/{show}/take")
            self.assertTrue(entered.wait(10))
            p = self.refused(409, "taking", f"/api/shows/{show}/go")
            self.assertEqual(p["item"], a)
            self.refused(409, "taking", f"/api/items/{b}/skip")
            self.refused(409, "taking", f"/api/items/{b}/kill")
            s, state, _ = self.call("GET", f"/api/state?show={show}")
            self.assertEqual(s, 200)
            self.assertEqual(state["rundown"]["items"][0]["status"], "taking")
            gate.set()
            self.assertEqual(take.result(15)[0], 200)
        self.assertEqual(self.statuses(show), [(a, "on air"), (b, "ready")])


class Items(Api):
    def test_skip(self):
        show, (a, b, c) = _show(self.store)
        s, p = self.post(f"/api/items/{b}/skip")
        self.assertEqual((s, p["rundown"]["items"][1]["status"]), (200, "skipped"))
        self.refused(409, "not_skippable", f"/api/items/{b}/skip")
        self.post(f"/api/shows/{show}/go")
        self.refused(409, "not_skippable", f"/api/items/{a}/skip")
        self.refused(404, "not_found", "/api/items/nope/skip")

    def test_kill(self):
        show, (a, b, c) = _show(self.store)
        s, p = self.post(f"/api/items/{b}/kill")
        self.assertEqual((s, p["rundown"]["items"][1]["status"]), (200, "killed"))
        self.refused(409, "not_killable", f"/api/items/{b}/kill")
        self.post(f"/api/shows/{show}/go")
        self.refused(409, "not_killable", f"/api/items/{a}/kill")
        self.refused(404, "not_found", "/api/items/nope/kill")
        # killed items are passed over, not aired
        s, p = self.post(f"/api/shows/{show}/go")
        self.assertEqual((p["target"], p["passed"]), (c, [b]))

    def test_attach(self):
        show = open_show(self.store, "S")
        item = add_item(self.store, show, "x")
        asset = self.store.mint_asset(owner="desk")
        index_words(self.store, asset.id, [("w", "01:00:00:00", "01:00:01:00")])
        span = self.store.conn.execute("SELECT id FROM span").fetchone()["id"]
        s, p = self.post(f"/api/items/{item}/attach", {"span": span})
        self.assertEqual((s, p["item"], p["span"]), (200, item, span))
        self.refused(404, "not_found", f"/api/items/{item}/attach", {"span": "nope"})
        self.refused(404, "not_found", "/api/items/nope/attach", {"span": span})
        self.refused(422, "bad_input", f"/api/items/{item}/attach", {})

    def test_reorder(self):
        show, (a, b, c) = _show(self.store)
        s, p = self.post(f"/api/shows/{show}/reorder", {"items": [c, a, b]})
        self.assertEqual((s, [i["id"] for i in p["rundown"]["items"]]), (200, [c, a, b]))
        for bad in ([a, b], [a, a, b, c], [a, b, "ghost"], "abc"):
            self.refused(422, "bad_input", f"/api/shows/{show}/reorder", {"items": bad})
        self.assertEqual([i for i, _ in self.statuses(show)], [c, a, b])
        self.refused(404, "not_found", "/api/shows/nope/reorder", {"items": []})


class Record(Api):
    def test_start_and_stop(self):
        iso = Path(self.tmp.name) / "iso.mp4"
        _clip(iso)
        serve.ROOM = FakeRoom([obs("producing", path=str(iso))])
        s, p = self.post("/api/record/start", {"camera": "iso-3", "show": "Final Four"})
        self.assertEqual((s, p["minted"], p["asset"]["camera"]), (200, True, "iso-3"))
        asset = p["asset"]["id"]
        serve.ROOM = FakeRoom([obs("completed", path=str(iso), finalized=True)])
        s, p = self.post("/api/record/stop", {"asset": asset})
        self.assertEqual((s, len(p["checksum"])), (200, 64))

    def test_refusals(self):
        self.refused(503, "no_room", "/api/record/start")
        serve.ROOM = FakeRoom([obs("requested")])
        self.refused(502, "room_refused", "/api/record/start")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 0)
        self.refused(422, "bad_input", "/api/record/start", {"camera": 4})
        self.refused(422, "bad_input", "/api/record/stop", {})
        self.refused(404, "not_found", "/api/record/stop", {"asset": "nope"})

    def test_stop_without_finalize_does_not_wrap(self):
        iso = Path(self.tmp.name) / "iso.mp4"
        _clip(iso)
        serve.ROOM = FakeRoom([obs("producing", path=str(iso))])
        asset = self.post("/api/record/start")[1]["asset"]["id"]
        serve.ROOM = FakeRoom([obs("stopping", path=str(iso))])
        self.refused(502, "not_wrapped", "/api/record/stop", {"asset": asset})
        self.assertEqual(self.store.conn.execute("SELECT open FROM essence WHERE role='hi-res'").fetchone()[0], 1)


class Rights(Api):
    def setUp(self):
        super().setUp()
        self.asset = self.store.mint_asset(owner="desk").id

    def test_cleared_and_legal_hold(self):
        s, p = self.post(f"/api/assets/{self.asset}/cleared", {"cleared": True})
        self.assertEqual((s, p["asset"]["cleared"], p["asset"]["legalHold"]), (200, True, False))
        s, p = self.post(f"/api/assets/{self.asset}/legal-hold", {"held": True})
        self.assertEqual((p["asset"]["cleared"], p["asset"]["legalHold"]), (True, True))
        s, p = self.post(f"/api/assets/{self.asset}/legal-hold", {"held": False})
        self.assertFalse(p["asset"]["legalHold"])

    def test_refusals(self):
        self.refused(404, "not_found", "/api/assets/nope/cleared", {"cleared": True})
        self.refused(404, "not_found", "/api/assets/nope/legal-hold", {"held": True})
        self.refused(422, "bad_input", f"/api/assets/{self.asset}/cleared", {"cleared": "yes"})
        self.refused(422, "bad_input", f"/api/assets/{self.asset}/cleared", {})
        self.refused(422, "bad_input", f"/api/assets/{self.asset}/legal-hold", {"held": 1})
        self.assertFalse(self.store.get_asset(self.asset).cleared)

    def test_legal_hold_blocks_air(self):
        show, ids = _show(self.store)
        asset = self.store.conn.execute("SELECT id FROM asset WHERE id != ?", (self.asset,)).fetchone()["id"]
        self.post(f"/api/assets/{asset}/legal-hold", {"held": True})
        self.refused(409, "not_cleared", f"/api/shows/{show}/go")


class Publish(Api):
    def _span(self, cleared):
        asset = self.store.mint_asset(owner="desk")
        self.store.set_cleared(asset.id, cleared)
        index_words(self.store, asset.id, [("w", "01:00:00:00", "01:00:01:00")])
        return self.store.conn.execute("SELECT id FROM span").fetchone()["id"]

    def test_publish(self):
        span = self._span(True)
        s, p = self.post(f"/api/spans/{span}/publish")
        self.assertEqual(s, 200)
        self.assertEqual([v["kind"] for v in p["versions"]], ["house", "ott", "social"])
        self.assertTrue(all(v["status"] == "done" for v in p["versions"]))

    def test_refusals(self):
        span = self._span(False)
        self.refused(409, "not_cleared", f"/api/spans/{span}/publish")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM version").fetchone()[0], 0)
        self.refused(404, "not_found", "/api/spans/nope/publish")
        self.refused(422, "bad_input", f"/api/spans/{span}/publish", {"x": 1})

    def test_render_failure_is_reported(self):
        span = self._span(True)
        serve.PUBLISH = lambda store, sp, out: publish(store, sp, fail="ott")
        p = self.refused(502, "publish_failed", f"/api/spans/{span}/publish")
        self.assertEqual([v["status"] for v in p["versions"]], ["done", "failed", "done"])


class Auth(Api):
    def test_loopback_needs_no_token(self):
        show, _ = _show(self.store)
        self.assertEqual(self.post(f"/api/shows/{show}/hold")[0], 200)

    def test_token_missing_or_wrong_is_401_and_changes_nothing(self):
        serve.TOKEN = TOKEN
        show, _ = _show(self.store)
        for kw in ({}, {"token": "wrong"}, {"token": TOKEN[:-1]}, {"token": TOKEN + "x"}):
            s, p, resp = self.call("POST", f"/api/shows/{show}/hold", {}, **kw)
            self.assertEqual((s, p["error"]["code"]), (401, "unauthorized"))
            self.assertEqual(resp.getheader("WWW-Authenticate"), "Bearer")
        s, p, _ = self.call("POST", f"/api/shows/{show}/hold", {}, headers={"Authorization": f"Basic {TOKEN}"})
        self.assertEqual(s, 401)
        self.assertIsNone(self.store.conn.execute("SELECT held_at FROM show").fetchone()[0])
        self.assertEqual(self.post(f"/api/shows/{show}/hold", token=TOKEN)[0], 200)

    def test_get_data_routes_need_the_token_but_the_shell_does_not(self):
        serve.TOKEN = TOKEN
        show, _ = _show(self.store)
        self.assertEqual(self.call("GET", f"/rundown?show={show}")[0], 401)
        self.assertEqual(self.call("GET", "/assets")[0], 401)
        s, body, _ = self.call("GET", f"/rundown?show={show}", token=TOKEN)
        self.assertEqual((s, sorted(body)), (200, ["hold", "items"]))

    def test_auth_precedes_routing(self):
        serve.TOKEN = TOKEN
        self.assertEqual(self.call("POST", "/api/nonsense", {})[0], 401)

    def test_foreign_host_is_refused_without_a_token(self):
        show, _ = _show(self.store)
        s, p, _ = self.call("POST", f"/api/shows/{show}/hold", {}, headers={"Host": "evil.example:8765"})
        self.assertEqual((s, p["error"]["code"]), (403, "bad_host"))

    def test_non_loopback_bind_without_token_refuses_to_start(self):
        for host in ("0.0.0.0", "192.168.1.50", "::", ""):
            with self.assertRaises(serve.ConfigError, msg=host):
                serve.check_bind(host, None)
            with self.assertRaises(serve.ConfigError, msg=host):
                serve.check_bind(host, "short")
        for host in ("127.0.0.1", "::1", "localhost", "127.0.0.2"):
            serve.check_bind(host, None)
        serve.check_bind("0.0.0.0", TOKEN)

    def test_main_exits_on_non_loopback_without_token(self):
        with mock.patch.dict(os.environ, {"VOLT_HOST": "0.0.0.0"}), mock.patch.object(serve, "TOKEN", None):
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()) as err:
                serve.main([])
        self.assertEqual(cm.exception.code, serve.EX_CONFIG)  # 78: a supervisor must not restart a bad configuration
        self.assertIn("VOLT_TOKEN", err.getvalue())

    def test_no_cors_headers_anywhere(self):
        show, _ = _show(self.store)
        _, _, resp = self.call("POST", f"/api/shows/{show}/hold", {}, headers={"Origin": "https://evil.example"})
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port)
        conn.request("OPTIONS", f"/api/shows/{show}/hold", headers={"Origin": "https://evil.example"})
        resp = conn.getresponse()
        resp.read()
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        self.assertGreaterEqual(resp.status, 400)
        conn.close()


class Body(Api):
    def test_limits_and_types(self):
        show, _ = _show(self.store)
        path = f"/api/shows/{show}/hold"
        serve.MAX_BODY = 100
        s, p, _ = self.call("POST", path, raw=b'{"reason": "' + b"x" * 200 + b'"}')
        self.assertEqual((s, p["error"]["code"]), (413, "too_large"))
        serve.MAX_BODY = 64 * 1024
        s, p, _ = self.call("POST", path, raw=b"{}", ctype="text/plain")
        self.assertEqual((s, p["error"]["code"]), (415, "unsupported_media_type"))
        for raw in (b"{nope", b"[1]", b'"s"', b"\xff\xfe"):
            s, p, _ = self.call("POST", path, raw=raw)
            self.assertEqual((s, p["error"]["code"]), (422, "bad_input"), raw)
        self.assertIsNone(self.store.conn.execute("SELECT held_at FROM show").fetchone()[0])
        s, p, _ = self.call("POST", path, raw=b"")
        self.assertEqual(s, 200)


class Existing(Api):
    def test_get_rundown_shape_is_unchanged(self):
        show, ids = _show(self.store)
        s, body, _ = self.call("GET", f"/rundown?show={show}")
        self.assertEqual((s, sorted(body)), (200, ["hold", "items"]))
        # The millisecond as-run (migration 13) is the one addition; every older key is still there.
        self.assertEqual(sorted(body["items"][0]), ["asRunIn", "asRunInPrecise", "asRunOut", "asRunOutPrecise",
                                                    "id", "slug", "status", "urls"])
        self.assertEqual(self.call("GET", "/nope")[0], 404)

    def test_get_rundown_unknown_show_is_a_404_json_error(self):
        s, body, _ = self.call("GET", "/rundown?show=no-such-show")
        self.assertEqual(s, 404)
        self.assertEqual(body["ok"], False)
        self.assertEqual(body["error"]["code"], "not_found")
        self.assertIn("no-such-show", body["error"]["reason"])
        self.assertEqual(self.call("GET", "/rundown")[1], {"hold": None, "items": []})


class Concurrency(Api):
    def test_parallel_go_never_double_advances(self):
        show, ids = _show(self.store, count=3)
        n = 8
        with ThreadPoolExecutor(n) as pool:
            out = list(pool.map(lambda _: self.post(f"/api/shows/{show}/go"), range(n)))
        self.assertTrue(all(s == 200 for s, _ in out), out)
        targets = [p["target"] for _, p in out if p["target"]]
        self.assertEqual(sorted(targets), sorted(ids))
        self.assertEqual(sum(1 for _, p in out if p["over"]), n - 3)
        self.assertEqual([st for _, st in self.statuses(show)], ["aired", "aired", "aired"])

    def test_hold_lands_while_a_take_waits_on_the_room(self):
        show, (a,) = _cue_show(self.store, [[("overlay.take", "on")]])
        gate, entered = threading.Event(), threading.Event()

        class SlowRoom(FakeRoom):
            def invoke(self, action, args):
                entered.set()
                gate.wait(10)
                super().invoke(action, args)

        serve.ROOM = SlowRoom([obs("idle")], states=[{"lowerThirdOnAir": True}])
        with ThreadPoolExecutor(2) as pool:
            take = pool.submit(self.post, f"/api/shows/{show}/take")
            self.assertTrue(entered.wait(10))
            s, p = self.post(f"/api/shows/{show}/hold", {"reason": "mid-take"})
            self.assertEqual((s, p["rundown"]["hold"]["reason"]), (200, "mid-take"))
            gate.set()
            self.assertEqual(take.result(15)[0], 200)

    def test_parallel_reads_each_get_a_connection(self):
        show, _ = _show(self.store)
        with ThreadPoolExecutor(10) as pool:
            out = list(pool.map(lambda _: self.call("GET", f"/rundown?show={show}")[0], range(20)))
        self.assertEqual(set(out), {200})


if __name__ == "__main__":
    unittest.main()
