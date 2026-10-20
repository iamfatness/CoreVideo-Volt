# Next bundle

MAM foundation is on `main` (PR #58). The room track is unblocked once CoreVideo Pro [PR #953](https://github.com/iamfatness/CoreVideoPro/pull/953) (`scene.cueWithRoutes` / `scene.route.set`, #861) is on an installed core.

Ordered. Each piece lists what done means. Do not start later MAM (tokens, MOS, archive) in this bundle.

## 1. Desk tells the truth about Program take

`show-input.take` has a FakeRoom adapter. Until a live Program take is recorded in [rooms.md](rooms.md), the cue picker must not treat it as live-proven.

- `verified` = recorded live in rooms.md (`overlay.take`, `record.start`, `stream.start`).
- `adapter` = dispatch has a handler (`show-input.take` included).
- Picker: `(not live-proven)` vs `(no verified adapter)`. A take still runs the adapter.
- `VOLT_SHOW_INPUT_ACTIONS` is read by serve and the clock.
- Done when: `/api/cue-commands` and the picker match that split; tests cover both warnings.

This branch (`room-track-cue-verified`) is that work.

## 2. Default pre-take uses the new core actions

After #953 is on a core the operator actually runs:

- Default `show_input_actions` become `input.assign [{slot}, {source}]`, `scene.route.set [{scene}, {route}, {slot}]` (or `scene.cueWithRoutes` with a JSON object), then the existing `transport.take`.
- Payload must name `scene` and `slot` (and a route id, or a documented default such as `ohg-host`).
- FakeRoom keeps covering evidence; live invoke names are config, not a second handler.
- Done when: a unit test fires those action names, and operations.md shows the JSON.

## 3. Live Program take

Same pattern as the 2026-10-16 stream run in rooms.md.

- Against `127.0.0.1:8011`: assign a source, cue a scene with routes, take, then `GET /snapshot` `programFrame.videoSources[]` holds the source, `sceneIdAttribution == "live"`, `deliverySequence` advanced.
- Record date, core build, action list, and any 422s.
- Then add `show-input.take` to `LIVE_PROVEN`. The picker drops `(not live-proven)`.
- Done when: rooms.md has the run and the picker shows Input to program without the unverified suffix.

## 4. Leave unsupported

`record.arm` and `audio.snapshot` stay without a handler. No stub.

## After this bundle

Per-user tokens. TLS reverse-proxy recipe. MOS. Hardware playout. Archive. Second NLE. SPDX license. NLE import check (owner).
