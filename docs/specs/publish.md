# Publish

An approved span becomes a house version, an OTT version, and a social version. Each URL writes back onto the rundown item.

Acceptance: three versions, one asset. A retry updates the failed version. An uncleared span publishes nothing.

## Real files

`publish_files(store, span_id, out_dir)` cuts the span from the closed hi-res essence at its source timecode (`control/volt/deliverables.py`):

| Kind | File |
|---|---|
| house | Source size, H.264 CRF 18, AAC 256k |
| ott | Up to 720p height, H.264 CRF 23, AAC 128k |
| social | 1080x1920 vertical, fill and center crop |

By default the URL is a `file://` URL under `out_dir/<asset>/<span>-<kind>.mp4`; with `VOLT_STORAGE=s3` each cut is uploaded, verified and given a live (presigned or public) URL instead, and the version records its storage backend and key so the URL can be re-signed (`publish.refresh_url`). See `docs/specs/storage.md`. A version that is already done is not re-rendered, so a retry only touches the failed ones. An asset that is still recording, a span outside the source, or an ffmpeg failure fails that version, records the reason on its job, and leaves no partial file. Timecode comes from `volt/timecode.py`: non-drop and drop-frame (29.97 and 59.94, `;` before the frames) on exact rational rates. The clip is drop-frame when its stored `timecode_start` has a `;`; a span label that does not exist in drop-frame (`00:01:00;00`) fails that version with the reason. The cut seeks a quarter frame before the span's first frame and stops on a frame count, so it holds exactly the labelled frames.

## Two writers, one span

`publish_files` claims each version before rendering it. The claim is the version's job row (`publish:<span>:<kind>`) taken with one conditional UPDATE (`status != 'running' OR lease lapsed` to `running`, `claimed_by`, `lease_expires_at`), the same columns the runner uses; the version row is created with `INSERT ... ON CONFLICT DO NOTHING`, so two first-time writers end with one row instead of a unique violation. A writer that does not get the claim neither renders nor uploads that version; it gets it back as status `busy` (or `done` if the holder finished meanwhile). The version and job outcome are written in one commit and only by the holder (guarded by `claimed_by`), and no write lock is held across a render. A render that raises an unexpected error gives the claim back as `failed` before the error propagates. Existing rules hold: a done version is skipped, a failed one is retried (`enqueue` re-queues it with its attempt counted). The runner's publish handler defers (no attempt used) when a version is busy, and `POST /api/spans/{span}/publish` answers 409 `publish_busy`. `deliverables.render` writes ffmpeg's output to a private `.part` name and renames it over the cut, so even a taken-over render cannot clobber a live one's half-made file.

Not covered: the claim has a lease (`CLAIM_LEASE`, 30 minutes) and no heartbeat, so a render that outlasts it can be taken over and run twice (the guarded settle means only one records the outcome, but both may upload the same key, and an S3 verification failure in one deletes the object the other just stored); a writer that dies mid-render blocks that version until the lease lapses. The placeholder path (no renderer, `volt://` URLs) is not claimed. Tests: `tests/test_race_publish.py`.
