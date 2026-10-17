# Dress rehearsal (2026-10-16)

One show, end to end, on real CoreVideo Pro recordings, against the real core at `127.0.0.1:8011`. The point was to find what the unit tests cannot. The driver is `control/bench/dress_rehearsal.py`; every number below was read from its output (`rehearsal-log.json`), not assumed.

## Setup

- Throwaway SQLite database and output directory under `D:\scratch\dress-rehearsal\r4\`. The operator's data and the core's preferences file were not touched or read.
- Footage: the `Program.mp4` of three recordings in `Videos\CoreVideo Pro` (1080p60 H.264 + stereo AAC, fragmented MP4): `20261001-181807` (26 s), `20261002-072351` (18.8 s), `20260929-162726` (64.8 s). The fourth asset is an ISO file from `20261002-072351`, copied and run through the open-file flow.
- Speech: `faster-whisper` 1.2.1 with `tiny.en`, from `D:\scratch\fw-venv` (the default Python has no engine). The driver ran under that interpreter. Device was left on `auto`.
- Operator API: `serve.Handler` on a `ThreadingHTTPServer` bound to port 0 inside the driver process, every call made over HTTP. A show, its items and cues have no HTTP endpoint, so those, ingest, and the runner were library calls.
- Core baseline before the live part: `engineOn` true, `recording` false, `lowerThirdOnAir` false, `streaming` false. The driver refuses to go live unless that is the case. After the run all four read the same again.
- The core was unreachable when the work started (connection refused) and up about 25 minutes later. The first two runs therefore used a scripted `FakeRoom`; the last used the real core.

## Steps and timings (live run, `r4`)

| Step | Result | Time |
|---|---|---|
| ingest 3 files (mint, quarantine copy, ffprobe, sha256) | 3 minted, `done` | 1.2 s |
| replay ingest of one source key | not minted, same asset | 0.1 s |
| ingest a non-media file | `quarantine`, no essence, job `failed: parse failed` | 0.1 s |
| `record_start` on a copied file, then `wrap` | sha256 recorded | 0.5 s |
| runner drain, assets not cleared | 4 proxies done; 4 asr jobs deferred `waiting: asset is not cleared` | 16.2 s |
| clear assets over HTTP, drain | 4 asr done | 6.5 s |
| transcript | 61, 39 and 116 words on the three programs; 0 on the ISO copy | |
| `POST shots/detect` x4, drain | 4 done; the programs had 1, 2 and 15 shots | 5.0 s |
| mark clips over HTTP | first shot of each asset, plus one from transcript words ("Effectively poses several challenges.", 127 frames) | under 0.1 s |
| rundown of 3 items | 3 cues attached | |
| take 1 (`record.start`, `overlay.take on`) | both fired; asset minted | 1.4 s |
| take 2 (`overlay.take off`) | fired | 0.8 s |
| `POST /api/record/stop` after 3 s | wrapped; sha256 matched the file on disk (4.1 MB, 252 frames, 60 fps, stereo) | 4.4 s |
| core state compared with baseline | identical | |
| take 3 (`record.arm`, no adapter) | HTTP 409 `cue_unsupported`, rundown rewound | |
| hold, then take | 409 `held` | |
| release, kill the last item, go | show over; last item `skipped` | |
| as-run | items 1 and 2 `aired` with in/out stamps (see rough edges) | |
| publish a clip to local storage | house 5.5 MB, ott 1.5 MB, social 1.5 MB, `file://` URLs | 12.3 s |
| thumb, proxy `Range`, `/seek?q=` | 200, 206 with `bytes 0-99/707412`, 200 | 0.2 s |
| NLE export, xmeml hi-res / EDL hi-res / xmeml proxy | 200 each; 5 clips, 1821 frames at 60 fps; hi-res paths point at the quarantine copy, proxy paths at the proxy file | under 0.1 s each |

Whole run 49 s. A fake-room run (`r3`) took 45.8 s with the same results except that cues went to the scripted room.

## Bugs found and fixed

All four are in the path from "a file exists" to "an editor can use it", and none could show up in the existing tests, which write `frame_rate` into the essence row by hand.

1. **No code wrote an essence's frame rate.** `ingest_file` and `record.wrap` left `essence.frame_rate` NULL, so `POST shots/detect`, `POST spans` and every other shot-span write answered 422 `the essence has no frame rate recorded`. First run: every asset refused. Fixed: `shots.record_media_facts` probes the rate and writes it on ingest and on wrap (never over an existing value). Checked on a real core recording: `frame_rate` 60 after the wrap.
2. **Ingest queued no follow-on work.** A file brought in by `ingest_file` never got a proxy job or a transcript job, so it could not be searched, thumbnailed or exported as proxy. Recordings got them from `wrap`; ingest did not. Fixed: ingest calls `record.chain_after_wrap` for the closed file (same job keys, so a replay queues nothing new).
3. **Audio layout was never written.** The NLE export warned `no source has a known audio layout: no audio tracks were written` for stereo footage, so an xmeml or EDL of real programs would have opened in an editor with no audio. Fixed with the same probe: the first audio stream's channel count is recorded as text (`2`). After the fix the warning is gone.
4. **Length counted on the average frame rate.** `ingest_file` computed `duration_frames` before it knew the essence rate, so it used `avg_frame_rate`. The 64.8 s program (variable frame rate, average 59.86) was stored as 3877 frames; at its 60 fps stream rate it is about 3886. The nine missing frames sat at the end of the last shot. Fixed: the rate is probed first and passed to the length count.

