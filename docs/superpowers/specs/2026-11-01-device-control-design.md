# Volt: control of more devices, design

Status: draft for the owner's review. Nothing here is built. Date 2026-11-01.

## Why

Volt plays a rundown and, on each take, sends the item's cues to a room: CoreVideo Pro (`volt/rooms.py`, handler table `dispatch.HANDLERS`), a HyperDeck (`volt/hyperdeck.py`), and it pushes the running order to a MOS device (`volt/mos_send.py`). The room registry (`volt/roomreg.py`, `RoomRegistry`, `from_env`, `configured`) and the per-room handler tables (`dispatch.ROOM_HANDLERS`) mean a new room kind is a registration and a table. What a news control room runs on top of that is more devices than that: a vision switcher, a graphics engine, PTZ cameras, an audio console, a video server. And it expects an automation system to do four things with them that Volt does not do yet:

1. **Drive them.** A cue can cut a switcher, play a graphic, recall a camera preset or an audio scene, not just roll a deck.
2. **Group them.** One named step ("Open the show") that does several device actions, and per-cue timing (a graphic 2 seconds after the cut; two things at the same instant).
3. **Preview before air.** Preload the next item's devices (switcher preview, graphic loaded in the background) so the take is one small move, and the desk can see it ready.
4. **Show their health and stop them.** A panel saying which devices are up, and a switch that makes Volt send nothing.

**Honest caveat.** What Ross (Inception, OverDrive), Vizrt (Mosart) and Grass Valley (automation) offer is stated here from general knowledge of those products, not from an audit of them: no manual, trial or demo was read for this document. Treat the competitor list as the shape of the gap, not as a checked feature matrix. The same applies to every wire protocol named below: the protocol facts in this document are from memory, are flagged as such where they appear, and the builder must check each against the published document (or, where there is none, against a named open implementation) before writing a byte of client code.

The first customer is a daily news / live-show team (owner decision).

## Owner decisions already made

Recorded here as the owner's, so no later section reopens them:

1. The first customer is a daily news or live-show team.
2. A cue is `fired` only when the device's own state shows it, never on an acknowledgement.
3. A failed non-CoreVideo cue never rewinds a take. It is advisory: recorded, shown red, the item stays on air, later cues still run ([hyperdeck.md](../../specs/hyperdeck.md), `dispatch.Result.advisory`).
4. Every send path is opt-in and visible: off unless configured, and the desk can see what is configured and what was sent.
5. Tests use fake devices on loopback sockets and never real equipment.
6. Anything that needs real hardware to prove is written down as unvalidated.
7. The operator UI is static files under the same-origin CSP, following [brand-guide.md](../../brand-guide.md) and [operator-ui.md](../../specs/operator-ui.md).
8. Every new POST is classified in `serve.ROUTE_ROLES` and audited (the generic path in `serve.py` calls `store.append_audit` for every successful POST; `_audit_fields` names it).

## Goals

1. One adapter contract that new device kinds implement, with the fired-means-observed rule, timeouts, reconnect and idempotence enforced by shared code, not re-derived per adapter.
2. An ordered set of adapters, each with its own spec page saying what its tests prove and do not prove.
3. Macros and per-cue timing (offset, pre-roll, simultaneous groups).
4. Preview (arm) before take, for the devices that have a preview concept.
5. A device status panel on the Show screen.
6. Safety rules: interlocks, two-key on-air actions, a global safe mode that stops all device sends.

## Non-goals

- Anything that needs real equipment to prove: every adapter ships unvalidated until a run against the real device is recorded (the way [rooms.md](../../rooms.md) records CoreVideo runs).
- **GPI and tally output.** A relay closure has no read-back, so it can never be `fired` by the owner's rule. Tally *input* (a switcher reporting which inputs are on air) is used as an observation inside the switcher adapter; that is all.
- Ross, Grass Valley and Viz/Chyron native adapters. The contract is meant to make them possible; their protocols are often licence-restricted or need an SDK, and none is the first customer's known gear (see Open question 1).
- Volt as a MOS *device* beyond the status listener in task 9 (the MOS receiver stays its own later spec, as in [the remaining-build design](2026-10-31-remaining-build-design.md)).
- Rewriting the HyperDeck adapter or the MOS running-order sender onto the new contract. They keep working as they are; they only gain the safe-mode gate and the event log.
- Moving CoreVideo cues onto the new contract, previewing CoreVideo cues, or giving CoreVideo cues timing. CoreVideo failures rewind the take, which timing would make unsound (see Calls).
- Free-form scripting inside a macro (conditions, loops, variables). A macro is an ordered list with offsets.
- Closed-loop automatic correction ("the switcher is wrong, fix it"). Volt reports; the desk decides.
- Any device control from outside the rundown except the narrow retry route below.

