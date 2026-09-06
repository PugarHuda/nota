# RYO Arena

A council of specialised AI agents debates live RYO market evidence, records the practice
trade it would make as a **replayable decision receipt**, and builds a public calibration
track record for every agent. Built for the RYO-CHAN Hackathon 2026.

Tracks entered: **1 Autonomous Agents** (council, receipts, `watch` loop), **2 Dashboards**
(diff-first receipt dashboard), **3 New Skills** (`narrative_convergence`, `news_verify`,
`price_crosscheck`, all in RYO's envelope).

## How a decision is made

```
gather ──────────► council ──► judge ──► risk (pure ATR math) ──► receipt ──► ledger ──► notify
 5 RYO tools        macro       weighs      entry / stop / target      id = hash(evidence,   Telegram
 + own skills       technician  opinions    or Blocked when price       model, prompts)      Discord
 (price_check,      narrative   by Brier    or ATR is missing
  voices, news)                 weights
```

- **Evidence pack**: `market_overview`, `monitor_market_sentiment_shift`, `deep_analysis`,
  `analyze_token`, `compare_tokens` (the token against BTC/ETH peers). `scan_market` drives
  `arena scan`, RYO's recommended funnel. A failed tool becomes a section with status `error`;
  the pack still exists. Own skills are added as further sections (`price_check` by default,
  `narrative_signal` with `--voices`, `news_check` with `--news`).
- **Council**: three agents (macro, technician, narrative) each return a stance, a
  probability, and **citations as dotted paths into the evidence**. Citations that point at
  a missing or null value are dropped in code (after normalising `x[0].y` to `x.0.y`) and the
  opinion is downgraded.
- **Judge**: weighs opinions by each agent's historical Brier score and decides
  long / short / no_trade.
- **Risk**: a pure function. Stop = 2×ATR(14), target = 3×ATR, size from 1% account risk,
  capped at 20% of the account. No price or no ATR means **Blocked**, never a guessed number.
- **Replay**: LLM outputs are cached by `(evidence hash, role, prompt version, model)`.
  `arena replay <id>` reproduces the receipt exactly; `--fresh` re-asks the model and prints
  the drift honestly. The dashboard's "Verify replay" button does the cached check only.
- **Calibration**: `arena resolve --all` scores every decision whose seven-day horizon has
  passed (Brier per agent) against a fresh RYO price read, falling back to the exchange median
  from `price_crosscheck` when RYO cannot give a price; the outcome records which source was used.
- **Autonomy**: `arena watch SOL,BTC --every 3600 --notify` decides on a schedule, resolves
  matured decisions, and posts each new receipt to Telegram / Discord.

## Honesty rules this code enforces

- Every number in a receipt carries its source path, RYO `as_of`, `data_mode` and trace id.
- `null` / `unavailable` is never converted to 0 (`Envelope.get`, `first_present`, risk,
  every skill).
- Recorded fixtures keep RYO's original `as_of` and `data_mode` and are labelled
  `source: recorded`. Synthetic test fixtures live only under `tests/fixtures/` and are
  labelled `source: fixture`; receipts print the label. There is no fake-LLM mode in the CLI.
- The RYO surface is read-only; practice trades exist only in `arena.db`.
- External sources say what they cannot do: Venice web search carries no dates, the X mirror
  is unofficial, RSS feeds that fail are listed, exchange prices never replace RYO's value.

## Run

```bash
uv sync
cp .env.example .env            # RYO_MCP_KEY + an LLM key (Anthropic, or ARENA_LLM=openai for Venice/OpenRouter)
uv run arena health             # MCP health (no key) + whoami/quota (with key)
uv run arena decide SOL         # live evidence + price cross-check, council, receipt
uv run arena decide SOL --voices tg:WatcherGuru,x:WatcherGuru --news --notify
uv run arena scan --top-n 5 --decide-top 2      # scan_market -> analyze_token -> council
uv run arena watch SOL,BTC --every 3600 --notify
uv run arena replay <id>        # identical: True
uv run arena replay <id> --fresh
uv run arena resolve --all      # after 7 days: Brier scores per agent
uv run arena scores
uv run arena record SOL         # capture all six live tools into fixtures/recorded
uv run arena decide SOL --source recorded   # judge-friendly run without a key
uv run arena serve                          # dashboard + read API on http://127.0.0.1:8000
uv run arena skill spec                     # Track 3 definitions
uv run arena skill run price_crosscheck '{"symbol":"SOL","reference_price":150}'
uv run pytest -q
```

## Skills (Track 3)

All three return RYO's public envelope field for field (`docs/skills/SKILL-SPEC.md`):

- `narrative_convergence`: up to 20 voices (`tg:` public Telegram previews, `x:` via a Nitter
  mirror with Tavily fallback), VADER sentiment plus a crypto lexicon, conviction, urgency,
  and convergence detection. Silence is `null`, not 0.
- `news_verify`: dated headlines from CoinDesk, Cointelegraph, The Block and Decrypt RSS,
  plus Tavily or Venice web search for breadth; counts independent domains and attaches RYO
  `analyze_token` context.
- `price_crosscheck`: keyless CoinGecko, Coinbase and Kraken spot prices, median, spread, and
  deviation of a reference price (RYO's) from the exchanges.

## Dashboard (Track 2)

`arena serve` exposes a read-only API over the ledger (`/api/decisions`, `/api/decisions/{id}`,
`/api/decisions/{id}/replay`, `/api/positions`, `/api/scores`, `/api/health`, exports
`/r/{id}.json` and `/r/{id}.md`, OpenAPI at `/docs`) and a single-page dashboard:

- **What changed**: every receipt is diffed against the previous receipt for the same symbol
  and the rows are ranked by impact. Verdict flips, trade unlock/block, availability and model
  changes come first, then the numbers the risk engine reads (price, ATR, RSI) by % move, then
  every other evidence leaf. A value that became `null` is shown as `null`, never as 0.
- **Degraded mode**: a banner names each evidence section that is not `ok`; provenance shows
  `as_of`, `data_mode` and trace id per section.
- **Open practice positions** against the latest evidence price, with distance-to-stop.
- **Agent leaderboard** by Brier score with the judge weights currently in force.
- **Verify replay** from the page (cached outputs only, never spends), share to X, exports.
- **Works for everyone**: skip link, real buttons, visible focus, `aria-live` updates,
  keyboard `j`/`k` move, `Enter` open, `p` previous receipt, `/` filter, `?` help.
- Polls the ledger every 30 s so a running `watch` loop shows up without a reload.

The dashboard reads receipts only. It cannot show a number that has no receipt behind it.

## Failure handling

- RYO client: exponential backoff with jitter on 429/503/network, honours `Retry-After`,
  never retries 4xx argument errors, records `X-RateLimit-*` headers.
- Evidence gathering continues past failed tools and skills; the judge is told which sections
  are missing and is instructed to prefer `no_trade` when primary evidence is gone.
- SQLite ledger in WAL mode; every write is idempotent by content hash, so a restart resumes.
- `watch` survives a failing symbol, a failing notifier and a failing resolution.
- RSS is parsed with `defusedxml` (no entity expansion from untrusted feeds).

## Layout

```
arena/
  envelope.py     RYO public response contract + REST/MCP parsers
  ryo_client.py   RyoClient (httpx) + RecordedRyoClient + record()
  ledger.py       SQLite: evidence, llm_cache, decisions, outcomes
  evidence.py     EvidencePack, ryo_args(), gather(), candidate_symbols(), path lookups
  paths.py        candidate paths for price / ATR (one place to fix when the live schema is recorded)
  llm.py          LLM protocol, AnthropicLLM (messages.parse), OpenAICompatLLM (Venice/OpenRouter)
  council.py      role prompts, Opinion/Verdict, citation validation, cached run_council()
  risk.py         size_trade(): PracticeTrade | Blocked
  receipt.py      Receipt + markdown rendering
  notify.py       Telegram Bot API + Discord webhook publishing
  api.py          FastAPI read API + static/index.html dashboard
  skills/         contract, sources (Telegram, Nitter, RSS, Tavily, Venice), narrative, news, price_check
  decide.py / replay.py / calibration.py / cli.py
docs/             hackathon analysis, MCP builder guide copy, design spec, skill spec, submission draft
tests/            pytest, no network (respx + FakeLLM test double)
```

## Disclosed third-party libraries

httpx, pydantic, anthropic, typer, python-dotenv, fastapi, uvicorn, vaderSentiment (MIT),
defusedxml; dev: pytest, respx. Data sources: RYO MCP, t.me/s previews, Nitter mirrors via
twiiit, CoinDesk / Cointelegraph / The Block / Decrypt RSS, CoinGecko, Coinbase, Kraken public
APIs, Tavily or Venice web search. All application code was written during the hackathon.
