# Speech at scale

Status: implemented; measured on one machine (RTX 4090 24 GB, 28-thread CPU, Windows 11,
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

## Real speech (measured 2026-10-16)

First run of the pipeline on recordings of real people, through the real runner CLI (`control/bench/asr_real.py`,
throwaway databases under `D:/scratch/real`, models offline from the local cache). Hardware: RTX 4090 24 GB, CUDA path
works (large-v3 float16 on `cuda`; the pip cublas DLLs were found); faster-whisper 1.2.1. **The GPU was shared** with a
running CoreVideo Pro and other desktop apps (30-94 % busy and 2-10 GB used by others at the start of each run), so every
wall time below is "on a busy box" and some are far from the idle numbers above. No ground-truth transcripts exist for any
of this audio; "accuracy" below means plausibility by reading plus agreement between models, never WER.

Inputs (copied to scratch, originals untouched):
- `dtns_1944s.mp3`: one episode of a daily tech-news podcast, 1944 s (32.4 min); several hosts, ad reads over music beds, jingles.
- `church_5667s.mp3`: a church anniversary-service recording, 5667 s (94.5 min); reverberant room, preaching, long
  stretches of congregational singing, a Spanish-interpreted passage. Essence given `01:00:00:00` at 30 fps.
- `Blessed Assurance.mp4`, 256 s: instrumental music (VAD finds 0.3 s of speech in it). A 105 s ISO that is digital silence.
- The CoreVideo Pro recordings (`Program.mp4`, `ISO-*.mp4`) are a scripted panel ("Dr. Carter", "Dr. Patel") that reads
  like synthetic voices; I did not count them as real speech. Most ISO files are silent (-91 dB). The longest recording is
  106 s, so no recording from the app is a long file.

**Speed and memory (large-v3, cuda, 900 s windows).** 1944 s podcast: 250 s wall (RTF 0.13) on the quietest run, 598 s and
772 s on busier ones (same audio, the 772 s one includes the recovery pass below). 5667 s church service, uninterrupted:
967 s (RTF 0.17), 7 windows. Peak RAM of the runner process tree (sum of per-process peaks, so an upper bound): 3.34 GB for
the 94 min file and 3.32 GB for a 3 min clip, so memory does not grow with length. 180 s of the podcast, wall including model
load: GPU tiny.en 5.5 s, distil-large-v3 7.1 s, large-v3 20.7 s; CPU int8 tiny.en 8.2 s (RTF 0.05), distil-large-v3 62.9 s
(0.35), large-v3 178.8 s (0.99). Peak RAM 0.4/1.8/3.3 GB (CPU), 0.9/1.8/3.3 GB (GPU). large-v3 on CPU int8 and on GPU
float16 differed in 2 of 513 words, with identical start times (median diff 0.00 s, p99 0.08 s).

**Found: long-form decoding silently skips stretches of real speech (fixed).** With the shipped defaults the podcast lost
25.7 s of an ad read (91.8 s to 117.5 s) with no error: the words were simply not in the transcript, while the same
stretch decoded alone gave 76 words. Changing settings only moved the holes: `condition_on_previous_text=False` recovered
that one and lost others (636-666 s, 697-717 s, 1351-1363 s); a 120 s window still lost 93-117 s and 637-661 s; disabling
the VAD or `no_speech_threshold` did not help. Using the VAD as a second opinion (a stretch of at least 5 s with no word
where at least half the audio is voiced) found the holes in every variant: 6 holes / 92 s (defaults), 4 / 83 s (no
previous-text), 3 / 74 s (120 s windows). Fix: `FasterWhisperEngine(recover=True)` (default; needs the VAD on) decodes each
such stretch again on its own, keeps only words whose midpoint falls inside it, and records `recover_gaps`,
`recovered_gaps`, `recovered_words` in the run's stats; `recover` is part of the engine's recorded settings, so it is a new
run identity. Result on the podcast: 3 holes recovered (150 words), 1 hole left (6 s, a jingle). Church: 16 holes found,
13-14 recovered (140-147 words); about 10 stretches of ~5 s with voice and no word remain (56 s of 5667 s, 1 %).
Tests: `RecoverSkippedSpeech` in `tests/test_speech_scale.py` (fake model).

**Found: a killed transcribe blocked its own retry for up to 30 minutes (fixed).** `transcribe` takes a per-asset
`asr-slot:<asset>` job with a 30 minute lease that was never renewed and is not released by a kill. A runner killed
mid-file and restarted after its job lease expired logged "recovered 1 job(s) with an expired lease" and then
`deferred=1`, because the dead process still held the slot. Now the slot owner records host and pid; a slot held by a dead
process on this host is taken over at once; the lease is renewed after every window (a file longer than 30 minutes cannot
lose it under the holder). A holder on another host still keeps the slot until its lease lapses. Tests:
`tests/test_job_limits.py`.

**Kill and resume on the 94.5 min file.** Runner killed (taskkill of the process tree the script started) 48 s in, after 2
windows were saved; restarted after the 30 s job lease: log `resuming at window 2 (1800 of 5667 s already done)`, windows
3-7 decoded, job done, `resumed_windows = 2`, 734 s. The uninterrupted run of the same file gave 6719 words, the resumed
one 6669 (0.7 % fewer; sequence ratio 0.955, 106 differing regions). The first two windows had identical word counts in
both (164 and 248). The differing regions are one run skipping or recovering a stretch the other did not, and the Spanish
passage (one run kept the Spanish words, the other produced English). So resume is "same within decoder variation", which
on real speech is wider than the 0.1 % seen on synthetic speech. I did not run the uninterrupted file twice, so I cannot
say how much of the 0.7 % is run-to-run variation rather than resume.

**Chunk boundaries.** The church file had 6 hard cuts and 1 quiet cut (noisy room: silencedetect found no quiet stretch);
the podcast had one cut, near 880 s. At the four church seams with words nearby (1800, 2700, 3600, 4500 s) and the podcast
seam: no doubled word runs except ones in the speech itself ("88 ... 88"), and the last word before and first word after
each cut were 0.3 to 2.7 s apart. One word ("The", 880.2 s) present when the neighbourhood is decoded alone is missing from
the windowed result; I did not establish the cause.

**Timecode.** Church words carry `01:00:00:00`-based labels (first word 64.07 s into the file). Against the waveform (only
pauses with at least 15 dB between the quietest and loudest 20 ms, starting or ending quiet): podcast onsets n=36, word
start minus sound onset median -0.09 s (abs median 0.20 s, p90 0.36 s), word ends n=44 median -0.18 s; church onsets n=159
median +0.22 s (abs median 0.26 s, p90 0.44 s), ends n=199 median -0.18 s. Starts later and ends earlier than the energy
burst by about 0.2 s: word spans sit inside the bursts, with no one-direction offset that would be a timecode shift. The
method is crude (reverb, music beds); read it as "within about +/-0.3 s", not frame accurate. Across models on 180 s of the
podcast, matched words' start times: tiny.en 0.24 s later than large-v3 (p90 0.36 s), distil-large-v3 0.08 s later (p90
0.24 s); the earlier synthetic result had large-v3 about 0.19 s early.

