# CoreVideo Volt

Volt is the show. CoreVideo is the switcher. Volt is the software wing that takes a recording from the moment it starts to air and publish, under one asset ID, for a crew that cannot buy a legacy broadcast suite and will not stitch five tools together.

This repository holds the product plan, the architecture, and the first brand pass. It is not the application.

## Read this first

- [Start here](docs/start-here.md) — what to build first, and the feature specs
- [Product brief](docs/product-brief.md) — who it is for, what it owns, what it refuses
- [Architecture](docs/architecture.md) — planes, objects, session, adapters
- [Data model](docs/data-model.md) — asset, essence, span, item, cue, version
- [Roadmap](docs/roadmap.md) — what has to be boring before the clock goes to air
- [Brand guide](docs/brand-guide.md) — name, color, type, logo proposals, UI direction

## Position

Legacy MAM and broadcast suites are one contract and five products, priced like an airframe and sold like one. Point tools are cheaper and leave the glue to the crew. Volt is the gap: one session that mints an ID when the file is still growing and is still that ID when the item is cued, aired, and published.

## Owns

Ingest, library, rundown, software playout, cues into CoreVideo, publish.

## Does not own

An MXF muxer, an LTO driver, a loudness library, IMF packaging, or a 200-channel network origination desk. Those are engines and adapters.
