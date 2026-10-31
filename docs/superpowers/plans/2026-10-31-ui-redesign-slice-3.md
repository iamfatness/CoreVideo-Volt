# Volt UI redesign, slice 3: the Library

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the Library screen (shots and clips, Mark from transcript, Selection to export) onto the slice-1 system: icons on headings and tabs, one state language for cleared, held and job status, and an Editor layout that gives the player and shot table the width.

**Architecture:** Markup and class changes only, plus small DOM-side helpers in `operator.js`. No server change, no new route, no key change. The Library already has its three tabs and the Export tab ("Selection to export"); this slice restyles them and does not move behaviour.

**Tech Stack:** As slices 1 and 2 (`unittest` in `control/tests/test_operator_ui.py`, `_node`, `FILES`, `Icons.SPRITE_IDS`; harness `python -m tests.operator_harness library --port 8791`; axe-core 4.10.2 via a routed same-origin script). The harness serves static files read at start: restart it after editing, and load pages with the browser cache disabled (the server sends no cache headers).

**Spec:** `docs/superpowers/specs/2026-10-30-ui-redesign-design.md` (Workspaces, Library, state language). Slice 2 shipped in PR #69; this plan is cut from that branch.

## Global Constraints

- Everything in slices 1 and 2's Global Constraints holds: brand colours only; no `http(s)://`, `style=`, `<style>` or inline script in the three files; every `id`, `data-fk`, `data-screen` and `aria-*` kept; Library keys unchanged (`K` play, `,` `.` frame, `[` `]` shot, `I` `O` mark, Enter adds a clip, Space never takes); two-press arming kept; status is icon plus word plus colour.
- Rights stays per asset; no bulk clear anywhere.
- The Library's panels keep every current piece of information; this slice changes how it is drawn, not what is shown.
- **J/K/L shuttle is in** (owner, 2026-10-31: very important to the people who will use this). `L` plays forward, `J` plays in reverse, `K` stops; repeated `L` or `J` doubles the speed (1x, 2x, 4x, 8x); the opposite key slows through zero by reversing direction at 1x. A browser video cannot play backwards, so reverse is a timed seek loop; it is the same feel, not the same decoder. `K` keeps today's behaviour when nothing is shuttling (play or pause), so no existing key changes meaning.

## Review Focus

- A cleared asset, an uncleared asset and one under legal hold each show icon, word and colour in the asset header on both the Shots and Transcript tabs, and an asset that is both uncleared and held shows both chips.
- A job status the page does not know (a future worker state) renders its word with the neutral icon and does not throw.
- Holding `J` or `L` (key repeat) must not stack timers or keep doubling: only a fresh press steps the speed. Leaving the Library, switching tab, changing asset, stepping a frame, jumping a shot or pressing `K` stops any shuttle and returns `playbackRate` to 1.
- Reverse shuttle reaching the start of the clip stops there and shows no error; forward shuttle reaching the end pauses.
- A focused field or button: `J`, `K`, `L` do nothing there (the existing `onControl` rule), so typing "jkl" in a clip name never moves the player.
- Switching tabs with the keyboard (arrow keys on the tablist) still works and keeps `aria-selected` and `tabindex` right after the icons are added.
- The Library at 1000px wide and below collapses to one column in every workspace (the Editor layout must not override the narrow rule).
- With no asset chosen, no recordings, a load error and an asset that cannot be marked, the empty-state cards still render and the tabs stay hidden.

---

### Task 1: Cleared, held and job status in the state language

**Files:**
- Modify: `control/volt/session/operator/operator.js` (asset header lines ~2027 and ~2435; ingest job chips ~1965; version chips ~1086)
- Test: `control/tests/test_operator_ui.py` (extend `StateLanguage`, new `LibraryState`)

**Interfaces:**
- Consumes: `stateChip`, `stateChipSpec`, `STATE`.
- Produces: `STATE.cleared` (`ready` icon, "Cleared", `st ok`), `STATE.uncleared` (`alert`, "Not cleared", `st fail`), `STATE.hold` (`lock`, "Legal hold", `st fail`), `STATE.running` (`pending`, "Running", `st pend`), `STATE.done` (`ready`, "Done", `st ok`), `STATE.queued` (`pending`, "Queued", `st pend`). `assetChips(d) -> HTMLElement[]` returning `[clearedChip, holdChip?]` for an object with `cleared` and `legalHold`. Job and version chips (`JOB_CHIP`, `VER_CHIP`) are drawn with `stateChip(status)`; `failed` already exists; the "Not made" chip becomes `stateChip('pending', 'Not made')`. The old `ok`/`no` glyph spans are removed from the two asset headers.

