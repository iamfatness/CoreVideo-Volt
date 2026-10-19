# Ingest

Bring a card or a CoreVideo recording in as one asset.

`ingest.request` mints or reuses the asset by source key and enqueues `type=ingest` with payload `{path, quarantine, owner, show_name, camera}`. It does not copy. The runner claims the job and copies the card to quarantine, then ffprobe and a checksum gate the promote. Record start mints the ID while the ISO is open. The same source key returns the same asset.

`POST /api/ingest` allow-lists the path, then calls `request`. It returns `202` with `asset`, `minted`, `job`, `status: queued`. A replay of the same key is the same job. A file that does not parse stays in quarantine (handler `Permanent`, job `failed: parse failed`). Disk low is `Defer`: the asset may already exist, the quarantine copy is not made, and the job stays queued. Missing source is `Permanent`.

A watch folder (`volt watch`, or the runner periodic when `VOLT_WATCH=1`) lists regular files directly in each `VOLT_INGEST_ROOTS` folder (not recursive; ignore `*.part` and names starting with `.`). A file whose size and mtime have been stable for `VOLT_WATCH_STABLE_SECONDS` (default 5) is requested. Growing cards wait. The first observation of a file never ingests: it only records the fingerprint. `volt watch --once` on a brand-new file therefore queues nothing until a later scan, after the stable window. The CLI writes that fingerprint next to the database (`<db>.watch-seen.json`, or `VOLT_WATCH_SEEN`). The runner periodic keeps `seen` in memory for the life of the process. `volt run` starts watch with the runner when the flag is set; it is not a fourth supervised process unless watch is run by hand.

Acceptance: a parsed card has one hi-res essence. A file that does not parse stays in quarantine. Replaying the job does not mint. HTTP returns before the copy.

Deleting an ingested asset is a two-step soft delete and later purge; the quarantine copy is deleted only if `VOLT_QUARANTINE_DIR` names the directory, and a recording registered by `record_start` is never deleted. See [retention](retention.md).

Ingest also records the file's frame rate and audio channel count, and queues its proxy and transcript jobs (the same ones a wrap queues). See [dress rehearsal](../dress-rehearsal.md).