## Anti-goals

Failure even if it works:

- A send that can reach a device by a path that skips the safe-mode check, the event log or the opt-in configuration.
- A cue marked `fired` on an acknowledgement, a `200 ok`-style reply, a completion message or a timer.
- A non-idempotent command sent twice (a switcher cut is a swap: sent twice it cuts back).
- A preview step that can put anything on air.
- A moved camera that was on air; an audio scene recalled over open microphones, with no second key, by a path other than the rundown.
- A device that hangs a take, the clock or the runner: every socket call has a timeout and a bound.
- A conformance claim no test or real counterpart supports.
- Colour as the only signal; red used for anything but a live path.

## Constraints

- Python standard library only (sockets, `struct`, `threading`, `xml.etree`); SQLite and Postgres both (`VOLT_DB`); the next migration number is whatever is free when the work starts, tested forward on both from every historical shape (`tests/historical_shapes.py`, `tests/test_migrations.py`). No claim is made here about which number.
- A device client never blocks the HTTP server: it runs in the take (bounded by the existing `POLL` attempts), the clock, or the runner, with a connect timeout (3 s, as `hyperdeck.CONNECT_TIMEOUT`) and a read timeout (3 s).
- Each cue opens its own short connection and closes it (the HyperDeck pattern). A separate poller in the runner reads status only; it never sends.
- The sqlite connection (`store.conn`) is not shared across threads: device I/O may run on worker threads, every database write happens on the calling thread.
- `ROUTE_ROLES` classifies every new route; `tests/test_users.py` fails otherwise (it compares the route table with `ROUTE_ROLES`).
- No new colour state: the UI reuses `ready`, `pending`, `failed` and `held` from the `STATE` map. Any new icon is added to the sprite and `Icons.SPRITE_IDS`, and the contrast test covers it in both themes.

## The design

### The adapter contract

A new package `volt/adapters/`. `base.py` holds the contract and the one executor; each device kind is a module. Adapters declare; the executor does. Nothing in an adapter module opens a socket outside `Session` or retries on its own.

```python
@dataclass(frozen=True)
class Observation:                 # one reading of the device's own state
    at: float                      # monotonic seconds
    state: dict                    # normalised, adapter-defined (e.g. {"program": 3, "preview": 5})
    detail: str = ""

class DeviceError(LiveError): ...  # refusal, timeout, unreachable, bad greeting
class SafeModeError(DeviceError): ...
class InterlockError(DeviceError): ...
class OutcomeUnknown(DeviceError): ...   # bytes may have been sent; no answer

@dataclass(frozen=True)
class ActionSpec:
    command: str                   # "atem.program", "caspar.play"
    parse: Callable[[str], dict]   # payload -> params; ValueError says what is wrong (add_cue shows it)
    perform: Callable[["Session", dict], None]
    observed: Callable[[Observation, Observation, dict], Optional[str]]
                                   # (baseline, now, params) -> None when the state proves it, else why not yet
    idempotent: bool               # may the same bytes be sent twice with the same effect?
    on_air: bool                   # does it change what viewers see or hear?
    prepare: Optional[Callable[["Session", dict], None]] = None
    prepared: Optional[Callable[[Observation, dict], Optional[str]]] = None
    interlocks: tuple = ()         # callables (adapter registry, params) -> Optional[str] (a reason to refuse)

class Session(Protocol):
    def observe(self) -> Observation: ...   # read the device's state now; never changes it
    def close(self) -> None: ...

class Adapter(Protocol):
    kind: str                      # the cue's `room` ("atem", "casparcg", ...)
    ACTIONS: dict                  # command -> ActionSpec
    def device_names(self) -> list: ...
    def open(self, name: str) -> Session: ...   # bounded connect; raises DeviceError
```

