"""Live check: show-input.take through the cue dispatcher against a running CoreVideo Pro core.

Local only: it touches slots, Preview routes and Program, never a stream or recording. It empties the scene's routes
and takes that first, so Program starts with NO sources and the take has a real before and after. Then the cue runs
the default actions (input.assign, scene.route.set, transport.take) and the dispatcher must see the source on
Program: sceneIdAttribution "live", deliverySequence advanced, on consecutive newer frames.

    python bench/live_take_check.py --source capture:screen:1 --scene speaker-slides --route speaker-slides-1 \
        --other-route speaker-slides-2 [--slot 1] [--url http://127.0.0.1:8011]
"""

import argparse
import json
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


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8011")
    ap.add_argument("--source", required=True)
    ap.add_argument("--scene", required=True)
    ap.add_argument("--route", required=True)
    ap.add_argument("--other-route", action="append", default=[], help="other routes on the scene, emptied first")
    ap.add_argument("--slot", type=int, default=1)
    args = ap.parse_args(argv)
    room = CoreVideoProRoom(args.url)
    ok_all = True

    def check(label, cond, detail=""):
        nonlocal ok_all
        ok_all = ok_all and cond
        print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if detail else ""))

    if room.state().get("recording") or room.state().get("streaming"):
        print("core is recording or streaming; REFUSING")
        return 2
    routes = {r: None for r in [args.route] + args.other_route}
    room.invoke("scene.cueWithRoutes", [args.scene, json.dumps(routes)])
    room.invoke("transport.take", [])
    time.sleep(1.5)
    base = room.program_observed()
    check("baseline Program has no sources", base.available and not base.sources,
          str([s.source_id for s in base.sources]))

    store = Store(str(Path(tempfile.mkdtemp(prefix="volt-take-")) / "take.db"))
    asset = store.mint_asset(owner="live-take-check")
    store.set_cleared(asset.id, True)
    index_words(store, asset.id, [("w0", "01:00:00:00", "01:00:00:08")])
    span = store.conn.execute("SELECT id FROM span").fetchone()["id"]
    show = open_show(store, "Live take check")
    item = add_item(store, show, "take")
    attach(store, item, span)
    payload = json.dumps({"source": args.source, "scene": args.scene, "slot": args.slot, "route": args.route})
    add_cue(store, item, "corevideo.pro", "show-input.take", payload)

    t = take_item(store, room, show, attempts=120)
    check("take advanced, show-input.take fired", t.advanced and [r.status for r in t.results] == ["fired"],
          str([(r.command, r.status, r.detail) for r in t.results]))
    o = room.program_observed()
    check("Program holds the source", any(args.source in (s.source_id, s.participant_id) for s in o.sources),
          str([(s.source_id, s.layer_id) for s in o.sources]))
    check("scene attribution live", o.attribution == "live", str(o.attribution))
    check("delivery sequence advanced", (o.delivery_sequence or 0) > (base.delivery_sequence or 0),
          f"{base.delivery_sequence} -> {o.delivery_sequence}")
    print("core calls:", [c for c in getattr(room, "calls", [])] or "(see core log)")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
