"""A seeded, FakeRoom-backed server for the operator UI. No live core, no real room.

    cd control
    python -m tests.operator_harness held --port 8791

`--no-runner` leaves queued detection and indexing jobs queued, so those states can be looked at.
Scenarios: normal, held, take_fail, record_requested, record_producing, record_finalizing,
record_completed, publish_progress, publish_partial, library, export. The page is then at /operator.
`library` adds assets for the Library screen (see seed_library). `export` is `library` plus assets that make each
export refusal reachable (see seed_export) and a path map. `--token T` makes the server require a bearer token.
Used for the screenshots in docs/mockups and by the accessibility and keyboard passes.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from volt import serve, shots
from volt.runner import Config, Runner, default_handlers
from volt.cues import add_cue
from volt.library import index_words
from volt.playout import hold
from volt.publish import publish
from volt.rooms import FakeRoom, Observation
from volt.rundown import add_item, attach, open_show
from volt.store import Store, _id, _now

SCENARIOS = ["normal", "held", "take_fail", "record_requested", "record_producing",
             "record_finalizing", "record_completed", "publish_progress", "publish_partial", "library", "export"]

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


# The Library scenario's shots, from docs/mockups/shot-spans.html: (label, in, out, origin) at 25 fps from 01:00:00:00.
LIB_SHOTS = [
    ("Wide, tip-off", "01:00:00:00", "01:00:06:12", "detect"),
    ("Bench reaction", "01:00:06:12", "01:00:11:03", "detect"),
    ("Steal and drive", "01:00:11:03", "01:00:19:20", "detect"),
    ("Layup, low angle", "01:00:19:20", "01:00:22:08", "detect"),
    ("Crowd and bench", "01:00:22:08", "01:00:31:00", "detect"),
    ("Coach, timeout", "01:00:31:00", "01:00:36:15", "manual"),
]
LIB_WORDS = ("And we are underway at the Dome. Okonkwo's teammates are already on their feet and the home bench is loud. "
             "Here we go. That is a steal and he is gone the other way. Okonkwo with the layup and the crowd is on its feet. "
             "That is a huge swing for the Wildcats. Coach calls the timeout.").split()


def _proxy_file(path: Path, seconds: float, rate: str) -> None:
    """A real, small, seekable proxy (testsrc, one keyframe a second). Without ffmpeg the file is junk bytes, so the
    page shows its 'could not play' state instead of a picture."""
    if shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=s=320x180:r={rate}:d={seconds}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "25", "-movflags", "+faststart", str(path)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        path.write_bytes(b"not a video")


def seed_library(store: Store, media_dir: Path) -> dict:
    """Assets for the Library screen. Returns {name: asset id}.

    iso-4  closed, cleared, proxy, six shots (five detected, one by hand), one clip from words, a transcript, a detection run
    iso-5  closed, cleared, proxy, nothing marked yet; its video has 3 real cuts (frames 60, 110, 180), so Detect finds 4 shots
    iso-8  closed, cleared, proxy, two shots from before the frame columns existed: opening it queues the indexing backfill
    iso-6  29.97 drop-frame, NOT cleared, proxy, three shots
    iso-7  still recording: no proxy, length unknown
    """
    out = {}

    def asset(camera, rate, start, frames, proxy_seconds=None, cleared=True, open_file=False, cuts=None):
        a = store.mint_asset(owner="desk", show_name="Final Four Watch", camera=camera)
        if cleared:
            store.set_cleared(a.id, True)
        hires = media_dir / f"{camera}.mxf"
        if cuts and shutil.which("ffmpeg"):           # a real file with known cuts, for the detection job to read
            from tests.test_shot_detect import make_clip
            hires = media_dir / f"{camera}.mp4"
            make_clip(hires, rate, cuts)
        store.add_essence(a.id, "hi-res", str(hires), open_file=open_file, frame_rate=rate,
                          timecode_start=start, duration_frames=frames)
        if proxy_seconds:
            path = media_dir / f"{camera}-proxy.mp4"
            if hires.suffix == ".mp4":                # the proxy must show the same cuts, or detection would read a different picture
                shutil.copy(hires, path)
            else:
                _proxy_file(path, proxy_seconds, rate)
            store.add_essence(a.id, "proxy", str(path))
        out[camera] = a.id
        return a

    a = asset("iso-4", "25", "01:00:00:00", 915, 36.6)
    for label, tin, tout, origin in LIB_SHOTS:
        shots.create(store, a.id, "shot", tin, tout, label=label, origin=origin)
    shots.create(store, a.id, "clip", "01:00:13:05", "01:00:19:20", label="Okonkwo steal and layup", origin="transcript",
                 text="That is a steal and he is gone the other way. Okonkwo with the layup.")
    f = shots.facts(store, a.id)
    for n, word in enumerate(LIB_WORDS):    # one word every 12 frames, from the start
        fin = f.start + 12 * n
        store.add_span(a.id, "transcript", f.label(fin), f.label(fin + 10), text=word, origin="transcript",
                       frame_in=fin, frame_out=fin + 10)
    job = store.enqueue(a.id, "shots", "shots-v1:harness")
    store.conn.execute(
        "INSERT INTO shot_run (job_id, asset_id, essence_id, engine, engine_version, threshold, min_frames, shot_count, created_at) "
        "SELECT ?, ?, id, 'ffmpeg-scene', 'harness', 0.3, 12, 5, ? FROM essence WHERE asset_id = ? AND role = 'hi-res'",
        (job.id, a.id, "2026-10-12T09:14:00Z", a.id))
    store.conn.commit()
    store.complete_job(job.id)                      # the seeded run is over, so a runner has nothing to do with it

    asset("iso-5", "25", "01:00:00:00", 250, 10, cuts=(60, 50, 70, 70))

    b = asset("iso-6", "30000/1001", "01:00:00;00", 1800, 20, cleared=False)
    for label, tin, tout in (("Tip", "01:00:00;00", "01:00:12;04"), ("Drive", "01:00:12;04", "01:00:30;02"), ("Dunk", "01:00:30;02", "01:00:59;28")):
        shots.create(store, b.id, "shot", tin, tout, label=label)

    asset("iso-7", "25", "01:00:00:00", None, None, open_file=True)

    c = asset("iso-8", "25", "01:00:00:00", 500, 20)
    store.add_span(c.id, "shot", "01:00:00:00", "01:00:08:00", label="Opening", origin="manual")    # no frame columns
    store.add_span(c.id, "shot", "01:00:08:00", "01:00:20:00", origin="manual")
    return out


def seed_export(store: Store, media_dir: Path, ids: dict) -> dict:
    """Extra assets for the Library's export tab, on top of seed_library. Returns {name: asset id}.

    iso-9   closed, cleared, LEGAL HOLD, proxy, one shot       -> exports (ready, legalHold); publish and air stay blocked
    iso-10  closed, cleared, proxy, hi-res is an s3:// key      -> hi-res: not_local; proxy exports
    iso-11  closed, cleared, 29.97 drop-frame, proxy, one shot  -> with a 25 fps span: mixed_rates
    iso-7 (from seed_library, still recording) gets one clip    -> hi-res: essence_open; proxy: no_proxy
    iso-6 (29.97, not cleared) and iso-4 (25) together          -> exports (ready, notCleared); mixed with 25 fps it is mixed_rates, with iso-11 (29.97) it builds
    The harness also points VOLT_EXPORT_PATH_MAP at a made-up share (see serve_scenario), so paths are rewritten.
    """
    out = {}
    nine = store.mint_asset(owner="desk", show_name="Final Four Watch", camera="iso-9")
    store.set_cleared(nine.id, True)
    store.set_hold(nine.id, True)
    store.add_essence(nine.id, "hi-res", str(media_dir / "iso-9.mxf"), frame_rate="25", timecode_start="01:00:00:00", duration_frames=500)
    p = media_dir / "iso-9-proxy.mp4"
    _proxy_file(p, 20, "25")
    store.add_essence(nine.id, "proxy", str(p))
    shots.create(store, nine.id, "shot", "01:00:04:00", "01:00:09:12", label="Post-game handshake")
    out["iso-9"] = nine.id

    ten = store.mint_asset(owner="desk", show_name="Final Four Watch", camera="iso-10")
    store.set_cleared(ten.id, True)
    store.add_essence(ten.id, "hi-res", "s3://volt-archive/iso-10.mxf", frame_rate="25", timecode_start="01:00:00:00", duration_frames=500)
    p = media_dir / "iso-10-proxy.mp4"
    _proxy_file(p, 20, "25")
    store.add_essence(ten.id, "proxy", str(p), frame_rate="25", timecode_start="01:00:00:00")
    shots.create(store, ten.id, "shot", "01:00:02:00", "01:00:08:00", label="Archive wide")
    out["iso-10"] = ten.id

    eleven = store.mint_asset(owner="desk", show_name="Final Four Watch", camera="iso-11")
    store.set_cleared(eleven.id, True)
    store.add_essence(eleven.id, "hi-res", str(media_dir / "iso-11.mxf"), frame_rate="30000/1001", timecode_start="01:00:00;00", duration_frames=1800)
    p = media_dir / "iso-11-proxy.mp4"
    _proxy_file(p, 20, "30000/1001")
    store.add_essence(eleven.id, "proxy", str(p))
    shots.create(store, eleven.id, "shot", "01:00:02;00", "01:00:09;10", label="Tip-in, 29.97")
    out["iso-11"] = eleven.id

    shots.create(store, ids["iso-7"], "clip", "01:00:05:00", "01:00:08:00", label="Live moment")
    return out


def seed(store: Store, scenario: str, media_dir=None):
    """Fill the store for a scenario. Returns the FakeRoom to serve it with."""
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
            if scenario in ("take_fail", "held"):  # extra cue that the scripted room cannot satisfy
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
    if scenario in ("library", "export"):
        media = Path(media_dir or tempfile.mkdtemp())
        lib = seed_library(store, media)
        if scenario == "export":
            lib.update(seed_export(store, media, lib))
        for camera, asset_id in lib.items():
            spans["asset:" + camera] = asset_id
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


def _run_jobs(path: str, out_dir: Path, stop: threading.Event) -> None:
    """A runner for the library scenario, so Detect and the indexing backfill finish the way they would for real."""
    store = Store(path)
    try:
        runner = Runner(store, default_handlers(Config(out_dir=out_dir)), target=path, name="harness-runner", poll=0.3)
        while not stop.is_set():
            runner.drain()
            stop.wait(0.3)
    finally:
        store.close()


def serve_scenario(scenario: str, port: int, slow: float = 600.0, token=None, runner: bool = True):
    tmp = tempfile.TemporaryDirectory()
    path = str(Path(tmp.name) / "volt.db")
    store = Store(path)
    room, show, spans = seed(store, scenario, media_dir=Path(tmp.name))
    # The rundown scenarios attach clips to assets with no essence; give each a 25 fps hi-res file so the page can total lengths.
    for r in store.conn.execute("SELECT DISTINCT s.asset_id FROM item i JOIN span s ON s.id = i.span_id WHERE s.asset_id NOT IN "
                                "(SELECT asset_id FROM essence WHERE role = 'hi-res')").fetchall():
        store.add_essence(r["asset_id"], "hi-res", str(Path(tmp.name) / "clip.mxf"), frame_rate="25", timecode_start="01:00:00:00")
    if scenario == "publish_partial":
        # Publish the Open clip first with a failing social render so the page opens on the aftermath.
        publish(store, spans["open"], fail="social")
    store.close()
    if scenario == "export":    # this process is the harness server, so setting its environment is its own business
        os.environ["VOLT_EXPORT_PATH_MAP"] = tmp.name + "=\\\\nas\\iso"
    serve.DB_PATH, serve.TOKEN, serve.ROOM = path, token, room
    serve.OUT_DIR = Path(tmp.name) / "out"          # thumbnails land here, not in the repo
    serve.POLL.update(attempts=3, interval=0.05, sleep=time.sleep)
    if scenario == "publish_partial":
        serve.PUBLISH = lambda st, span, out: publish(st, span, fail="social")
    elif scenario == "publish_progress":
        serve.PUBLISH = _slow_publish(slow)
    else:
        serve.PUBLISH = lambda st, span, out: publish(st, span)
    srv = ThreadingHTTPServer(("127.0.0.1", port), serve.Handler)
    srv.daemon_threads = True
    if scenario == "library" and runner:
        stop = threading.Event()
        threading.Thread(target=_run_jobs, args=(path, serve.OUT_DIR, stop), daemon=True).start()
        srv.stop_runner = stop.set
    return srv, tmp, show, spans


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=SCENARIOS)
    ap.add_argument("--port", type=int, default=8791, help="0 lets the OS pick a free one")
    ap.add_argument("--no-runner", action="store_true", help="library: do not run jobs, so detection stays queued")
    ap.add_argument("--ingest-root", default=None, help="allow ingest from this folder (sets VOLT_INGEST_ROOTS for this process)")
    ap.add_argument("--token", default=None, help="require this bearer token (16+ chars is the rule off loopback)")
    args = ap.parse_args()
    if args.ingest_root:
        os.environ["VOLT_INGEST_ROOTS"] = args.ingest_root
    server, _tmp, _show, _spans = serve_scenario(args.scenario, args.port, token=args.token, runner=not args.no_runner)
    args.port = server.server_port
    print(f"{args.scenario}: http://127.0.0.1:{args.port}/operator", flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
