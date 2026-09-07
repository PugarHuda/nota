# Submission draft (fill the blanks when the organiser sends the repo and key)

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
| demo_video_url | https://nota-ryo.vercel.app/demo.mp4 |
| hosted demo | https://nota-ryo.vercel.app (read-only ledger snapshot) |
| x_post_url | _(TBD)_ |
| submission form | `docs/project-submission-form.md` + `.pdf` (official link broken) |
| agree_rules / confirm_no_secrets | yes / yes (`.env` is gitignored, `.env.example` has no values) |

## project_description (≤ 1000 chars, paste as is)

Nota makes an AI trading opinion auditable. Every decision is a receipt you can re-run:
`nota replay <id>` rebuilds it from the stored evidence and the model outputs cached beside it,
keyed by the model that produced them, and prints identical: true - no API key, no network, no
drift. Each cited number carries its dotted RYO path and is read back out of the evidence, never
retyped by the model; citations pointing at absent evidence are dropped in code; null is never
turned into 0. Independent sources audit RYO itself - exchange medians check its price, Wilder
RSI/ATR recomputed from public OHLC check its indicators - and the judge refuses to size a trade
when they disagree. Three specialists (macro, technician, narrative) debate the five tools a
decision reads; the sixth, scan_market, drives the `nota scan` funnel. Track 3: four skills on
RYO's own /api/skills paths. Track 2: a dashboard that diffs each receipt against the last, ranked
by impact. Read-only; no orders.

## X post (draft)

> Built Nota for the @ryodigital #RYOCHAN hackathon: an AI council over live RYO market
> evidence where every call is a receipt you can re-run and get identical output - and it audits
> RYO's own price and RSI against independent sources, refusing to trade when they disagree.
> Diff-first dashboard + 4 skills. https://nota-ryo.vercel.app/demo.mp4

(268 characters with a 23-character link. Lead with "re-run it and get the same answer" - the
council itself is a commodity in 2026, the verification is not.)

## Demo video script (≈ 3 min)

Recordable today without the builder key: every RYO section comes from the labelled fixture source,
which the receipt and the dashboard both say out loud. Swap `--source fixture` for a live run and
re-record the moment the key lands; nothing else in the script changes.

1. (0:00) One line: what a "decision receipt" is. `uv run nota health` — MCP health is `ok` with
   six tools, `ryo_key_set: false`. Name the constraint instead of hiding it.
2. (0:20) `uv run nota decide SOL --source fixture`. Point at the availability block: a partial
   section, a warning, a null that stayed null, `data_mode` carried per section.
3. (1:00) Council: three opinions with dotted-path citations; a dropped citation. Judge rationale.
   Practice trade: stop = 2×ATR, size from 1% risk. "No order was placed."
4. (1:40) `uv run nota replay <id>` → identical: True.
5. (2:00) `NOTA_DB=data/demo.db uv run nota serve`: dashboard, "what changed" ranked by impact,
   degraded banner, j/k/Enter, permalink, open positions, leaderboard.
6. (2:30) Live, keyless, right now: `uv run nota skill run price_crosscheck '{"symbol":"SOL"}'`,
   `technicals_crosscheck`, and `narrative_convergence '{"voices":["tg:WatcherGuru"]}'` — real
   exchanges, real Telegram, RYO envelope shape, per-voice availability, method named.
7. (2:55) Close: read-only, practice trades only, not financial advice.

`uv run python scripts/demo_video.py` records the whole walkthrough with on-screen captions (~2 min, `docs/demo/dashboard-<ts>.webm`/`.mp4`, gitignored): summary, no-trade guard, keyboard nav, ranked diff, verify-replay, council citations, provenance, a live `narrative_convergence` run, positions and the leaderboard. Nothing is staged - it drives the real page against the shipped ledger snapshot. The terminal steps (1-4 above) are still screen-recorded by hand if you want them; the captioned dashboard video stands alone as the submission video otherwise.

## Help Desk message (paste into https://discord.gg/qkWPjxzxtC)

> Hi team - Pugar Huda Mantoro (Discord: Lynx / hajislamet, GitHub: PugarHuda), project "Nota",
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
> Demo running now with no key at all: https://nota-ryo.vercel.app - thanks!

Status 2026-09-06 13:57 UTC: no reply, no key, no repo DM (inbox checked). `/api/mcp/health` is up
(`tools: 6`); `/api/hackathon/config` is byte-for-byte unchanged.

## What the organiser asks for (Discord, #submit-your-buidl)

- [x] Documentation in the repository explaining the project, its purpose, features and how it works
      (README, `docs/skills/SKILL-SPEC.md`, `docs/HACKATHON-ANALYSIS.md`)
- [x] All final code on the main branch of the provided repository
      (`RYO-Digital/ryochan-hackathon_repository-235`, remote `organiser`)
- [x] No secret in the repository **or in the git history**, which they say they review: 49 commits
      and 448 blobs scanned against twelve credential patterns, one match and it is the placeholder
      inside RYO's own builder guide
- [x] Third-party code and resources disclosed in the README
- [x] Track stated (1, 2 and 3)
- [ ] **Application Form downloaded from the #participant-setup-process channel**, filled in and
      committed. `docs/project-submission-form.md` + `.pdf` in this repository is a reconstruction
      from the platform's `HackathonSubmissionFields` schema, made because the public link
      (`ryobuild.com/project-submission-form.pdf`) serves the site's SPA shell rather than a PDF.
      The organiser's own template is only in Discord, so it has to be downloaded there and
      committed; that is the one submission item this repository cannot produce for itself.
- [ ] Demo video uploaded to a cloud service with an accessible link (and its password, if any).
      The project already serves it at `/demo.mp4` with no password, which satisfies "any similar
      service"; a Drive or Dropbox copy is the safer reading of their wording.
- [ ] Submitted with `/apply` in #submit-your-buidl, entering the repository name

## Pre-flight checklist

- [x] Pushed into the organiser's repo 2026-09-07 (invited by @johnzenza; it arrived empty):
      all 39 commits, 87 files at `f5771e7`, including `docs/project-submission-form.pdf` and the
      bundled walkthrough. `.env` is absent, only `.env.example`. Remote `organiser`; re-push with
      `git push organiser main`.

- [x] `uv run pytest -q` green (164 passed, 2026-09-07: unit, API, MCP server and resources, X syndication, public-surface hardening, and 13 browser QA checks)
- [x] `git grep -nE "ryo_mcp_[A-Za-z0-9]|VENICE_INFERENCE_KEY_|sk-or-v1-|tvly-[A-Za-z0-9]"` returns
      only doc placeholders and test doubles (re-checked 2026-09-07)
- [x] Project Submission Form committed: `docs/project-submission-form.md` + `.pdf`
      (`uv run python scripts/submission_pdf.py` regenerates it) - official link still broken
- [x] README "Disclosed third-party libraries" matches `pyproject.toml` (2026-09-06)
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
- [x] Demo video bundled at `nota/static/demo.mp4` and served at `/demo.mp4`; `demo_video_url`
      filled in both files and live
- [ ] Optional: mirror the same file on YouTube if the judges prefer a player
- [ ] X post published (tag @ryodigital), `x_post_url` filled in both
