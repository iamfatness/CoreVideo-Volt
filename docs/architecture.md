# Architecture

Volt is one session over a control plane. The end state includes rundown, playout, cues, and publish. The first release does not.

## Planes

```text
Session  (the product)
  rundown · program monitor · cue list · library search · publish
        │
Control plane
  asset service · rights · version graph · rundown · clock · search
        │
Data plane  (replaceable workers)
  ingest agents · FFmpeg farm · ASR · cue dispatcher · publish workers
        │
Essence
  hi-res store · proxy store · sidecars
        │
Room
  CoreVideo inputs, PTZ, graphics, audio snapshots
```

The control plane is ours and stays boring. Workers can be swapped: FFmpeg now, a GPU farm later, Whisper now, a better ASR later. A hardware playout frame is an output adapter, not a process inside the session.

## The ID

An asset ID is minted when a recording starts, including a file that is still growing. That ID is the only handle Premiere, a MOS object, a playout item, and a social cut are allowed to hold. Replacing the file does not replace the ID. A failed transcode replay does not mint a second asset.

## Session surfaces

One login. Four surfaces, not four products.

- Show. Rundown, program monitor, next item, cue list, on-air tally.
- Library. Search over spans. Transcript hits seek the proxy.
- Item. In, out, script, cues, rights, versions.
- Publish. House file, OTT ladder, social cut. URLs write back.

CoreVideo is not a sibling app the operator alt-tabs into for the cue. The cue dispatcher talks to the CoreVideo control API. The operator sees the result on the program monitor.

## Clock

The clock lives in Volt once playout ships. Until then, CoreVideo is the live switcher and Volt is the rundown and the library. A cue that fires the wrong camera is a worse failure than a missing MOS profile, so the clock is not sketched into the first release.

## Adapters

| Edge | Contract |
|---|---|
| CoreVideo | Record start mints the ID. Cue sets input, PTZ preset, graphic, audio snapshot. |
| Premiere / Resolve | Panel searches spans and imports a proxy. Relink path is the hi-res essence. One NLE first. |
| MOS | Profile 1 pushes an object. Profile 2 pushes a running order. Export only. |
| Hardware playout | Conformed file plus asset ID. As-run writes back. |
| Social and OTT | Version records. Platform APIs are workers. |

## What we will not build

No MXF muxer. No LTO driver. No loudness engine. No switcher. No 200-channel origination. No face gallery before ASR and shot detection are trusted. No promise of frame-accurate relink in both Premiere and Resolve in v1.

## Stack direction

Matches the suite already in motion: a desktop session beside CoreVideo on Windows, FFmpeg for proxy and deliverables, MediaInfo for technical metadata, Postgres for assets, OpenSearch for spans, a queue for jobs, object or SAN storage for essence. The session UI can be the same WinUI or Qt family as CoreVideo. Do not start a second design system.
