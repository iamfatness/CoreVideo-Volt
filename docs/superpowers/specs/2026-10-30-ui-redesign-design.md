# Volt UI redesign: design

Status: draft for the owner's review. Nothing here is built. Date 2026-10-30.

## Why

The owner opened the desk and said it is understandable but feels thrown together, with no well-defined brand or feel. The target they described: a polished software suite that fits into any broadcast, that feels familiar, and where each of three roles (producer, editor, operator) feels in control of the information they need.

What is actually wrong, from the code and the brand guide:

- The brand exists on paper ([brand-guide.md](../../brand-guide.md)) and partly in `operator.css` (the colour tokens), but not as a system. There is no icon set, no type scale, no spacing scale and no shared component set. Each screen is styled for itself.
- Status is a glyph and a word in places and colour alone in others. Icons are Unicode characters, so they differ by font.
- There is one flat row of screens (Show, Record, Rights, Publish, Library). Nothing says who a screen is for, so every role sees every control.

## Goals

1. **One visual system**, in CSS custom properties and a small set of components, that every screen uses. Brand-guide colours, unchanged.
2. **A real icon set**: custom SVG, 24px grid, 1.75px round stroke, 38 icons to start (first pass in [ui-icons-first-pass.html](../../mockups/ui-icons-first-pass.html), open it in a browser). Status is always icon plus word plus colour.
3. **Three workspaces: Producer, Editor, Operator.** A workspace picks which screens appear in the rail, which is the default, and how the first screen's panels are arranged. It is a saved layout, as in Premiere ([Adobe: workspaces](https://helpx.adobe.com/ee/premiere/desktop/get-started/tour-the-workspace/what-are-workspaces.html)), not a permission. What a user may do is still decided by the server (operator and admin tokens, `ROUTE_ROLES`).
4. **Familiar broadcast conventions** where they are public and consistent (see Research). The rundown gains front time and a signed over/under.
5. **No regressions.** Every existing behaviour, key, refusal message and test keeps working. axe-core reports no violations on the screens it reported none on before.

## Non-goals

- No new product features, routes or schema. This is the front end only.
- No framework, build step or CDN. The operator UI stays static files served by `serve.py` under its same-origin CSP ([operator-ui.md](../../specs/operator-ui.md)).
- No rewrite of `operator.js` logic (166 KB). Markup and class names change; behaviour does not.
- No free-docking or draggable panels. Layouts are fixed per workspace.
- No light theme in this pass. (Owner-visible call: a light "newsroom" variant was mocked and set aside.)
- No change to the logo or the name.

## Anti-goals

Failure even if it technically works: looking like a generic admin template; colour used as decoration; a second blue; red used for anything but a live path; any screen where state is readable only by colour; a polished screen that is slower or harder to use at a live desk than today's.

## Constraints

