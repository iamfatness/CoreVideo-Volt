"""End-to-end long-file run through the real runner CLI: wrap-equivalent essence -> asr job -> `python -m volt.runner`.
prepare: builds the 74 min essence + database.   run: runs the runner, optionally killing it mid-file, then resumes."""
import argparse, ctypes, json, os, subprocess, sys, time
from ctypes import wintypes
from pathlib import Path

from _paths import BENCH, CONTROL, FW_PYTHON, HERE  # noqa: F401
from volt import timecode
from volt.store import Store
import scoring

W = Path(BENCH + "/long")
PY = FW_PYTHON


class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("a", ctypes.c_size_t), ("b", ctypes.c_size_t),
                ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def peak_ws(pid):
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1000 | 0x0400, False, pid)
    if not h:
        return 0
    p = PMC(); p.cb = ctypes.sizeof(p)
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(p), p.cb)
    k.CloseHandle(h)
    return p.PeakWorkingSetSize / 2**20


def prepare(args):
    W.mkdir(parents=True, exist_ok=True)
    mp4 = W / "long.mp4"
    if not mp4.exists():
        subprocess.check_call(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=160x90:r=25:d=4428",
                               "-i", args.src, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                               "-c:a", "aac", "-shortest", str(mp4)])
    db = W / "volt.db"
    if db.exists():
        db.unlink()
    store = Store(str(db))
    asset = store.mint_asset(owner="desk")
    store.set_cleared(asset.id, True)
    store.add_essence(asset.id, role="hi-res", location=str(mp4), open_file=False,
                      frame_rate=args.rate, timecode_start=args.tc)
    store.enqueue(asset.id, "asr", f"asr:{asset.id}")
    print(asset.id)
    store.close()


def runner_cmd(args):
    return [PY, "-m", "volt.runner", str(W / "volt.db"), "--once", "--lease", str(args.lease), "--backoff", "1",
            "--asr-engine", "faster-whisper", "--asr-model", args.model, "--asr-device", args.device,
            "--asr-window", str(args.window), "--asr-overlap", "15", "--asr-vad", str(args.vad),
            "--asr-vocab-mode", args.vocab_mode]


def chunks(db):
    s = Store(str(db), init=False)
    try:
        return s.conn.execute("SELECT COUNT(*) FROM asr_chunk").fetchone()[0]
    finally:
        s.close()


def run(args):
    db = W / "volt.db"
    env = dict(os.environ, PYTHONPATH=CONTROL, PYTHONIOENCODING="utf-8")
    report = {"model": args.model, "device": args.device, "window": args.window}
    t0 = time.time()
    peak = 0.0
    killed = False
    p = subprocess.Popen(runner_cmd(args), env=env, cwd=CONTROL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    while p.poll() is None:
        peak = max(peak, peak_ws(p.pid))
        if args.kill_after and not killed and chunks(db) >= args.kill_after:
            p.kill(); p.wait(); killed = True
            report["killed_after_windows"] = chunks(db)
            report["run1_s"] = round(time.time() - t0, 1)
            report["run1_peak_ram_mb"] = round(peak)
            break
        time.sleep(1.0)
    out1 = p.stdout.read() if p.stdout else ""
    if killed:
        time.sleep(args.lease + 3)  # let the lease lapse so the next claim cycle recovers the job
        t1 = time.time()
        peak2 = 0.0
        p = subprocess.Popen(runner_cmd(args), env=env, cwd=CONTROL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        while p.poll() is None:
            peak2 = max(peak2, peak_ws(p.pid))
            time.sleep(1.0)
        out2 = p.stdout.read()
        report["run2_s"] = round(time.time() - t1, 1)
        report["run2_peak_ram_mb"] = round(peak2)
        report["run2_log_tail"] = out2.strip().splitlines()[-4:]
    else:
        report["run_s"] = round(time.time() - t0, 1)
        report["peak_ram_mb"] = round(peak)
        report["log_tail"] = out1.strip().splitlines()[-4:]
    # score against truth
    s = Store(str(db), init=False)
    job = s.conn.execute("SELECT status, attempt, error FROM job WHERE type = 'asr'").fetchone()
    report["job"] = dict(job)
    ess = s.conn.execute("SELECT frame_rate, timecode_start FROM essence").fetchone()
    rate = timecode.parse_rate(ess["frame_rate"]); drop = timecode.is_drop(ess["timecode_start"])
    origin = timecode.tc_to_seconds(ess["timecode_start"], rate, drop)
    rows = s.conn.execute("SELECT text, tc_in FROM span WHERE kind = 'transcript' ORDER BY rowid").fetchall()
    hyp = [(r["text"], float(timecode.tc_to_seconds(r["tc_in"], rate, drop)) - origin) for r in rows]
    run = s.conn.execute("SELECT settings, word_count FROM asr_run").fetchone()
    if run:
        report["asr_run_settings"] = json.loads(run["settings"])
        report["word_count"] = run["word_count"]
    s.close()
    truth = json.load(open(BENCH + "/corpus/full_truth.json"))
    report["score"] = scoring.score(truth, hyp)
    # drift: error of matched words in the first and last 10 minutes (drop-frame misuse would show as drift)
    report["score_first10min"] = scoring.score(truth, hyp, 0, 600)
    report["score_last10min"] = scoring.score(truth, hyp, truth["duration"] - 600, truth["duration"])
    hs = json.dumps([h[0] for h in hyp])
    report["transcript_hash"] = __import__("hashlib").sha256(hs.encode()).hexdigest()[:12]
    print(json.dumps(report, indent=1))
    if args.out:
        open(args.out, "a").write(json.dumps(report) + "\n")


ap = argparse.ArgumentParser()
ap.add_argument("cmd", choices=["prepare", "run"])
ap.add_argument("--src", default=BENCH + "/corpus/full_noisy.wav")
ap.add_argument("--rate", default="30000/1001")
ap.add_argument("--tc", default="00:58:58;00")
ap.add_argument("--model", default="distil-large-v3")
ap.add_argument("--device", default="cuda")
ap.add_argument("--window", type=float, default=600)
ap.add_argument("--vad", type=int, default=1)
ap.add_argument("--vocab-mode", default="initial_prompt")
ap.add_argument("--lease", type=float, default=10)
ap.add_argument("--kill-after", type=int, default=0)
ap.add_argument("--out")
a = ap.parse_args()
{"prepare": prepare, "run": run}[a.cmd](a)
