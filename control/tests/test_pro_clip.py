import json
import os
import shutil
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from tests import test_operator_api
from tests.test_deliverables import _asset, _span
from volt import serve
from volt.playout import on_air
from volt.rooms import CoreVideoProRoom
from volt.rundown import add_item, attach, open_show


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs FFmpeg")
class ProClip(test_operator_api.Api):
    def setUp(self):
        super().setUp()
        self.folder = Path(self.tmp.name) / "CoreVideoPro" / "media" / "Volt Rundown"
        self.enterContext(mock.patch.dict(os.environ, {
            "LOCALAPPDATA": self.tmp.name, "VOLT_PRO_CLIP_TARGET": '{"scene":"clips","route":"video"}',
        }))
        self.asset = _asset(self.store, Path(self.tmp.name))
        self.span = _span(self.store, self.asset)
        self.show = open_show(self.store, "News")
        self.previous = add_item(self.store, self.show, "Studio")
        self.item = add_item(self.store, self.show, "Package")
        attach(self.store, self.previous, self.span)
        attach(self.store, self.item, self.span)
        self.commands = []
        self.previewed = None
        self.taken = False
        self.ack_only = False
        self.sequence = 0
        self.media_id = "media:media-000000000001"
        test = self

        class Core(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send(self, data, code=200):
                encoded = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                action, args = data["action"], data["args"]
                test.commands.append(action)
                if action == "media.rundown.load" and (test.folder / (args[0] + ".mp4")).is_file():
                    return self.send({"ok": True, "data": {"sourceId": test.media_id}})
                if action == "media.rundown.preview" and args[1:] == ["clips", "video"]:
                    test.previewed = args
                    return self.send({"ok": True})
                if action == "media.rundown.take" and args == test.previewed:
                    test.taken = not test.ack_only
                    return self.send({"ok": True})
                self.send({"ok": False, "error": "Cue the clip on Preview first."}, 422)

            def do_GET(self):
                test.sequence += 1
                source = test.media_id if test.taken else "capture:camera"
                self.send({"available": True, "snapshot": {"programFrame": {
                    "sceneIdAttribution": "live", "deliverySequence": test.sequence,
                    "sceneId": "clips" if test.taken else "studio",
                    "videoSources": [{"layerId": "video", "sourceId": source}],
                }}})

        core = HTTPServer(("127.0.0.1", 0), Core)
        self.addCleanup(core.server_close)
        threading.Thread(target=core.serve_forever, daemon=True).start()
        self.addCleanup(core.shutdown)
        serve.ROOM = CoreVideoProRoom(f"http://127.0.0.1:{core.server_port}")
        self.assertEqual(self.post(f"/api/shows/{self.show}/go")[0], 200)

    def clip_action(self, action):
        return f"/api/items/{self.item}/pro-clip/{action}"

    def test_exact_clip_is_staged_once_then_previewed_and_confirmed_on_program(self):
        for _ in range(2):
            status, result = self.post(self.clip_action("stage"))
            self.assertEqual(status, 200, result)
            self.assertEqual(result["rundown"]["items"][1]["proClip"]["status"], "ready")
        clip, = self.folder.glob("*.mp4")
        frames = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=nb_frames",
            "-of", "csv=p=0", str(clip),
        ], text=True)
        self.assertEqual(frames.strip(), "15")
        self.assertEqual(self.post(self.clip_action("preview"))[0], 200)
        self.assertEqual(on_air(self.store, self.show), self.previous)
        self.refused(409, "pro_clip_needs_take", f"/api/shows/{self.show}/go")
        status, result = self.post(f"/api/shows/{self.show}/take")
        self.assertEqual(status, 200, result)
        self.assertEqual(on_air(self.store, self.show), self.item)
        self.assertEqual(self.commands, ["media.rundown.load", "media.rundown.load", "media.rundown.preview", "media.rundown.take"])

    def test_take_needs_preview_and_fresh_program_evidence(self):
        self.assertEqual(self.post(self.clip_action("stage"))[0], 200)
        path = f"/api/shows/{self.show}/take"
        refused = self.refused(409, "cue_failed", path)
        self.assertIn("Preview", refused["cue"]["detail"])
        self.assertEqual(self.post(self.clip_action("preview"))[0], 200)
        self.ack_only = True
        self.refused(409, "cue_failed", path)
        self.assertEqual(self.statuses(self.show), [(self.previous, "on air"), (self.item, "ready")])

    def test_replacement_and_legal_hold_cannot_use_the_old_cut(self):
        self.assertEqual(self.post(self.clip_action("stage"))[0], 200)
        replacement = _span(self.store, self.asset, tc_out="01:12:09:10")
        attach(self.store, self.item, replacement)
        self.refused(409, "pro_clip_failed", self.clip_action("preview"))
        self.store.set_hold(self.asset.id, True)
        self.refused(409, "pro_clip_failed", self.clip_action("stage"))
        self.assertEqual(self.commands, ["media.rundown.load"])
