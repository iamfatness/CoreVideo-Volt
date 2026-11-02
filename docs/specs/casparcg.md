# CasparCG adapter (`casparcg`)

Status: built against a fake server. **Unvalidated: no real CasparCG server has been used.** Code: `control/volt/adapters/caspar.py`. Tests: `control/tests/test_caspar.py`. Part of [device control](../superpowers/specs/2026-11-01-device-control-design.md).

## Configuration

`VOLT_CASPAR_HOSTS=name=host[:port],...` (default port 5250). Nothing set means the adapter is not registered, nothing is polled or sent, and no CasparCG command is offered. `VOLT_CASPAR_INFO_LAYOUT=<name>` names the INFO reply layout (below); unset means the adapter fails closed.

## Source read

"AMCP Protocol", CasparCG/help wiki on GitHub (`https://github.com/CasparCG/help/wiki/AMCP-Protocol`), page last edited 20 March 2026 (17 revisions). The page says the wiki is no longer maintained and has moved to casparcg.com/docs/wiki; that address answered 404 when tried. Read through a page-to-text tool, not byte for byte.

Relied on, as the page states them:

- Each command ends with CR LF.
- Replies: `202 <CMD> OK` done; `201 <CMD> OK` then one data line; `200 <CMD> OK` then data lines and a closing blank line; `400` not understood, `401`-`404` client errors, `500`-`503` server errors.
- `LOADBG [channel]-[layer] [clip] ...`, `PLAY [channel]-[layer] {[clip]}`, `STOP [channel]-[layer]`, `CLEAR [channel]-[layer]`, `INFO [channel]-[layer]`, `VERSION`.
- `PLAY` "moves the clip from background to foreground and starts it".

## Not confirmed, so not built

- **The XML (or other text) that `INFO <channel>-<layer>` returns.** The page gives the command and no sample, and notes nothing about differences between 2.3 LTS and 2.4/2.5. Without it a cue cannot be proved. The adapter therefore takes the reply through a named layout parser (`caspar.LAYOUTS`) and ships **none**. With no layout configured every cue fails before any command is sent ("INFO layout ... is not configured"). Adding a layout needs a captured `INFO 1-10` reply, with the layer idle, with a clip in the foreground, and with one in the background, from the customer's server version.
- **CG commands** (`CG ADD/PLAY/UPDATE/STOP`) and **template data**. The page gives the syntax but says only that data is "inline XML or a reference to a saved dataset", with no element names, and nothing on how the template host shows up in `INFO`. `caspar.cg.*` and `render_template_data` are not built. Without them "template playing" cannot be told from "template added".
- The quoting and escape rules for clip names. The adapter accepts only letters, digits, space and `_ . / -`, quotes a name that has a space, and rejects anything else (a quote, backslash or line break would let a cue write a second AMCP command).
- OSC as an alternative observation. Not evaluated; INFO polling is the only path built.

## Commands

| Command | Sends | Idempotent | On air | Fired when |
| --- | --- | --- | --- | --- |
| `caspar.load` | `LOADBG ch-layer clip` | yes | no | background is the clip |
| `caspar.play` | `PLAY ch-layer clip`, or bare `PLAY ch-layer` after a load | **no** | yes | foreground is the clip (or the earlier background) on two reads |
| `caspar.stop` | `STOP ch-layer` | yes | yes | foreground is empty on two reads |
| `caspar.clear` | `CLEAR ch-layer` | yes | yes | both foreground and background are empty on two reads |

Payload: JSON `{"server": "G", "channel": 1, "layer": 10, "clip": "NEWS/OPEN"}`; channel defaults to 1, layer to 10, clip is optional for play.

A `4xx`/`5xx` reply is a refusal and final: the cue fails with the server's words and nothing is retried. A play that gets no answer is never sent again; the state is read, and if it does not show the play the cue fails with "sent; no answer; the device does not show it; check it before taking again".

## What the tests prove

Against a fake AMCP server on loopback: the CRLF framing and the 202/201/200/4xx replies as read above; that a `202` with no change is `failed`; that a bare play is sent once; that the cue fails closed with no layout configured; that a hostile clip name cannot add a command; that an unreachable or silent server fails fast. The fake's INFO reply uses a **made-up layout** registered only inside the tests.

## What they do not prove

Anything about a real server: its INFO reply, its version differences, its timing, its behaviour when a channel is busy or a clip is missing. Anything about CG.