The executor (`volt/adapters/executor.py`) is the only code that calls `perform` and `prepare`:

`execute(adapter, device, spec, params, ctx, store) -> Outcome` does, in order:

1. **Gate.** `safety.check_send(store, kind, spec)` raises `SafeModeError` when safe mode is on. Then each `interlocks` callable; a reason becomes `InterlockError`. Nothing has touched the device yet, and an `event` row records the refusal.
2. **Connect** (bounded; `DeviceError` if unreachable).
3. **Baseline.** `observe()` before sending. Without a baseline a transition cannot be told from a state that was already true.
4. **Perform** once.
5. **Observe** by polling with the same attempts, interval and sleep as every other handler (`ctx["attempts"]`, `ctx["interval"]`, `ctx["sleep"]`); done when `observed(baseline, now, params)` is `None`. Cues need the proof on two consecutive reads for `on_air` actions (the rule `show_input` uses against a roll-back), one for others.
6. **Result.** Proven means `fired`. Not proven in time means `failed` with the adapter's last reason (`"timed out, <reason>"`, like `show_input`).

**Reconnect and idempotence** (one rule, in one place). If the connection drops *before* `perform` wrote anything, reconnect and try again, at most twice. If it drops or times out *after* `perform` started, the outcome is unknown: for an idempotent spec the executor reconnects, observes, and if the state already proves the action returns `fired`, otherwise sends once more; for a non-idempotent spec it reconnects and **only observes**, never sends again, and if the state does not prove it the cue is `failed` with `OutcomeUnknown` worded "sent; no answer; the device does not show it; check it before taking again". This is the same stance `mos_send` takes with an unacknowledged `roCreate`. Observation, not the reply, decides.

`handler_for(adapter, spec)` returns a function with the shape dispatch already uses, `handler(store, client, cue, ctx)`, raising `DeviceError` (a `LiveError`) on any failure. `ROOM_HANDLERS` gains one entry per adapter kind built from `ACTIONS`; `take_item`, the clock, and `dispatch_item`'s advisory logic (`cue.room != COREVIDEO`) are untouched, so a failed device cue is advisory for free.

`roomreg.from_env` registers an adapter's room when its environment variable is set (below) and `roomreg.configured` counts it, so `volt run` starts the clock for a device-only setup. The registry's client for a device kind is the adapter object itself.

### Adapters, in build order

Each adapter has a spec page under `docs/specs/`, written when it is built, with a "what the tests prove" section. Configuration follows `VOLT_HYPERDECK_HOSTS`: `name=host[:port]`, nothing set means off.

**Why this order.** The switcher is what a news cut is made of and every other adapter's interlocks read it (ATEM). The graphics engine is the next most frequent cue in a news rundown, and CasparCG has a published text protocol with a published state query, so it exercises the contract on a second, very different wire (CasparCG). MOS object push is the standard way a newsroom pushes stories and graphic items to a graphics or playout device and reuses the sender's framing, but it needs a status listener to be observable, so it comes after the two adapters whose state is directly readable (MOS). PTZ presets need the switcher's program state for the "never move an on-air camera" interlock, so they follow the switcher (PTZ). Audio scenes are high value but console-specific and the console is not yet known (Open question 1) (audio). VDCP is last: it is a legacy serial protocol whose document may not be obtainable and which a news team on files rarely needs (VDCP).

| # | Adapter (room) | Wire | Fired means observed |
|---|---|---|---|
| 1 | `atem` | Blackmagic ATEM, UDP port 9910 (from memory) | state packets show the program or preview input |
| 2 | `casparcg` | AMCP over TCP 5250 (from memory) | `INFO` for the layer shows the producer, or the template host |
| 3 | `mos-objects` | MOS 2.8.5 over the device's upper port, plus a status listener | the device's own status message names the object ready or on air |
| 4 | `ptz` | VISCA over IP, UDP 52381 (from memory) | position inquiry matches the learned preset on two reads |
| 5 | `audio` | one console family's network protocol, OSC for the first candidate | scene query reports the recalled scene |
| 6 | `vdcp` | VDCP | status sense reports the cued or playing state |

