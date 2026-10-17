"""Real-speech proof harness: put a real recording through the real runner and measure what came out.

    python asr_real.py add DB FILE [--tc 00:00:00:00 --rate 25]   asset + hi-res essence + queued asr job (cleared)
    python asr_real.py run DB [runner options...]                 runner --once; wall time, peak RAM, window log
    python asr_real.py dump DB OUT.json                           the stored transcript spans + latest asr_run
    python asr_real.py timing FILE DUMP.json                      word times against the waveform energy of FILE
    python asr_real.py seams DUMP.json CUT...                     repeated/missing words around window cuts

Nothing here touches a core or a network. The database is whatever path you give it (use a throwaway one)."""
import argparse, json, math, os, re, subprocess, sys, time
from pathlib import Path

from _paths import CONTROL, FW_PYTHON  # noqa: F401
from volt import timecode
from volt.store import Store
import ctypes
from ctypes import wintypes


class _PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("a", ctypes.c_size_t), ("b", ctypes.c_size_t),
                ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def peak_ws(pid):
    """Peak working set (MB) of a process we started; 0 if it is gone."""
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1000 | 0x0400, False, pid)
    if not h:
        return 0
    p = _PMC(); p.cb = ctypes.sizeof(p)
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
    ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(p), p.cb)
    k.CloseHandle(h)
    return p.PeakWorkingSetSize / 2**20


def descendants(pid):
    """pids of every process below `pid` (a venv python.exe is a launcher; the real interpreter is its child)."""
    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260)]
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k.CreateToolhelp32Snapshot(2, 0)
    pe = PE(); pe.dwSize = ctypes.sizeof(pe)
    kids: dict[int, list[int]] = {}
    ok = k.Process32First(snap, ctypes.byref(pe))
    while ok:
        kids.setdefault(pe.th32ParentProcessID, []).append(pe.th32ProcessID)
        ok = k.Process32Next(snap, ctypes.byref(pe))
    k.CloseHandle(snap)
    out, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            out.append(c); todo.append(c)
    return out


def add(args):
    store = Store(args.db)
    asset = store.mint_asset(owner="desk")
    store.set_cleared(asset.id, True)
    store.add_essence(asset.id, role="hi-res", location=str(Path(args.file).resolve()), open_file=False,
                      frame_rate=args.rate, timecode_start=args.tc)
    store.enqueue(asset.id, "asr", f"asr:{asset.id}")
    print(asset.id)
    store.close()


