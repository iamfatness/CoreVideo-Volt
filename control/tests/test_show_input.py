import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests.test_dispatch import _armed_item, _statuses
from tests.test_playout import Clock
from tests.test_rooms import _no_sleep, obs
from tests.test_take import _show
from volt.dispatch import dispatch_item
from volt.playout import on_air
from volt.rooms import CoreVideoProRoom, FakeRoom, LiveError, ProgramObservation, ProgramSource
from volt.show_input import parse_payload, plan_actions, show_input_take
from volt.store import Store
from volt.take import take_item


def src(source_id, participant_id="", layer="L1", kind="zoom"):
    return ProgramSource(layer, source_id, participant_id, kind)


def prog(seq, *sources, attribution="live", stale=False, available=True):
    return ProgramObservation(available, stale, attribution, seq, "scene-a", list(sources))


OLD = src("zoom:1", "1", layer="L1")
NEW = src("zoom:2", "2", layer="L1")
CUT = [prog(10, OLD), prog(11, NEW), prog(12, NEW)]
SCENE = json.dumps({"source": "zoom:2", "scene": "scene-b"})


def _run(room, payload=SCENE, **kw):
    show_input_take(room, payload, 4, 0, _no_sleep, **kw)


def _fails(test, room, text, payload=SCENE, **kw):
    with test.assertRaises(LiveError) as ctx:
        _run(room, payload, **kw)
    test.assertIn(text, str(ctx.exception))


class Payload(unittest.TestCase):
    def test_bare_string_is_a_source_id(self):
        si = parse_payload("camera-2")
        self.assertEqual((si.source, si.scene, si.transition), ("camera-2", None, "cut"))

    def test_bad_payloads_fail_closed(self):
        for bad in ("", "  ", "{", '{"scene":"x"}', '{"source":"a","transition":"wipe"}',
                    '{"source":"a","slot":"two"}'):
            with self.assertRaises(LiveError, msg=bad):
                parse_payload(bad)

    def test_default_action_is_scene_select_and_needs_a_scene(self):
        self.assertEqual(plan_actions(parse_payload(SCENE), None), [("scene.select", ["scene-b"])])
        with self.assertRaises(LiveError):
            plan_actions(parse_payload("zoom:2"), None)

    def test_templates_keep_slot_an_int(self):
        si = parse_payload('{"source":"zoom:2","slot":3,"scene":"s"}')
        steps = plan_actions(si, [{"action": "input.assign", "args": ["{slot}", "{source}"]},
                                  {"action": "scene.select", "args": ["{scene}"]}])
        self.assertEqual(steps, [("input.assign", [3, "zoom:2"]), ("scene.select", ["s"])])