- [ ] **Step 1: Write the failing tests.** `StateLanguage` expected key set gains `cleared done hold queued running uncleared`; `LibraryState.test_cleared_and_hold_specs` asserts `stateChipSpec` for `cleared`, `uncleared`, `hold` returns the icon, word and class above and that every icon is in the sprite; `test_headers_use_the_chips` asserts `operator.js` contains no `'✓ ' : '✕ '` and no `'⚑ '` text (`re.findall` count 0) and that `assetChips(` appears at least twice.
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_operator_ui.StateLanguage tests.test_operator_ui.LibraryState -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the map entries and `assetChips`; replace both header glyph spans, the ingest job chips and the version chips. The Rights table cell at ~1035 stays for slice 4.
- [ ] **Step 4: Run** the whole file. Expected: PASS (update any test that pinned the removed glyph text).
- [ ] **Step 5: Commit** `Library: cleared, held and job status in the state language`.

### Task 2: Icons on headings and tabs

**Files:**
- Modify: `control/volt/session/operator/index.html` (Library section), `operator.css` (`.lib-tabs`), `operator.js` (any code that sets a tab or heading `textContent`)
- Test: `test_operator_ui.py` (new class `LibraryMarkup`)

**Interfaces:**
- Produces: icons (`<svg class="ic s" aria-hidden="true" focusable="false"><use href="#i-..."/></svg>`, inline in `index.html`) on: `h-ingest` (`download`), `h-player` (`eye`), `h-shots` (`shot`), `h-tray` (`library`), `h-span` (`sliders`), and on the three tabs: Shots and clips (`shot`), Mark from transcript (`transcript`), Selection to export (`export`). Each tab keeps its text as the accessible name; its icon is `aria-hidden`. Tab ids, `aria-controls`, `aria-selected` and `tabindex` are untouched. Any JS that wrote a heading's `textContent` is changed to write a `<span>` inside it so the icon survives (as `h-cues-text` did in slice 2).

