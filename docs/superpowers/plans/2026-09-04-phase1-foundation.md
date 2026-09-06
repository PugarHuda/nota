# Phase 1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A CLI that gathers RYO evidence for a symbol, runs a council of LLM agents, sizes a practice trade, stores a replayable decision receipt, and scores agent calibration.

**Architecture:** One Python package `nota/` with small single-purpose modules. All external data enters through `RyoSource.call(tool, args) -> Envelope`. Everything derived is stored in a SQLite ledger keyed by the content hash of the evidence pack, and LLM outputs are cached by `(pack_hash, role, prompt_version, model)` so replay is deterministic.

**Tech Stack:** Python 3.12, uv, httpx (RYO REST), pydantic v2, anthropic 1.x (`client.messages.parse` structured outputs, model `claude-opus-5`), typer CLI, sqlite3 stdlib, pytest + respx.

**Spec:** `docs/superpowers/specs/2026-09-04-nota-design.md`

## Global Constraints

- Only the six builder tools: `market_overview`, `scan_market`, `analyze_token`, `deep_analysis`, `compare_tokens`, `monitor_market_sentiment_shift`.
- Env: `RYO_MCP_URL` default `https://app-ryochan.com/api/mcp`, `RYO_MCP_KEY`, `ANTHROPIC_API_KEY`, `NOTA_MODEL` default `claude-opus-5`, `NOTA_DB` default `nota.db`.
- Never convert `null`/unavailable to 0. Never write a real key to disk. Test fixtures live in `tests/fixtures/` and carry `source: "fixture"`; recorded live captures go to `fixtures/recorded/` with `source: "recorded"`.
- No network in tests (respx mocks httpx; FakeLLM replaces Anthropic).

## File structure

| File | Responsibility |
|---|---|
| `nota/envelope.py` | RYO public response contract + parsers for REST and MCP shapes |
| `nota/ryo_client.py` | `RyoSource` protocol, `RyoClient` (httpx, backoff, rate-limit headers), `RecordedRyoClient` (fixtures), `record()` |
| `nota/ledger.py` | SQLite tables: evidence, llm_cache, decisions, outcomes |
| `nota/evidence.py` | `EvidencePack`, `gather()`, path lookup, available paths |
| `nota/paths.py` | Candidate dotted paths for price and ATR (single place to fix when live schema is known) |
| `nota/llm.py` | `LLM` protocol, `AnthropicLLM`, `FakeLLM` |
| `nota/council.py` | Role prompts, `Opinion`, `Verdict`, citation validation, cached `run_council()` |
| `nota/risk.py` | Pure `size_trade()` -> `PracticeTrade | Blocked` |
| `nota/receipt.py` | `Receipt` model, `build_receipt()`, `render_markdown()` |
| `nota/decide.py` | `decide(symbol)` orchestration: gather -> council -> risk -> receipt -> ledger |
| `nota/replay.py` | `replay(decision_id, fresh)` -> identical flag + diff |
| `nota/calibration.py` | `resolve()`, Brier scores, role weights |
| `nota/cli.py` | typer commands: health, decide, replay, resolve, scores, show |

---

### Task 1: Envelope contract and parsers

**Files:** Create `nota/__init__.py`, `nota/envelope.py`, `tests/test_envelope.py`

**Produces:** `Envelope` (fields per guide), `Envelope.get(path)`, `parse_rest(body)`, `parse_mcp(body)`, `RyoToolError`.

- [ ] Write failing tests: parse REST `{"result": {...}}`, parse MCP text block, `isError` raises, `get("a.b")` returns None for missing, never 0.
- [ ] Implement `nota/envelope.py`.
- [ ] `uv run pytest tests/test_envelope.py -q` passes. Commit `feat: RYO envelope contract`.

### Task 2: RyoClient with backoff and RecordedRyoClient

**Files:** Create `nota/ryo_client.py`, `tests/test_ryo_client.py`, `tests/fixtures/analyze_token/SOL.json`

**Produces:** `RyoSource` protocol (`call(tool, args) -> Envelope`), `RyoClient(base_url, key, http=None, sleep=time.sleep)`, `.health()`, `.whoami()`, `.tools()`, `.call()`, `.last_rate_limit`, `RyoError(status_code, code, message, trace_id)`, `RecordedRyoClient(root)`, `record(client, tool, args, root)`, `fixture_name(tool, args)`.

- [ ] Tests with respx: 429 with Retry-After then 200 -> success and `sleep` called with 2; 400 error envelope -> `RyoError` no retry; 503 x5 -> raises; rate-limit headers captured; recorded client returns fixture and raises on missing.
- [ ] Implement. Commit `feat: RYO client with backoff and recorded source`.

### Task 3: Ledger

**Files:** Create `nota/ledger.py`, `tests/test_ledger.py`

**Produces:** `Ledger(path)` with `save_pack(pack_hash, symbol, source, pack_json)`, `get_pack(pack_hash)`, `get_cached(key)`, `put_cached(key, pack_hash, role, prompt_version, model, output_json)`, `save_decision(id, pack_hash, symbol, model, receipt_json)`, `get_decision(id)`, `list_decisions(limit)`, `save_outcome(decision_id, outcome_json)`, `get_outcome(decision_id)`, `unresolved()`.

