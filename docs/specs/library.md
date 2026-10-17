# Library

Find a moment. Search returns a span on source timecode.

Words from the transcript are spans. A hit seeks the proxy at the span in-point. Rights are a field on the asset.

Acceptance: "steal" returns the span and the asset ID. Reindexing does not duplicate words.

## Built: speech worker

`volt/speech.py` fills the transcript spans that `index_words` used to take by hand. Stdlib only; faster-whisper is imported lazily and only when `FasterWhisperEngine` runs.

- Runs only on a closed hi-res essence. An open asset is refused and no job is created.
- Consent gate: an asset that is not `cleared` is refused before any audio is read (`SpeechError: asset is not cleared`), and `transcribe_pending` reports it as `skipped: not cleared`. `require_cleared=False` overrides, deliberately and per call. Legal hold does not block transcription; it blocks publish.
- ffmpeg pulls the first audio stream to 16 kHz mono. An essence with no audio fails the job with "essence has no audio".
- The worker adds the essence start timecode to word times and writes spans on source timecode through `volt/timecode.py`, counted in whole frames at the essence's exact `frame_rate` (e.g. `30000/1001`). A start stored with `;` before the frames (`01:00:00;00`) is drop-frame, and the spans are written with `;` labels that skip the labels that do not exist; any other start is non-drop. A word with no length gets one frame.
- `FasterWhisperEngine(device="auto"|"cuda"|"cpu", model=None, compute_type=None)`: CUDA defaults to large-v3 float16, otherwise CPU distil-large-v3 int8. `model` and `compute_type` override. `device="cuda"` without a GPU raises; there is no quiet CPU fallback. A missing package or model raises `SpeechError` with the reason.
- Vocabulary: `transcribe(..., vocabulary=[...])` is an ordered priority list (most important first, de-duplicated). The engine keeps the leading terms that fit 220 tokens (measured with the model tokenizer) and passes them as hotwords. `show_vocabulary(store, show_name)` builds one from the show title then its rundown slugs. Volt has no guest or sponsor records yet; put those in front when it does.
- Corrections: `parse_corrections` reads `heard => should be` lines; `apply_corrections` runs after recognition, whole-word, case-insensitive, punctuation around a match kept, longest rule first, multi-word matches merged into one word spanning first start to last end.
- Provenance: each run writes an `asr_run` row (engine, model, device, compute type, vocabulary requested and actually used, corrections that fired with counts, word count). The job key is `asr-v2:<hash of engine/model/device/compute/vocabulary/corrections>`. The same configuration on the same asset is a no-op; any change is a new run that replaces that asset's transcript spans (so a better model can be re-run on purpose). `transcribe_pending` only touches assets with no transcript. This replaces the old fixed `asr-v1` key. `index_words` still works for hand-fed words and is untouched.
- `python -m volt.speech <db> [--model M] [--device D] [--vocabulary file] [--corrections file]`.
- The openai-whisper CLI engine from the first cut was dropped: it was only ever tested against a stub, and faster-whisper gives the tokenizer access that vocabulary budgeting needs.

### Real-engine check (2026-10-11)

Engine faster-whisper 1.2.1, model `tiny.en`, CPU int8, VAD off. Clips were made with Windows SAPI (default voice), muxed with ffmpeg test video at 25 fps, start timecode 01:00:00:00, minted as a cleared asset.

- Sentence clip, "They steal home. The Okonkwo interview airs Tuesday.": 8 words, all correct except "Okonquo" (name misspelled). `search("steal")` hits. Real output carries trailing punctuation on words ("home.", "Tuesday."); search is substring so it still matches.
- Timing clip: eight words spoken singly, concatenated with known placement; true onsets measured with ffmpeg silencedetect. Word starts versus truth: mean absolute error 0.08 s, worst 0.24 s ("steal", early; about 6 frames at 25 fps), all others within 0.13 s. The first word starts at exactly 0.00 although speech begins 0.13 s in (whisper snaps the first word to segment start). Ends are looser (a word's end often runs into the next word's start). Treat spans as good to roughly a quarter second, not frame accurate.
- Without help, "Okonkwo" came out as "o" + "'conquo". With vocabulary `["Okonkwo", "Tuesday show"]` and corrections it came out right, and the `asr_run` row recorded the terms used and the two corrections that fired.
- Caveat found: on the sequence clip, tiny.en with hotwords hallucinated trailing words ("Thursday, Sunday, Saturday" over the silent tail at 12.2 to 13.4 s) and turned "airs" into "Ayres". The same clip without hotwords did not. Hotwords on a tiny model are not free; the real default models have not been compared.
- The JSON shape question from the CLI engine is moot with faster-whisper: words come from `segment.words` objects directly.
- Environment gotcha: faster-whisper 1.2.1 fails on av 19 (`open() got an unexpected keyword argument 'metadata_errors'`); pin `av<19` (16.1.0 used here). It surfaces as a loud `SpeechError`.
- Test: `RealEngine` in `tests/test_speech.py` is skipped unless `VOLT_ASR_REAL=1` on Windows with faster-whisper importable; it speaks "They steal home" via SAPI, transcribes, and asserts the hit lands in the first seconds. Run with a Python that has faster-whisper (venv was `D:\scratch\fw-venv`). Everything else runs on the stub engine in CI.

Reused from SwarmScribe (read, never imported): ordered vocabulary with a hard token budget and a record of terms actually used; whole-word case-insensitive `heard => should be` corrections after recognition; device-aware default model with an explicit recorded choice; per-run provenance; consent gate; loud failure with no silent fallback.

Not reused or not done: leader/follower distribution of long archives; stereo-split with channel labels for ISO and call audio; SwarmScribe's full correction edge-punctuation rules (Volt's are simpler); consent as a publish-OK flag separate from `cleared` (Volt uses `cleared`).

Real speech (2026-10-16; numbers in `speech-scale.md`, section "Real speech"): a 32 min podcast and a 94.5 min church recording ran end to end on large-v3/CUDA (RTF 0.13-0.17 on a shared 4090, about 3.3 GB RAM regardless of length); kill and resume worked after a stuck-slot bug was fixed; a silently-skipped-speech defect was found and patched. Still unproven: accuracy against any ground truth (none exists; only model-vs-model agreement and reading), real Zoom participant audio, multi-asset `transcribe_pending` with a real engine, whether `show_vocabulary` helps on a real show, Postgres with a real engine.
