# Submission notes

Submission platform: DoraHacks BUIDL (per organiser's Discord pin, 17 Aug 2026). The X post URL must be
added to the BUIDL before judging for the Social Media award; tag @ryodigital, show what was built,
explain why it matters, link the demo (https://nota-ryo.vercel.app).

Field values for the Project Submission Form / `HackathonSubmissionFields`.

| field | value |
|---|---|
| team_name | Nota |
| participant_name | Pugar Huda Mantoro |
| email | hudapugar@gmail.com |
| discord_id | Lynx (hajislamet) |
| github_username | PugarHuda |
| project_name | Nota |
| tracks | track_1, track_2, track_3 |
| repo_url | https://github.com/RYO-Digital/ryochan-hackathon_repository-235 |
| demo_video_url | https://nota-ryo.vercel.app/demo (page with transcript) - bare file: /demo.mp4 |
| hosted demo | https://nota-ryo.vercel.app (ledger snapshot committed by the ledger cycle; backings live on Neon) |
| x_post_url | _(TBD)_ |
| submission form | `docs/project-submission-form.md` + `.pdf` (official link broken) |
| agree_rules / confirm_no_secrets | yes / yes (`.env` is gitignored, `.env.example` has no values) |

## project_description (≤ 1000 chars, paste as is)

Nota makes RYO's research auditable. An AI council argues over live RYO evidence and stores each
call as a receipt: `nota replay <id>` rebuilds it offline and prints identical: true. Every cited
number is read back from its RYO path; null never becomes 0. It audits RYO too. Its Verdict
Scorecard locks deep_analysis verdicts and trade plans for 25 majors daily and settles them on
OKX candles, asking whether a verdict changes how often RYO's own plan works (day 0: 7 of 25 plans
were long under a "cautious" verdict). A gate stops agents citing RYO derivatives fields that are
not about the token: one OI figure repeated across unrelated tokens. Each agent is scored against
the token's own base rate, not a coin flip. Track 1: council, gate, receipts. Track 2: diff
dashboard and /scorecard. Track 3: nine skills in RYO's envelope, also an MCP server. Read-only;
no orders.

## X post

Figures read from https://nota-ryo.vercel.app/api/scorecard on 2026-09-20 (both horizons): 50 plans
locked over 2 lock days, none settled yet, 7 plans long while RYO's own verdict was cautious. Re-read
them before posting; the page moves every hour.

> RYO's deep_analysis gives a verdict and a trade plan. It never tells you what became of either.
>
> For @ryodigital's #RYOCHAN hackathon I built Nota, which keeps that record: every day it locks
> RYO's verdict and plan for 25 majors, anchors each lock in Bitcoin (OpenTimestamps) and settles
> it on OKX candles at 24 h and 72 h. 50 plans locked so far; 7 of them long under RYO's own
> "cautious" verdict.
>
> Its AI council reads the same evidence through a derivatives gate: a RYO field repeated across
> unrelated tokens cannot be cited. Every call is a receipt that replays identically, offline.
>
> 9 research skills in RYO's envelope, over REST, MCP and A2A. Read-only, no orders.
>
> https://nota-ryo.vercel.app/scorecard

The walkthrough (`nota/static/demo.mp4`) is attached as an upload so it plays in the timeline; the
link goes to the scorecard, and `/demo` holds the same video with its transcript.

## What the organiser asks for (Discord, #submit-your-buidl)

- [x] Documentation in the repository explaining the project, its purpose, features and how it works
      (README, `docs/skills/SKILL-SPEC.md`, `docs/HACKATHON-ANALYSIS.md`)
- [x] All final code on the main branch of the provided repository
      (`RYO-Digital/ryochan-hackathon_repository-235`, remote `organiser`)
- [x] No secret in the repository **or in the git history**, which they say they review: on
      2026-09-20 every commit's patches were searched for the exact value of every local key and
      for thirteen credential patterns; the one match is the placeholder inside RYO's own builder guide
- [x] Third-party code and resources disclosed in the README
- [x] Track stated (1, 2 and 3)
- [x] **Application Form**: the organiser's own template downloaded 2026-09-07 from
      `https://ryobuild.com/RYOCHAN-Hackthon-Project-Submission-Form.pdf` (note their spelling of
      "Hackthon", which is why every earlier guess at the URL missed) and committed unchanged as
      `docs/RYOCHAN-Hackathon-Project-Submission-Form-BLANK.pdf`. It carries no interactive fields,
      so `docs/project-submission-form.md` fills the same sections in the same order (Members,
      Overview, Tech Stack, Repository / Demo, Testing Information) and
      `scripts/submission_pdf.py` renders it to `docs/project-submission-form.pdf`.
- [ ] Demo video uploaded to a cloud service with an accessible link (and its password, if any).
      The project already serves it at `/demo` (page, transcript) and `/demo.mp4` (bare file) with
      no password, which satisfies "any similar service"; a Drive or Dropbox copy is the safer
      reading of their wording.
- [ ] Submitted with `/apply` in #submit-your-buidl, entering the repository name

## Pre-flight checklist

- [x] First push into the organiser's repo 2026-09-07 (invited by @johnzenza; it arrived empty).
      `.env` is absent, only `.env.example`. Remote `organiser`.
- [ ] Final code synced: `git push organiser main`, then `git ls-remote organiser main` must print
      the same commit as `git rev-parse HEAD` on `origin/main` once `test` and `deploy` are green.

- [x] `uv run pytest -q` green, and `.github/workflows/test.yml` runs it on every push with the
      browser tests; production deploys only after it passes on `main`
- [x] `git grep -nE "ryo_mcp_[A-Za-z0-9]|VENICE_INFERENCE_KEY_|sk-or-v1-|tvly-[A-Za-z0-9]"` returns
      only doc placeholders and test doubles (re-checked 2026-09-07)
- [x] Project Submission Form committed: `docs/project-submission-form.md` + `.pdf`
      (`uv run python scripts/submission_pdf.py` regenerates it) - official link still broken
- [x] README "Disclosed third-party libraries" matches `pyproject.toml` (2026-09-08), and the
      walkthrough toolchain is disclosed with its licence: edge-tts (GPL-3.0) and Remotion
      (Remotion License, free at this team size) build the video and ship no code into the app
- [x] Hosted demo live and public at https://nota-ryo.vercel.app (2026-09-07): `/api/health`,
      `/api/decisions`, `/api/skills/`, `/demo.mp4` (4.2 MB video/mp4) and the receipt card PNG all
      answer 200 with no SSO redirect, the page title is `Nota`, and `price_crosscheck` invoked
      through the hosted skill endpoint returned three exchanges (median $105.44) - Vercel reaches
      Coinbase and Kraken, which this developer's ISP blocks. https://ryo-arena.vercel.app serves
      the same build as a fallback.
- [x] Builder key issued 2026-09-07 (batch 2, expires 2026-12-17, 60/min). `nota record SOL` captured
      all six tools into `fixtures/recorded/` (committed, no key material) and `paths.py` now holds
      RYO's real field names: every guessed ATR and RSI path was wrong, and RYO reports ATR both as a
      percentage and in dollars - see the commit for why that distinction matters on BTC.
- [x] Four live receipts (SOL / BTC / ETH, 2026-09-07) replace the fixture snapshot in
      `data/demo.db`; all four replay `identical: True`.
- [x] Narrated demo video (3:04, AI voice, on-screen cursor and highlight box) bundled at
      `nota/static/demo.mp4`, served at `/demo.mp4` and at `/demo` with a clickable transcript
      generated from the recorder's own offsets; `demo_video_url` filled in both files and live
- [ ] Optional: mirror the same file on YouTube if the judges prefer a player
- [ ] X post published (tag @ryodigital), `x_post_url` filled in both
