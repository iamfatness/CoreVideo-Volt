import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.parse import urlparse
from urllib.request import url2pathname

from volt import deliverables
from volt.deliverables import tc_to_seconds
from volt.publish import publish_files
from volt.session import rundown
from volt.store import Store


def _clip(path: Path) -> None:
    subprocess.check_call(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=320x180:r=25:d=2",
         "-f", "lavfi", "-i", "sine=f=440:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _probe(path: Path) -> dict:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
         "-of", "json", str(path)], text=True)
    data = json.loads(out)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    return {"w": video["width"], "h": video["height"], "dur": float(data["format"]["duration"]),
            "audio": any(s["codec_type"] == "audio" for s in data["streams"])}


def _asset(store, root, open_file=False, tc_start="01:12:08:00"):
    clip = root / "iso.mp4"
    _clip(clip)
    asset = store.mint_asset(owner="desk")
    store.set_cleared(asset.id, True)
    store.add_essence(asset.id, role="hi-res", location=str(clip), open_file=open_file,
                      frame_rate="25", timecode_start=tc_start)
    return asset


def _span(store, asset, tc_in="01:12:08:25", tc_out="01:12:09:10"):
    return store.add_span(asset.id, "transcript", tc_in, tc_out, text="steal")


class Timecode(unittest.TestCase):
    def test_frames_to_seconds(self):
        self.assertAlmostEqual(tc_to_seconds("00:00:01:12", 25), 1.48)
        self.assertAlmostEqual(tc_to_seconds("01:00:00:00", 25), 3600)

    def test_bad_timecode_raises(self):
        with self.assertRaises(deliverables.DeliverableError):
            tc_to_seconds("nope", 25)


class Cuts(unittest.TestCase):
    def test_three_real_files_cut_at_the_span(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store()
            asset = _asset(store, root)
            span = _span(store, asset)  # 25 frames in, 15 frames long = 0.6 s
            versions = publish_files(store, span, root / "out")
            self.assertEqual([v.status for v in versions], ["done"] * 3)
            files = {v.kind: Path(url2pathname(urlparse(v.url).path)) for v in versions}
            for kind, path in files.items():
                self.assertTrue(path and path.exists(), kind)
            house, ott, social = (_probe(files[k]) for k in ("house", "ott", "social"))
            for info in (house, ott, social):
                self.assertAlmostEqual(info["dur"], 0.6, delta=0.2)
                self.assertTrue(info["audio"])
            self.assertEqual((house["w"], house["h"]), (320, 180))
            self.assertLessEqual(ott["h"], 720)
            self.assertEqual(social["w"] * 16, social["h"] * 9)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)

    def test_urls_land_on_the_rundown_item(self):
        from volt.rundown import add_item, attach, open_show
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store()
            asset = _asset(store, root)
            span = _span(store, asset)
            show = open_show(store, "Final Four")
            item = add_item(store, show, "Steal")
            attach(store, item, span)
            publish_files(store, span, root / "out")
            self.assertEqual(len(rundown(store, show)[0].urls), 3)

    def test_still_recording_asset_publishes_nothing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store()
            asset = _asset(store, root, open_file=True)
            span = _span(store, asset)
            versions = publish_files(store, span, root / "out")
            self.assertEqual([v.status for v in versions], ["failed"] * 3)
            self.assertFalse((root / "out").exists() and any((root / "out").rglob("*.mp4")))
            job = store.conn.execute("SELECT error FROM job WHERE type='publish' LIMIT 1").fetchone()
            self.assertIn("still recording", job["error"])

    def test_span_outside_the_clip_fails_and_leaves_no_file(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store()
            asset = _asset(store, root)
            span = _span(store, asset, "01:12:20:00", "01:12:21:00")
            versions = publish_files(store, span, root / "out")
            self.assertEqual([v.status for v in versions], ["failed"] * 3)
            self.assertEqual(list((root / "out").rglob("*.mp4")) if (root / "out").exists() else [], [])

    def test_retry_only_rerenders_failed_versions(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store()
            asset = _asset(store, root)
            span = _span(store, asset)
            real = deliverables.render
            calls = []

            def flaky(store_, span_row, kind, out_dir):
                calls.append(kind)
                if kind == "ott" and calls.count("ott") == 1:
                    raise deliverables.DeliverableError("encoder died")
                return real(store_, span_row, kind, out_dir)

            with mock.patch.object(deliverables, "render", flaky):
                first = publish_files(store, span, root / "out")
                self.assertEqual([v.status for v in first], ["done", "failed", "done"])
                second = publish_files(store, span, root / "out")
            self.assertEqual([v.status for v in second], ["done"] * 3)
            self.assertEqual(calls, ["house", "ott", "social", "ott"])
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM version").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
