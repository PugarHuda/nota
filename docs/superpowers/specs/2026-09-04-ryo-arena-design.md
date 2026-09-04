# RYO Arena: Design Spec

Date: 2026-09-04. Hackathon: RYO-CHAN Hackathon 2026 (see `docs/HACKATHON-ANALYSIS.md`).

## One-liner

A council of specialised AI agents debates live RYO market evidence, records the practice trade it would make as a replayable "decision receipt", builds a public calibration track record, and exposes new research skills that follow RYO's tool contract.

## Tracks entered

| Track | Deliverable | Judged on its own as |
|---|---|---|
| 1 Autonomous Agents | Council agent + deterministic replay + calibration | CLI/API that produces receipts from evidence |
| 3 New Skills | `narrative_convergence`, `news_verify` skills in RYO envelope | Standalone Python skills with spec + tests |
| 2 Dashboards | Diff-first dashboard, public reasoning feed, agent leaderboard | Web UI reading the same ledger |

## Phases (each phase is submittable alone)

1. **Foundation (this spec's plan):** RYO client, evidence ledger, evidence gathering, council agents, risk sizing, receipts, replay, calibration, CLI.
2. **Skills:** `narrative_convergence` (Telegram public channels via t.me/s preview, X via Tavily), `news_verify` (Tavily). Both return the RYO envelope. Council's Narrative agent consumes them.
3. **API + Dashboard:** FastAPI read API over the ledger; React UI: "what changed" diff view ranked by impact on open practice positions, receipt permalinks, degraded-mode display, keyboard navigation.
4. **SocialFi:** agent leaderboard by Brier score, user "backing" of calls, OG receipt images, X share.

## Hard rules (from hackathon)

- Never commit a real key. `.env.example` lists every variable.
- Never present fabricated or placeholder data as real. Every number in a receipt carries `source`, `as_of`, `trace_id`. Recorded fixtures are labelled `source: "recorded"` and keep RYO's original `as_of` and `data_mode`.
- `null`/unavailable is never converted to 0. Agents may only cite evidence paths that exist in the pack.
- MCP is read-only. Practice trades live in our ledger only.

## Foundation architecture

```
arena/
  envelope.py     RYO public response contract (pydantic) + parsers (REST, MCP text block)
  ryo_client.py   RyoClient (httpx, backoff, rate-limit headers, error envelope) + RecordedRyoClient (fixtures)
  ledger.py       SQLite: evidence, decisions, positions, llm_cache. Content-hash packs.
  evidence.py     gather(symbol) -> EvidencePack from 3-4 tools, tolerant of partial failure
  llm.py          LLM protocol: complete_json(system, user, schema) ; AnthropicLLM ; FakeLLM (tests)
  council.py      Roles (Macro, Technician, Narrative, Risk), Judge; Opinion/Verdict models; evidence-path citation check
  risk.py         Pure function: verdict + pack -> PracticeTrade (ATR-based) or blocked
  receipt.py      Receipt model, build + render (JSON, markdown)
  replay.py       replay(decision_id): same pack + cached LLM outputs -> identical receipt; fresh mode shows drift
  calibration.py  resolve outcomes, Brier score per agent, vote weights
  cli.py          arena decide SOL | replay <id> | resolve | health
tests/            pytest, FakeLLM + recorded fixtures, no network
```

### Determinism model

LLM outputs are cached in the ledger keyed by `(pack_hash, role, prompt_version, model)`. Replay reuses the cache, so the same evidence always yields the same receipt. `replay --fresh` bypasses the cache and reports the diff, which is shown honestly as model drift. Risk sizing is a pure function and needs no cache.

### Failure handling

- RyoClient: exponential backoff with jitter on 429/503/network, honour `Retry-After`, never retry 4xx argument errors. Record `X-RateLimit-*`.
- Evidence gathering continues when a tool fails; the pack's `availability` lists each section as `ok|partial|unavailable|error`. The Risk agent blocks a trade when primary evidence (`deep_analysis`) is unavailable.
- Ledger is SQLite in WAL mode; every step is idempotent by pack hash so a restart resumes.

### Global constraints

- Python 3.12, `uv` for env. Dependencies: httpx, pydantic>=2, anthropic, typer. Tests: pytest, respx.
- Default model `claude-sonnet-5`; configurable via `ARENA_MODEL`.
- Env vars: `RYO_MCP_URL` (default `https://app-ryochan.com/api/mcp`), `RYO_MCP_KEY`, `ANTHROPIC_API_KEY`, `ARENA_MODEL`, `ARENA_DB` (default `arena.db`), `TAVILY_API_KEY` (phase 2).
- Tool calls only through the 6-tool builder catalog: market_overview, scan_market, analyze_token, deep_analysis, compare_tokens, monitor_market_sentiment_shift.