class Evidence(unittest.TestCase):
    def test_happy_cut_sends_actions_in_order_then_take(self):
        room = FakeRoom([obs("idle")], program=CUT)
        _run(room)
        self.assertEqual(room.calls, [("scene.select", ["scene-b"]), ("transport.take", [])])

    def test_configured_actions_replace_the_default(self):
        room = FakeRoom([obs("idle")], program=CUT)
        _run(room, actions=[{"action": "input.assign", "args": [1, "{source}"]},
                            {"action": "scene.select", "args": ["{scene}"]}])
        self.assertEqual([c[0] for c in room.calls], ["input.assign", "scene.select", "transport.take"])

    def test_ack_without_program_change_is_not_evidence(self):
        room = FakeRoom([obs("idle")], program=[prog(10, OLD), prog(11, OLD)])
        _fails(self, room, "source 'zoom:2' not on program")

    def test_unavailable_before_the_take_sends_nothing(self):
        for base in (prog(10, OLD, available=False), prog(10, OLD, stale=True),
                     prog(None, OLD)):
            room = FakeRoom([obs("idle")], program=[base])
            _fails(self, room, "nothing sent")
            self.assertEqual(room.calls, [])

    def test_no_program_script_is_unavailable(self):
        room = FakeRoom([obs("idle")])
        _fails(self, room, "nothing sent")

    def test_bad_payload_or_template_sends_nothing(self):
        room = FakeRoom([obs("idle")], program=CUT)
        _fails(self, room, "no source", payload="")
        _fails(self, room, "needs scene", payload="zoom:2")
        self.assertEqual(room.calls, [])

    def test_stale(self):
        room = FakeRoom([obs("idle")], program=[prog(10, OLD), prog(11, NEW, stale=True)])
        _fails(self, room, "stale")

    def test_unavailable_after_the_take(self):
        room = FakeRoom([obs("idle")], program=[prog(10, OLD), prog(None, available=False)])
        _fails(self, room, "unavailable")

    def test_attribution_holding_or_unknown_is_not_live(self):
        for attr in ("holding", "unknown"):
            room = FakeRoom([obs("idle")], program=[prog(10, OLD), prog(11, NEW, attribution=attr)])
            _fails(self, room, f"scene attribution not live ({attr})")

    def test_sequence_not_advanced(self):
        room = FakeRoom([obs("idle")], program=[prog(10, OLD), prog(10, NEW)])
        _fails(self, room, "delivery sequence not advanced")

    def test_missing_sequence_after_take_is_not_advanced(self):
        room = FakeRoom([obs("idle")], program=[prog(10, OLD), prog(None, NEW)])
        _fails(self, room, "delivery sequence not advanced")

    def test_hard_cut_requires_outgoing_absent(self):
        room = FakeRoom([obs("idle")], program=[prog(10, OLD), prog(11, NEW, OLD), prog(12, NEW, OLD)])
        _fails(self, room, "outgoing source 'zoom:1' still on program")

    def test_a_source_that_was_already_on_program_is_not_outgoing(self):
        both = [prog(10, NEW), prog(11, NEW), prog(12, NEW)]
        _run(FakeRoom([obs("idle")], program=both))

    def test_rollback_after_one_good_reading_does_not_fire(self):
        room = FakeRoom([obs("idle")], program=[prog(10, OLD), prog(11, NEW), prog(12, OLD), prog(13, OLD)])
        _fails(self, room, "not on program")

    def test_single_good_reading_is_not_enough_if_delivery_stalls(self):
        room = FakeRoom([obs("idle")], program=[prog(10, OLD), prog(11, NEW), prog(11, NEW), prog(11, NEW)])
        _fails(self, room, "delivery stalled")

    def test_timeout_when_never_confirmed(self):
        room = FakeRoom([obs("idle")], program=[prog(10, OLD), prog(11, NEW)])
        _fails(self, room, "timed out")

    def test_participant_id_matches_and_ids_may_be_bare_or_prefixed(self):
        cases = [("2", src("zoom:2", "")), ("zoom:2", src("", "2")), ("2", src("", "zoom:2")),
                 ("media:clip-7", src("media:clip-7")), ("capture:hdmi", src("hdmi", "")),
                 ("hdmi", src("capture:hdmi"))]
        for target, new in cases:
            room = FakeRoom([obs("idle")], program=[prog(10, OLD), prog(11, new), prog(12, new)])
            _run(room, json.dumps({"source": target, "scene": "s"}))

    def test_prefixed_target_does_not_match_a_different_scheme(self):
        new = src("media:2", "")
        room = FakeRoom([obs("idle")], program=[prog(10, OLD), prog(11, new), prog(12, new)])
        _fails(self, room, "not on program", json.dumps({"source": "zoom:2", "scene": "s"}))


class Fade(unittest.TestCase):
    FADE = json.dumps({"source": "zoom:2", "scene": "scene-b", "transition": "fade"})

    def test_fade_allows_outgoing_but_waits_for_the_transition(self):
        both = [prog(10, OLD), prog(11, NEW, OLD), prog(12, NEW, OLD), prog(13, NEW), prog(14, NEW)]
        states = [{"takeTransition": {"status": "running"}}] * 2 + [{"takeTransition": {"status": "completed"}}]
        room = FakeRoom([obs("idle")], program=both, states=states)
        show_input_take(room, self.FADE, 6, 0, _no_sleep)

    def test_fade_with_target_present_but_transition_unfinished_fails(self):
        room = FakeRoom([obs("idle")], program=[prog(10, OLD), prog(11, NEW, OLD), prog(12, NEW, OLD)],
                        states=[{"takeTransition": {"status": "running"}}])
        _fails(self, room, "fade not finished", self.FADE)

    def test_fade_missing_transition_status_fails_closed(self):
        room = FakeRoom([obs("idle")], program=[prog(10, OLD), prog(11, NEW), prog(12, NEW)], states=[{}])
        _fails(self, room, "fade not finished", self.FADE)


