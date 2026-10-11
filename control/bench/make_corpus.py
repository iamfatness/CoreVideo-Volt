"""Build the synthetic benchmark corpus: about 74 minutes of Windows SAPI speech (two voices, four rates) read from
the prose of markdown files, with long non-speech gaps, plus a copy mixed with a pink-noise and tone "music" bed
about 10 dB under the speech. This is text-to-speech, not human speech; say so wherever its numbers are quoted.

Word-start truth comes from SAPI's SpeakProgress events. Their AudioPosition runs on a 16 kHz clock while the
audio is written at 22.05 kHz, so positions are scaled by 16000/22050 (checked: scaled truth agrees with Whisper's
word starts to about 0.1 s with a constant +0.09 s lag, unscaled it drifts by a third).

    python make_corpus.py <markdown dir or file> [more ...]

Output (under VOLT_BENCH_DIR): corpus/full_clean.wav, corpus/full_noisy.wav, corpus/full_truth.json
({"duration", "words": [{"w","t","raw"}], "silences": [[start, end], ...]})."""
import glob
import json
import random
import re
import subprocess
import sys
import wave
from pathlib import Path

from _paths import BENCH, HERE

VOICES = [("Microsoft David Desktop", -1), ("Microsoft Zira Desktop", 0), ("Microsoft David Desktop", 1),
          ("Microsoft Zira Desktop", -2)]


def paragraphs(roots):
    files = []
    for r in roots:
        files += [r] if r.endswith(".md") else glob.glob(r + "/**/*.md", recursive=True)
    sents, seen = [], set()
    for f in sorted(set(files)):
        if any(x in f for x in ("node_modules", ".claude", "worktrees")):
            continue
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line or line[0] in "|#-*>`" or line[0].isdigit():
                continue
            line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
            line = re.sub(r"[*_`]", "", line)
            for s in re.split(r"(?<=[.!?])\s+", line):
                s = s.strip().replace("—", ", ").replace("–", "-")
                if re.search(r"[\d/\\=<>{}\[\]@#$%^&|~:;]", s):
                    continue
                if 6 <= len(s.split()) <= 28 and s[-1] in ".?!" and s not in seen:
                    seen.add(s)
                    sents.append(s)
    return [" ".join(sents[i:i + 7]) for i in range(0, len(sents), 7)]


def main(roots):
    out = Path(BENCH) / "corpus"
    tts = Path(BENCH) / "tts"
    out.mkdir(parents=True, exist_ok=True)
    paras = paragraphs(roots)
    items = [{"id": i, "voice": VOICES[i % 4][0], "rate": VOICES[i % 4][1], "text": p} for i, p in enumerate(paras)]
    manifest = Path(BENCH) / "manifest.json"
    manifest.write_text(json.dumps(items), encoding="utf-8")
    subprocess.check_call(["powershell", "-NoProfile", "-File", str(Path(HERE) / "tts.ps1"), "-Manifest", str(manifest),
                           "-OutDir", str(tts)])
    rng = random.Random(7)
    words, gaps, t, w_out = [], [], 0.0, None
    for i, wavp in enumerate(sorted(glob.glob(str(tts / "p*.wav")))):
        with wave.open(wavp) as w:
            if w_out is None:
                rate = w.getframerate()
                w_out = wave.open(str(out / "full_clean.wav"), "wb")
                w_out.setnchannels(1)
                w_out.setsampwidth(2)
                w_out.setframerate(rate)
            data, dur = w.readframes(w.getnframes()), w.getnframes() / rate
        gap = rng.uniform(8, 20) if i % 11 == 10 else rng.uniform(0.4, 1.6)  # every 11th paragraph: a long gap
        for line in open(wavp.replace(".wav", ".log"), encoding="utf-8-sig").read().splitlines():
            pos, _, text = line.split("\t", 2)
            words.append({"w": re.sub(r"[^a-z']", "", text.lower()), "t": round(t + float(pos) * 16000 / 22050, 3), "raw": text})
        w_out.writeframes(data)
        t += dur
        if gap > 3:
            gaps.append([round(t + 0.5, 2), round(t + gap - 0.5, 2)])
        w_out.writeframes(b"\x00\x00" * int(gap * rate))
        t += gap
    w_out.close()
    words = [x for x in words if x["w"]]
    (out / "full_truth.json").write_text(json.dumps({"duration": t, "words": words, "silences": gaps}), encoding="utf-8")
    subprocess.check_call([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(out / "full_clean.wav"),
        "-f", "lavfi", "-i", "anoisesrc=c=pink:r=22050:a=0.03:seed=3", "-f", "lavfi", "-i", "sine=f=220:r=22050,volume=0.03",
        "-f", "lavfi", "-i", "sine=f=277.18:r=22050,volume=0.025", "-f", "lavfi", "-i", "sine=f=329.63:r=22050,volume=0.025",
        "-filter_complex", "[2][3][4]amix=inputs=3:normalize=0,tremolo=f=0.5:d=0.5[mus];"
        "[1][mus]amix=inputs=2:normalize=0,volume=14dB[bed];[0][bed]amix=inputs=2:duration=first:normalize=0[o]",
        "-map", "[o]", "-ac", "1", str(out / "full_noisy.wav")])
    print(f"{t:.0f} s, {len(words)} words, {len(gaps)} long gaps")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
