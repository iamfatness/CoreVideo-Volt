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

The URL is a `file://` URL under `out_dir/<asset>/<span>-<kind>.mp4`. A version that is already done is not re-rendered, so a retry only touches the failed ones. An asset that is still recording, a span outside the source, or an ffmpeg failure fails that version, records the reason on its job, and leaves no partial file. Timecode is non-drop only. Object storage and live URLs are still to come.
