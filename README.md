# RYO Arena

A council of specialised AI agents debates live RYO market evidence, records the practice
trade it would make as a **replayable decision receipt**, and builds a public calibration
track record for every agent. Built for the RYO-CHAN Hackathon 2026.

Tracks entered: **1 Autonomous Agents** (this repo's core), **3 New Skills** and
**2 Dashboards** (phases 2 and 3, see `docs/superpowers/specs/`).

## How a decision is made

```
gather ──► council ──► judge ──► risk (pure ATR math) ──► receipt ──► ledger
 4 RYO      macro       weighs      entry / stop / target      id = hash(evidence,
 tools      technician  opinions    or Blocked when price       model, prompts)
            narrative   by Brier    or ATR is missing
                        weights
```

- **Evidence pack**: `market_overview`, `monitor_market_sentiment_shift`, `deep_analysis`,
  `analyze_token`. A failed tool becomes a section with status `error`; the pack still exists.
- **Council**: three agents (macro, technician, narrative) each return a stance, a
  probability, and **citations as dotted paths into the evidence**. Citations that point at
  a missing or null value are dropped in code and the opinion is downgraded.
- **Judge**: weighs opinions by each agent's historical Brier score and decides
  long / short / no_trade.
- **Risk**: a pure function. Stop = 2×ATR(14), target = 3×ATR, size from 1% account risk,
  capped at 20% of the account. No price or no ATR means **Blocked**, never a guessed number.
- **Replay**: LLM outputs are cached by `(evidence hash, role, prompt version, model)`.
  `arena replay <id>` reproduces the receipt exactly; `--fresh` re-asks the model and prints
  the drift honestly.
- **Calibration**: `arena resolve` scores every agent with a Brier score against a fresh
  price read and feeds the weights back to the judge.

## Honesty rules this code enforces

- Every number in a receipt carries its source path, RYO `as_of`, `data_mode` and trace id.
- `null` / `unavailable` is never converted to 0 (`Envelope.get`, `first_present`, risk).
- Recorded fixtures keep RYO's original `as_of` and `data_mode` and are labelled
  `source: recorded`. Synthetic test fixtures live only under `tests/fixtures/` and are
  labelled `source: fixture`; receipts print the label.
- The RYO surface is read-only; practice trades exist only in `arena.db`.

## Run

```bash
uv sync
cp .env.example .env            # fill RYO_MCP_KEY and ANTHROPIC_API_KEY
uv run arena health             # MCP health (no key) + whoami/quota (with key)
uv run arena decide SOL         # live evidence, Anthropic council
uv run arena replay <id>        # identical: True
uv run arena replay <id> --fresh
uv run arena resolve --all      # after some days: Brier scores per agent
uv run arena scores
uv run arena record SOL         # capture live responses into fixtures/recorded
uv run arena decide SOL --source recorded   # judge-friendly run without a key
uv run pytest -q
```

Smoke test with no credentials at all (clearly labelled placeholder output):

```bash
uv run arena decide SOL --source fixture --llm fake
```

## Failure handling

- RYO client: exponential backoff with jitter on 429/503/network, honours `Retry-After`,
  never retries 4xx argument errors, records `X-RateLimit-*` headers.
- Evidence gathering continues past failed tools; the judge is told which sections are
  missing and is instructed to prefer `no_trade` when primary evidence is gone.
- SQLite ledger in WAL mode; every write is idempotent by content hash, so a restart resumes.

## Layout

```
arena/
  envelope.py     RYO public response contract + REST/MCP parsers
  ryo_client.py   RyoClient (httpx) + RecordedRyoClient + record()
  ledger.py       SQLite: evidence, llm_cache, decisions, outcomes
  evidence.py     EvidencePack, gather(), path lookups
  paths.py        candidate paths for price / ATR (one place to fix when the live schema is recorded)
  llm.py          LLM protocol, AnthropicLLM (messages.parse), FakeLLM
  council.py      role prompts, Opinion/Verdict, citation validation, cached run_council()
  risk.py         size_trade(): PracticeTrade | Blocked
  receipt.py      Receipt + markdown rendering
  decide.py / replay.py / calibration.py / cli.py
docs/             hackathon analysis, MCP builder guide copy, design spec, plans
tests/            pytest, no network (respx + FakeLLM)
```

## Disclosed third-party libraries

httpx, pydantic, anthropic, typer, python-dotenv; dev: pytest, respx. All application code
was written during the hackathon.

## Status

Phase 1 (this) complete. Phase 2: `narrative_convergence` and `news_verify` skills in the
RYO envelope. Phase 3: FastAPI read API and the diff-first dashboard. Phase 4: SocialFi layer.
