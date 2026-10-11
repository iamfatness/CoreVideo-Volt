# Speech at scale

Status: implemented on branch `speech-scale`; measured on one machine (RTX 4090 24 GB, 28-thread CPU, Windows 11,
faster-whisper 1.2.1 / ctranslate2, av 16.1.0). Everything under "Measured" was observed by running the scripts in
`control/bench/`; raw rows live outside the repo (`VOLT_BENCH_DIR`, default `D:/scratch/bench`).

Principles kept: an ack is not evidence (a window counts only once its words are stored); fail closed (an unreadable
essence, a bad setting or a missing CUDA library is an error, never a quiet CPU fallback); the consent gate
(`cleared`) is unchanged; legal hold does not block transcription (nothing here reads it); every `asr_run` row keeps its
provenance, now including the guard and window settings.

## Design

**Windowed reading (`volt/chunking.py`, `speech._window_run`).** An essence is never loaded whole. Each window is cut by
ffmpeg to a 16 kHz mono WAV of `own_start - overlap .. own_end + overlap`, transcribed, and deleted. The cut is
chosen lazily near `window` seconds on: ffmpeg `silencedetect` (an energy gate, not a speech model) scans +/-30 s and
the longest quiet stretch wins, cut at its middle; with no quiet stretch the cut is the nominal point. A tail up to
1.25 windows stays whole. A word belongs to the window that owns its midpoint, so overlap context never duplicates or
drops a word. A file that fits in one window is one engine call and stores nothing extra.

**Resume.** After each window its owned words and its cut are written to `asr_chunk` (one row per window, keyed
`(job_id, idx)`), committed before the next window starts. On the next attempt the saved windows are accepted only if
they carry the same fingerprint (file path/size/mtime, engine settings, vocabulary, windowing) and join up from 0
without a gap; anything else is deleted and re-done. Rows are deleted when the final `asr_run` + spans commit.
Resume reuses the stored cuts, so it does not depend on silencedetect being repeatable.

**Run identity.** `_run_key` is now `asr-v3` and includes the windowing, so changing window size is a new, recorded run.
`asr_run.settings` (JSON) records the engine guards, chunk settings, audio seconds, windows, resumed windows,
quiet/hard cuts and engine stats.

