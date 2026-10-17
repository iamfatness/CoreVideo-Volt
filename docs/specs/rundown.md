# Rundown

An ordered show. Each item points at a span and holds the script.

Status moves ready, on air, killed. Reorder updates position. Attach stores the span ID.

Acceptance: reorder leaves the asset count unchanged. Killing an item leaves the asset in place. An uncleared asset can be attached and cannot be taken.

Attach is one transaction under the show lock (the lock go and take hold) ending in `UPDATE item SET span_id = ? WHERE id = ? AND status = 'ready'`. It raises `ItemNotReady` (`409 not_attachable` over HTTP) for an item that is on air, aired, killed or skipped, so an attach racing a take is applied before the item airs or refused, never after.

Delete show (`building.delete_show`, `POST /api/shows/{show}/delete`): refused with `show_not_deletable` if any item is on air or has aired (status or as-run time). Otherwise its ready, killed and skipped items and their cues, then the show, go in one transaction under the show lock. Spans and assets are not touched.
