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
