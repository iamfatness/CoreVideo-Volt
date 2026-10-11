"""Engine-level benchmark on a window of the synthetic corpus. Reports RTF, peak RAM, WER, word-start error and
words invented inside known non-speech gaps. One process per configuration so peak memory is attributable."""
import argparse, json, os, re, subprocess, sys, tempfile, threading, time, ctypes
from ctypes import wintypes
from pathlib import Path

from _paths import BENCH, CONTROL, FW_PYTHON, HERE  # noqa: F401
from volt import chunking, speech

VOCAB = ["Volt", "CoreVideo", "SwarmScribe", "essence", "proxy", "rundown", "timecode", "Postgres", "ingest",
         "deliverables", "Okonkwo", "Tuesday Show", "Thursday", "Sunday", "Saturday"]


class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def mem():
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = wintypes.HANDLE
    p = PMC(); p.cb = ctypes.sizeof(p)
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    ctypes.windll.psapi.GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(p), p.cb)
    return p.WorkingSetSize / 2**20, p.PeakWorkingSetSize / 2**20


def vram():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True).stdout.strip().splitlines()[0]
        return float(out)
    except Exception:
        return 0.0


def norm_tokens(text):
    out = []
    for piece in re.split(r"[\s\-\u2014]+", text.lower()):
        piece = re.sub(r"[^a-z']", "", piece)
        if piece:
            out.append(piece)
    return out


def align(ref, hyp):
    """Levenshtein with backtrace. Returns (S, D, I, matches[(ref_i, hyp_j)])."""
    n, m = len(ref), len(hyp)
    prev = list(range(m + 1))
    back = [bytearray(m + 1) for _ in range(n + 1)]  # 0 diag-match,1 diag-sub,2 del(ref only),3 ins(hyp only)
    for j in range(1, m + 1):
        back[0][j] = 3
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        back[i][0] = 2
        ri = ref[i - 1]
        for j in range(1, m + 1):
            if ri == hyp[j - 1]:
                best, b = prev[j - 1], 0
            else:
                best, b = prev[j - 1] + 1, 1
            d = prev[j] + 1
            if d < best:
                best, b = d, 2
            ins = cur[j - 1] + 1
            if ins < best:
                best, b = ins, 3
            cur[j] = best
            back[i][j] = b
        prev = cur
    i, j = n, m
    S = D = I = 0
    matches = []
    while i > 0 or j > 0:
        b = back[i][j]
        if b == 0:
            matches.append((i - 1, j - 1)); i -= 1; j -= 1
        elif b == 1:
            S += 1; i -= 1; j -= 1
        elif b == 2:
            D += 1; i -= 1
        else:
            I += 1; j -= 1
    return S, D, I, matches[::-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--compute")
    ap.add_argument("--src", default=BENCH + "/corpus/full_noisy.wav")
    ap.add_argument("--truth", default=BENCH + "/corpus/full_truth.json")
    ap.add_argument("--start", type=float, default=300)
    ap.add_argument("--dur", type=float, default=600)
    ap.add_argument("--vad", type=int, default=1)
    ap.add_argument("--vocab", default="none", choices=["none", "hotwords", "initial_prompt"])
    ap.add_argument("--minprob", type=float)
    ap.add_argument("--silguard", type=float)
    ap.add_argument("--cond", type=int, default=1)
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--label", default="")
    ap.add_argument("--out")
    ap.add_argument("--release", action="store_true")
    a = ap.parse_args()

    truth = json.load(open(a.truth))
    t_end = a.start + a.dur
    ref, ref_t = [], []
    for w in truth["words"]:
        if a.start <= w["t"] < t_end:
            for tok in norm_tokens(w["raw"]):
                ref.append(tok); ref_t.append(w["t"] - a.start)
    gaps = [(g[0] - a.start, g[1] - a.start) for g in truth["silences"] if g[1] > a.start and g[0] < t_end]

    eng = speech.FasterWhisperEngine(device=a.device, model=a.model, compute_type=a.compute, vad_filter=bool(a.vad),
                                     vocab_mode=("none" if a.vocab == "none" else a.vocab), min_word_prob=a.minprob,
                                     silence_guard=a.silguard, cpu_threads=a.threads,
                                     condition_on_previous_text=bool(a.cond))
    base_ram = mem()[0]
    base_vram = vram()
    t0 = time.time()
    eng._load()
    load_s = time.time() - t0
    loaded_ram = mem()[0]
    loaded_vram = vram()
    peak_vram = [loaded_vram]
    stop = threading.Event()

    def sampler():
        while not stop.wait(1.0):
            peak_vram[0] = max(peak_vram[0], vram())
    th = threading.Thread(target=sampler, daemon=True); th.start()

    with tempfile.TemporaryDirectory(dir=BENCH) as tmp:
        wav = Path(tmp) / "w.wav"
        chunking.extract(a.src, a.start, a.dur, wav)
        t1 = time.time()
        res = eng.transcribe(wav, VOCAB if a.vocab != "none" else [])
        run_s = time.time() - t1
    stop.set()
    cur_ram, peak_ram = mem()
    hyp, hyp_t, hyp_raw = [], [], []
    for w in res.words:
        for tok in norm_tokens(w.text):
            hyp.append(tok); hyp_t.append(w.start); hyp_raw.append(w.text)
    S, D, I, matches = align(ref, hyp)
    errs = [hyp_t[j] - ref_t[i] for i, j in matches]
    abserr = sorted(abs(e) for e in errs)
    inv = []
    for w in res.words:
        mid = (w.start + w.end) / 2
        if any(g0 + 0.5 <= mid <= g1 - 0.5 for g0, g1 in gaps):
            inv.append(w.text)
    report = {
        "label": a.label, "model": a.model, "device": a.device, "compute": eng.compute_type, "vad": a.vad,
        "vocab": a.vocab, "minprob": a.minprob, "silguard": a.silguard, "cond": a.cond, "threads": a.threads,
        "audio_s": a.dur, "load_s": round(load_s, 1), "run_s": round(run_s, 1), "rtf": round(run_s / a.dur, 4),
        "ref_words": len(ref), "hyp_words": len(hyp), "wer": round((S + D + I) / len(ref), 4),
        "S": S, "D": D, "I": I,
        "start_mae_s": round(sum(abserr) / len(abserr), 3), "start_p50_abs": round(abserr[len(abserr) // 2], 3),
        "start_p95_abs": round(abserr[int(len(abserr) * .95)], 3), "start_bias_s": round(sum(errs) / len(errs), 3),
        "gap_words": len(inv), "gap_word_sample": inv[:12],
        "peak_ram_mb": round(peak_ram), "ram_after_load_delta_mb": round(loaded_ram - base_ram),
        "vram_delta_mb": round(peak_vram[0] - base_vram) if a.device == "cuda" else None,
    }
    if a.release:
        eng.release()
        time.sleep(2)
        report["ram_after_release_mb"] = round(mem()[0])
        report["ram_before_release_mb"] = round(cur_ram)
        if a.device == "cuda":
            report["vram_after_release_delta_mb"] = round(vram() - base_vram)
    print(json.dumps(report))
    if a.out:
        with open(a.out, "a") as f:
            f.write(json.dumps(report) + "\n")


main()
