# Submission draft (fill the blanks when the organiser sends the repo and key)

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
| agree_rules / confirm_no_secrets | yes / yes (`.env` is gitignored, `.env.example` has no values) |

## project_description (≤ 1000 chars, paste as is)

RYO Arena is a council of specialised AI agents (macro, technician, narrative) that debates live
RYO evidence from all six builder tools, lets a judge weigh them by each agent's historical Brier
score, sizes a practice trade with pure ATR math, and records everything as a replayable decision
receipt. Every number carries its RYO path, as_of, data_mode and trace id; null is never turned
into 0; citations that point at missing evidence are dropped in code; an independent exchange
price check flags stale or wrong prices before any sizing. `arena watch` runs it autonomously and
publishes receipts to Telegram/Discord. Track 2: a keyboard-first, accessible dashboard that diffs
each receipt against the previous one, ranks what changed by impact, shows degraded evidence, and
verifies replay from the page; receipts share as Open Graph cards and anyone can back a call, scored
when it resolves. Track 3: three RYO-shaped skills (narrative_convergence, news_verify with dated RSS
corroboration, price_crosscheck), all returning the RYO envelope. Read-only; no orders.

## X post (draft)

> Built RYO Arena for the @ryodigital #RYOCHAN hackathon: a council of AI agents that argues over
> live RYO market evidence, sizes a practice trade with pure ATR math, and stores every decision as
> a replayable receipt you can audit line by line. Diff-first dashboard + 2 new skills. [video link]

## Demo video script (≈ 3 min)

1. (0:00) One line: what a "decision receipt" is. Show `uv run arena health` (whoami, quota).
2. (0:20) `uv run arena decide SOL` live. Point at the availability block: a partial section, a
   warning, a null that stayed null.
3. (1:00) Council: three opinions with dotted-path citations; one dropped citation. Judge rationale.
   Practice trade: stop = 2×ATR, size from 1% risk. "No order was placed."
4. (1:40) `uv run arena replay <id>` → identical: True. `--fresh` → drift printed honestly.
5. (2:00) `uv run arena serve`: dashboard, "what changed" ranked, degraded banner, j/k/Enter,
   permalink, open positions, leaderboard.
6. (2:40) `uv run arena skill run narrative_convergence '{"voices":["tg:WatcherGuru"]}'` and
   `news_verify`: RYO envelope shape, per-voice availability, method named.
7. (2:55) Close: read-only, practice trades only, not financial advice.

## Pre-flight checklist

- [ ] `uv run pytest -q` green
- [ ] `fixtures/recorded/` captured with `uv run arena record SOL` (real schema), `paths.py` trimmed
- [ ] `git grep -n "ryo_mcp_\|VENICE_INFERENCE\|sk-or-"` returns only `.env.example` placeholders / docs
- [ ] Project Submission Form PDF committed (ask Help Desk for the working link)
- [ ] README "Disclosed third-party libraries" up to date
