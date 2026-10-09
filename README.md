# CoreVideo Volt

Volt is the show session for CoreVideo. It orchestrates the rundown, stores the media, transcodes proxies and deliverables, indexes the library, and distributes finished versions.

## Now

- MAM: an asset ID minted at record start or card ingest. Essence, span, rights, idempotent jobs.
- Storage: quarantine until checksum and parse, then a located essence. Open files stay open until wrap.
- Transcoding: a proxy essence on the same asset, refreshable while the ISO is open.
- Library: transcript words stored as spans. Search returns the span.
- Orchestration: a rundown of items pointing at spans. Cues stored on the item. A take arms them.
- Distribution: an approved span becomes house, OTT, and social versions. URLs write back onto the item.

## Next

- MAM: speech worker writing those word spans. Premiere panel that imports a span.
- Storage: Postgres as the running store. Object store for essence.
- Transcoding: deliverable workers that write the house, OTT, and social files.
- Orchestration: cue dispatch into CoreVideo. Software playout of the rundown. As-run on the item.
- Distribution: real publish URLs from those workers.

## Read

- [Areas](docs/areas.md)
- [Architecture](docs/architecture.md)
- [Data model](docs/data-model.md)
- [Roadmap](docs/roadmap.md)
- [Brand](docs/brand-guide.md)

Run the control plane from `control/`:

```bash
python3 -m unittest discover -s tests -v
python3 -m volt.serve
```
