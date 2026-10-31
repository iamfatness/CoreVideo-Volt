# Volt UI redesign, slice 4: Record, Rights, Publish

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the screens: Record, Rights and Publish move onto the shared system (state chips instead of loose glyphs, icons on headings and on the recording lifecycle), with no change to behaviour.

**Architecture:** Front end only, in `control/volt/session/operator/`. Reuse `STATE`, `stateChip`, `icon()` from slices 1-3. No server change, no new route, no key change.

**Tech Stack:** As slices 1-3 (`unittest` in `control/tests/test_operator_ui.py` with `_node`, `FILES`, `Icons.SPRITE_IDS`; harness scenarios `record_requested record_producing record_finalizing record_completed publish_progress publish_partial normal`; axe-core 4.10.2 via a routed same-origin script). Restart the harness after editing and load with the browser cache disabled.

**Spec:** `docs/superpowers/specs/2026-10-30-ui-redesign-design.md`. Slices 1-3 shipped in #68-#70.

## Global Constraints

- All earlier slices' constraints hold: brand colours only (red is for a live path, so an open recording uses the on-air style); no `http(s)://`, `style=`, `<style>`, inline script; every `id`, `data-fk`, `aria-*` kept; two-press arming kept; status is icon plus word plus colour.
- Rights stays per asset. No bulk clear or bulk hold anywhere (spec).
- Every refusal message, hint and button label keeps its text (existing tests and the operator rely on them).

## Review Focus

- A recording that is open, wrapped, failed or interrupted: the lifecycle shows the right step as current, a failed or interrupted room shows a failed step with its word, and the colour is never the only signal.
- A room that is unreachable or not configured: no step is marked done or current; Snapshot and Finalized read "unknown" or "none" without a chip that implies health.
- A clip blocked for rights on Publish: the blocked reason, the "Fix on Rights" link and a disabled Publish button all still show.
- An asset that is uncleared and on legal hold, and one that is cleared and not held: each Rights row reads correctly in both Cleared, Legal hold and Can air columns.
- A publish version status the page does not know renders its word with the neutral icon.

---

### Task 1: State chips on Record, Rights and Publish

**Files:**
- Modify: `control/volt/session/operator/operator.js` (`STATE`, `renderRecord` ~971, `assetsTable` ~1022, `renderRights` ~1040, `renderPublish` ~1084), `operator.css`
- Test: `control/tests/test_operator_ui.py` (extend `StateLanguage`; new class `ScreensState`)

**Interfaces:**
- Produces: `STATE.recording` (`record` icon, "Recording", `st air`), `STATE.wrapped` (`ready`, "Wrapped", `st ok`), `STATE.fresh` (`ready`, "Fresh", `st ok`), `STATE.stale` (`alert`, "Stale", `st pend`), `STATE.canair` (`ready`, "Yes", `st ok`), `STATE.blocked` (`alert`, "No", `st fail`). Rights and Record tables use `stateChip` for Cleared (via `cleared`/`uncleared`), Legal hold (`hold`, else plain "No"), Can air or publish (`canair`, or `blocked` followed by the reason text), Recording/Wrapped, and Snapshot (`fresh`/`stale`). The Publish blocked line becomes `stateChip('blocked', 'Blocked')` followed by the reason and the existing "Fix on Rights" link. Lifecycle steps (`REC_STEPS`) draw `icon('ready')` for done, `icon('record')` for the current step, `icon('pending')` for later steps and `icon('alert')` for the failed step, instead of `✓ ▶ ○ ✕`; the `done`/`now`/`fail` classes and `aria-current="step"` stay.

- [ ] **Step 1: Write the failing tests.** `StateLanguage` expected keys gain `blocked canair fresh recording stale wrapped`; `ScreensState.test_specs` asserts `stateChipSpec` for those six returns the icon, word and class above and each icon is in the sprite; `ScreensState.test_no_loose_glyphs_left_on_these_screens` asserts that, within the source of `renderRecord`, `assetsTable`, `renderRights` and `renderPublish` (sliced by function name), the characters `✓`, `✕`, `⚑`, `⚠` and `▶` do not appear.
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_operator_ui.StateLanguage tests.test_operator_ui.ScreensState -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the map entries, the chip swaps and the lifecycle icons; add CSS so `.step .ic` is 20px and centered above the word, and `.step.now`'s icon uses the step's text colour.
- [ ] **Step 4: Run** the whole file. Expected: PASS (update any test that pinned a removed glyph string).
- [ ] **Step 5: Commit** `Record, Rights, Publish: state chips and lifecycle icons`.

### Task 2: Icons on headings and a sprite check

**Files:**
- Modify: `operator.js` (the `h-recstart`, `h-lifecycle`, `h-assets`, `h-assets2`, `h-clips` headings are built in JS), `operator.css`
- Test: `test_operator_ui.py` (new class `ScreensMarkup`)

**Interfaces:**
- Produces: a leading `icon(name, 's')` inside each heading: Start a recording (`record`), Recording lifecycle (`wave`), Recordings (`library`), Assets on Rights (`rights`), Clips on Publish (`publish`). The heading element keeps its `id`; its visible text is unchanged and `aria-labelledby` still resolves to the full text because the icon is `aria-hidden`. The Rights and Publish tables get the same `.st` chip alignment as the Library header (chips in a table cell do not wrap mid-word).

- [ ] **Step 1: Write the failing tests.** `ScreensMarkup.test_headings_carry_icons` (the source of each of the five `el('h2', { id: '<id>'` calls contains `icon(`); `test_icons_exist` (every `icon('x'` name in `operator.js` is in `Icons.SPRITE_IDS`; this is the existing check, re-run).
- [ ] **Step 2: Run** the class. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file and the full suite `python -m unittest discover -s tests`. Expected: PASS.
- [ ] **Step 5: Commit** `Record, Rights, Publish: icons on headings`.

### Task 3: Browser check, docs, screenshots

**Files:**
- Modify: `docs/specs/operator-ui.md` (Screens), `CHANGELOG.md`, `docs/mockups/04-record-requested.png`, `05-record-producing.png`, `06-record-finalizing.png`, `07-record-completed.png`, `08-publish-in-progress.png`, `09-publish-partial-failure.png`, `10-rights.png`, `11-publish-clips.png` (re-shot, same names)

- [ ] **Step 1:** For each scenario (`record_requested`, `record_producing`, `record_finalizing`, `record_completed` on `#record`; `publish_progress`, `publish_partial` on `#publish`; `normal` on `#rights` and `#publish`), start `python -m tests.operator_harness <scenario> --port <n>`, open it with the cache disabled at 1920, 1366 and 768 wide in each workspace, and read the screen for clipped text, wrapped chips and misaligned columns. Stop the server to see the connection-lost state on Record.
- [ ] **Step 2:** Run axe-core 4.10.2 on each of those screens in each workspace at 1920 and 768. Expected: no violations, no horizontal scroll.
- [ ] **Step 3:** Fix what the check finds and re-run Steps 1-2.
- [ ] **Step 4:** Update the docs, re-shoot the eight PNGs at 1920 wide (Operator workspace for Record, Producer for Rights and Publish), run `python -m unittest discover -s tests`. Expected: OK.
- [ ] **Step 5: Commit** `UI slice 4: browser check, docs, screenshots`; open a PR titled "UI redesign slice 4: Record, Rights, Publish".