class ThroughDispatchAndTake(unittest.TestCase):
    def test_fires_cue_only_on_evidence(self):
        store = Store()
        item = _armed_item(store, ("corevideo.pro", "show-input.take", SCENE))
        results = dispatch_item(store, FakeRoom([obs("idle")], program=CUT), item, attempts=4, sleep=_no_sleep)
        self.assertEqual([r.status for r in results], ["fired"])
        self.assertEqual(_statuses(store, item), ["fired"])

    def test_failed_cue_stays_armed_and_stops_later_cues(self):
        store = Store()
        item = _armed_item(store, ("corevideo.pro", "show-input.take", SCENE),
                           ("corevideo.pro", "overlay.take", "on"))
        room = FakeRoom([obs("idle")], program=[prog(10, OLD), prog(11, OLD)], states=[{"lowerThirdOnAir": True}])
        results = dispatch_item(store, room, item, attempts=3, sleep=_no_sleep)
        self.assertEqual([r.status for r in results], ["failed"])
        self.assertIn("not on program", results[0].detail)
        self.assertEqual(_statuses(store, item), ["armed", "armed"])
        self.assertFalse(room.invoked("graphics.lowerThird.set", [True]))

    def test_configured_actions_reach_the_handler(self):
        store = Store()
        item = _armed_item(store, ("corevideo.pro", "show-input.take", "zoom:2"))
        room = FakeRoom([obs("idle")], program=CUT)
        acts = [{"action": "scene.select", "args": ["fixed-scene"]}]
        dispatch_item(store, room, item, attempts=4, sleep=_no_sleep, show_input_actions=acts)
        self.assertTrue(room.invoked("scene.select", ["fixed-scene"]))

    def test_take_rewinds_the_rundown_on_failure(self):
        store = Store()
        show, (a,) = _show(store, [[("show-input.take", SCENE)]])
        room = FakeRoom([obs("idle")], program=[prog(10, OLD), prog(11, OLD)])
        result = take_item(store, room, show, clock=Clock(), attempts=3, sleep=_no_sleep)
        self.assertFalse(result.advanced)
        self.assertIsNone(on_air(store, show))
        self.assertEqual(_statuses(store, a), ["armed"])

    def test_take_advances_on_evidence(self):
        store = Store()
        show, (a,) = _show(store, [[("show-input.take", SCENE)]])
        room = FakeRoom([obs("idle")], program=CUT)
        result = take_item(store, room, show, clock=Clock(), attempts=4, sleep=_no_sleep)
        self.assertTrue(result.advanced)
        self.assertEqual(on_air(store, show), a)


class _Core(BaseHTTPRequestHandler):
    """Stub core: transport.take flips Program to the new source after `lag` snapshots."""

    posted = []
    taken = False
    seq = 100
    lag = 0
    polls_after_take = 0
    envelope = {}
    attribution = "live"
    flip = True
    transition = "completed"

    def log_message(self, *a):
        pass

    def _send(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        cls = _Core
        if self.path == "/state":
            return self._send({"takeTransition": {"status": cls.transition}})
        cls.seq += 1
        if cls.taken:
            cls.polls_after_take += 1
        on_new = cls.flip and cls.taken and cls.polls_after_take > cls.lag
        sources = [{"layerId": "L1", "sourceId": "zoom:2" if on_new else "zoom:1",
                    "participantId": "2" if on_new else "1", "kind": "zoom"}]
        env = {"available": True, "stale": False, "ageMs": 3,
               "snapshot": {"programFrame": {"sceneId": "scene-b", "sceneIdAttribution": cls.attribution,
                                             "deliverySequence": cls.seq, "videoSources": sources}}}
        env.update(cls.envelope)
        self._send(env)

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(n))
        _Core.posted.append((body["action"], body["args"]))
        if body["action"] == "transport.take":
            _Core.taken = True
        self._send({"ok": True})


class StubServer(unittest.TestCase):
    def setUp(self):
        _Core.posted, _Core.taken, _Core.seq, _Core.lag = [], False, 100, 0
        _Core.polls_after_take, _Core.envelope, _Core.attribution = 0, {}, "live"
        _Core.flip, _Core.transition = True, "completed"
        self.srv = HTTPServer(("127.0.0.1", 0), _Core)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.room = CoreVideoProRoom(f"http://127.0.0.1:{self.srv.server_port}")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_program_observed_parses_the_snapshot(self):
        o = self.room.program_observed()
        self.assertEqual((o.available, o.stale, o.attribution, o.scene_id), (True, False, "live", "scene-b"))
        self.assertEqual((o.sources[0].source_id, o.sources[0].participant_id, o.sources[0].kind),
                         ("zoom:1", "1", "zoom"))
        self.assertGreater(o.delivery_sequence, 100)

    def test_happy_path_over_http(self):
        _Core.lag = 1
        _run(self.room)
        self.assertEqual(_Core.posted, [("scene.select", ["scene-b"]), ("transport.take", [])])

    def test_ack_only_server_never_fires(self):
        _Core.flip = False
        _fails(self, self.room, "not on program")
        self.assertIn(("transport.take", []), _Core.posted)

    def test_stale_and_unavailable_envelopes(self):
        _Core.envelope = {"stale": True}
        _fails(self, self.room, "nothing sent")
        _Core.envelope = {"available": False, "reasonCode": "core-starting"}
        _fails(self, self.room, "nothing sent")
        self.assertEqual(_Core.posted, [])

    def test_holding_attribution_over_http(self):
        _Core.attribution = "holding"
        _fails(self, self.room, "scene attribution not live (holding)")

    def test_fade_waits_for_transition_over_http(self):
        _Core.transition = "running"
        fade = json.dumps({"source": "zoom:2", "scene": "scene-b", "transition": "fade"})
        _fails(self, self.room, "fade not finished", fade)

    def test_missing_program_frame_is_unavailable(self):
        _Core.envelope = {"snapshot": {}}
        _fails(self, self.room, "nothing sent")


if __name__ == "__main__":
    unittest.main()