Regression tests: `control/tests/test_ingest.py` (frame rate and audio layout recorded on ingest, follow-on jobs queued once and not for a file that does not parse, wrap records the rate, an existing rate is kept, the length is counted on the essence rate).

## Rough edges not fixed

- **No HTTP way to build a show.** Creating a show, an item or a cue, and ingesting a file, are library calls. The operator API can mark, reorder, take and publish, but not set up the rundown it takes. Nothing starts a runner either; jobs sit queued until `python -m volt.runner` runs.
- **As-run has one-second resolution.** `as_run_in` and `as_run_out` are `...Z` to the second. Items 1 and 2 aired for under a second in the fake run and show identical in and out. Item 1 on the live run read 23:32:21 to 23:32:22.
- **Timecode and codec were not read** (fixed afterwards, see below). `timecode_start` and `codec` stayed NULL after ingest, so a file with embedded timecode was treated as starting at 00:00:00:00 in spans and in the NLE export.
- **The cue's `record.start` leaves a real recording on disk.** The live run created `Videos\CoreVideo Pro\corevideo-recording-20261016-193221` (about 4 s). It is the user's to keep or delete.
- **The ISO copy produced 0 transcript words** with `tiny.en` (explained below: that file is digital silence).
- The `proxy:` job from `record_start` and the `proxy-final:` job from `wrap` are two proxy builds of one asset (already documented in `runner.md`); both ran here without a clash.

## Not run

- Any stream. `transport.stream.set [true]` was never called.
- `show-input.take` (needs the route action, [CoreVideoPro #861](https://github.com/iamfatness/CoreVideoPro/issues/861)), `record.arm`, audio snapshot.
- The NLE files were not opened in Premiere, Resolve or Final Cut. They are still unverified against a real editor.
- Publishing to S3, Azure or GCS; only local storage.
- Postgres (CI only), a crash mid-take, two runners at once, a hold landing during a take.
- Transcription with a model larger than `tiny.en`, and on the GPU specifically.
- An ack is not evidence: every cue above was counted fired only because the dispatcher saw `recording.lifecycle` and `lowerThirdOnAir` change on the core, and the core was compared with its baseline afterwards.

## Follow-up: embedded timecode, codecs, and the 0-word ISO

- **Timecode and codecs are now read** (`shots.probe_media`, written by `shots.record_media_facts` at ingest and wrap, never over an existing value). The start comes from the QuickTime/MP4 timecode track (`tmcd_track`), else the video stream's `timecode` tag (`video_stream_tag`), else the container tag (`format_tag`, Matroska). It is stored in `essence.timecode_start` with the label as written (`01:00:00;00` is drop-frame), and `essence.timecode_source` records where it came from. A file with no timecode keeps `timecode_start` NULL (read everywhere as 00:00:00:00) and source `none`; a label that cannot be placed on the file's rate (drop-frame at 25 fps, a skipped drop-frame label) is ignored and recorded as `invalid`. Also stored: video `codec`, `video_profile`, `pix_fmt`, `audio_codec`, `audio_sample_rate` (migration 10). Shot spans, transcript timecodes and the NLE export already offset by `timecode_start`; they now get a real one.
- **Observed on the real recordings:** none of the CoreVideo Pro Program or ISO files carries a timecode (source `none`); they are H.264 Constrained Baseline, `yuvj420p` (full range), AAC 48 kHz stereo. The variable-rate program still counts 3886 frames on its 60 fps rate.
- **The ISO copy was silent, not mis-extracted.** In `corevideo-recording-20261002-072351`, seven of the eight ISO files (all but `ISO-06-Elena-Kova.mp4`) have one stereo AAC stream whose loudest sample is -91.0 dB, the codec's floor for all-zero input (Volt measured -120 dBFS on the extracted 16 kHz mono). `ISO-06` has the same audio as `Program.mp4` (mean -29.4 dB, max -8.1 dB) and `tiny.en` found 40 words in it. There is one audio stream per file, so no stream or channel was missed. The run used to look like a successful empty transcript.
- **Reporting:** a run that finds no words now records an outcome in `asr_run.settings` (`result`): `no_speech` with reason `no_audio_stream` (the job finishes `done`; it used to fail), `silent_audio` (peak at or below -70 dBFS), or `no_words_recognized` (there is sound but no speech found). `GET /api/assets/{id}/transcript` carries it as `asr` (`null` before any run), with `wordCount` and `peakDbfs`.