def run(args):
    env = dict(os.environ, PYTHONPATH=CONTROL, PYTHONIOENCODING="utf-8")
    cmd = [FW_PYTHON, "-m", "volt.runner", args.db, "--once", "--backoff", "1", *args.rest]
    t0 = time.time()
    p = subprocess.Popen(cmd, env=env, cwd=CONTROL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    peak = 0.0
    import threading
    lines: list[str] = []
    threading.Thread(target=lambda: [lines.append(l.rstrip()) for l in p.stdout], daemon=True).start()
    while p.poll() is None:
        peak = max(peak, sum(peak_ws(x) for x in [p.pid, *descendants(p.pid)]))
        time.sleep(0.5)
    time.sleep(0.3)
    print(json.dumps({"exit": p.returncode, "wall_s": round(time.time() - t0, 2), "peak_rss_mb": round(peak),
                      "log_tail": lines[-12:]}, indent=1))


def killresume(args):
    """Run the runner, kill its process tree (only pids this script started) once `--after` windows are saved,
    wait out the job lease, run it again, and report how the second run got on."""
    env = dict(os.environ, PYTHONPATH=CONTROL, PYTHONIOENCODING="utf-8")
    cmd = [FW_PYTHON, "-m", "volt.runner", args.db, "--once", "--backoff", "1", "--lease", str(args.lease), *args.rest]
    import threading
    t0 = time.time()
    p = subprocess.Popen(cmd, env=env, cwd=CONTROL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: list[str] = []
    threading.Thread(target=lambda: [lines.append(l.rstrip()) for l in p.stdout], daemon=True).start()
    while p.poll() is None and sum(" done, " in l and "window" in l for l in lines) < args.after:
        time.sleep(0.5)
    kids = descendants(p.pid)
    for pid in [*kids, p.pid]:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    first = round(time.time() - t0, 1)
    saved = [l.split("INFO ")[-1] for l in lines if "window" in l and " done, " in l]
    time.sleep(args.lease + 2)
    t1 = time.time()
    p2 = subprocess.Popen(cmd, env=env, cwd=CONTROL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines2: list[str] = []
    threading.Thread(target=lambda: [lines2.append(l.rstrip()) for l in p2.stdout], daemon=True).start()
    peak = 0.0
    while p2.poll() is None:
        peak = max(peak, sum(peak_ws(x) for x in [p2.pid, *descendants(p2.pid)]))
        time.sleep(0.5)
    time.sleep(0.3)
    print(json.dumps({"first_run_s_until_kill": first, "windows_saved_before_kill": saved, "second_exit": p2.returncode,
                      "second_wall_s": round(time.time() - t1, 1), "second_peak_rss_mb": round(peak),
                      "second_log": [l.split("INFO ")[-1] for l in lines2 if "asr " in l or "resum" in l or "=" in l][-14:],
                      "first_tail": lines[-4:] if not saved else []}, indent=1))


def dump(args):
    s = Store(args.db, init=False)
    spans = s.conn.execute("SELECT text, tc_in, tc_out, frame_in, frame_out FROM span WHERE kind='transcript' "
                           "ORDER BY frame_in, rowid").fetchall()
    runs = s.conn.execute("SELECT job_id, model, device, word_count, settings FROM asr_run").fetchall()
    out = {"words": [[r["text"], r["tc_in"], r["tc_out"], r["frame_in"], r["frame_out"]] for r in spans],
           "runs": [dict(r) for r in runs]}
    Path(args.out).write_text(json.dumps(out), encoding="utf-8")
    print(len(spans), "spans", [dict(r)["word_count"] for r in runs])
    s.close()


def _energy(path, hop=0.01):
    """RMS in dB per hop, from a 16 kHz mono decode."""
    import array
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    a = array.array("h"); a.frombytes(raw)
    n = int(16000 * hop)
    out = []
    for i in range(0, len(a) - n, n):
        seg = a[i:i + n]
        out.append(10 * math.log10(sum(x * x for x in seg) / n + 1.0))
    return out


def timing(args):
    d = json.loads(Path(args.dump).read_text(encoding="utf-8"))
    rate = timecode.parse_rate(args.rate)
    origin = timecode.tc_to_frames(args.tc, rate, timecode.is_drop(args.tc))
    words = [(w[0], float((w[3] - origin) / rate), float((w[4] - origin) / rate)) for w in d["words"]]
    e = _energy(args.file)
    hop = 0.01
    srt = sorted(e)
    floor, loud = srt[len(srt) // 10], srt[int(len(srt) * 0.95)]
    thr = floor + 0.35 * (loud - floor)   # "speech-like" level: a third of the way up from the floor
    def mx(a, b):
        i, j = int(a / hop), max(int(a / hop) + 1, int(b / hop))
        return max(e[i:j]) if i < len(e) else floor
    on_quiet = sum(1 for _, s, t in words if mx(s, t) < floor + 0.15 * (loud - floor))
    # Onsets and offsets against the waveform. Only pauses that are clearly audible are judged: for a word that
    # follows a gap of 0.5 s or more, take the audio from 0.3 s before the word to its end; if that stretch spans
    # at least 15 dB between its quietest and loudest 20 ms, the onset is the first frame past the half-way level.
    # The same for the end of a word that precedes such a gap. Music beds and noisy rooms make many pauses fail the
    # 15 dB test, and a stretch that does not start (or, for ends, finish) quiet; those are skipped, not scored.
    errs, end_errs = [], []
    def level(a, b):
        i, j = max(0, int(a / hop)), min(len(e), int(b / hop))
        seg = e[i:j]
        return (i, seg, min(seg), max(seg)) if len(seg) > 5 else None
    for (_, s0, t0), (_, s1, t1) in zip(words, words[1:]):
        if s1 - t0 < 0.5:
            continue
        lv = level(s1 - 0.3, min(t1, s1 + 0.6))
        if lv and lv[3] - lv[2] >= 15:
            i, seg, lo, hi = lv
            k = next((n for n, x in enumerate(seg) if x > lo + 0.5 * (hi - lo)), None)
            if k is not None and k > 0 and seg[0] < lo + 0.5 * (hi - lo):  # must start quiet, or it is no onset
                errs.append(s1 - (i + k) * hop)
        lv = level(max(s0, t0 - 0.6), t0 + 0.3)
        if lv and lv[3] - lv[2] >= 15:
            i, seg, lo, hi = lv
            k = next((n for n in range(len(seg) - 1, -1, -1) if seg[n] > lo + 0.5 * (hi - lo)), None)
            if k is not None and k < len(seg) - 1 and seg[-1] < lo + 0.5 * (hi - lo):
                end_errs.append(t0 - (i + k) * hop)
    errs_abs = sorted(abs(x) for x in errs)
    end_abs = sorted(abs(x) for x in end_errs)
    def pct(v, p): return round(v[min(len(v) - 1, int(len(v) * p))], 3) if v else None
    def pct(v, p): return round(v[min(len(v) - 1, int(len(v) * p))], 3) if v else None
    def med(v): return round(sorted(v)[len(v) // 2], 3) if v else None
    print(json.dumps({"words": len(words), "audio_s": round(len(e) * hop, 1), "words_on_near_silence": on_quiet,
                      "onset_n": len(errs), "onset_signed_median_s": med(errs), "onset_abs_median_s": pct(errs_abs, 0.5),
                      "onset_abs_p90_s": pct(errs_abs, 0.9), "onset_within_0.2s_pct": round(100 * sum(x <= 0.2 for x in errs_abs) / max(1, len(errs_abs)), 1),
                      "end_n": len(end_errs), "end_signed_median_s": med(end_errs), "end_abs_median_s": pct(end_abs, 0.5),
                      "end_abs_p90_s": pct(end_abs, 0.9)}))


def seams(args):
    d = json.loads(Path(args.dump).read_text(encoding="utf-8"))
    rate = timecode.parse_rate(args.rate)
    origin = timecode.tc_to_frames(args.tc, rate, timecode.is_drop(args.tc))
    ws = [(re.sub(r"\W", "", w[0]).lower(), float((w[3] - origin) / rate)) for w in d["words"]]
    for cut in args.cuts:
        near = [(t, x) for t, x in ws if abs(x - cut) < 20]
        dup = 0
        for n in (4, 3, 2):  # a repeated run of n words right after itself
            for i in range(len(near) - 2 * n + 1):
                if [a for a, _ in near[i:i + n]] == [a for a, _ in near[i + n:i + 2 * n]] and n > 1:
                    dup += 1
        before = [x for _, x in near if x < cut][-1:] ; after = [x for _, x in near if x >= cut][:1]
        print(f"cut {cut:.1f}s: words within 20s {len(near)}, repeated n-gram runs {dup}, last word before {before}, first after {after}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("db"); a.add_argument("file")
    a.add_argument("--tc", default="00:00:00:00"); a.add_argument("--rate", default="25"); a.set_defaults(f=add)
    r = sub.add_parser("run"); r.add_argument("db"); r.add_argument("rest", nargs=argparse.REMAINDER); r.set_defaults(f=run)
    k = sub.add_parser("killresume"); k.add_argument("db"); k.add_argument("--after", type=int, default=2)
    k.add_argument("--lease", type=float, default=30); k.add_argument("rest", nargs=argparse.REMAINDER); k.set_defaults(f=killresume)
    d = sub.add_parser("dump"); d.add_argument("db"); d.add_argument("out"); d.set_defaults(f=dump)
    t = sub.add_parser("timing"); t.add_argument("file"); t.add_argument("dump")
    t.add_argument("--tc", default="00:00:00:00"); t.add_argument("--rate", default="25"); t.set_defaults(f=timing)
    s = sub.add_parser("seams"); s.add_argument("dump"); s.add_argument("cuts", nargs="+", type=float)
    s.add_argument("--rate", default="25"); s.add_argument("--tc", default="00:00:00:00"); s.set_defaults(f=seams)
    args = ap.parse_args()
    args.f(args)


if __name__ == "__main__":
    main()
