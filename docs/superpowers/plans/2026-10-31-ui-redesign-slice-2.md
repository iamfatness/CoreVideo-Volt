# Volt UI redesign, slice 2: the Show screen

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the Show screen on the slice-1 system: a rundown with front time and drift, the state language on every row and cue, a large blue TAKE spaced from HOLD and SKIP, and Producer and Operator layouts that differ.

**Architecture:** One small server addition (each item's media length and rate in `/api/state`) so the page can total durations. Timing is pure functions in `operator.js` above the node early-return (testable with `_node`). Markup stays in the one DOM; workspaces change it with `ws-*` body classes in `operator.css`.

**Tech Stack:** As slice 1: plain HTML/CSS/JS, `unittest` in `control/tests/test_operator_ui.py` (`_node` helper, `FILES`, `Icons.SPRITE_IDS`), harness `python -m tests.operator_harness normal --port 8791`, axe-core 4.10.2 (fulfil `/operator/axe.min.js` from a local file with `page.route`, load it with `addScriptTag({url})`).

**Spec:** `docs/superpowers/specs/2026-10-30-ui-redesign-design.md` (sections Rundown, Operator controls, State language, Workspaces). Slice 1 shipped in PR #68.

## Global Constraints

- Everything in slice 1's Global Constraints still holds: brand colours only (amber = next/selected/current, red = live path only, suite blue = actions incl. TAKE, green = cleared/ready); no `http(s)://`, `style=`, `<style>`, inline script in the three files; every element `id`, `data-fk`, `data-screen` and `aria-*` kept; keys unchanged (`T` takes, `H` holds, Space never takes except on Show with nothing focused; two-press arming for Kill and Remove); status is icon plus word plus colour.
- Front time and drift definitions (spec): **front time** is when an item starts: the first item's actual start plus the running sum of the planned durations of the items before it that are not killed and not skipped (ruling: a skipped item takes no time). An item with no known duration counts as zero and its own duration shows `--:--`. **Drift** = the on-air item's actual start minus its planned front time, signed `+MM:SS` (behind) / `-MM:SS` (ahead); blank when nothing is on air. Back time and slot over/under are out of scope (no show target exists; owner answered "not now" on 2026-10-31).
- No new routes or tables. The only server change is two additive fields on the existing `media` entries.
- Owner answer 2026-10-31: no CoreVideo widget sheet is available; build from the shipped tokens and icons.

## Review Focus

- An item whose clip has no recorded frame rate or length: its duration and front time show `--:--`, it adds zero, the screen does not break.
- Killed and skipped items: excluded from the running sum, shown struck through, front column `--:--`.
- Nothing has aired yet: no anchor, so front shows running offsets (`T+00:00`, `T+02:10`), drift blank. After the first item airs, front shows clock times.
- The on-air item started later than planned: drift `+00:45` in red with the word "behind"; earlier: `-00:10` neutral with "ahead"; exactly on plan: `00:00`.
- Reorder (`move`), Kill or Skip changes the planned order: front times recompute from the new list on the next paint; no stale value stays on screen.
- A show with more than 100 items or a total over an hour: `H:MM:SS` formatting, no wrapping at 1366 wide.
- A held rundown and a connection loss: TAKE stays disabled with its reason shown beside it; the colour of TAKE does not change.

---

### Task 1: Item length and rate in the state payload

**Files:**
- Modify: `control/volt/operator_state.py` (`item_media`, ~line 135), `docs/specs/operator-ui.md` (Data section)
- Test: `control/tests/test_operator_ui.py` (new class `ItemTiming(Api)`)

**Interfaces:**
- Produces: each `media[itemId]` that has a span now also carries `frames: int | null` (span length at the asset's rate) and `rate: str | null` (the hi-res essence `frame_rate` as stored, e.g. `"25"` or `"30000/1001"`). Items with no span are unchanged (`{span: None, blockedBy: "no media"}`). Reuse the length logic of `volt/mos.py::_media` (prefer `frame_in`/`frame_out`, else `facts.frames(tc_out) - facts.frames(tc_in)`, `None` when not positive or `ShotError`); do not import `mos`: move the length rule into a small shared function in `volt/shots.py` or `volt/operator_state.py` and have `mos._media` call it only if that is a one-line change, otherwise duplicate nothing and call `shots.try_facts` from `operator_state`.

- [ ] **Step 1: Write the failing tests.** `ItemTiming.test_media_carries_frames_and_rate` builds a show from the harness `normal` seed (see `tests/operator_harness.seed`), calls `GET /api/state`, and asserts for an item with an attached span: `isinstance(frames, int) and frames > 0` and `rate` parses with `timecode.parse_rate`; `test_item_without_a_span_is_unchanged` asserts `{"span": None, "blockedBy": "no media"}` exactly; `test_unknown_rate_gives_nulls` attaches a span whose asset has no hi-res essence rate (insert one with `store`'s helpers used in `LibraryScenario.setUp`) and asserts `frames is None and rate is None` and that `/api/state` still answers 200.
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_operator_ui.ItemTiming -v`. Expected: FAIL (`KeyError: 'frames'`).
- [ ] **Step 3: Implement** the two fields in `item_media`, selecting `s.frame_in, s.frame_out, s.asset_id` and resolving facts once per asset.
- [ ] **Step 4: Run** the class, then `python -m unittest tests.test_operator_api tests.test_mos tests.test_operator_ui`. Expected: PASS.
- [ ] **Step 5: Document** the two fields in `docs/specs/operator-ui.md` (Data) and commit `Show: item length and rate in the state payload`.

### Task 2: Timing functions (pure)

**Files:**
- Modify: `control/volt/session/operator/operator.js` (above the node early-return, beside `deriveShow`; export them)
- Test: `control/tests/test_operator_ui.py` (new class `RundownTiming`, uses `_node`)

**Interfaces:**
- Produces: `itemSeconds(media: {frames?: number|null, rate?: string|null} | undefined) -> number | null` (frames divided by the exact rate from `parseRate`; null when either is missing or the rate is invalid); `frontTimes(items, seconds, nowMs) -> {rows: Array<{id, offset: number|null, startMs: number|null}>, totalSec: number, driftSec: number|null}` where `items` is `deriveShow(...).items` (each has `id`, `status`, `asRunIn` as ISO or null), `seconds` is an object `{[itemId]: number|null}`, `nowMs` is epoch ms. `offset` is the planned seconds from the first item's start (null for killed or skipped); `startMs` is `anchorMs + offset*1000` when an anchor exists (the first item with `asRunIn`, taken as the planned start of the first item that is not killed or skipped), else null. `totalSec` sums known durations of non-killed, non-skipped items. `driftSec` = (on-air or taking item's actual `asRunIn` ms − its `startMs`) / 1000 rounded to a whole second, or null with no anchor or nothing on air. `fmtOffset(sec) -> "MM:SS" | "H:MM:SS"` (null -> `"--:--"`), `fmtSigned(sec) -> "+MM:SS" | "-MM:SS" | "00:00"` (null -> `""`).

- [ ] **Step 1: Write the failing tests** (node, `_node`): `itemSeconds({frames:250, rate:"25"}) == 10`; `itemSeconds({frames:30000, rate:"30000/1001"}) == 1001`; `itemSeconds({frames:10})`, `itemSeconds(None)`, `itemSeconds({frames:10, rate:"abc"})` are null. For `frontTimes`, with three ready items of 60, null and 30 seconds and nothing aired: offsets `[0, 60, 60]`, `startMs` all null, `totalSec == 90`, `driftSec` null. With item 1 `aired` (asRunIn T0), item 2 `on air` (asRunIn T0+75s), durations 60 and 30: `rows[1].startMs == T0+60000`, `driftSec == 15`. A killed middle item has `offset` null and is not summed. Item 2 on air 10 s early gives `driftSec == -10`. `fmtOffset(3725) == "1:02:05"`, `fmtOffset(65) == "01:05"`, `fmtOffset(null) == "--:--"`; `fmtSigned(15) == "+00:15"`, `fmtSigned(-10) == "-00:10"`, `fmtSigned(0) == "00:00"`, `fmtSigned(null) == ""`.
- [ ] **Step 2: Run** `python -m unittest tests.test_operator_ui.RundownTiming -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the four functions and add them to `module.exports`.
- [ ] **Step 4: Run** the class. Expected: PASS.
- [ ] **Step 5: Commit** `Show: front time and drift as pure functions`.

### Task 3: One state vocabulary for rows

**Files:**
- Modify: `operator.js` (`STATE`, `stateChipSpec`; replace `ITEM_CHIP`/`CUE_CHIP` rendering of rundown and cue chips by `stateChip`), `operator.css` (`.st.done`, `.st.next`, remove chip rules no longer used)
- Test: `test_operator_ui.py` (update `StateLanguage.test_each_state_maps_to_an_existing_icon_and_a_class`; new `StateLanguage.test_row_states`)

**Interfaces:**
- Consumes: `stateChip`, `STATE`, `icon`.
- Produces: `STATE` gains `next` (`take` icon, "Next", `st next`), `aired` (`ready`, "Aired", `st done`), `skipped` (`skip`, "Skipped", `st done`), `killed` (`close`, "Killed", `st done`), `taking` (`pending`, "Taking", `st pend`). Rundown rows call `stateChip` with the item's status (`'on air'` maps to `onair`; `isNext` maps to `next`). Cue chips: fired -> `ready` word "Fired", failed -> `failed`, armed -> `pending` word "Armed", stored -> `pending` word "Stored", unsupported -> `locked` word "Unsupported", via a `CUE_STATE` map.

- [ ] **Step 1: Write the failing tests.** The updated test expects the sorted key set `aired failed killed locked next onair pending ready skipped taking`; `test_row_states` asserts `stateChipSpec("next")`, `("aired")`, `("killed")` return the icon, word and class above, and that every icon exists in the sprite.
- [ ] **Step 2: Run** the class. Expected: FAIL.
- [ ] **Step 3: Implement** the map entries, `CUE_STATE`, the `.st.next` (amber) and `.st.done` (muted) classes, and switch `rundownRow` and the cue list to `stateChip`. Keep the visible words identical to today's (`On air`, `Taking`, `Ready`, `Aired`, `Skipped`, `Killed`, `Next`, `Armed`, ...) so no text assertion in the suite changes.
- [ ] **Step 4: Run** the whole file. Expected: PASS.
- [ ] **Step 5: Commit** `Show: state language on rundown and cue rows`.

### Task 4: The rundown

**Files:**
- Modify: `operator.js` (`rundownRow`, `renderShow`'s `paint('rundown', ...)`; add a summary line), `operator.css`, `index.html` (add `<div id="rundown-sum">` above `#rundown`)
- Test: `test_operator_ui.py` (new class `RundownMarkup`, static)

**Interfaces:**
- Consumes: `itemSeconds`, `frontTimes`, `fmtOffset`, `fmtSigned`, `stateChip`.
- Produces: each row is `<li class="rrow ...">` with, in order: number, state chip, name (`.pick` button, unchanged hooks), `.dur` (`fmtOffset(seconds)`), `.front` (clock `HH:MM:SS` via `fmtTime` when the item has `startMs`, else `T+` + `fmtOffset(offset)`, else `--:--`), then the tools. Row tools: Up, Down, Skip together; Kill after a `.sep` gap of at least `var(--space-4)`. The current (on-air or taking) row has a 3px left bar in `--air`; Next has it in `--signal`. `#rundown-sum` reads `Total <fmtOffset(totalSec)> · <n> items` and, with drift not null, a `.drift` span: `Drift +00:15 behind` in `--air`, `Drift -00:10 ahead` or `Drift 00:00 on time` in `--text`, each with an icon (`alert` for behind, `ready` otherwise) and live region `role="status"`. A column header row (`.rhead`, `aria-hidden="true"`) labels `#`, State, Item, Length, Front.

- [ ] **Step 1: Write the failing tests** (static, `FILES`): `RundownMarkup.test_summary_and_columns_exist` (`id="rundown-sum"` in `index.html`; `operator.js` contains `className`-free strings `'dur'`, `'front'`, `'drift'` and calls `frontTimes(`); `test_kill_is_set_apart` (`operator.css` has a `.rrow .sep` rule whose margin or gap uses `--space-4`); `test_no_inline_style_added`.
- [ ] **Step 2: Run** the class. Expected: FAIL.
- [ ] **Step 3: Implement.** Compute `seconds` from `mediaOf(id)` with `itemSeconds`, call `frontTimes(items, seconds, Date.now())` once per paint and include its result in the paint signature (so the summary repaints with the 2 s poll). Grid columns for `.rrow`: `2.2em 8.5em minmax(0,1fr) 5.5em 6.5em auto`; below 900px drop `.front` and the header row.
- [ ] **Step 4: Run** the file, then open the harness `normal` scenario and read the rows. Expected: PASS, and the on-air row shows a start time with the drift line.
- [ ] **Step 5: Commit** `Show: rundown with length, front time and drift`.

### Task 5: Transport, workspaces and the side panels

**Files:**
- Modify: `operator.js` (`renderShow` transport and cue chips already done in Task 3; headings with icons), `operator.css` (`.transport`, `.take`, `.ws-operator`, `.ws-producer` rules), `index.html` (heading icons)
- Test: `test_operator_ui.py` (new class `ShowLayouts`, static)

**Interfaces:**
- Produces: the transport block shows a large `On air` and `Next` readout, then TAKE (suite blue, `take` icon, label `TAKE`, min height 56px, `Key T` hint kept), then HOLD and `Advance, no cues` grouped apart from TAKE by at least `var(--space-5)`. No new colour on TAKE in any state. `h2` headings of the four panels get leading icons (`rundown`, `cue`, `room`; build panel `plus`). Layout classes: `.ws-operator` hides `#build` and `#cue-add` (`display:none`, controls stay in the DOM), widens the transport (program readout font `var(--fs-xl)`), and makes the rundown column wider (`grid-template-columns: 2fr 1fr`); `.ws-producer` shows everything with the cues panel open and `grid-template-columns: 3fr 2fr`; `.ws-editor` on the Show screen behaves like Producer.

- [ ] **Step 1: Write the failing tests:** `ShowLayouts.test_take_is_blue_and_has_the_take_icon` (the `renderShow` source builds TAKE with `icon('take'`; `.take` rule contains `var(--blue)` and no `--ok`); `test_operator_layout_hides_the_build_forms_not_the_dom` (CSS has `.ws-operator #build` and `.ws-operator #cue-add` with `display:none`; `index.html` still has `id="build"` and `id="cue-add"`); `test_panel_headings_have_icons` (`index.html` Show headings contain `href="#i-cue"` and `href="#i-room"`).
- [ ] **Step 2: Run** the class. Expected: FAIL.
- [ ] **Step 3: Implement** markup and CSS. Do not touch any `data-fk`, `id` or key handler. Heading icons use `<svg class="ic s" aria-hidden="true" focusable="false"><use href="#i-..."/></svg>` inline in `index.html`.
- [ ] **Step 4: Run** the whole file and `python -m unittest discover -s tests`. Expected: PASS.
- [ ] **Step 5: Commit** `Show: transport, workspace layouts, panel headings`.

### Task 6: Browser check and docs

**Files:**
- Modify: `docs/specs/operator-ui.md` (Screens: Show rundown timing, workspace layouts), `docs/superpowers/specs/2026-10-30-ui-redesign-design.md` (answer both open questions: no widget sheet; slot length deferred), `CHANGELOG.md`, `docs/mockups/14-show-normal-1920.png`, `13-show-768.png`, `12-show-held-1920.png` (re-shot)

- [ ] **Step 1:** Run `python -m tests.operator_harness normal --port 8791`; in a real browser at 1920, 1366 and 768 wide, in each workspace: read the rundown, confirm front times and drift against the as-run times in the rows, click each workspace button, take with `T` (harness scenario only), Kill with two presses. Run the `held` scenario for the held banner and TAKE disabled reason, and with the server stopped for the connection-lost state.
- [ ] **Step 2:** Run axe-core 4.10.2 on the Show screen in each workspace at 1920 and 768 (and on `held`). Expected: no violations, no horizontal scroll.
- [ ] **Step 3:** Fix what the check finds and re-run Steps 1-2.
- [ ] **Step 4:** Update the three docs, re-shoot the three PNGs, run `python -m unittest discover -s tests`. Expected: OK.
- [ ] **Step 5: Commit** `UI slice 2: browser check and docs`; open a PR titled "UI redesign slice 2: the Show screen".