**1. ATEM.** Commands: `atem.preview` (set preview input), `atem.program` (set program input directly), `atem.cut`, `atem.auto` (preview to program with the configured transition). Payload `{"switcher": "A", "input": 3, "me": 0}` or `A/3`. Fired: after `atem.program` the program input of the ME equals the target and no transition is in flight; after `atem.cut` or `atem.auto` the program input equals the previous preview target and the old program is now preview (or the transition-in-progress flag has cleared); `atem.preview` shows preview equal to the target. **Idempotence:** a cut is a swap (sent twice it undoes itself), so `atem.cut` and `atem.auto` are non-idempotent; `atem.program` and `atem.preview` set an absolute input and are idempotent. The default cue for "put camera 3 on air" is `atem.program`, not `atem.cut`; `atem.cut` is used after a preview preload (see Preview). *From memory, to verify:* the protocol is UDP on port 9910 with a session handshake, numbered and acknowledged packets with resend, and four-letter command and state names such as `PrgI`, `PrvI`, `CPgI`, `CPvI`, `DCut`, `DAut`, `TrSS`, a state dump on connect and tally packets. **Blackmagic is not believed to publish the wire protocol**; it publishes the ATEM Switchers SDK (interfaces, not the wire). The builder checks: whether any Blackmagic document describes the wire; SKAARHOJ's published ATEM protocol notes; and the open-source `atem-connection` library's command and state definitions (read its licence before copying anything: read, do not copy). If the wire cannot be confirmed against a source other than memory, the ATEM adapter does not ship and the order moves to CasparCG first. Also verify the controller-session limit per switcher and behaviour across firmware versions (`_ver`).

**2. CasparCG.** Commands: `caspar.load` (`LOADBG`, background, preview-like), `caspar.play` (`PLAY`, takes the background to the foreground, or plays a clip), `caspar.stop`, `caspar.clear`, `caspar.cg.add`, `caspar.cg.play`, `caspar.cg.update`, `caspar.cg.stop`. Payload `{"server": "G", "channel": 1, "layer": 10, "clip": "NEWS/OPEN"}` and for templates `"template"` and `"data"` (a JSON object Volt renders to the template-data XML the server expects). Fired: `INFO <channel>-<layer>` shows the foreground producer with the expected file or template; after a load, the *background* producer. Idempotent: `LOADBG`, `STOP`, `CLEAR`, `CG ADD`+`CG PLAY` of the same template instance are treated as idempotent only where the executor re-observes first; a bare `PLAY` of a clip restarts it, so it is non-idempotent. *From memory, to verify:* one command per line, CRLF, reply first line `<code> <COMMAND> OK` (2xx success, 4xx client error, 5xx server error), data blocks for `INFO` ending in a blank line, the `INFO` XML layout, template-data XML. Verify against the CasparCG wiki AMCP protocol page *for the server version the customer runs* (2.3 LTS and 2.4/2.5 differ in `INFO` output) and decide whether `INFO` polling is enough or the server's OSC output (UDP, per-layer file and playback state) is the better observation.

**3. MOS object / template push (`mos-objects`).** Volt as the NCS pushes an object or template item to a MOS device (a graphics or playout engine). Commands `mos.item.preload` and `mos.item.take` (names to verify) carry `{"device": "gfx", "object": "<objID>", "story": ...}`. **A `roAck` is an acknowledgement, not observation.** The device's own status arrives as device-to-NCS messages (MOS item/element status; names and fields to read in the 2.8.5 text) on the lower port (10540), which the NCS must listen on. So this adapter includes an opt-in status listener (`VOLT_MOS_LISTEN=host:port`, default off, loopback if only a port is given) and a cue is `fired` only when a status message from the device names the item in the wanted state. A device configured without the listener cannot receive these cues: `add_cue` refuses with `no_observation`. The builder reads the sections for item and element messages and device status, which earlier work did not need (the export read only up to section 3.6, [mos.md](../../specs/mos.md)); the existing `mos_send.frame`, `Reader` and `parse` are reused for framing.

