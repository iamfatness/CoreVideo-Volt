# UI slice 1: measured contrast

WCAG relative-luminance ratios of the `operator.css` colour tokens, computed by `tests/test_operator_ui.py::Tokens` (which fails if any pair drops below its minimum). Measured 2026-10-31.

| Foreground | Background | Ratio | Minimum |
|---|---|---|---|
| text `#e2e2e2` | ground `#141414` | 14.22 | 4.5 |
| text `#e2e2e2` | panel `#1c1c1c` | 13.16 | 4.5 |
| muted `#a0a0a0` | panel `#1c1c1c` | 6.52 | 4.5 |
| signal `#f0b429` | panel `#1c1c1c` | 9.14 | 3 |
| air `#ff4d4d` | panel `#1c1c1c` | 5.21 | 3 |
| ok `#3ddc97` | panel `#1c1c1c` | 9.64 | 3 |
| blue-hi `#5ea3ea` | panel `#1c1c1c` | 6.41 | 3 |

axe-core 4.10.2 in headless Chromium against the `normal` harness scenario: no violations on the Show screen in each of Producer, Editor and Operator at 1920 and 768 wide, and no horizontal page scroll at either width.