**Hallucination guards (`FasterWhisperEngine`).** `vad_filter` (default on; faster-whisper's bundled Silero VAD),
`vocab_mode` (`initial_prompt` default, `hotwords`, `none`), `min_word_prob`, `silence_guard`
(`hallucination_silence_threshold`). All are in `describe()` and therefore in the run record. Defaults changed from the
old behaviour (`vad_filter=False`, hotwords) on the strength of the measurements below.

**Concurrency across runners.** Unchanged mechanism (lease + heartbeat claim in `runner.py`; an `asr` job is claimed by
exactly one runner; a runner that loses its lease cannot record an outcome). New: one model per process and
configuration (`speech.shared_engine`), and the model is unloaded after `--asr-idle-release` seconds without a job
(default 900, `0` = keep). Concurrency is "N runner processes", each with its own model; see limits.

**CUDA on Windows.** `_cuda_libs_error` adds the pip `nvidia/*/bin` directories to the DLL search path and checks
`cublas64_12.dll` loads; asking for `cuda` without them fails with the reason instead of crashing later.

New runner flags / env: `--asr-window`/`VOLT_ASR_WINDOW` (900), `--asr-overlap` (15), `--asr-vad` (1),
`--asr-vocab-mode` (initial_prompt), `--asr-min-word-prob`, `--asr-silence-guard`, `--asr-cpu-threads`,
`--asr-idle-release`.

## Measured

Corpus: synthetic SAPI speech (clean and with a noise bed), 600 s clips and a 4427 s file with known word truth;
WER is on that synthetic corpus and says nothing about real speakers.

**Hallucination over non-speech** (clips: speech + 2 s/30 s/40 s tail, 10 s lead, noise-bed-only 60 s, silence 60 s;
words counted where there was no speech). With `vad_filter=on`: **0 invented words for tiny.en, distil-large-v3 and
large-v3 in every vocab mode** (18 configurations). With VAD off: tiny.en invented words in some configurations
(e.g. 3 on the bed-only clip, 2 on silence, 7 with initial_prompt on the noisy tail), distil-large-v3 up to 15 on the noisy tail
and 10 on bed-only (hotwords), and large-v3 was worst, up to 72 (hotwords) and 81 (initial_prompt) invented words on the 60 s
bed-only clip, including "Thank you for watching" style phrases and URLs. Conclusion: VAD on is the guard that works; `min_word_prob` and
`silence_guard` were not shown necessary and are not tested as replacements.

**Accuracy/speed grid** (600 s, GPU, VAD on, no vocab): tiny.en RTF 0.013, WER 0.106-0.130; distil-large-v3 RTF
0.013, WER 0.119-0.121, start-time MAE 0.06 s; large-v3 RTF 0.072, WER 0.098-0.132, start-time MAE ~0.20 s with a
-0.19 s bias. CPU int8: tiny.en RTF 0.022, distil-large-v3 0.159 (WER 0.124), large-v3 0.771 (4.8 GB RAM peak).
Hotwords hurt tiny.en badly (WER up to 0.24, up to 228 deletions with VAD off); `initial_prompt` did not.
distil-large-v3 was the best accuracy/speed/timing trade; it is not a new default of the code (default stays
cuda=large-v3, cpu=distil-large-v3), the runner's `--asr-model` selects it.

**Concurrency** (N processes, each 300 s, same GPU/CPU; aggregate = audio seconds / wall):

| model | device | N | aggregate x realtime | peak VRAM delta | note |
|---|---|---|---|---|---|
| distil-large-v3 | cuda | 1/2/4 | 56.6 / 70.6 / 78.4 | 2.4 / 4.8 / 9.7 GB | |
| large-v3 | cuda | 1/2/3 | 11.9 / 14.5 / 14.5 | 5.5 / 10.2 / 15.7 GB | |
| large-v3 | cuda | 4 | 13.1 | 20.5 GB | completed, no errors |
| large-v3 | cuda | 5 | n/a | ~22 GB used of 24 | did not finish in 10 min; VRAM full; killed by me. Not a clean OOM error. |
| distil-large-v3 | cpu, 4 threads each | 1/2/4/7 | 5.8 / 9.0 / 10.9 / 13.3 | n/a | 12.6 GB RAM summed at 7 |

Takeaway: GPU concurrency beyond 2 buys almost nothing for large-v3 (throughput flat at 2-3, falling at 4) and costs
~5 GB each; distil-large-v3 scales to 4. Budget VRAM per runner before starting more; the code does not enforce it.

**Long file, end to end through the real runner CLI** (74 min / 4427 s essence, distil-large-v3 cuda, 600 s windows,
15 s overlap, drop-frame 29.97 start timecode `00:58:58;00`):
- Clean run: 69.0 s and 70.0 s wall in two runs (about 64x realtime), 8 windows, job done on attempt 1.
- Killed (process kill) after 3 windows (24 s), lease lapsed, runner restarted: resumed at window 3 (log: 3 resumed
  windows), finished the other 5 in 50 s, job done on attempt 2, `asr_run.settings.resumed_windows = 3`.
- Score against truth for the killed+resumed run: WER 0.092 overall (0.105 first 10 min, 0.067 last 10 min; no
  growth, so no drift from drop-frame timecode), start-time MAE 0.065 s, p95 0.199 s, 0 words in gaps.
- Not bit-identical between runs: word counts 9569 (resumed), 9577 and 9574 (two clean runs); the transcript hashes
  all differ. The two clean runs also differ from each other, so this is engine run-to-run variation (~0.1% of
  words), not a resume defect. Resume equivalence is therefore "same within engine noise", not "identical".
- Peak RAM during the long run was not measured: the harness read a launcher stub (12 MB). Per-process peak from the
  grid (distil-large-v3 GPU 1.8 GB, large-v3 3.3 GB, CPU large-v3 4.8 GB) is the only RAM evidence, and it
  is for 300-600 s clips; windowing is designed for constant memory but the 74 min figure is unmeasured.
- Only one of 8 cuts found a quiet stretch (the synthetic speech has no pauses at -35 dB); the other seven were hard
  cuts at the nominal point, and ownership still produced no duplicates or gaps (the gap-word count is 0).

CUDA OOM: the old agent's earlier report of a CUDA OOM was not reproduced; large-v3 x4 fit in 24 GB and x5 stalled at
full VRAM instead of raising. Treat "out of VRAM" as a possible hang as well as an error.

## Tests

`control/tests/test_speech_scale.py` (fake engines): every word exactly once across windows, quiet-cut choice, hard
cut, clipped edge words, constant-memory cutting, single-window files store nothing, windowing in run identity, bad
settings refused, no-audio reason, crash resumes at the first unfinished window with the same cuts, saved windows from
another configuration or past a gap are discarded, runner retries from saved windows, drop-frame labels across a
minute and window boundary, idle release, one model per process, env config. Opt-in real-engine tests
(`VOLT_ASR_REAL=1`): silence/noise with a vocabulary emits nothing (tiny.en, VAD default), and six sentences across
real windows each appear once; both passed here.

## Limits and unproven

- Windows are sequential inside a job; one long file does not use more than one runner. Parallelism is across jobs.
- A stale runner and its replacement could both write the same `asr_chunk` row; the second insert fails on the primary
  key and that attempt errors (outcome is lease-guarded, so nothing is corrupted), it is then retried. Not exercised
  against a live Postgres here; Postgres paths were only exercised by the existing skipped tests.
- Quality numbers are synthetic TTS speech; real voices, accents, crosstalk and music beds are untested.
- Silence-guard and `min_word_prob` are plumbed and recorded but their effect was not measured.
- Resume was proven by killing the runner process between windows, not mid-window (a mid-window kill just redoes
  that window).
- The idle-release path is unit-tested; unloading was not observed to return VRAM in a long-lived runner.
- macOS/Linux CUDA path and non-Windows DLL handling untested.

## Schema (for the migration)

- `asr_run.settings` TEXT NULL (JSON), existing rows NULL.
- New table `asr_chunk(job_id TEXT NOT NULL REFERENCES job(id), idx INTEGER NOT NULL, fingerprint TEXT NOT NULL,
  own_start DOUBLE PRECISION NOT NULL, own_end DOUBLE PRECISION NOT NULL, words TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (job_id, idx))` (SQLite: REAL / TEXT).
