"""N runner-sized engine processes at once on the same box: aggregate throughput, per-process RTF, memory."""
import argparse, json, subprocess, sys, time, threading
from _paths import BENCH, CONTROL, FW_PYTHON, HERE  # noqa: F401

ap = argparse.ArgumentParser()
ap.add_argument("--model", required=True)
ap.add_argument("--device", required=True)
ap.add_argument("--n", type=int, required=True)
ap.add_argument("--threads", type=int, default=0)
ap.add_argument("--dur", type=float, default=300)
ap.add_argument("--out", default=BENCH + "/concurrency.jsonl")
a = ap.parse_args()
PY = FW_PYTHON


def vram():
    try:
        return float(subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                    capture_output=True, text=True).stdout.split()[0])
    except Exception:
        return 0.0

base = vram()
peak = [base]
stop = threading.Event()
def samp():
    while not stop.wait(1):
        peak[0] = max(peak[0], vram())
threading.Thread(target=samp, daemon=True).start()
t0 = time.time()
procs = []
for i in range(a.n):
    # each process takes a different region so they are not byte-identical work
    start = 300 + i * 20
    procs.append(subprocess.Popen([PY, "asr_bench.py", "--model", a.model, "--device", a.device, "--start", str(start),
                                   "--dur", str(a.dur), "--threads", str(a.threads), "--label", f"conc{a.n}"],
                                  cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
res = []
for p in procs:
    out, err = p.communicate()
    try:
        res.append(json.loads(out.strip().splitlines()[-1]))
    except Exception:
        res.append({"error": (err or out)[-200:]})
wall = time.time() - t0
stop.set()
ok = [r for r in res if "rtf" in r]
row = {"model": a.model, "device": a.device, "n": a.n, "threads": a.threads, "audio_each_s": a.dur, "wall_s": round(wall, 1),
       "ok": len(ok), "mean_rtf": round(sum(r["rtf"] for r in ok) / max(1, len(ok)), 4),
       "mean_run_s": round(sum(r["run_s"] for r in ok) / max(1, len(ok)), 1),
       "aggregate_x_realtime": round(sum(r["audio_s"] for r in ok) / max(max(r["run_s"] + r["load_s"] for r in ok), 1e-9), 1) if ok else 0,
       "sum_peak_ram_mb": sum(r["peak_ram_mb"] for r in ok), "vram_peak_delta_mb": round(peak[0] - base) if a.device == "cuda" else None,
       "mean_wer": round(sum(r["wer"] for r in ok) / max(1, len(ok)), 3), "errors": [r for r in res if "error" in r][:2]}
print(json.dumps(row))
open(a.out, "a").write(json.dumps(row) + "\n")
