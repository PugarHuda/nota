# 30-second usability review

Pages a Track 2 judge opens, served from `data/demo.db` with `NOTA_READONLY=1` and shot with Playwright at
1440x900 and 390x844 (above the fold and full page). The screenshots are in `docs/demo/ux/before/` and
`docs/demo/ux/after/`, which git ignores. To shoot them again, run a script like the one in the review
session: it starts the app the same way `tests/test_qa_browser.py` does and saves `<page>-<width>-fold|full.png`.

For each page, the three questions are: (a) what is this page for, (b) what is the single most important
thing on it right now, (c) what should I do next? Each answer is judged from the first screen only.

## / (landing)

| | Before | After |
|---|---|---|
| a | Yes: "A trading call you can re-run." with a one-line lede | unchanged |
| b | Yes: the newest receipt (ETH long, 61%) sits beside the headline | unchanged |
| c | Yes: "Open the dashboard", "Watch the demo", "Verify a receipt now" | unchanged |

Changed: nothing. The page already answers all three questions.
Recommendation: the large 0.61 rosette under the receipt pulls the eye as hard as the headline does, and its
caption ("the ticks are its evidence hash...") explains decoration rather than the call. A judge reads it
before the buttons. Consider making it smaller, or moving it below the fold at 1440.

## /app (dashboard, newest receipt open)

| | Before | After |
|---|---|---|
| a | Partly: the tagline says "Council decisions on read-only RYO evidence" | "One receipt per AI-council trading call on RYO evidence, re-runnable on demand." |
| b | Desktop: yes (headline and five-line summary). Phone: **no**. The status badges plus the whole ledger list fill the first screen, and the receipt starts below it | Phone: the receipt is the first thing under the header, and the list follows it |
| c | Partly: "Biggest change" led with the dotted path `verdict.action`, and the list showed `p_up 0.61` | "Biggest change: council verdict flipped: no_trade → long (verdict.action)"; rows read "61% up in 7d" |

Changed:
- Up to 1100px wide, `#detail` is visually first (`order:-1`). "Skip to receipt" is still the first tab stop, and tapping a row still scrolls to the receipt.
- In "What changed", the first column now leads with the plain-language reason. The dotted RYO path sits under it in small type. The header changed from "path" to "change".
- Wherever the dashboard showed `p_up` (list rows, council badges, the screen-reader status line), it now shows a percentage such as "61% up in 7d".

Recommendations:
- The four model/key/health badges and the keyboard hints take about 40% of a phone's first screen. Folding them into a `<details>` on narrow widths would bring the verdict up further.
- The skill runner's long description in the right column competes with "Needs attention". Collapsing it by default would help.
- "Next: … (`nota resolve --all`)" puts a CLI command in the summary. It could go behind a tooltip.

## /scorecard

| | Before | After |
|---|---|---|
| a | The question is the h1, but nothing said what a "plan" is | A one-line hint: RYO attaches a stop and target to every verdict; Nota locks the plans daily and settles them on OKX candles |
| b | The answer ("not yet distinguishable") came last, after `+1R` and `p = 0.0298` | The paragraph opens with **Not yet:** and says it plainly (CONFIRMED plans hit their target 8.6 points more often than MIXED; not yet distinguishable). The counts follow |
| c | Horizon toggle, then the tables | unchanged |

Changed:
- `answer()` now puts the verdict first and the backing counts after it.
- `+1R` is explained the first time it appears.
- Fixed a merge-mangled nav script. The Escape and click-away handlers had been pasted five times over, so each fired several times per key or click. The same fix was applied to `/demo`.

Recommendation: "Settling next" comes before "Does the verdict matter?". For a reader who wants the argument, the
contrast blocks are the evidence for the headline answer and could come straight after it.

## /judges

| | Before | After |
|---|---|---|
| a | Yes, from the lede | unchanged |
| b | **No.** Every Track 1/2/3 step rendered one word per line (landing.css's `ol.steps` grid split the inline text into grid cells), and the full page was 11,768px tall | Steps read as normal paragraphs, with the number hanging in the margin |
| c | No quick path: a judge had to read three tables | "One minute, three clicks": dashboard plus Verify replay, the scorecard's first paragraph, and the drill's *RYO down* |

Changed: a local `ol.steps li` override, the three-click list, and the menu now closes on Escape or a click elsewhere, as on the other pages.

## /kol

| | Before | After |
|---|---|---|
| a | Yes, from the lede | unchanged |
| b | It is a form, but the voices label was broken: each `<code>` example sat on its own grid row, one comma per line | The label reads on one line |
| c | Unclear: the run button is below the fold and nothing pointed to it | "Try it: three news channels are already filled in; press *Run the agent*…" |

Changed: wrapped the label text in a `<span>`, and "Minimum |sentiment|" now reads "Minimum sentiment strength, either way".

Recommendation: the read-only demo has no stored KOL run (`/api/kol` returns `[]`). Until a judge presses Run, the
page has no result to show. Shipping one stored run in `demo.db`, and rendering the newest one on load, would
answer (b) without a click.

## /feed

| | Before | After |
|---|---|---|
| a | Yes, from the lede | unchanged |
| b | Newest call, titled "ETH: long, p_up 0.61"; the timestamp read "2026 09 30T21:04:28+00:00" (the hyphens drop out in that font at 13px) | "ETH: long, 61% chance higher in 7 days"; "Sep 30, 21:04 UTC" |
| c | Share / Card / Back or disagree links | unchanged |

Changed: the agents' `p_up` became "NN% up", and the date became a plain UTC date.

Recommendation: the judge's own rationale is model text and still says `p_up_7d=0.65`. That comes from the prompt and is not rewritten here.

## /drill

| | Before | After |
|---|---|---|
| a | Yes, from the lede | unchanged |
| b | Nothing until a button is pressed (by design). At 1401px and wider, **the menu and the theme switch were invisible**: the summary is hidden there and nothing opened the menu | The menu is held open when wide, as on every other page |
| c | "No drill run yet. Each button runs one." | "Press any failure above. Each runs offline in a few seconds and shows its receipt beside a healthy one." |

Changed: added the shared nav script. The judge line in each column now reads "long, 60% chance higher in 7 days" instead of `p_up_7d 0.6`.

## Tests

- `test_wide_the_menu_is_held_open_on_every_page_so_its_links_stay_reachable` is new. It checks the drill bug on all eight pages.
- `test_the_folded_menu_closes_on_escape_and_on_a_click_elsewhere` now also covers `/judges`, `/drill` and `/kol`.
- No existing assertion needed rewording. The full suite (590 tests, including the axe, overflow and keyboard checks) passes.

## Left as they are (visual identity)

The 採点 seal, the gold corner ornaments, the 壱/弐 section seals and the 大福帳 ledger tag are all kept. Only the
landing's rosette (see above) competes with the main message enough to be worth reconsidering.
