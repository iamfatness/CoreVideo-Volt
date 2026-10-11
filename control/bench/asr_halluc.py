"""Does the engine invent words over silence or a noise/music bed, with and without a vocabulary?
Builds six adversarial clips from the synthetic corpus, runs one model over them with every vad x vocab_mode
combination, and counts words emitted where there is no speech."""
import argparse, json, subprocess, sys
from pathlib import Path

from _paths import BENCH, CONTROL, FW_PYTHON, HERE  # noqa: F401
from volt import speech

CLEAN = BENCH + "/corpus/full_clean.wav"
NOISY = BENCH + "/corpus/full_noisy.wav"
H = Path(BENCH + "/hall")
VOCAB = ["Volt", "CoreVideo", "SwarmScribe", "essence", "proxy", "rundown", "timecode", "Postgres", "ingest",
         "deliverables", "Okonkwo", "Tuesday Show", "Thursday", "Sunday", "Saturday"]
SPEECH_AT, SPEECH_LEN = 1000.0, 20.0


def ff(*args):
    subprocess.check_call(["ffmpeg", "-y", "-v", "error", *args])


def build():
    H.mkdir(exist_ok=True)
    cut_c, cut_n = H / "sp_clean.wav", H / "sp_noisy.wav"
    ff("-ss", str(SPEECH_AT), "-t", str(SPEECH_LEN), "-i", CLEAN, "-ac", "1", "-ar", "16000", str(cut_c))
    ff("-ss", str(SPEECH_AT), "-t", str(SPEECH_LEN), "-i", NOISY, "-ac", "1", "-ar", "16000", str(cut_n))
    def sil(d): return ["-f", "lavfi", "-t", str(d), "-i", "anullsrc=r=16000:cl=mono"]
    bed = ("anoisesrc=c=pink:r=16000:a=0.03:seed=5,volume=14dB[n];"
           "sine=f=220:r=16000,volume=0.03[a];sine=f=277.18:r=16000,volume=0.025[b];sine=f=329.63:r=16000,volume=0.025[c];"
           "[a][b][c]amix=inputs=3:normalize=0,tremolo=f=0.5:d=0.5,volume=14dB[m];[n][m]amix=inputs=2:normalize=0")
    def bed_clip(path, d):
        ff("-f", "lavfi", "-t", str(d), "-i", bed.replace("[n];", "[n];", 1), "-ac", "1", str(path)) if False else \
            ff("-filter_complex", f"anoisesrc=c=pink:r=16000:a=0.03:seed=5:d={d},volume=14dB[n];"
               f"sine=f=220:r=16000:d={d},volume=0.03[a];sine=f=277.18:r=16000:d={d},volume=0.025[b];"
               f"sine=f=329.63:r=16000:d={d},volume=0.025[c];[a][b][c]amix=inputs=3:normalize=0,tremolo=f=0.5:d=0.5,volume=14dB[m];"
               f"[n][m]amix=inputs=2:normalize=0,aresample=16000[o]", "-map", "[o]", "-ac", "1", str(path))
    bed60 = H / "bed60.wav"; bed_clip(bed60, 60)
    bed40 = H / "bed40.wav"; bed_clip(bed40, 40)
    clips = {}
    def cat(name, parts, speech):
        out = H / f"{name}.wav"
        ins = []
        for p in parts:
            ins += (["-i", str(p)] if isinstance(p, Path) else sil(p))
        n = len(parts)
        ff(*ins, "-filter_complex", "".join(f"[{i}:a]aresample=16000,aformat=channel_layouts=mono[a{i}];" for i in range(n))
           + "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[o]", "-map", "[o]", str(out))
        clips[name] = {"path": str(out), "speech": speech}
    cat("A_tail2s", [cut_c, 2], [0, SPEECH_LEN])
    cat("B_tail30s", [cut_c, 30], [0, SPEECH_LEN])
    cat("C_lead10s", [10, cut_c], [10, 10 + SPEECH_LEN])
    cat("D_noisy_tail40s", [cut_n, bed40], [0, SPEECH_LEN])
    cat("E_bed_only60s", [bed60], None)
    cat("F_silence60s", [60], None)
    json.dump(clips, open(H / "clips.json", "w"))


def invented(words, speech_span):
    out = []
    for w in words:
        mid = (w.start + w.end) / 2
        if speech_span is None or mid > speech_span[1] + 1.0 or mid < speech_span[0] - 1.0:
            out.append(w.text)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--out", default=BENCH + "/halluc.jsonl")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--minprob", type=float)
    a = ap.parse_args()
    if a.build or not (H / "clips.json").exists():
        build()
    clips = json.load(open(H / "clips.json"))
    for vad in (True, False):
        eng = speech.FasterWhisperEngine(device=a.device, model=a.model, vad_filter=vad, min_word_prob=a.minprob)
        for mode in ("none", "hotwords", "initial_prompt"):
            eng.vocab_mode = mode
            row = {"model": a.model, "device": a.device, "vad": vad, "mode": mode, "minprob": a.minprob, "clips": {}}
            for name, c in clips.items():
                r = eng.transcribe(Path(c["path"]), VOCAB)
                inv = invented(r.words, c["speech"])
                row["clips"][name] = {"n_words": len(r.words), "invented": len(inv), "sample": inv[:8]}
            print(json.dumps(row))
            open(a.out, "a").write(json.dumps(row) + "\n")


main()