- [ ] **Step 1: Write the failing tests.** `LibraryMarkup.test_headings_and_tabs_have_icons` (each listed id's element contains `href="#i-<name>"`, checked by slicing `index.html` from the id to the next `</h2>` or `</button>`); `test_tab_attributes_survive` (three `role="tab"` buttons keep `aria-controls="lib-body"`, `"lib-words"`, `"lib-export"` and `tabindex="-1"` on the two inactive ones); `test_no_script_overwrites_a_heading_with_an_icon` (for each of the five heading ids, `operator.js` has no `$('<id>').textContent`).
- [ ] **Step 2: Run** the class. Expected: FAIL.
- [ ] **Step 3: Implement** the markup and the tab CSS (icon before label, `gap: var(--space-2)`, selected tab keeps its amber top bar).
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit** `Library: icons on headings and tabs`.

### Task 3: J/K/L shuttle

**Files:**
- Modify: `operator.js` (pure `shuttleNext` above the node early-return and exported; `shuttleStop`, `shuttleRun` beside `togglePlay` ~line 1538; keys in `libKey` ~1842), `index.html` (a "Shuttle" hint next to the transport), `docs/specs/operator-ui.md` (Keys (Library screen) table), the Keys dialog (`#keys-dlg`) rows for the Library
- Test: `test_operator_ui.py` (new class `Shuttle`, `_node` for the pure part, static for wiring)

**Interfaces:**
- Produces: `shuttleNext(state: {dir: -1|0|1, speed: number}, key: 'j'|'k'|'l') -> {dir, speed}`: `l` from dir 0 or -1 gives `{dir:1, speed:1}`; `l` from dir 1 gives speed doubled up to 8 (8 stays 8); `j` mirrored with `dir:-1`; `k` gives `{dir:0, speed:0}`. `shuttleRun(state)` applies it: dir 1 sets `video.playbackRate = speed` and plays; dir -1 pauses the video and runs a `requestAnimationFrame` loop that subtracts `dt * speed` seconds from `currentTime`, stopping at 0; dir 0 cancels the loop, pauses and sets `playbackRate = 1`. `shuttleStop()` is the same as `shuttleRun({dir:0, speed:0})` and is called from `togglePlay` when pausing, `stepFrames`, `jumpShot`, `libLeave`, `proxyRelease` and a tab switch. Key repeat is ignored (`!e.repeat`) for `j` and `l`. The arrow-key `L.pos` playhead readout keeps following `currentTime` as it does for normal play.

- [ ] **Step 1: Write the failing tests.** `Shuttle.test_speed_ladder`: from `{dir:0,speed:0}` `l,l,l,l,l` gives speeds `1,2,4,8,8` with dir 1; `j,j,j` gives `-1` with speeds `1,2,4`; `l` then `j` gives `{dir:-1,speed:1}`; any state then `k` gives `{dir:0,speed:0}`. `Shuttle.test_keys_are_wired_and_ignore_repeat_and_fields` (static: `libKey` handles `'j'` and `'l'` with `!e.repeat`, and sits after the `onControl` check). `Shuttle.test_every_exit_stops_the_shuttle` (static: `shuttleStop(` appears in `libLeave`, `stepFrames`, `jumpShot`, `togglePlay` and the tab-switch code). `Shuttle.test_documented` (the keys table in `docs/specs/operator-ui.md` has rows for `J`, `K`, `L`).
- [ ] **Step 2: Run** `python -m unittest tests.test_operator_ui.Shuttle -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the pure function, the run/stop functions and the key wiring; add the table rows and the dialog rows; add `aria-keyshortcuts` hints only where a visible button exists (there is none for J and L, so add a visible `J K L` hint text beside the transport).
- [ ] **Step 4: Run** the class and the file. Expected: PASS.
- [ ] **Step 5: Commit** `Library: J/K/L shuttle`.

### Task 4: Editor layout and transport

**Files:**
- Modify: `operator.css`, `index.html` (transport buttons)
- Test: `test_operator_ui.py` (new class `LibraryLayouts`)

**Interfaces:**
- Produces: `.ws-editor .cols.lib { grid-template-columns: minmax(0,1fr) 300px }` and `.ws-producer .cols.lib` left at the default 340px, both inside `@media(min-width:1001px)`; the one-column rule at 1000px stays and wins below it. The player's transport buttons get icons (`take` for Play, `hold` for nothing: only Play gets one) and the Mark in/out buttons keep their text; the Add clip button is the one primary (blue) button of the form. The `.screenbox` fills the available width up to 16:9.

- [ ] **Step 1: Write the failing tests.** `LibraryLayouts.test_editor_gives_the_main_column_the_width` (regex for the `.ws-editor .cols.lib` rule with `300px` inside a `min-width:1001px` media block); `test_narrow_rule_still_collapses` (`@media(max-width:1000px){.cols.lib{grid-template-columns:1fr}}` still present and appears after the editor rule in the file); `test_play_has_an_icon_and_keeps_its_key` (`id="lib-play"` button contains `href="#i-take"` and `aria-keyshortcuts="K"`).
- [ ] **Step 2: Run** the class. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file and `python -m unittest discover -s tests`. Expected: PASS.
- [ ] **Step 5: Commit** `Library: Editor layout and transport icon`.

### Task 5: Browser check and docs

**Files:**
- Modify: `docs/specs/operator-ui.md` (Library section), `CHANGELOG.md`, `docs/mockups/` Library PNGs that exist (re-shot; list them with `ls docs/mockups`, keep names)

- [ ] **Step 1:** Run `python -m tests.operator_harness library --port 8791` (and `export --port 8792`). With the cache disabled, at 1920, 1366 and 768 wide in each workspace, open `#library`: choose an asset, play the proxy, mark a range with `I`/`O` (keyboard only), add a clip, open the transcript tab, tick two clips and open Selection to export, then use the arrow keys on the tablist. Read each screen for clipped text and misaligned columns.
- [ ] **Step 2:** Run axe-core 4.10.2 on the Library with each of the three tabs open, in each workspace at 1920 and 768. Expected: no violations, no horizontal scroll.
- [ ] **Step 3:** Fix what the check finds and re-run Steps 1-2.
- [ ] **Step 4:** Update the docs and re-shoot the PNGs; run `python -m unittest discover -s tests`. Expected: OK.
- [ ] **Step 5: Commit** `UI slice 3: browser check and docs`; open a PR titled "UI redesign slice 3: the Library" with base `ui-redesign-slice-2` (or `main` if #69 has merged).
