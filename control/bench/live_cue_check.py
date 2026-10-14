"""Live check: record and lower third driven through the cue dispatcher against a running CoreVideo Pro core.

Needs the core's engine ON. It records a few seconds into a throwaway database, puts the lower third on and off
air, and exercises the rewind path with a cue that has no adapter (no core call). It does not start a stream: a
core's saved stream destination may be a real platform.

    python bench/live_cue_check.py --seconds 6 [--url http://127.0.0.1:8011]
"""

import argparse
import hashlib
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from volt.cues import add_cue, cues  # noqa: E402
from volt.library import index_words  # noqa: E402
from volt.live import finish_recording  # noqa: E402
from volt.playout import on_air  # noqa: E402
from volt.rooms import CoreVideoProRoom  # noqa: E402
from volt.rundown import add_item, attach, open_show  # noqa: E402
from volt.store import Store  # noqa: E402
from volt.take import take_item  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8011")
    ap.add_argument("--seconds", type=float, default=6.0)
    args = ap.parse_args(argv)
    store = Store(str(Path(tempfile.mkdtemp(prefix="volt-cue-")) / "cue.db"))
    room = CoreVideoProRoom(args.url)
    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok = ok and cond
        print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if detail else ""))

    src = store.mint_asset(owner="live-cue-check")
    store.set_cleared(src.id, True)
    index_words(store, src.id, [(f"w{n}", f"01:00:0{n}:00", f"01:00:0{n}:08") for n in range(3)])
    spans = [r["id"] for r in store.conn.execute("SELECT id FROM span ORDER BY tc_in").fetchall()]
    show = open_show(store, "Live cue check")
    items = []
    for n, (span, cmds) in enumerate(zip(spans, [
        [("record.start", ""), ("overlay.take", "on")],
        [("overlay.take", "off")],
        [("record.arm", "")],  # no adapter: reported unsupported, take must rewind
    ])):
        item = add_item(store, show, f"item-{n}")
        attach(store, item, span)
        for command, payload in cmds:
            add_cue(store, item, "corevideo.pro", command, payload)
        items.append(item)

    before = room.state()
    check("starts idle", not before.get("recording") and not before.get("lowerThirdOnAir"))

    t1 = take_item(store, room, show, attempts=80)
    check("take 1 advanced, both cues fired", t1.advanced and [r.status for r in t1.results] == ["fired", "fired"],
          str([(r.command, r.status, r.detail) for r in t1.results]))
    mid = room.observe()
    st = room.state()
    check("core shows producing/healthy", mid.state == "producing" and mid.health == "healthy",
          f"{mid.state}/{mid.health}")
    check("core shows lower third on air", st.get("lowerThirdOnAir") is True)
    rec = store.conn.execute("SELECT id FROM asset WHERE id <> ? ORDER BY rowid DESC LIMIT 1", (src.id,)).fetchone()
    check("recording asset minted", rec is not None)
    time.sleep(args.seconds)

    t2 = take_item(store, room, show, attempts=80)
    check("take 2 advanced, lower third off", t2.advanced and [r.status for r in t2.results] == ["fired"],
          str([(r.command, r.status, r.detail) for r in t2.results]))
    check("core shows lower third off", room.state().get("lowerThirdOnAir") is False)

    t3 = take_item(store, room, show, attempts=4)
    check("take 3 refused as unsupported, rundown rewound",
          (not t3.advanced) and [r.status for r in t3.results] == ["unsupported"] and on_air(store, show) == items[1],
          str([(r.command, r.status) for r in t3.results]))
    check("failed cue stays armed", [c.status for c in cues(store, items[2])] == ["armed"])

    sha = finish_recording(store, room, rec["id"], attempts=240)
    path = Path(mid.artifact_path)
    ondisk = hashlib.sha256(path.read_bytes()).hexdigest()
    check("wrapped sha256 matches file on disk", ondisk == sha, f"{sha[:8]} vs {ondisk[:8]}")
    after = room.state()
    check("ends idle", not after.get("recording") and not after.get("lowerThirdOnAir"))
    print("artifact", path)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