**4. PTZ presets.** Commands `ptz.recall` (preset number), `ptz.learn` (store the camera's current position as the reference for that preset: an operator-time action, never in a take). Payload `{"camera": "cam1", "preset": 4}`. Fired: a pan/tilt/zoom position inquiry reads within a tolerance of the learned reference on two consecutive reads (the camera has stopped moving there). A completion message from the camera is an acknowledgement and is not used as proof. Idempotent. *From memory, to verify:* VISCA over IP uses UDP 52381 with an 8-byte header (payload type, length, sequence) around the 0x81... VISCA frame, some cameras also accept TCP; recall is the `01 04 3F 02 pp` command and the pan/tilt and zoom inquiries are `09 06 12` and `09 04 47`. Verify against the camera maker's VISCA command list for the customer's model and the VISCA-over-IP document; models differ in position resolution and in whether they report a stored-preset index at all. **Interlock:** `VOLT_TALLY_MAP=cam1=A:3` ties a camera to a switcher input; `ptz.recall` is refused (`InterlockError`, "camera cam1 is on program") when that switcher shows the input on program.

**5. Audio scenes.** Commands `audio.scene.recall` (scene number or name). Fired: the console's own "current scene" query reports the scene. `on_air=True`. Which console is Open question 1; the first candidate is a console family with openly documented OSC (the Behringer/Midas X32 family: UDP 10023, scene recall and current-scene nodes, from memory; the available documentation is an unofficial community document, so the builder says so). Not built until the customer names a console.

**6. VDCP.** Commands `vdcp.cue`, `vdcp.play`, `vdcp.stop`, port or server named in configuration. Fired: the status-sense reply reports the wanted state and the clip id. The document was not read for this design; if the builder cannot obtain the published VDCP specification, this adapter is dropped, not guessed.

### Data model (a new migration)

All additions are in one forward migration (the number is whatever follows the head when the work starts), with both SQLite and Postgres statements as `migrations.Migration` provides, and `schema.sql` updated to match.

| Table or column | Purpose |
| --- | --- |
| `cue.offset_ms INTEGER NOT NULL DEFAULT 0` | start offset from the take, 0 to 600000 |
| `cue.preroll_ms INTEGER NOT NULL DEFAULT 0` | how long before its start the cue's prepare step runs, 0 to 10000 |
| `cue.group_key TEXT NULL` | cues sharing a key and an offset start together |
| `cue.due_at TIMESTAMP NULL`, `cue.claimed_at TIMESTAMP NULL` | set at take for a timed cue; claimed by one conditional UPDATE |
| `cue.preload_status TEXT NULL`, `cue.preload_detail TEXT NULL`, `cue.preloaded_at TIMESTAMP NULL` | preview state: `preloaded`, `failed`, or null |
| `device_status` (`device TEXT PK` as `kind:name`, `kind`, `state`, `detail`, `observed TEXT` JSON, `updated_at`) | what the poller last saw; like `mos_device` |
| `device_event` (`id`, `at`, `device`, `command`, `params TEXT`, `outcome` (`fired`/`failed`/`refused`/`prepared`), `detail`, `cue_id NULL`, `actor`) | one row per send attempt or refusal; the audit of what went out |
| `device_safety` (`id=1`, `safe_mode`, `reason`, `set_by`, `set_at`) | the global safe mode, one row |
| `macro` (`id`, `name` unique, `version`, `created_at`) and `macro_step` (`macro_id`, `version`, `position`, `room`, `command`, `payload`, `offset_ms`, `group_key`) | named macros; each edit makes a new version, old versions stay |
| `device_confirm` (`id`, `cue_id`, `requested_by`, `requested_at`, `expires_at`, `state`) | the second key for a retry of an on-air device cue |

`cue.status` values are unchanged (`stored`, `armed`, `fired`); `fail_status` gains `refused`, `interlocked` and `late` beside `failed` and `unsupported`. A forward test applies the migration to every historical shape on SQLite and on Postgres (`VOLT_TEST_PG_DSN`), and a test asserts existing cues read back with offset 0 and no group.

### Macros and cue timing

**A macro** is a named list of device actions (`room`, `command`, `payload`, `offset_ms`, `group_key`), up to 20 steps and 60 s long, device rooms only (no CoreVideo steps, no nested macros). A cue with room `macro`, command `macro.run` and payload `{"macro": "<id>", "version": 3}` runs it; the cue stores the version, so editing a macro never changes a cue already on a rundown. The macro cue is `fired` only when **every** step is `fired` by its own adapter's observation; the cue's failure detail lists each failed step ("step 2 atem.program: timed out, program is 5"). A macro cue is a device cue, so it is advisory.

**Timing on a cue** (`offsetMs`, `prerollMs`, `group`), all for device rooms only:

- **Take moment T0** is when dispatch for the item begins.
- **Immediate cues** (`offset_ms = 0`) run in position order as today. Consecutive immediate cues sharing a `group_key` run concurrently (device I/O on worker threads, results written on the calling thread); two cues for the same device in one group still run in position order. The group's cues all start before any is observed. A failure in one never stops the others (advisory).
- **Timed cues** (`offset_ms > 0`) are not run inside the take request. At take, each gets `due_at = T0 + offset` and stays `armed`; the clock's new `device_tick` claims and runs them (one conditional `UPDATE ... SET claimed_at ... WHERE claimed_at IS NULL`, the pattern the schedule clock uses, so two clocks cannot both send). Due and still inside a grace window (default 2 s, `VOLT_DEVICE_GRACE`) it runs; later than that it is `failed` / `late`, nothing sent, shown red. If the item is no longer on air when the cue is due, it is `failed` / `late` with "item left air before the cue was due; nothing sent".
- **Pre-roll** `P`: the cue's `prepare` step runs at `T0 + offset - P` if it was not already preloaded, and the take action at `T0 + offset`. A cue whose adapter has no `prepare` ignores pre-roll and `add_cue` says so. If `P > offset` the prepare runs at T0 and the event log notes it.
- A restart between claim and send leaves the cue claimed and unsent; after the claim lease (300 s, as the schedule clock) it is settled from the device's state: observed means `fired`, anything else `failed` with "check the device", never resent (a send may have reached it).

### Preview (arm) before take

`arm` already means "make the rundown item ready for take" in `cues.arm`; this feature is called **preview** in the API and UI to avoid the clash.

`POST /api/items/{id}/preview` runs `prepare` for every cue of the **next ready item** whose spec has one (`atem.preview`-like: switcher preview; `caspar.load`: background; `mos.item.preload`; `ptz.recall` on an off-air camera) and records `preload_status`. One item per show is in preview at a time; previewing another clears the first (`.../preview/clear`, which sends nothing). Rules:

- A prepare step may never put anything on air. Each adapter declares its prepare commands, and a test per adapter runs a fake device that records every command and asserts none is in the adapter's on-air set.
- At take, a preloaded cue is **re-verified** (`prepared(obs, params)` against a fresh observation). If the device still shows the preload, only the small take action is sent (`atem.cut`, `caspar.play`); if someone changed it, the cue falls back to its full action (`atem.program`, `LOADBG`+`PLAY`) and the event log says "preload lost". A preview is a hint that saves time, never trusted state.
- Previewing is refused in safe mode (it sends), on an item that is not the next ready item, and for an item with no previewable cue (`not_previewable`).
- A preview surviving a restart is re-checked at take, so a stale `preloaded` flag costs nothing.

### Safety

- **Safe mode.** `device_safety.safe_mode`. When set, `executor.execute`, `prepare`, `mos_send.MosSender.step` and the HyperDeck handlers refuse to send to any non-CoreVideo device: a cue fails `refused` ("safe mode is on; nothing sent"), advisory, the take proceeds. Observation and the status poller keep working, so the panel stays truthful. Safe mode is read from the database at every send (no cache), so the server, the clock and the runner all obey one switch within one call. Hold ([playout.md](../../specs/playout.md)) stops the rundown; safe mode stops the devices; they are independent. Safe mode does not stop CoreVideo cues (see Calls).
- **Interlocks.** Declared on an `ActionSpec`, evaluated by the executor before any byte is sent, refusal recorded as `interlocked`. First interlock: a PTZ move on an on-air camera (above). Others are added only with a documented reason; the builder does not invent them.
- **Two-key on-air.** An `on_air` device action started outside a take (the only such route is `POST /api/cues/{id}/retry`) returns 202 `needs_second_key` with a `device_confirm` id. A **different** user must `POST /api/device-confirms/{id}/confirm` within 60 s; the same user gets 403 `same_user`. Under the legacy single token (`LEGACY_ACTOR`) there is only one identity and two keys are impossible, so the route answers 409 `two_key_unavailable`. A cue run by the take is not two-keyed: the rundown author added the cue, the audit log names them, and the person who takes is the second party.
- **Opt-in and visible.** No adapter registers without its environment variable; the panel lists every configured device; every attempt and refusal has a `device_event` row.

### API routes

All JSON, camelCase, errors `{ok:false, code, reason}` as `ApiError`/`BuildError` today. Each POST goes in `ROUTE_ROLES` and is audited by the generic path.

| Route | Role | Body | Errors |
| --- | --- | --- | --- |
| `GET /api/devices` | token | none | 422 `bad_input` on parameters |
| `POST /api/devices/safe-mode` | operator | `{on: bool, reason?}` | 422 `bad_input` |
| `POST /api/items/{id}/cues` (existing `act_cue_add`) | operator | adds optional `offsetMs`, `prerollMs`, `group`; `room` may be a device kind or `macro` | 422 `bad_command`, `bad_input`, `bad_timing`, `no_observation`; 404; 409 item not editable |
| `POST /api/cues/{id}/timing` | operator | `{offsetMs, prerollMs, group}` on an unfired cue | 404; 409 `cue_fired`; 422 `bad_timing` |
| `POST /api/items/{id}/preview` | operator | `{}` | 404; 409 `not_next_item`, `not_previewable`, `safe_mode`, `held`; device failures are reported per cue in the body, not as an HTTP error |
| `POST /api/items/{id}/preview/clear` | operator | `{}` | 404 |
| `POST /api/cues/{id}/retry` | operator | `{}` for a failed device cue on the on-air item | 404; 409 `not_retryable`, `safe_mode`; 202 `needs_second_key`; 409 `two_key_unavailable` |
| `POST /api/device-confirms/{id}/confirm` and `/cancel` | operator | `{}` | 404; 403 `same_user`; 410 `confirm_expired` |
| `GET /api/macros`, `POST /api/macros` | token / operator | `{name, steps:[...]}` | 422 `bad_input`, `bad_timing`; 409 `name_taken` |
| `POST /api/macros/{id}` | operator | `{steps:[...]}`, makes a new version | 404; 422 |
| `POST /api/macros/{id}/delete` | admin | `{}` | 404; 409 `in_use` (a cue uses it) |

`GET /api/state` gains `devices` (the same list as `/api/devices`), a `safeMode` object, and per cue `timing: {offsetMs, prerollMs, group}` and `preload: {status, detail} | null`. `GET /api/cue-commands` lists device commands with `adapter`, `verified` (always false until a real run is recorded in the adapter's spec page) and `previewable`.

### UI

Static files in `control/volt/session/operator/`, same CSP (no `http(s)://`, inline style or script), icon plus word plus colour, existing keys unchanged, **no new single-key shortcut** for any device action.

- **Device panel**, beside `roomPanel` and `mosPanel` in the Show screen's room paint: one row per configured device with kind and name, a chip (Connected `ready`, Connecting `pending`, Down and Refused `failed`, Safe `held`), the one-line observed state ("Program 3, Preview 5"), and the last event ("cut, fired, 14:02:11"). Hidden entirely when no device is configured.
- **Safe mode bar** across the top of the Show screen when on: a `held` chip reading "Safe mode: devices will not be sent anything", with the reason and who set it. Entering and leaving are two-press buttons like Kill. Not red: red is for live paths.
- **Cue rows** show timing chips ("+2.0 s", "pre-roll 1.0 s", "group A") and a preview chip (Preloaded `ready`, Preload failed `failed`). The add-cue form gains Offset, Pre-roll and Group fields (seconds in the UI, milliseconds on the wire), a Macro entry in the room picker, and the Preview button on the next item. A device cue labelled "(no verified adapter)" or "(not live-proven)" keeps today's wording rules.
- **Macros** are edited in a disclosure in the build panel (name, ordered steps with the same cue fields).
- A failed advisory device cue shows Retry; an `on_air` one shows "Retry (needs a second person)", then the confirm control for the second user.
- axe-core and the contrast test run on the new states in both themes; harness scenarios `devices_up`, `devices_down`, `safe_mode`, `preview_ready` render them.

### Behaviour rules (the ones tests pin)

1. A device cue is `fired` only when `observed()` returns `None` on fresh state read after the send; no reply content ever counts.
2. A failed or refused device cue is advisory; it never rewinds a take.
3. A non-idempotent action is never sent twice by the executor.
4. Safe mode blocks every device send path, including `mos_send` and the HyperDeck handlers, and nothing else.
5. Preview never sends an on-air command.
6. Two timed-cue claimers send at most once.
7. Nothing configured means nothing sent, nothing shown.

## What the tests will and will not prove

Will prove, against fake devices on loopback sockets (TCP and UDP) and a frozen clock: parsing and framing of each protocol as the builder read it; the executor's rules (gate, baseline, observe-on-two-reads, reconnect, idempotent resend versus observe-only); that a device which acknowledges and never changes state is not `fired`; the advisory rule; macros and timing, including late and restart; preview never sending an on-air command and re-verifying; safe mode across every send path; the interlock and two-key refusals; the migration forward on both databases; routes, roles and audit; the UI states in the harness.

Will **not** prove: that any real ATEM, CasparCG server, MOS device, camera, console or video server behaves as the fake does; that the wire details recalled from memory are correct (the fakes encode the builder's reading of the published documents, so a misreading is in both sides); real timing and latency; firmware and server-version differences; real hardware failure modes (a camera that reports a position and never arrives, a switcher that drops the session under load). Each adapter spec page lists these and carries an **Unvalidated** line until a recorded run on real equipment.

## Left to the builder

Exact column types and indexes; the wire details of each protocol (read the published document, not this one); the fake servers' internals; thread-pool sizing; how `prepared` and `observed` normalise each device's state; the layout of each control within the redesign's rules; the exact error wording beyond the codes above; which of the `device_event` fields are shown in the panel.

## Calls I made, for the owner to check

- **Order:** ATEM, CasparCG, MOS object push, PTZ, audio, VDCP, as justified above. If the customer's gear differs, the contract stays and the order changes.
- **Plan scope:** the plan builds the contract and the first four adapters; audio scenes and VDCP wait for a named console and an obtainable document, each as its own later plan.
- **CoreVideo cues stay out** of timing, macros and preview, because a CoreVideo failure rewinds the take and a timed or grouped cue cannot be un-run.
- **Timed cues run in the clock, not in the take request,** so a 5-second offset does not hold an HTTP request open. They are advisory.
- **Safe mode does not stop CoreVideo cues.** Hold is the tool for stopping the rundown. Safe mode is the tool for "the switcher is misbehaving, send it nothing".
- **Any operator may turn safe mode on or off** (operator role, two-press, audited). Making release admin-only would be safer and slower in a live crisis.
- **A take is the second key for rundown cues;** two-key applies only to the retry route. A solo operator therefore can take but cannot retry an on-air device cue.
- **A cut is a swap**, so the default "camera on air" cue is `atem.program`, which is idempotent.
- **A macro cue stores a macro version**, so editing a macro never changes a rundown already built.
- **Per-cue short connections,** as the HyperDeck adapter does, rather than long-lived sessions; the poller is the only long reader.
- **No new colour states;** safe mode reuses `held`.

## Open questions

1. **What gear does the first customer's control room actually run?** Switcher model, graphics engine, PTZ camera model, audio console, video server. The adapter order, and whether audio and VDCP are built at all, depend on the answer.
2. **Can a second person confirm an on-air retry?** If the usual control room is one person at the desk, two-key on retry makes retry unusable and the rule needs another form (for example a deliberate wait or a typed confirmation). Which does the owner want?
