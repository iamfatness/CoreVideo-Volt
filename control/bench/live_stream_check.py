"""Live check: stream.start through the cue dispatcher against a running CoreVideo Pro core, into a LOCAL SRT sink only.

SAFETY. A core's saved stream destination can be a real platform. This script refuses to start unless the core's
output preferences file shows RTMP off, SRT on, and the SRT host on loopback. It never prints preference values.
Set that up with the app CLOSED (the app reads the file at launch and rewrites it), start a local listener first
(`ffmpeg -i "srt://127.0.0.1:<port>?mode=listener" -c copy sink.ts`), then launch the app, engine on:

    python bench/live_stream_check.py --sink sink.ts [--url http://127.0.0.1:8011] [--seconds 8]

It starts the stream from an armed `stream.start` cue, waits for a sender observed producing in `outputSenderSession`,
checks the sink received bytes, then stops the stream.
"""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from volt.cues import add_cue  # noqa: E402
from volt.library import index_words  # noqa: E402
from volt.rooms import CoreVideoProRoom  # noqa: E402
from volt.rundown import add_item, attach, open_show  # noqa: E402
from volt.store import Store  # noqa: E402
from volt.take import take_item  # noqa: E402

PREFS = Path(os.path.expandvars(r"%LOCALAPPDATA%\CoreVideoPro\production-output-preferences.json"))


def destination_is_local() -> tuple:
    try:
        d = json.loads(PREFS.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return False, f"cannot read preferences: {type(exc).__name__}"
    if d.get("StreamRtmpEnabled") or d.get("StreamHlsEnabled") or d.get("StreamNdiEnabled"):
        return False, "RTMP, HLS or NDI output is still enabled"
    if not d.get("StreamSrtEnabled"):
        return False, "SRT output is not enabled"
    if str(d.get("StreamSrtHost") or "").strip() not in ("127.0.0.1", "localhost", "::1"):
        return False, "SRT host is not loopback"
    return True, "SRT to loopback only"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8011")
    ap.add_argument("--sink", required=True, help="file the local SRT listener writes to")
    ap.add_argument("--seconds", type=float, default=8.0)
    args = ap.parse_args(argv)
    ok, why = destination_is_local()
    print("destination:", why)
    if not ok:
        print("REFUSING to start a stream.")
        return 2
    sink = Path(args.sink)
    store = Store(str(Path(tempfile.mkdtemp(prefix="volt-stream-")) / "stream.db"))
    room = CoreVideoProRoom(args.url)
    st = room.state()
    if st.get("streaming"):
        print("core is already streaming; REFUSING")
        return 2
    asset = store.mint_asset(owner="live-stream-check")
    store.set_cleared(asset.id, True)
    index_words(store, asset.id, [("w0", "01:00:00:00", "01:00:00:08")])
    span = store.conn.execute("SELECT id FROM span").fetchone()["id"]
    show = open_show(store, "Live stream check")
    item = add_item(store, show, "stream")
    attach(store, item, span)
    add_cue(store, item, "corevideo.pro", "stream.start", "")
    results_ok = True

    def check(label, cond, detail=""):
        nonlocal results_ok
        results_ok = results_ok and cond
        print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if detail else ""))

    try:
        t = take_item(store, room, show, attempts=120)
        check("take advanced, stream.start fired", t.advanced and [r.status for r in t.results] == ["fired"],
              str([(r.command, r.status, r.detail) for r in t.results]))
        senders = room.senders_observed()
        check("outputSenderSession shows a sender producing", any(o.state in ("producing", "live") for o in senders),
              str([(o.state, o.health) for o in senders]))
        size0 = sink.stat().st_size if sink.exists() else 0
        time.sleep(args.seconds)
        size1 = sink.stat().st_size if sink.exists() else 0
        check("local sink received bytes", size1 > size0 and size1 > 0, f"{size0} -> {size1}")
        check("core reports streaming", room.state().get("streaming") is True)
    finally:
        try:
            room.invoke("transport.stream.set", [False])
        except Exception as exc:  # noqa: BLE001 - report, then still look at the state
            print("stop failed:", exc)
        for _ in range(40):
            if not room.state().get("streaming"):
                break
            time.sleep(0.25)
    check("stream stopped", not room.state().get("streaming"))
    return 0 if results_ok else 1


if __name__ == "__main__":
    sys.exit(main())

