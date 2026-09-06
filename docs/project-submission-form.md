# RYO-CHAN Virtual Hackathon 2026 — Project Submission Form

> **Why this file exists.** The organiser's official form is linked from
> `GET https://app-ryochan.com/api/hackathon/config` as
> `https://ryobuild.com/project-submission-form.pdf`, but that URL does not serve a PDF. Checked
> again on 2026-09-06: `HTTP 200`, `content-type: text/html`, 2 034 bytes of the site's SPA shell
> (same for `MCP-Builder-Guide.pdf`; only the `.md` guide is a real file). The Help Desk has been
> asked for a working link. Until it arrives this form reproduces every field of the platform's own
> `HackathonSubmissionFields` schema (`PUT /api/hackathon/submission/draft`), so the committed
> answers match what will be submitted through the API.

| field | value |
|---|---|
| `team_name` | RYO Arena |
| `participant_name` | Pugar Huda Mantoro |
| `email` | hudapugar@gmail.com |
| `discord_id` | Lynx (hajislamet) |
| `github_username` | PugarHuda |
| `project_name` | RYO Arena |
| `tracks` | `track_1`, `track_2`, `track_3` |
| `repo_url` | _pending_ — organiser's private repo not yet issued; working mirror: https://github.com/PugarHuda/ryo-arena (private) |
| `demo_video_url` | _pending upload_ |
| `x_post_url` | _pending post_ |
| `agree_rules` | yes |
| `confirm_no_secrets` | yes — `.env` is gitignored, `.env.example` carries names only, `git grep` for `ryo_mcp_`, `tvly-`, `sk-or-v1-` and `VENICE_INFERENCE_KEY_` returns only placeholders and test doubles (verified 2026-09-06) |

## `project_description`

RYO Arena makes an AI trading opinion auditable. Every decision is a receipt you can re-run:
`arena replay <id>` rebuilds it from the stored evidence and cached model output and prints
identical: true, so nothing can be rewritten after the fact. Each cited number carries its dotted RYO
path and is read back out of the evidence, never retyped by the model; citations pointing at absent
evidence are dropped in code; null is never turned into 0. Independent sources audit RYO itself -
exchange medians check its price, Wilder RSI/ATR recomputed from public OHLC check its indicators -
and the judge refuses to size a trade when they disagree. Three specialists (macro, technician,
narrative) debate all six builder tools, weighted by their own Brier score once calls resolve.
Track 3: four skills on RYO's own /api/skills paths, in RYO's envelope. Track 2: a dashboard that
diffs each receipt against the last, ranked by impact. Read-only; no orders.

## Evidence the judges can check without any key

- Hosted dashboard: https://ryo-arena.vercel.app (read-only ledger snapshot, `/api/health` reports
  `ryo_key_set: false` — nothing is disguised as live).
- `ARENA_DB=data/demo.db uv run arena serve`, `uv run arena skill run price_crosscheck '{"symbol":"SOL"}'`,
  `uv run pytest -q` (110 tests, no network).

## Declarations

- Read-only research tool. It never places, signs or routes an order; positions are paper only.
- No RYO builder key has ever been committed. Fixtures under `tests/fixtures/` are hand-built and
  labelled; nothing simulated is ever presented as live (`data_mode` is carried through and the
  risk layer refuses to size on `simulated`).
- Third-party libraries are disclosed in README under "Disclosed third-party libraries". All
  application code was written during the hackathon.
