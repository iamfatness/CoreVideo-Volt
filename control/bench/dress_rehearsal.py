"""Dress rehearsal: one show end to end on real recordings, driven through the HTTP operator API where one exists.

Run it with a Python that has faster-whisper if you want real transcripts (stdlib is enough for everything else):

    python bench/dress_rehearsal.py --work D:\\scratch\\dress-rehearsal-1 [--url http://127.0.0.1:8011] [--live]

It uses a throwaway database and output directory under --work, never the operator's data. It never starts a
stream. Without --live (or when the core is not reachable) cues go to a scripted FakeRoom and the report says so.
"""

import argparse
import http.client
import json
import os
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

T0 = time.monotonic()
LOG: list[dict] = []


def step(name, fn):
    t = time.monotonic()
    try:
        out = fn()
        status = "ok"
    except Exception as exc:  # noqa: BLE001 - a rehearsal records and goes on
        out, status = f"{type(exc).__name__}: {exc}", "ERROR"
    dt = time.monotonic() - t
    LOG.append({"step": name, "status": status, "seconds": round(dt, 2), "result": out})
    print(f"[{status:5}] {name} ({dt:.1f}s) {str(out)[:300]}", flush=True)
    return out


class Api:
    def __init__(self, port):
        self.port = port

    def call(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        c.request(method, path, json.dumps(body or {}) if method == "POST" else None, headers)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw


def core_state(url):
    try:
        with urllib.request.urlopen(url + "/state", timeout=3) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--url", default="http://127.0.0.1:8011")
    ap.add_argument("--live", action="store_true", help="use the real core for cues when it is reachable")
    ap.add_argument("--recordings", default=str(Path.home() / "Videos" / "CoreVideo Pro"))
    ap.add_argument("--pick", nargs="*", default=["20261001-181807", "20261002-072351", "20260929-162726"])
    args = ap.parse_args(argv)
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    db = work / "rehearsal.db"
    os.environ["VOLT_DB"] = str(db)
    os.environ["VOLT_OUT"] = str(work / "out")
    os.environ.setdefault("VOLT_ASR_ENGINE", "faster-whisper")
    os.environ.setdefault("VOLT_ASR_MODEL", "tiny.en")

    from http.server import ThreadingHTTPServer
    from volt import ingest, record, serve
    from volt.cues import add_cue
    from volt.rooms import CoreVideoProRoom, FakeRoom, Observation
    from volt.rundown import add_item, attach, open_show
    from volt.runner import Config, Runner, default_handlers
    from volt.store import Store

    store = Store(str(db))
    proxy_dir, publish_dir = work / "proxy", work / "publish"
    quarantine = work / "quarantine"

    # ---- ingest -------------------------------------------------------------------------------------------
    rec_dirs = [Path(args.recordings) / f"corevideo-recording-{p}" for p in args.pick]
    assets = {}

    def do_ingest():
        out = []
        for d in rec_dirs:
            r = ingest.ingest_file(store, d / "Program.mp4", quarantine, owner="rehearsal",
                                   source_key=f"rehearsal:{d.name}", show_name="Dress rehearsal", camera="Program")
            assets[d.name] = r.asset_id
            out.append((d.name[-15:], r.status, r.minted))
        return out
    step("ingest 3 Program.mp4 (mint, quarantine copy, probe, sha256)", do_ingest)

    def replay():
        d = rec_dirs[0]
        r = ingest.ingest_file(store, d / "Program.mp4", quarantine, owner="rehearsal",
                               source_key=f"rehearsal:{d.name}", show_name="Dress rehearsal", camera="Program")
        return f"minted={r.minted} same_asset={r.asset_id == assets[d.name]}"
    step("replay ingest of the same source key (must not mint)", replay)

    def bad_file():
        junk = work / "not-media.mp4"
        junk.write_bytes(b"this is not a video" * 100)
        r = ingest.ingest_file(store, junk, quarantine, owner="rehearsal", source_key="rehearsal:junk")
        return f"status={r.status} essence={r.essence_id!r}"
    step("ingest a file that does not parse (must stay in quarantine)", bad_file)

    def essence_rows():
        rows = store.conn.execute("SELECT asset_id, role, frame_rate, timecode_start, duration_frames, codec, "
                                  "audio_layout, checksum IS NOT NULL AS sum FROM essence").fetchall()
        return [dict(r) for r in rows]
    step("essence rows after ingest (frame_rate/timecode recorded?)", essence_rows)

    def open_flow():
        d = rec_dirs[1]
        iso = next(d.glob("ISO-*.mp4"), None)
        target = work / "live-growing.mp4"
        shutil.copy2(iso or d / "Program.mp4", target)
        r = record.record_start(store, "rehearsal:open-file", target, owner="rehearsal", show_name="Dress rehearsal")
        digest = record.wrap(store, r.asset_id)
        assets["open-file"] = r.asset_id
        return f"asset={r.asset_id} sha={digest[:8]}"
    step("record_start on an open file, then wrap", open_flow)

    # ---- server -------------------------------------------------------------------------------------------
    serve.DB_PATH, serve.OUT_DIR = str(db), work / "out"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    api = Api(srv.server_address[1])
    print("operator API on port", api.port, flush=True)

    state = core_state(args.url)
    live = bool(args.live and state is not None)
    LOG.append({"step": "core reachable", "status": "ok" if state else "n/a", "seconds": 0,
                "result": "yes" if state else "no (cues use FakeRoom)"})

    # ---- jobs ---------------------------------------------------------------------------------------------
    config = Config(proxy_dir=proxy_dir, publish_dir=publish_dir, asr_engine=os.environ["VOLT_ASR_ENGINE"],
                    asr_model=os.environ["VOLT_ASR_MODEL"], out_dir=work / "out")
    runner = Runner(store, default_handlers(config), target=str(db), poll=0.1)

    def drain():
        return runner.drain()

    def jobs():
        return [(r["type"], r["status"], (r["error"] or "")[:80]) for r in
                store.conn.execute("SELECT type, status, error FROM job ORDER BY created_at").fetchall()]

    def make_due():
        store.conn.execute("UPDATE job SET run_after = NULL WHERE status = 'queued'")
        store.conn.commit()

    step("runner drain #1 (assets NOT cleared: proxy runs, asr must defer)", drain)
    step("job table", jobs)

    for aid in assets.values():
        st, body = api.call("POST", f"/api/assets/{aid}/cleared", {"cleared": True})
        if st != 200:
            print("clear failed", st, body)
    make_due()
    step("runner drain #2 (cleared: transcription)", drain)
    step("job table after transcription", jobs)

    def words():
        return {k[-15:]: store.conn.execute(
            "SELECT COUNT(*) FROM span WHERE asset_id=? AND kind='transcript'", (v,)).fetchone()[0]
            for k, v in assets.items()}
    step("transcript word counts per asset", words)

    # ---- shots --------------------------------------------------------------------------------------------
    first = assets[rec_dirs[0].name]

    def detect_all():
        out = {}
        for name, aid in assets.items():
            st, body = api.call("POST", f"/api/assets/{aid}/shots/detect", {})
            out[name[-6:]] = (st, body.get("error") or body.get("alreadyDone"))
        return out
    step("POST shots/detect for every asset", detect_all)
    make_due()
    step("runner drain #3 (shot detection)", drain)
    step("job table after detect", jobs)

    def spans_first():
        st, body = api.call("GET", f"/api/assets/{first}/spans?kind=shot,clip")
        return st, len(body.get("spans", [])) if st == 200 else body
    step("GET spans (shots) of first asset", spans_first)

    # ---- mark clips ---------------------------------------------------------------------------------------
    clips = []

    def mark():
        out = []
        for name, aid in assets.items():
            st, h = api.call("GET", f"/api/assets/{aid}/spans?kind=shot")
            if st != 200:
                out.append((name[-6:], st, h.get("error")))
                continue
            if not h["spans"]:
                out.append((name[-6:], "no shots"))
                continue
            s = h["spans"][0]
            st, c = api.call("POST", f"/api/assets/{aid}/spans",
                             {"kind": "clip", "tc_in": s["tcIn"], "tc_out": s["tcOut"], "label": f"clip {name[-6:]}"})
            out.append((name[-6:], st, c.get("error") or c["span"]["id"]))
            if st == 200:
                clips.append((aid, c["span"]["id"]))
        return out
    step("mark the first shot of each asset as a clip (POST spans)", mark)

    def word_clip():
        for name, aid in assets.items():
            st, t = api.call("GET", f"/api/assets/{aid}/transcript?limit=500")
            w = t.get("words", []) if st == 200 else []
            if len(w) >= 2:
                st, c = api.call("POST", f"/api/assets/{aid}/spans",
                                 {"kind": "clip", "from_words": [w[0]["id"], w[min(3, len(w) - 1)]["id"]],
                                  "handle_in": 0, "handle_out": 6})
                if st == 200:
                    clips.append((aid, c["span"]["id"]))
                return (name[-6:], st, c.get("error") or c["span"].get("text"))
        return "no asset had two transcript words"
    step("mark a clip from transcript words", word_clip)

    # ---- rundown ------------------------------------------------------------------------------------------
    show = open_show(store, "Dress rehearsal")
    items = []
    if live:
        room = CoreVideoProRoom(args.url)
    else:
        rec_obs = Observation("producing", "healthy", False, "fake:1:1", str(work / "fake-rec.mp4"))
        room = FakeRoom([rec_obs], states=[{"lowerThirdOnAir": True}, {"lowerThirdOnAir": True},
                                           {"lowerThirdOnAir": False}])
    serve.ROOM = room
    if live:
        serve.POLL.update(attempts=80, interval=0.25)
    else:
        serve.POLL.update(attempts=6, interval=0.05)
    base = None
    if live:
        base = {k: state.get(k) for k in ("engineOn", "recording", "lowerThirdOnAir", "streaming")}
        print("core baseline:", base, flush=True)
        if base["streaming"] or base["recording"] or base["lowerThirdOnAir"] or not base["engineOn"]:
            print("refusing: the core is not idle with the engine on; run the rest with the fake room instead")
            return 2

    def build():
        plan = [
            ("cold open", ([("record.start", "")] if live else []) + [("overlay.take", "on")]),
            ("interview", [("overlay.take", "off")]),
            ("package", [("record.arm", "")]),
        ]
        for n, (slug, cm) in enumerate(plan):
            if n >= len(clips):
                break
            it = add_item(store, show, slug, script=f"script for {slug}")
            attach(store, it, clips[n][1])
            for command, payload in cm:
                add_cue(store, it, "corevideo.pro", command, payload)
            items.append(it)
        return len(items)
    step("build rundown (items, cues, attach clips)", build)

    def take():
        st, b = api.call("POST", f"/api/shows/{show}/take", {})
        return st, b.get("error") or [(r["command"], r["status"]) for r in b.get("results", [])]
    step("POST take #1", take)
    step("POST take #2", take)
    def stop_live():
        if not live:
            return "skipped (fake room)"
        time.sleep(3)
        row = store.conn.execute("SELECT id FROM asset WHERE source_key LIKE 'cv:%' ORDER BY rowid DESC").fetchone()
        if row is None:
            return "no live recording asset was minted"
        st, b = api.call("POST", "/api/record/stop", {"asset": row["id"]})
        return st, b.get("error") or {"checksum": str(b.get("checksum"))[:8]}
    step("POST /api/record/stop (live only)", stop_live)

    def restored():
        if not live:
            return "skipped (fake room)"
        now = core_state(args.url) or {}
        cur = {k: now.get(k) for k in ("engineOn", "recording", "lowerThirdOnAir", "streaming")}
        return {"baseline": base, "now": cur, "restored": cur == base}
    step("core back to its baseline (engineOn/recording/lowerThirdOnAir/streaming)", restored)
    step("POST take #3 (record.arm has no adapter: must 409 cue_unsupported and rewind)", take)
    step("POST hold then take (must 409 held)",
         lambda: (api.call("POST", f"/api/shows/{show}/hold", {"reason": "rehearsal"})[0], take()))
    step("POST release", lambda: api.call("POST", f"/api/shows/{show}/release", {})[0])
    step("kill unfired last item, then go",
         lambda: (api.call("POST", f"/api/items/{items[-1]}/kill", {})[0],
                  api.call("POST", f"/api/shows/{show}/go", {})[1].get("target")))

    def asrun():
        rows = store.conn.execute("SELECT slug, status, as_run_in, as_run_out FROM item WHERE show_id=? "
                                  "ORDER BY position", (show,)).fetchall()
        return [tuple(r) for r in rows]
    step("as-run log (item status, in, out)", asrun)
    step("GET /api/state", lambda: (lambda sb: (sb[0], sorted(sb[1].keys())))(api.call("GET", "/api/state")))

    # ---- publish ------------------------------------------------------------------------------------------
    def publish():
        if not clips:
            return "no clip"
        st, b = api.call("POST", f"/api/spans/{clips[0][1]}/publish", {})
        return st, b.get("error") or [(v["kind"], v["status"], str(v.get("url", ""))[:60]) for v in b.get("versions", [])]
    step("POST publish first clip (house/ott/social to local storage)", publish)
    step("files in out/", lambda: sorted((p.name, p.stat().st_size) for p in (work / "out").rglob("*.mp4")))

    def reads():
        out = {}
        c = clips[0]
        st, _ = api.call("GET", f"/api/spans/{c[1]}/thumb")
        out["thumb"] = st
        conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=30)
        conn.request("GET", f"/api/assets/{c[0]}/proxy", headers={"Range": "bytes=0-99"})
        r = conn.getresponse()
        out["proxy range"] = (r.status, len(r.read()), r.getheader("Content-Range"))
        conn.close()
        st, hit = api.call("GET", "/seek?q=challenges")
        out["seek"] = (st, hit.get("tc_in") if isinstance(hit, dict) else hit)
        return out
    step("thumb, proxy Range, seek", reads)

    # ---- NLE ----------------------------------------------------------------------------------------------
    def nle(fmt, media="hi-res"):
        st, b = api.call("POST", "/api/exports/nle", {"format": fmt, "spans": [c[1] for c in clips],
                                                      "media": media, "name": "Rehearsal selects"})
        if st == 200:
            (work / f"{media}-{b['filename']}").write_text(b["content"], encoding="utf-8")
            return st, b["filename"], b["durationFrames"], b["rate"], b["warnings"][:2], b["paths"][0]["path"][-48:]
        return st, b.get("error")
    step("NLE export xmeml (hi-res)", lambda: nle("xmeml"))
    step("NLE export edl (hi-res)", lambda: nle("edl"))
    step("NLE export xmeml (proxy)", lambda: nle("xmeml", "proxy"))

    (work / "rehearsal-log.json").write_text(json.dumps(
        {"total_seconds": round(time.monotonic() - T0, 1), "live": live, "steps": LOG}, indent=1, default=str))
    print("done in", round(time.monotonic() - T0, 1), "s; log at", work / "rehearsal-log.json")
    srv.shutdown()
    return 0 if all(s["status"] != "ERROR" for s in LOG) else 1


if __name__ == "__main__":
    sys.exit(main())