**Word accuracy.** No truth. The first 90 words of the podcast (a toilet advertisement) and the church opening ("We are
celebrating 88 years at the Apostolic Church...") read as correct, coherent English. Disagreement with large-v3 on the 180 s
clip (substitutions + deletions + insertions over large-v3's word count): tiny.en 6.4 % (GPU) / 5.5 % (CPU),
distil-large-v3 6.6 % / 5.8 %. That is agreement, not error rate.

**Non-speech.** 105 s of digital silence: 0 words. 256 s of instrumental music: 2 words, "Thank you." at 2.5 s, from a 0.3 s
blip the VAD called speech, so the "0 invented words with VAD on" result on synthetic noise did not hold on this music. Sung
words are not transcribed: about 25 minutes of the church file (112-628 s and 682-1589 s) have no words, the VAD finds
under 1 s of speech in them, and a standalone decode of 1000-1030 s without the VAD gives intelligible lyrics ("Even when I
was unaware, you were consistent..."). That is the VAD guard doing what it was chosen for, but singing is invisible.

**Job behaviour on a real file (tiny.en CPU, 180 s of the podcast, asset on legal hold):** transcribed (511 words) with the
hold set; the same configuration again: 0 engine calls, still 1 `asr_run`; a different model replaced the transcript (2
`asr_run` rows). With a hand-run `transcribe` (distil-large-v3, 46 s) holding the slot: a second hand-run raised "another
transcribe holds this asset" with 0 engine calls, and `runner --once` printed `deferred=1`; after the first finished, the
runner ran its job. A refused hand-run leaves one `queued` `asr` job row with a run-key idempotency key that no runner
claims (`CLAIM_PREFIX` only claims `asr:`); not fixed.

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
- The quality numbers in "Measured" above are synthetic TTS. Real speech was run once ("Real speech" above): one podcast, one
  church recording, no ground truth, one machine with a shared GPU. Still untested: real Zoom participant audio from the
  app's ISO path, a whole non-English file (only a Spanish passage inside English), overlapping speakers on separate
  channels, telephone-band audio, a second run of the same long file to measure run-to-run variation, models other than
  tiny.en / distil-large-v3 / large-v3, and the `recover` pass under Postgres.
- The recovery pass finds holes with the same Silero VAD that gates the main pass: speech the VAD calls non-speech (singing,
  speech under loud music) stays missing, and a VAD blip can still produce a hallucinated phrase (see Non-speech).
- Silence-guard and `min_word_prob` are plumbed and recorded but their effect was not measured.
- Resume was proven by killing the runner process between windows, not mid-window (a mid-window kill just redoes
  that window).
- The idle-release path is unit-tested; unloading was not observed to return VRAM in a long-lived runner.
- macOS/Linux CUDA path and non-Windows DLL handling untested.

## Replacing a transcript is atomic

`transcribe` used to delete the old transcript spans and commit, then insert the new spans one commit each, so a reader on another connection could see no transcript or half of one, a failure in between left a partial transcript, and two transcribes of one asset could leave both runs' spans mixed. The delete, the span inserts, this run's `asr_run` row and the removal of its `asr_chunk` windows are now one transaction under a per-asset lock (`transcript:<asset>`; a Postgres advisory lock, and SQLite's write lock already serializes). A reader sees the old transcript with the old run record or the new one with the new, never a half; a failure rolls back to the old transcript; two transcribes of one asset queue at that point (after the engine work, so the model is never held behind a lock) and the later one replaces the earlier one whole. The per-window checkpoints are unchanged: windows are still committed one by one while the engine works, and are deleted only in the same commit that publishes the transcript. Legal hold does not block this; the consent gate (`cleared`) is checked as before. Tests: `tests/test_race_transcribe.py`.

Not covered: two runners transcribing the same asset still both pay for the engine work (the runner claim stops this for one job, not for a hand-run `transcribe` beside a runner); the last to finish wins. The `asr_chunk` primary-key collision between a stale runner and its replacement (above) is unchanged.

## Schema (for the migration)

- `asr_run.settings` TEXT NULL (JSON), existing rows NULL.
- New table `asr_chunk(job_id TEXT NOT NULL REFERENCES job(id), idx INTEGER NOT NULL, fingerprint TEXT NOT NULL,
  own_start DOUBLE PRECISION NOT NULL, own_end DOUBLE PRECISION NOT NULL, words TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (job_id, idx))` (SQLite: REAL / TEXT).