- **Brand guide is the source.** Ground `#141414`, panel `#1C1C1C`, hairline `#303030`, text `#E2E2E2`. **Signal amber `#F0B429` is Volt's accent** (Next, the selected row or span, publish). **Suite blue `#1D6DC2`** is the CoreVideo family colour: actions and focus. **On air `#FF4D4D`** only for a live path. **Cleared `#3DDC97`** for rights and ready, sparingly. Logo is the four amber slashes plus VOLT. The brand guide asks for the next pass to be built in the CoreVideo widget sheet, not a separate web theme.
- The first-pass mockups used a different blue for selection and a gradient logo square. Both break the brand guide and are dropped. Selection and current row use amber.
- Keys stay as in operator-ui.md. Space never takes. Two-press arming for destructive actions stays. Nothing is shown optimistically.
- Contrast: text 4.5:1, icons and control borders 3:1 against the surface they sit on ([WCAG 1.4.11](https://w3.org/WAI/WCAG22/Understanding/non-text-contrast)), measured and recorded, not assumed.

## The design

### Shell

Header: logo, show picker, workspace switcher (Producer / Editor / Operator), connection pill (room), signed-in user and access level, clock. Left rail: the screens for the current workspace, each an icon over a short label. Footer: show name, item count, total duration, version.

### Workspaces

| Workspace | Rail | Default | Built for |
|---|---|---|---|
| Producer | Show, Rights, Publish, Library | Show, rundown wide, cues and build panel open | Planning the rundown, clearing rights, publishing |
| Editor | Library, Publish, Rights | Library, player and shot table wide | Shots, clips, transcript, NLE export |
| Operator | Show, Record | Show, program large, TAKE/HOLD/SKIP prominent | Running the show on air |

The choice is remembered in `localStorage` (with try/catch; it renders without it). The URL hashes (`#show`, `#library` and so on) keep working and always win over the saved default.

### Rundown

Columns: number, state icon, name, duration, **front time**, state word. Header shows total and **drift as a signed time** (`+01:23` / `-01:23`), red when behind, neutral when ahead. On-air row: amber is Next, red is live; the current row has a bar down its left edge. Terms follow public usage: front time is when a segment starts, back time is when it must start to finish on schedule ([Rundown Creator](https://www1.rundowncreator.com/how-it-works/timing-101), [Ross Inception](https://help.rossvideo.com/inception/help/v12.0/UserHelp/Online_Help_System/Procedures/Playing_Out_a_Running_Order_as_an_NCS/Watch_Running_Order_Timing.htm), [Viz Mosart](https://docs.vizrt.com/viz-mosart-users-guide/5.8/Timing_Display.html)). Front time is computed in the page: the show's first start (or the first scheduled item) plus the running sum of the durations of items not killed; an item with no duration counts as zero and shows `--:--`. **Over/under in this pass is drift**: for the on-air item, its actual start minus its planned front time (`+` behind, `-` ahead), shown beside the on-air row and in the header; with nothing on air it is blank. This needs no new data. **Over/under against a slot length, and back time, need a target the data model does not have, so they are out of this pass** (see Open questions).

### Operator controls

TAKE is the largest control on screen, in suite blue (the brand's action colour, not a new green; green stays for cleared and ready), with the take icon, and spaced away from HOLD and SKIP. Destructive actions (Kill, Remove, Delete) are separated from routine controls and keep two-press arming, with the button text saying what the second press does and whether it can be undone.

### State language

One mapping, used everywhere (Take is an action, not a state, so it is not in this list): on air (red, onair icon), ready (green, ready), pending (amber, pending), failed (red, alert), locked or held (neutral, lock). Always icon, word and colour.

### Library, Publish, Rights, Record

Same shell and components; layouts keep the current information. Library keeps its player, shot table, tray and tabs. Rights clearing stays per asset; no bulk clear (a known failure in other systems: [Adobe bulk metadata](https://experienceleague.adobe.com/en/docs/experience-cloud-kcs/kbarticles/ka-30160)).

## Research behind it

Public sources, with the caveat that most are vendor documentation, not standards:

- Workspaces as saved task layouts: Premiere (Assembly enlarges the media panel; Editing, Color and Audio rearrange the rest).
- Switcher software uses pages for major functions ([ATEM Software Control](https://alternativeto.net/software/atem-software-control/about)), and vMix marks Preview and Output with coloured borders ([vMix guide](https://wwwregister.vmix.com/help26/vMixUserGuide.pdf)).
- Rundown timing vocabulary: front time, back time, signed over/under. Avid's Rundown app has an optional timing bar ([Avid](https://mediacentral.avid.com/mccux/Content/Users_Guide/Show%20Timing.htm)). Colours for over/under are not standardised; red for over is the only common point.
- Dark theme with limited, reserved colour for operators: [Extron](https://www.extron.com/article/environconhumanfact), [UXmatters](https://www.uxmatters.com/mt/archives/2017/08/ux-for-the-industrial-environment-part-1.php). Colour alone is not enough: [control.com](https://control.com/technical-articles/human-factor-engineering-designing-an-intuitive-hmi/).
- Library: faceted search and proxies ([Iconik](https://www.iconik.io/blog/digital-asset-management-workflows-what-to-streamline-first)); transcript and time-based metadata as panels on the asset page ([Iconik help](https://help.iconik.backlight.co/hc/en-us/articles/25303835740439)); review comments as timeline markers ([Frame.io](https://helpx.adobe.com/premiere-pro-next/export-and-share/collaborate-with-others/import-comments-as-markers.html)).
- J/K/L transport is shared by Resolve and Avid ([ProVideo Coalition](https://www.provideocoalition.com/avid-media-composer-updated-to-2022-7-now-with-competing-keyboard-layouts/)). HH:MM:SS:FF timecode is the display norm.
- Not found: EBU or ITU guidance on tally colours or control-room UI, a definition of "floated story", and any detail of vMix or Avid rundown layouts beyond the above. The red/green/amber scheme is convention.

## Delivery

The hook contract: element `id`s, `data-screen` and other `data-` attributes, and `aria-*` roles are kept. Class names may change. `tests/test_operator_ui.py` pins some markup strings (for example the Library nav link) and the CSP rules (no inline `style=`, `<style>` or inline `<script>`); those tests are updated in the slice that changes the markup, and the CSP rules stay. An inline SVG sprite in `index.html` is allowed by them.

Slices, each ending in screenshots from a real browser against the operator harness at 1920, 1366 and 768 wide, an axe-core run, and the existing suite:

1. Tokens, icon sprite, components, shell (header, rail, workspace switcher, footer).
2. Show screen for Operator and Producer: rundown with front time and over/under, program, controls, cues, build panel.
3. Library for Editor, including its Export tab ("Selection to export").
4. Publish, Rights, Record.
5. Contrast table, updated `docs/mockups/*.png`, `operator-ui.md` and `brand-guide.md` edited to match, CHANGELOG.

## Left to the builder

Exact icon drawings (the first pass is a start); how the icon sprite is served (inline in `index.html` or a same-origin SVG file; both fit the CSP); the type scale and spacing scale; how the 2 s state poll feeds the front-time calculation; whether J/K/L can be added to the Library player without clashing with existing keys (check first, and skip it if they clash).

## Calls I made, for the owner to check

- Workspace names Producer / Editor / Operator, and the rail contents in the table above. "Operator" is also an access level in the token system, so the user pill shows access as "operator" or "admin" next to a workspace named "Operator". If that confuses, rename the workspace.
- Dark only, no light theme.
- Selection is amber, not blue.
- Back time deferred.
- Workspaces filter the rail only; every screen stays reachable by URL. If the hash names a screen outside the workspace's rail, the workspace stays and that screen appears as a temporary highlighted rail item.
- All workspaces share one DOM. A workspace is a CSS class on the page that changes the rail, grid proportions and which panels start open; there are no per-workspace templates.
- Drift, not slot over/under, for now (above).

## Open questions

1. **Where is the CoreVideo widget sheet** the brand guide says to build on (CoreVideo Pro repo, a file, a Figma)? The builder needs it to match the family. Without it the system is built from the existing `operator.css` tokens alone.
2. **Slot length and back time:** do you want a show target (a planned length or end time, a schema and API change, later) so the header can show total against slot and the rundown can show back time? Or is front time plus drift enough for now?
