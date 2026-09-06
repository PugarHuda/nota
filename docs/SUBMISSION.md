# Submission draft (fill the blanks when the organiser sends the repo and key)

Submission platform: DoraHacks BUIDL (per organiser's Discord pin, 17 Aug 2026). The X post URL must be
added to the BUIDL before judging for the Social Media award; tag @ryodigital, show what was built,
explain why it matters, link the demo (https://ryo-arena.vercel.app).

Field values for the Project Submission Form / `HackathonSubmissionFields`.

| field | value |
|---|---|
| team_name | RYO Arena |
| participant_name | Pugar Huda Mantoro |
| email | hudapugar@gmail.com |
| discord_id | Lynx (hajislamet) |
| github_username | PugarHuda |
| project_name | RYO Arena |
| tracks | track_1, track_2, track_3 |
| repo_url | _(private repo from organiser; current mirror: https://github.com/PugarHuda/ryo-arena, private)_ |
| demo_video_url | _(TBD)_ |
| hosted demo | https://ryo-arena.vercel.app (read-only ledger snapshot) |
| x_post_url | _(TBD)_ |
| submission form | `docs/project-submission-form.md` + `.pdf` (official link broken) |
| agree_rules / confirm_no_secrets | yes / yes (`.env` is gitignored, `.env.example` has no values) |

## project_description (≤ 1000 chars, paste as is)

RYO Arena is a council of AI agents (macro, technician, narrative) that debates live RYO evidence
from all six builder tools, weights each agent by its Brier score, sizes a practice trade with ATR
math, and stores every decision as a replayable receipt. Every number carries its RYO path, as_of,
data_mode and trace id; null is never turned into 0; citations pointing at missing evidence are
dropped in code; an independent exchange price check flags stale prices. `arena watch` runs
autonomously and publishes receipts to Telegram/Discord. Track 2: a keyboard-first, accessible
dashboard that diffs each receipt against the previous one, ranks changes by impact, shows degraded
evidence, verifies replay in-page, shares receipts as Open Graph cards, and lets anyone back a call,
scored when it resolves. Track 3: four RYO-shaped skills (narrative_convergence, news_verify with
dated RSS corroboration, price_crosscheck, technicals_crosscheck), all returning the RYO envelope.
Read-only; no orders.

## X post (draft)

> Built RYO Arena for the @ryodigital #RYOCHAN hackathon: a council of AI agents that argues over
> live RYO market evidence, sizes a practice trade with pure ATR math, and stores every decision as
> a replayable receipt you can audit line by line. Diff-first dashboard + 4 new skills. [video link]

## Demo video script (≈ 3 min)

Recordable today without the builder key: every RYO section comes from the labelled fixture source,
which the receipt and the dashboard both say out loud. Swap `--source fixture` for a live run and
re-record the moment the key lands; nothing else in the script changes.

1. (0:00) One line: what a "decision receipt" is. `uv run arena health` — MCP health is `ok` with
   six tools, `ryo_key_set: false`. Name the constraint instead of hiding it.
2. (0:20) `uv run arena decide SOL --source fixture`. Point at the availability block: a partial
   section, a warning, a null that stayed null, `data_mode` carried per section.
3. (1:00) Council: three opinions with dotted-path citations; a dropped citation. Judge rationale.
   Practice trade: stop = 2×ATR, size from 1% risk. "No order was placed."
4. (1:40) `uv run arena replay <id>` → identical: True.
5. (2:00) `ARENA_DB=data/demo.db uv run arena serve`: dashboard, "what changed" ranked by impact,
   degraded banner, j/k/Enter, permalink, open positions, leaderboard.
6. (2:30) Live, keyless, right now: `uv run arena skill run price_crosscheck '{"symbol":"SOL"}'`,
   `technicals_crosscheck`, and `narrative_convergence '{"voices":["tg:WatcherGuru"]}'` — real
   exchanges, real Telegram, RYO envelope shape, per-voice availability, method named.
7. (2:55) Close: read-only, practice trades only, not financial advice.

`uv run python scripts/demo_video.py` records the whole walkthrough with on-screen captions (~2 min, `docs/demo/dashboard-<ts>.webm`/`.mp4`, gitignored): summary, no-trade guard, keyboard nav, ranked diff, verify-replay, council citations, provenance, a live `narrative_convergence` run, positions and the leaderboard. Nothing is staged - it drives the real page against the shipped ledger snapshot. The terminal steps (1-4 above) are still screen-recorded by hand if you want them; the captioned dashboard video stands alone as the submission video otherwise.

## Help Desk message (paste into https://discord.gg/qkWPjxzxtC)

> Hi team - Pugar Huda Mantoro (Discord: Lynx / hajislamet, GitHub: PugarHuda), project "RYO Arena",
> tracks 1 + 2 + 3. Three things I still need before I can submit:
>
> 1. **Builder MCP key.** I registered but have not received a `ryo_mcp_...` key or the private
>    repo DM. Everything RYO-side needs the bearer (`/api/mcp/whoami`, `/api/hackathon/submission`
>    and even `/api/market/*` all answer 401), so I have built against the response contract in the
>    guide with clearly-labelled fixtures and cannot record a single live call until the key lands.
> 2. **Project Submission Form.** `https://ryobuild.com/project-submission-form.pdf` returns HTTP 200
>    with `content-type: text/html` - it is the SPA shell, not a PDF (same for
>    `MCP-Builder-Guide.pdf`; only the `.md` guide is a real file). Could you post a working link?
>    In the meantime I committed a filled copy built from the `HackathonSubmissionFields` schema.
> 3. **Deadline.** The landing page counts down to 8 Sep 23:59 JST but
>    `GET /api/hackathon/config` has said `current_phase: submission_close` with
>    `2026-08-31T23:59:59Z` since August. Which one is binding, and is DoraHacks BUIDL still the
>    submission route?
>
> Demo running now with no key at all: https://ryo-arena.vercel.app - thanks!

Status 2026-09-06 13:57 UTC: no reply, no key, no repo DM (inbox checked). `/api/mcp/health` is up
(`tools: 6`); `/api/hackathon/config` is byte-for-byte unchanged.

## Pre-flight checklist

- [x] `uv run pytest -q` green (109 passed, 2026-09-06)
- [x] `git grep -nE "ryo_mcp_[A-Za-z0-9]|VENICE_INFERENCE_KEY_|sk-or-v1-|tvly-[A-Za-z0-9]"` returns
      only doc placeholders and test doubles (2026-09-06)
- [x] Project Submission Form committed: `docs/project-submission-form.md` + `.pdf`
      (`uv run python scripts/submission_pdf.py` regenerates it) - official link still broken
- [x] README "Disclosed third-party libraries" matches `pyproject.toml` (2026-09-06)
- [x] Hosted demo answers `/api/health`, `/api/decisions`, `/api/skills/` (2026-09-06)
- [ ] `fixtures/recorded/` captured with `uv run arena record SOL` (real schema), `paths.py` trimmed
      - **blocked on the builder key**
- [ ] Demo video uploaded, `demo_video_url` filled in both this file and the form
- [ ] X post published (tag @ryodigital), `x_post_url` filled in both