- [ ] Tests: round-trip each table with `:memory:`; save_pack idempotent.
- [ ] Implement with WAL. Commit `feat: sqlite ledger`.

### Task 4: Evidence pack and gather

**Files:** Create `nota/paths.py`, `nota/evidence.py`, `tests/test_evidence.py`, fixtures for `market_overview/default.json`, `monitor_market_sentiment_shift/default.json`, `deep_analysis/SOL.json`

**Produces:** `Section(tool, status, envelope, error)`, `EvidencePack(symbol, created_at, source, sections)`, `.pack_hash()`, `.get(path)`, `.available_paths()`, `.primary_ok`, `.availability()`, `.provenance()`, `gather(source, symbol, include_perp=True) -> EvidencePack`, `first_present(pack, candidates) -> (path, value) | (None, None)`.

- [ ] Tests: gather with recorded fixtures yields 4 sections; missing fixture -> section `error` and pack still built; hash stable across created_at; available_paths contains `deep_analysis.data...` leaves; `get` on unavailable returns None.
- [ ] Implement. Commit `feat: evidence pack gathering`.

### Task 5: LLM abstraction

**Files:** Create `nota/llm.py`, `tests/test_llm.py`

**Produces:** `LLM` protocol `complete_json(system, user, schema: type[T]) -> T`, `AnthropicLLM(model=env NOTA_MODEL or "claude-opus-5")` using `client.messages.parse(..., output_format=schema)`, `FakeLLM(handlers: dict[str, Callable[[str], BaseModel]])` keyed by role tag found in `system`.

- [ ] Tests: FakeLLM dispatch by role tag; unknown role raises.
- [ ] Implement. Commit `feat: LLM abstraction`.

### Task 6: Council with cache and citation validation

**Files:** Create `nota/council.py`, `tests/test_council.py`

**Produces:** `Citation(path, value, note)`, `Opinion(role, stance, p_up_7d, confidence, thesis, citations, invalidation, dropped_citations)`, `Verdict(action, p_up_7d, rationale, agreed_with, disagreed_with, key_risks)`, `CouncilResult(opinions, verdict, cache_hits)`, `ROLES = ("macro","technician","narrative")`, `PROMPT_VERSION = "v1"`, `run_council(pack, llm, ledger, weights=None, use_cache=True) -> CouncilResult`.

- [ ] Tests with FakeLLM: three opinions + verdict; citations with unknown paths dropped and confidence lowered; second run hits cache (FakeLLM call count unchanged); `use_cache=False` calls again.
- [ ] Implement. Commit `feat: council agents`.

### Task 7: Risk sizing

**Files:** Create `nota/risk.py`, `tests/test_risk.py`

**Produces:** `RiskLimits`, `PracticeTrade`, `Blocked(reason)`, `size_trade(verdict, pack, limits) -> PracticeTrade | Blocked`.

- [ ] Tests: no_trade -> Blocked; primary unavailable -> Blocked; missing ATR -> Blocked (not zero); long sizing math: account 10000, risk 1%, atr 2, mult 2 -> risk_usd 100, stop = entry-4, size_units 25; max position cap applied.
- [ ] Implement. Commit `feat: ATR risk sizing`.

### Task 8: Receipt, decide, replay

**Files:** Create `nota/receipt.py`, `nota/decide.py`, `nota/replay.py`, `tests/test_decide_replay.py`

**Produces:** `Receipt`, `build_receipt(pack, council, trade, model) -> Receipt`, `render_markdown(receipt) -> str`, `decide(symbol, source, llm, ledger, limits, model) -> Receipt`, `replay(decision_id, ledger, llm, limits, fresh=False) -> ReplayResult(original, replayed, identical, diff)`.

- [ ] Tests: decide stores pack + decision; replay identical True with cache; fresh replay with a FakeLLM that changes answer -> identical False and diff lists `verdict.action`.
- [ ] Implement. Commit `feat: receipts, decide, replay`.

### Task 9: Calibration

**Files:** Create `nota/calibration.py`, `tests/test_calibration.py`

**Produces:** `resolve(decision_id, ledger, source) -> Outcome`, `Outcome(decision_id, price_then, price_now, return_pct, went_up, brier: dict[role, float])`, `role_scores(ledger) -> dict[role, {n, brier_mean}]`, `role_weights(scores) -> dict[role, float]`.

- [ ] Tests: brier = (p - outcome)^2; weights favour lower brier; missing price -> resolve raises `CannotResolve` (no zero).
- [ ] Implement. Commit `feat: calibration`.

### Task 10: CLI, README, .env.example

**Files:** Create `nota/cli.py`, `README.md`, `.env.example`; modify `pyproject.toml` (script `nota = "nota.cli:app"`)

- [ ] `uv run nota health` prints MCP health JSON. `uv run nota decide SOL --source recorded` prints markdown receipt. `replay`, `resolve`, `scores`, `show` wired.
- [ ] README: what, how to run, tracks, honesty rules, disclosed libraries. Commit `feat: CLI and docs`.
