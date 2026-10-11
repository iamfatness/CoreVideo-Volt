# Library

Find a moment. Search returns a span on source timecode.

Words from the transcript are spans. A hit seeks the proxy at the span in-point. Rights are a field on the asset.

Acceptance: "steal" returns the span and the asset ID. Reindexing does not duplicate words.

## Built: speech worker

`volt/speech.py` fills the transcript spans that `index_words` used to take by hand.

- Runs only on a closed hi-res essence. An open asset is refused and no job is created.
- ffmpeg pulls the first audio stream to 16 kHz mono. An essence with no audio fails the job with "essence has no audio".
- An `Engine` returns words with seconds from the start of the essence. The worker adds the essence start timecode and writes spans on source timecode (non-drop). A word with no length gets one frame.
- `WhisperCliEngine` runs the openai-whisper CLI with word timestamps. Any program that writes the same JSON works. `FakeEngine` is for tests.
- Same job key as `index_words`, so a second run re-runs nothing and adds no duplicates. A failed job keeps the reason and a retry runs again.
- `transcribe_pending` does every closed asset without a transcript. One failure does not stop the rest. `python -m volt.speech <db> [model]` runs it.

Not proven: no real ASR engine is installed here. The CLI engine is tested against a stub that writes the whisper JSON shape, so run it once against real whisper before relying on word accuracy or on the exact JSON.
