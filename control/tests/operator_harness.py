"""A seeded, FakeRoom-backed server for the operator UI. No live core, no real room.

    cd control
    python -m tests.operator_harness held --port 8791

Scenarios: normal, held, take_fail, record_requested, record_producing, record_finalizing,
record_completed, publish_progress, publish_partial. The page is then at /operator.
Used for the screenshots in docs/mockups and by the accessibility and keyboard passes.
"""

from __future__ import annotations

import argparse
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from volt import serve
from volt.cues import add_cue
from volt.library import index_words
from volt.playout import hold
from volt.publish import publish
from volt.rooms import FakeRoom, Observation
from volt.rundown import add_item, attach, open_show
from volt.store import Store, _id, _now

SCENARIOS = ["normal", "held", "take_fail", "record_requested", "record_producing",
             "record_finalizing", "record_completed", "publish_progress", "publish_partial"]

# (slug, status, as_run_in, as_run_out)
ITEMS = [
    ("Pre-show", "aired", "2026-10-10T20:00:01Z", "2026-10-10T20:00:09Z"),
    ("Open / theme", "on air", "2026-10-10T20:00:09Z", None),
    ("Game action", "ready", None, None),
    ("Under review", "ready", None, None),
    ("Tough call", "killed", None, None),
    ("Player spotlight", "ready", None, None),
    ("Commercial break", "skipped", None, None),
]
WORDS = ["pre", "open", "game", "review", "tough", "player", "break"]


def obs(state, session="s1", path=None, finalized=False, health="healthy", stale=False, error=None):
    return Observation(state, health, finalized, session, path, stale, error)


def seed(store: Store, scenario: str):
    """Fill the store for a scenario. Returns the FakeRoom to serve it with."""
    cam3 = store.mint_asset(owner="desk", show_name="Final Four Watch", camera="iso-3")
    store.set_cleared(cam3.id, True)
    cam1 = store.mint_asset(owner="desk", show_name="Final Four Watch", camera="iso-1")
    cam2 = store.mint_asset(owner="desk", show_name="Final Four Watch", camera="iso-2")
    store.set_cleared(cam2.id, True)
    store.set_hold(cam2.id, True)
    index_words(store, cam3.id, [(w, f"01:2{n}:30:00", f"01:2{n}:32:00") for n, w in enumerate(WORDS)])
    index_words(store, cam1.id, [("buzzer", "01:40:00:00", "01:40:02:00"), ("timeout", "01:41:00:00", "01:41:02:00")])
    index_words(store, cam2.id, [("replay", "01:50:00:00", "01:50:02:00")])
    spans = {r["text"]: r["id"] for r in store.conn.execute("SELECT id, text FROM span")}
    show = open_show(store, "Final Four Watch")
    for n, (slug, status, t_in, t_out) in enumerate(ITEMS):
        item = add_item(store, show, slug)
        attach(store, item, spans[WORDS[n]])
        store.conn.execute("UPDATE item SET status = ?, as_run_in = ?, as_run_out = ? WHERE id = ?",
                           (status, t_in, t_out, item))
        if slug == "Game action":
            add_cue(store, item, "corevideo.pro", "overlay.take", "on")
            add_cue(store, item, "corevideo.pro", "stream.start", "")
            if scenario in ("take_fail", "held"):  # no verified adapter yet, so a take cannot pass it
                add_cue(store, item, "corevideo.pro", "show-input.take", "Camera 2")
            store.conn.execute("UPDATE cue SET status = 'armed' WHERE item_id = ?", (item,))
        if slug == "Under review":
            add_cue(store, item, "corevideo.pro", "overlay.take", "on")
    store.conn.commit()
    if scenario == "held":
        hold(store, show, "Waiting on the producer for the replay cue")

    # The room. Take succeeds only where its cues have evidence: a lower third on air and a sender
    # producing. take_fail never shows a sender, so stream.start fails after overlay.take fires.
    senders = [[]] if scenario == "take_fail" else [["producing"]]
    room_obs = obs("idle", session=None)
    if scenario == "record_requested":
        room_obs = obs("requested")  # no asset yet: it is minted only once output is observed
    elif scenario.startswith("record_"):
        asset = store.mint_source("cv:s1", owner="desk", camera="iso-3", show_name="Final Four Watch")
        store.add_essence(asset.id, "hi-res", r"C:\iso\iso-3.mp4", open_file=scenario != "record_completed")
        if scenario == "record_completed":
            store.conn.execute("UPDATE essence SET checksum = ? WHERE asset_id = ?", ("9f2c" + "ab" * 30, asset.id))
            store.conn.commit()
        room_obs = {
            "record_producing": obs("producing", path=r"C:\iso\iso-3.mp4"),
            "record_finalizing": obs("finalizing", path=r"C:\iso\iso-3.mp4"),
            "record_completed": obs("completed", path=r"C:\iso\iso-3.mp4", finalized=True),
        }[scenario]
    return FakeRoom([room_obs], senders=senders, states=[{"lowerThirdOnAir": True}]), show, spans


def _slow_publish(seconds: float):
    def run(store, span_id, out):
        asset = store.conn.execute("SELECT asset_id FROM span WHERE id = ?", (span_id,)).fetchone()["asset_id"]
        for kind, status, url in (("house", "done", "file:///out/house.mp4"), ("ott", "queued", None), ("social", "queued", None)):
            store.conn.execute(
                "INSERT INTO version (id, asset_id, span_id, kind, status, url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_id(), asset, span_id, kind, status, url, _now()))
        store.conn.commit()
        time.sleep(seconds)
        return publish(store, span_id)
    return run


def serve_scenario(scenario: str, port: int, slow: float = 600.0):
    tmp = tempfile.TemporaryDirectory()
    path = str(Path(tmp.name) / "volt.db")
    store = Store(path)
    room, show, spans = seed(store, scenario)
    if scenario == "publish_partial":
        # Publish the Open clip first with a failing social render so the page opens on the aftermath.
        publish(store, spans["open"], fail="social")
    store.close()
    serve.DB_PATH, serve.TOKEN, serve.ROOM = path, None, room
    serve.POLL.update(attempts=3, interval=0.05, sleep=time.sleep)
    if scenario == "publish_partial":
        serve.PUBLISH = lambda st, span, out: publish(st, span, fail="social")
    elif scenario == "publish_progress":
        serve.PUBLISH = _slow_publish(slow)
    else:
        serve.PUBLISH = lambda st, span, out: publish(st, span)
    srv = ThreadingHTTPServer(("127.0.0.1", port), serve.Handler)
    srv.daemon_threads = True
    return srv, tmp, show, spans


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=SCENARIOS)
    ap.add_argument("--port", type=int, default=8791)
    args = ap.parse_args()
    server, _tmp, _show, _spans = serve_scenario(args.scenario, args.port)
    print(f"{args.scenario}: http://127.0.0.1:{args.port}/operator", flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
