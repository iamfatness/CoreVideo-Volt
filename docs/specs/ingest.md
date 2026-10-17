# Ingest

Bring a card or a CoreVideo recording in as one asset.

A watch folder copies the card to quarantine. ffprobe and a checksum gate the promote. Record start mints the ID while the ISO is open. The same source key returns the same asset.

Acceptance: a parsed card has one hi-res essence. A file that does not parse stays in quarantine. Replaying the job does not mint.

Deleting an ingested asset is a two-step soft delete and later purge; the quarantine copy is deleted only if `VOLT_QUARANTINE_DIR` names the directory, and a recording registered by `record_start` is never deleted. See [retention](retention.md).

Ingest also records the file's frame rate and audio channel count, and queues its proxy and transcript jobs (the same ones a wrap queues). See [dress rehearsal](../dress-rehearsal.md).
