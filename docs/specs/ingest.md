# Ingest

Status: first implementation.

## Job

Mint an asset ID before the recording wraps, and only promote the essence after checksum and a parse.

## In scope

- Watch folder and explicit card offload.
- CoreVideo program and ISO record. The record start is the mint.
- SRT and NDI contribution as a file or a growing file, not a separate product.
- Quarantine volume. The card is not cleared until promote.
- Checksum. MediaInfo or ffprobe for codec, frame rate, timecode start, audio layout.
- Open essence. `open = true` until wrap. Partial proxy is allowed.
- One job record. Replay does not mint a second asset.

## Out of scope

Baseband SDI ports. Virus scanning as a product. Camera-native RED or ARRIRAW toolkits beyond "copy, checksum, hand to FFmpeg." Loudness. QC beyond "the file parses and the checksum matches."

## Acceptance

- Pull a card, checksum matches, asset exists, proxy is scrubbable before the operator wipes the card.
- Start a CoreVideo ISO. The asset ID exists within a few seconds. A proxy covers the growing file and extends as it grows.
- Kill the transcode and replay the job. One asset, one essence, a new job attempt.
- A file that fails checksum stays in quarantine and is not searchable.

## Depends on

Asset, essence, job. Nothing else.
