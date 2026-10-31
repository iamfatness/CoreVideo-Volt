# Volt UI redesign, slice 1: tokens, icons, components, shell

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the operator UI one visual system (tokens, 39 SVG icons, shared components) and a new shell (header, workspace switcher, icon rail, footer), without changing any screen's behaviour.

**Architecture:** Static files only, in `control/volt/session/operator/`. Tokens and components go in `operator.css`; the icon sprite is inline in `index.html`; icon, state and workspace helpers go in `operator.js` next to `el()`. A workspace is a body class plus a rail filter; all screens stay in the one DOM.

**Tech Stack:** Plain HTML/CSS/JS, no build, no framework, no CDN. Tests: `unittest` in `control/tests/test_operator_ui.py`, with `node` for page functions (the file's existing pattern: `require` of `operator.js`). Browser check: `python -m tests.operator_harness <scenario> --port 8791` plus axe-core.

**Spec:** `docs/superpowers/specs/2026-10-30-ui-redesign-design.md`. Later slices (Show, Library, Publish/Rights/Record) get their own plans once this one is seen in a browser.

## Global Constraints

- Colours (from `docs/brand-guide.md`; keep the names already in `operator.css`): ground `#141414`, panel `#1c1c1c`, hairline `#303030`, text `#e2e2e2`, suite blue `#1d6dc2` (actions, focus), signal amber `#f0b429` (Volt accent: next, selected, current), on-air `#ff4d4d` (live only), cleared `#3ddc97` (ready/cleared, sparingly). No second blue. No new green for Take: Take stays suite blue.
- Contrast: text 4.5:1, icons and control borders 3:1, measured against the surface they sit on.
- Status is always icon plus word plus colour.
- `tests/test_operator_ui.py::test_references_only_same_origin_assets` stays true: no `http(s)://` in `index.html`, `operator.css` or `operator.js`; no `style=` attribute, `<style>` element or inline `<script>` in `index.html`; no CSS `url()` except `data:`. So the sprite is inline in `index.html` with no `xmlns` attribute.
- Keep every element `id`, `data-screen`/`data-fk` attribute and `aria-*` role. Class names may change. Keys unchanged: `1`-`5` still go to the fixed screens in `SCREENS`; Space never takes outside Show.
- Logo: four amber slashes plus VOLT (`brand/logos/mark-slashes.svg`). No gradient, no bolt.
- `localStorage` access is wrapped in try/catch and the page works without it.

## Review Focus

- The URL hash names a screen outside the current workspace's rail (`#rights` while in Operator): the workspace stays, the screen shows, its rail item appears highlighted as a temporary extra.
- `localStorage` throws or holds an unknown workspace name (`"director"`): fall back to Operator, no error shown.
- `/api/whoami` fails or answers 401 before a token is entered: the user pill stays hidden, no banner.
- A page loaded at 768px wide: the rail is icons only, the header wraps without horizontal page scroll.
- A status word the page does not know (a future server state): `stateChip` renders the word with the neutral icon rather than throwing.

---

### Task 1: Tokens, scales and a contrast test

**Files:**
- Modify: `control/volt/session/operator/operator.css:1-6` (the `:root` block)
- Test: `control/tests/test_operator_ui.py` (new class `Tokens`)

**Interfaces:**
- Produces: CSS custom properties used by every later task: `--space-1..6` (4, 8, 12, 16, 24, 32px), `--radius-sm` (3px), `--radius` (6px), `--fs-xs` (.75rem), `--fs-sm` (.875rem), `--fs-md` (1rem), `--fs-lg` (1.25rem), `--fs-xl` (1.75rem), `--rail-w` (64px), `--head-h` (48px), plus the existing colour tokens unchanged. No new colour tokens: failed uses `--air`.

- [ ] **Step 1: Write the failing test.** `Tokens.test_colour_pairs_meet_contrast` parses the `:root` hex values from `operator.css` and computes WCAG relative-luminance ratios. Assertions: `--text`/`--ground` >= 4.5, `--text`/`--panel` >= 4.5, `--muted`/`--panel` >= 4.5, and for each of `--signal`, `--air`, `--ok`, `--blue-hi` against `--panel` >= 3.0. `Tokens.test_scales_exist` asserts each token name in the Interfaces list appears in `:root`.
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_operator_ui.Tokens -v`. Expected: `test_scales_exist` FAILS (tokens missing); the colour test passes or fails on real numbers (record them).
- [ ] **Step 3: Add the scale tokens to `:root`.** If a colour pair fails, adjust only that token's lightness and note the new value in the commit message; do not change hue.
- [ ] **Step 4: Run the class again.** Expected: PASS.
- [ ] **Step 5: Commit** `git add control && git commit -m "UI: spacing, radius, type and shell tokens, with a contrast test"`.

### Task 2: Icon sprite and the `icon()` helper

**Files:**
- Modify: `control/volt/session/operator/index.html` (sprite right after `<body>`), `control/volt/session/operator/operator.js` (helper beside `el()`, ~line 183; add to the `module.exports` block if one exists, else follow how `framesToTc` is exported), `control/volt/session/operator/operator.css` (`.ic`)
- Reference: `docs/mockups/ui-icons-first-pass.html` (the 38 `<symbol>` drawings, ids `i-*`), `brand/logos/mark-slashes.svg` (the mark)
- Test: `control/tests/test_operator_ui.py` (new class `Icons`)

**Interfaces:**
- Produces: sprite symbols with ids `i-<name>` for these 39 names: `rundown record rights publish library take hold skip stop cut wave transcript marker onair ready pending alert sliders user key search filter download link trash lock room cue export shot plus close chevron more mic eye lower3 mark`. JS: `icon(name: string, cls?: string) -> SVGElement` returning `<svg class="ic [cls]" aria-hidden="true" focusable="false"><use href="#i-name"/></svg>`, built by setting `innerHTML` on a `<template>` element (not `createElementNS`: its namespace URL would trip the "no `http://`" test) and returning `template.content.firstElementChild`. CSS: `.ic` 20px, `fill:none; stroke:currentColor; stroke-width:1.75; stroke-linecap:round; stroke-linejoin:round`, `.ic.s` 16px with stroke-width 2.

- [ ] **Step 1: Write the failing tests.** `Icons.test_every_named_icon_has_a_symbol` (all 39 ids present in `index.html`); `Icons.test_every_icon_the_page_asks_for_exists` (regex every `icon('x'` and `#i-x` in `operator.js`/`index.html`; each must be a sprite id); `Icons.test_sprite_obeys_the_csp_rules` (no `xmlns`, no `style=` in `index.html`).
- [ ] **Step 2: Run** `python -m unittest tests.test_operator_ui.Icons -v`. Expected: FAIL.
- [ ] **Step 3: Paste the 38 symbols from the mockup and add `i-mark`** from `mark-slashes.svg` (four slashes, `fill:currentColor`, no stroke) into a hidden `<svg width="0" height="0" aria-hidden="true">` at the top of `<body>`. Implement `icon()` and `.ic`.
- [ ] **Step 4: Run** the `Icons` class, then the whole file. Expected: PASS (the `test_references_only_same_origin_assets` test still passes).
- [ ] **Step 5: Commit** `UI: icon sprite (39 icons) and icon() helper`.

### Task 3: State chips and shared components

**Files:**
- Modify: `operator.js` (add `STATE` map and `stateChip`), `operator.css` (component classes), no screen markup yet
- Test: `test_operator_ui.py` (new class `StateLanguage`, node-run like `TimecodeParity.run_js`)

**Interfaces:**
- Consumes: `icon()` from Task 2.
- Produces: `STATE` object with keys `onair ready pending failed locked`, each `{icon, word, cls}`: onair -> `onair`, "On air", `st air`; ready -> `ready`, "Ready", `st ok`; pending -> `pending`, "Pending", `st pend`; failed -> `alert`, "Failed", `st fail`; locked -> `lock`, "Held", `st hold`. `stateChip(kind: string, word?: string) -> HTMLElement` (span with icon then text; unknown `kind` uses `pending`'s icon and class `st`, never throws). CSS components: `.btn` (blue primary, kept), `.btn.ghost`, `.btn.danger` (armed state), `.btn.take` (large, blue, min-height 56px), `.chip`/`.pill` (rounded, hairline border), `.panel > h2` header with optional leading `.ic`, `.st.air|ok|pend|fail|hold` colours from tokens.

- [ ] **Step 1: Write the failing tests.** `StateLanguage.test_each_state_maps_to_an_existing_icon_and_a_class` (static: parse `STATE`, assert every `icon` is a sprite id); `test_state_chip_never_throws_on_an_unknown_kind` (node: `require` the module with a minimal `document` stub only if the existing tests already stub one; otherwise test `STATE` shape and `stateChipSpec(kind, word)`, a pure function returning `{icon, word, cls}`, which `stateChip` wraps).
- [ ] **Step 2: Run** the class. Expected: FAIL.
- [ ] **Step 3: Implement** `STATE`, `stateChipSpec`, `stateChip`; add the component CSS.
- [ ] **Step 4: Run** the class and the file. Expected: PASS.
- [ ] **Step 5: Commit** `UI: state language and shared components`.

### Task 4: Workspaces (pure logic)

**Files:**
- Modify: `operator.js` (add beside `SCREENS`, line ~123)
- Test: `test_operator_ui.py` (new class `Workspaces`, node-run)

**Interfaces:**
- Produces: `WORKSPACES = { producer: ['show','rights','publish','library'], editor: ['library','publish','rights'], operator: ['show','record'] }`; `DEFAULT_WORKSPACE = 'operator'`; `workspaceName(stored: unknown) -> string` (returns `stored` if it is a key of `WORKSPACES`, else `DEFAULT_WORKSPACE`); `railFor(workspace: string, screen: string) -> {screens: string[], extra: string|null}`: `screens` is the workspace's list; `extra` is `screen` when it is a valid screen not in the list, else null; `defaultScreen(workspace) -> string` (first entry). All exported for node.

- [ ] **Step 1: Write the failing tests** (node): `workspaceName("director") == "operator"`, `workspaceName(null) == "operator"`, `workspaceName("editor") == "editor"`; `railFor("operator","rights") == {screens:["show","record"], extra:"rights"}`; `railFor("editor","library").extra is null`; `railFor("operator","nonsense").extra is null`; `defaultScreen("editor") == "library"`.
- [ ] **Step 2: Run** `python -m unittest tests.test_operator_ui.Workspaces -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the four functions and export them.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `UI: workspace rules (producer, editor, operator)`.

### Task 5: The shell

**Files:**
- Modify: `index.html` (header, rail, footer), `operator.js` (`renderChrome` ~line 535, `setScreen` ~2742, init ~2795; new `setWorkspace`, `renderWho`), `operator.css` (shell layout)
- Test: `test_operator_ui.py` (update `test_the_fifth_screen_is_in_the_nav_and_the_page`; new class `Shell`)

**Interfaces:**
- Consumes: `icon`, `stateChip`, `railFor`, `workspaceName`, `defaultScreen`.
- Produces (markup): header `.top` holds `.brand` (the `i-mark` icon in amber, then `<strong>VOLT</strong>`), `#show-select` (unchanged), `#workspaces` (`role="group" aria-label="Workspace"`; three `<button type="button" data-ws="producer|editor|operator" aria-pressed>` each with icon `rundown|cut|onair` and label), `#who` (hidden until whoami answers; text `name` and `role`), `#conn`, `#token-btn`, `#keys-btn`, `#clock` (all ids kept). The nav becomes a left rail: `<nav aria-label="Screens" id="screens" class="rail">` with the same five `<a href="#x" data-screen="x">`, each containing `<svg class="ic">` (rundown/record/rights/publish/library icons for show/record/rights/publish/library) and `<span>Label</span>`. A footer `<footer class="foot">` shows `#foot-show`, `#foot-count`, `#foot-version`. Body gets class `ws-<name>`. Rail links not in `railFor(...).screens` get `hidden`, except `extra`, which is shown with class `off-rail`.
- Behaviour: `setWorkspace(name)` stores `volt.workspace` in `localStorage` (try/catch), sets the body class and `aria-pressed`, and, when the URL hash is empty, goes to `defaultScreen(name)`; a non-empty hash is never overridden. Initial workspace is `workspaceName(storedValue)`. `renderWho()` calls `/api/whoami` through the page's existing header-aware fetch pattern (see line ~310), shows `#who` on success, leaves it hidden on any failure.

- [ ] **Step 1: Update the existing test and write new ones.** Change the Library nav assertion to `'data-screen="library"'` plus `'id="screen-library"'`. `Shell.test_ids_the_script_depends_on_survive` (each id above present), `test_workspace_buttons_exist_for_all_three`, `test_rail_links_keep_their_hash_and_data_screen`, `test_footer_ids_present`.
- [ ] **Step 2: Run** the file. Expected: FAIL on the new tests and the updated one.
- [ ] **Step 3: Replace the header/nav markup and add the footer**; implement `setWorkspace`, the rail filter in `renderChrome`, `renderWho`; add shell CSS (grid: header over rail+main over footer; below 900px the rail is 56px icons-only and labels get `vh`-style visually hidden; no horizontal page scroll).
- [ ] **Step 4: Run the whole file** `python -m unittest tests.test_operator_ui -v` and the full suite `python -m unittest discover -s tests`. Expected: PASS.
- [ ] **Step 5: Commit** `UI: shell with workspaces, icon rail, footer`.

### Task 6: Browser check and docs

**Files:**
- Modify: `docs/specs/operator-ui.md` (Screens section: workspaces, rail, icon helper), `docs/brand-guide.md` (UI direction: point to the system), `CHANGELOG.md`, `docs/mockups/14-show-normal-1920.png` and `13-show-768.png` (re-shot)
- Create: `docs/superpowers/plans/ui-slice-1-contrast.md` (the measured contrast table from Task 1)

- [ ] **Step 1:** Run `python -m tests.operator_harness normal --port 8791`, open `/operator` in a real browser at 1920, 1366 and 768 wide; switch each workspace; load `#rights` while in Operator; with `localStorage` blocked; with the server stopped (conn pill shows the failure).
- [ ] **Step 2:** Run axe-core 4.10 as `operator-ui.md` describes (script fulfilled from the same origin). Expected: no violations on Show in each workspace.
- [ ] **Step 3:** Fix anything found; re-run Steps 1-2.
- [ ] **Step 4:** Update the three docs and re-shoot the two PNGs.
- [ ] **Step 5: Commit** `UI slice 1: browser check, contrast table, docs`; open a PR titled "UI redesign slice 1: tokens, icons, shell".
