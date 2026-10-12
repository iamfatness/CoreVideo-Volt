"""Race 4: the record-start proxy and the final proxy building one file on two runners."""

import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from volt import record

from tests.racekit import Bg, PgBase, SqliteBase


# ---- race 4: two proxy builds of one asset -------------------------------------------------------

class ProxyRaceTests:
    def _asset(self):
        self.a, self.b = self.new_store(), self.new_store()
        self.src = self.root / "iso.mp4"
        self.src.write_bytes(b"source")
        asset = self.a.mint_asset(owner="desk")
        self.a.add_essence(asset.id, role="hi-res", location=str(self.src), open_file=False)
        self.asset = asset.id
        self.proxy_dir = self.root / "proxy"
        self.outs = []
        self.fake_hook = None

    def _ffmpeg(self, content=b"proxy", rc=0):
        def run(cmd, **kw):
            out = Path(cmd[-1])
            self.outs.append(out)
            out.write_bytes(content)
            hook, self.fake_hook = self.fake_hook, None
            if hook:
                hook()
            return subprocess.CompletedProcess(cmd, rc, "", "boom" if rc else "")
        return mock.patch.object(record.subprocess, "run", run)

    def test_two_builds_at_once_do_not_write_the_same_output_path(self):
        self._asset()
        with self._ffmpeg():
            def second():
                with self.assertRaises(record.ProxyBusy):
                    record.build_proxy(self.b, self.asset, self.proxy_dir, owned=True)
            self.fake_hook = second
            record.build_proxy(self.a, self.asset, self.proxy_dir, owned=True)
        self.assertEqual(len(self.outs), 1)
        self.assertFalse(any(p.name.endswith(".proxy.lock") for p in self.proxy_dir.iterdir()))

    def test_a_failed_rebuild_leaves_the_good_proxy_alone(self):
        self._asset()
        with self._ffmpeg(b"good"):
            record.build_proxy(self.a, self.asset, self.proxy_dir, owned=True)
        import os
        os.utime(self.src, (4102444800, 4102444800))  # the source changed: the proxy is stale and gets rebuilt
        with self._ffmpeg(b"partial", rc=1):
            with self.assertRaises(record.RecordError):
                record.build_proxy(self.b, self.asset, self.proxy_dir, owned=True)
        self.assertEqual((self.proxy_dir / f"{self.asset}.mp4").read_bytes(), b"good")
        self.assertEqual(sorted(p.name for p in self.proxy_dir.iterdir()), [f"{self.asset}.mp4"])

    def test_two_builds_leave_one_proxy_essence_row(self):
        self._asset()
        outcome = {"bg": self.in_thread(lambda st: record.build_proxy(st, self.asset, self.proxy_dir, owned=True))}
        real_add = self.a.add_essence

        def add_while_other_builds(*args, **kw):
            outcome["bg"].wait(1.0)
            return real_add(*args, **kw)

        self.a.add_essence = add_while_other_builds
        with self._ffmpeg():
            record.build_proxy(self.a, self.asset, self.proxy_dir, owned=True)
            outcome["bg"].finish()
        self.assertIsNone(outcome["bg"].error, repr(outcome["bg"].error))
        rows = self.b.conn.execute("SELECT id FROM essence WHERE asset_id = ? AND role = 'proxy'",
                                   (self.asset,)).fetchall()
        self.assertEqual(len(rows), 1, "a second proxy essence row was written")


class ProxyRaceSqlite(ProxyRaceTests, SqliteBase):
    pass


class ProxyRacePg(ProxyRaceTests, PgBase):
    pass



if __name__ == "__main__":
    unittest.main()
