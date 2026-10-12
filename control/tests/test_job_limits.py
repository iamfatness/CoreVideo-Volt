"""The four job limits: one claim, one encode, one transcript."""

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from volt import publish, record, speech
from volt.store import Store


def _clip(path: Path) -> None:
    path.write_bytes(b"media")


class JobLimitTests(unittest.TestCase):
    def test_publish_heartbeat_keeps_the_claim(self):
        with TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "volt.db"))
            asset = store.mint_asset(owner="desk")
            store.set_cleared(asset.id, True)
            span = store.add_span(asset.id, "marker", "00:00:00:00", "00:00:01:00")
            seen = {}

            def renderer(store, span, kind):
                row = store.conn.execute(
                    "SELECT lease_expires_at, claimed_by FROM job WHERE idempotency_key = ?",
                    (f"publish:{span['id']}:{kind}",)).fetchone()
                seen["lease"] = row["lease_expires_at"]
                seen["owner"] = row["claimed_by"]
                return f"volt://{kind}/{span['asset_id']}"

            versions = publish.publish(store, span, renderer=renderer, lease=3.0)
            self.assertTrue(all(v.status == "done" for v in versions))
            self.assertTrue(seen["lease"])
            self.assertTrue(seen["owner"])
            store.close()

    def test_second_proxy_does_not_encode(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "iso.mp4"
            _clip(src)
            store = Store()
            asset = store.mint_asset(owner="desk")
            store.add_essence(asset.id, role="hi-res", location=str(src), open_file=True)
            calls = []

            def run(cmd, **kw):
                calls.append(cmd[-1])
                Path(cmd[-1]).write_bytes(b"proxy")
                with self.assertRaises(record.ProxyBusy):
                    record.build_proxy(store, asset.id, root / "proxy", owned=True)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(record.subprocess, "run", run):
                record.build_proxy(store, asset.id, root / "proxy", owned=True)
            self.assertEqual(len(calls), 1)

    def test_open_file_proxy_does_not_replace_a_wrapped_one(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "iso.mp4"
            _clip(src)
            store = Store()
            asset = store.mint_asset(owner="desk")
            store.add_essence(asset.id, role="hi-res", location=str(src), open_file=False)
            dest = root / "proxy" / f"{asset.id}.mp4"

            def final(cmd, **kw):
                Path(cmd[-1]).write_bytes(b"final")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(record.subprocess, "run", final):
                record.build_proxy(store, asset.id, root / "proxy", owned=True)
            store.conn.execute("UPDATE essence SET open = 1 WHERE asset_id = ? AND role = 'hi-res'", (asset.id,))
            store.conn.commit()

            def late(cmd, **kw):
                store.conn.execute("UPDATE essence SET open = 0 WHERE asset_id = ? AND role = 'hi-res'", (asset.id,))
                store.conn.commit()
                Path(cmd[-1]).write_bytes(b"short")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(record.subprocess, "run", late):
                record.build_proxy(store, asset.id, root / "proxy", owned=True)
            self.assertEqual(dest.read_bytes(), b"final")

    def test_second_transcribe_does_not_run_the_engine(self):
        with TemporaryDirectory() as tmp:
            store = Store()
            asset = store.mint_asset(owner="desk")
            store.set_cleared(asset.id, True)
            src = Path(tmp) / "iso.wav"
            src.write_bytes(b"wav")
            store.add_essence(asset.id, role="hi-res", location=str(src), open_file=False, frame_rate="30")
            calls = []

            class Engine:
                def describe(self):
                    return {"engine": "fake", "model": "tiny"}

                def transcribe(self, wav, vocabulary=()):
                    calls.append(wav)
                    return [speech.Word("steal", 0.0, 0.4)]

            slot = speech._claim_slot(store, asset.id)
            try:
                with self.assertRaises(speech.SpeechError):
                    speech.transcribe(store, asset.id, Engine())
            finally:
                speech._release_slot(store, slot)
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
