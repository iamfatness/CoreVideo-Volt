# Brand guide

Status: established product direction. The four-slash mark, semantic palette, and operational voice are the shared system.

## Name

Volt. Family line CoreVideo Volt. Say Volt in the product, on comms, and on the box. CoreVideo Volt on a price sheet only.

Do not say Call. vMix Call is a guest return, and CoreVideo already has guest inputs. Do not say Desk, Bin, Control, Media, Orchestra, or MOS.

## What the name has to survive

A TD saying it on comms. A buyer who has never logged a show. A search result that is not a battery, a protocol, or a video call.

## Color

Volt extends the CoreVideo control-room sheet. It does not invent a second product palette.

| Role | Hex | Use |
|---|---|---|
| Ground | `#141414` | Application background |
| Panel | `#1C1C1C` | Surfaces |
| Hairline | `#303030` | Borders |
| Text | `#E2E2E2` | Primary type |
| Muted | `#A0A0A0` | Secondary type |
| Suite blue | `#1D6DC2` | CoreVideo family actions, links, focus |
| Signal | `#F0B429` | Volt accent. Next, selected span, publish |
| On air | `#FF4D4D` | Only a live path. Never decoration |
| Cleared | `#3DDC97` | Rights and ready, used sparingly |

Signal amber is the Volt color. Suite blue stays the CoreVideo color. Red is a tally, not a brand.

## Type

Geometric grotesque for the wordmark. Product UI uses the system UI font CoreVideo already uses. No display serif. No condensed sports font.

## Voice

Short. Operational. "The clip is in Volt." Not "unlock your media supply chain."

## Logo

Locked direction: four amber slashes, then VOLT.

The mark is four short parallel strokes, leaning forward, in signal amber `#F0B429`, set before the word. White geometric VOLT. Charcoal ground. No bolt. No ring. The slashes are the tally and the voltage, without drawing a lightning cliché.

- Wordmark: [brand/logos/lockup-slashes.svg](../brand/logos/lockup-slashes.svg)
- Mark only, for the app icon: [brand/logos/mark-slashes.svg](../brand/logos/mark-slashes.svg)

Rules: always four. Same angle, same gap, same weight. Do not add a fifth, do not connect them, do not turn them into a Z. At icon size the four strokes stand alone. Beside the word they sit left of the V, optically centered on the cap height.

Earlier passes, not the logo: a bolt V, a single tally tick, and an O drawn as a ring. Kept in `brand/logos/` as rejected studies.

## UI direction

Dark control room. One session, three proofs.

- Show. Rundown, program, cues. On air is red. Next is amber. [brand/ui/show-session.jpg](../brand/ui/show-session.jpg)
- Library. Search returns spans with transcript hits, not files. [brand/ui/library.jpg](../brand/ui/library.jpg)
- Publish. One span, three deliverables, one button. [brand/ui/publish.jpg](../brand/ui/publish.jpg)

The UI now follows one system: tokens and components in `operator.css`, a 38-icon sprite (24px grid, 1.75px round stroke), and three workspaces (Producer, Editor, Operator) in a shared shell; see [operator-ui.md](specs/operator-ui.md) and the [redesign design](superpowers/specs/2026-10-30-ui-redesign-design.md). These are direction frames, not spec. The next pass should be built in the CoreVideo widget sheet, not a separate web theme.

## Light theme

A light set exists for bright rooms and is chosen with the header's Theme control (Auto follows the operating system). Dark stays the default and the brand's home. In light, ground is `#F3F3F0`, panel `#FFFFFF`, text `#1A1A1A`. Suite blue stays `#1D6DC2` for fills (white text, 5.2:1); text and focus use `#14589F`. Signal amber stays `#F0B429` as a fill or bar with `#1A1200` text; amber used as text becomes `#8A5A00`. On-air red stays `#FF4D4D` as a fill; as text it becomes `#C01F1F`. Cleared green text becomes `#0B7A48`. Red is still only for a live path. Every pair is measured by a test, not judged by eye (see [operator-ui.md](specs/operator-ui.md)).

## Do not

Lightning photography. Sports clipart. Gradients behind the wordmark. Red as a brand color. A second blue.

## Interface rules

The operator application is a task surface. Apply Impeccable's Operate guidance: precision, legibility, familiar controls, and restrained brand expression. Reference: [Impeccable](https://github.com/pbakaus/impeccable), `skill/SKILL.src.md` and the polish, operate, and craft-floor references (reviewed November 9, 2026).

- Keep the four-slash VOLT lockup in the header. The browser title carries the full CoreVideo Volt family name.
- Use one system sans family. Screen titles use 1.75rem, section titles .875rem semibold, body 1rem, and supporting data .875rem. Use monospace for clocks, timecodes, and identifiers only.
- Use the existing 4/8/12/16/24/32px spacing scale. Screen margins are 24px on desktop and 12px on phones. Section headers and comments use 16px insets.
- Show a visible title on every screen. Keep navigation labels visible. On phones, navigation uses a horizontal strip with a visible current screen, workspace controls retain their text, and More opens secondary header controls.
- Keep live red, next amber, action blue, and cleared green meanings consistent. Pair state color with a word or icon. Errors and destructive actions also use the existing danger treatment.
- Give rundown titles priority over row tools. In Operator, place actions and timecode detail in the selected item panel. Keep timing columns aligned and let secondary times yield on phones. Producer retains inline actions.
- Group On Air, Next, timing and TAKE in the Operator console. Pair TAKE with its availability and any reason it is disabled. Keep Hold and Advance, no cues on the secondary strip. On phones, the four console areas reflow to two columns.
- Keep the existing light, dark, and Auto choices. Do not add remote fonts or component dependencies for visual polish.

Future changes should be checked in both themes at phone, laptop, and wide desktop sizes, with keyboard focus and permission-limited states. Palette contrast tests live in `control/tests/test_operator_ui.py`; theme tokens live in `control/volt/session/operator/operator.css`.
