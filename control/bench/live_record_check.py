"""Live check: record start, observe, stop, wrap against a running CoreVideo Pro core.

Needs the core's engine ON (`transport.engine.set [true]`); with it off the core refuses the record action. It records
a few seconds into a throwaway database and touches nothing else. It does not start a stream: a core's saved stream
destination may be a real platform.

    python bench/live_record_check.py --seconds 6 [--url http://127.0.0.1:8011]
"""

import argparse
import hashlib
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from volt.live import finish_recording, start_recording  # noqa: E402
from volt.rooms import CoreVideoProRoom  # noqa: E402
from volt.store import Store  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8011")
    ap.add_argument("--seconds", type=float, default=6.0)
    args = ap.parse_args(argv)
    store = Store(str(Path(tempfile.mkdtemp(prefix="volt-live-")) / "live.db"))
    room = CoreVideoProRoom(args.url)
    rec = start_recording(store, room, owner="live-check", show_name="Live check", attempts=80)
    print("minted", rec.asset_id)
    time.sleep(args.seconds)
    mid = room.observe()
    print("mid-record", mid.state, mid.health, mid.artifact_path)
    sha = finish_recording(store, room, rec.asset_id, attempts=240)
    path = Path(mid.artifact_path)
    ondisk = hashlib.sha256(path.read_bytes()).hexdigest()
    print("wrapped sha256", sha)
    print("on disk sha256", ondisk, "MATCH" if ondisk == sha else "MISMATCH")
    return 0 if ondisk == sha else 1


if __name__ == "__main__":
    sys.exit(main())
