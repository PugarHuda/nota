# Arena skills: contract and definitions (Track 3)

Two research tools RYO does not have yet. Both follow the published tool specification:
the definition shape is RYO's own `SkillDefinition` (`name`, `description`, `args[]`,
`requires_guard`, `xp`), and the response is RYO's public builder envelope, field for field:

```
schema_version · tool · status (ok|partial|unavailable) · data_mode (live|mixed|simulated|unknown)
as_of · request · data · summary{headline,key_points} · availability · warnings
```

Honesty convention as implemented:

- A failed dependency becomes `availability[<section>] = "unavailable"` plus a warning that
  names the cause. `status` is derived from availability (`arena/skills/contract.py`), never
  set by hand.
- A measurement that cannot be made is `null`. Sentiment with no sentiment-bearing words is
  `null`, not `0`. Timestamps the source does not supply are `null` and a warning says so.
- The method is named in the output (`data.method.sentiment = "lexicon_v1"`), and thresholds
  are echoed (`data.thresholds`) so a reader can audit every verdict.
- Both skills are read-only, `requires_guard: false`, and never touch wallets or orders.

## `narrative_convergence`

Monitor up to 20 user-selected voices and detect when several converge on one token.

| arg | type | required | notes |
|---|---|---|---|
| `voices` | array[string] | yes | `tg:<channel>` (Telegram public preview), `bs:<handle>` (Bluesky public API), `x:<handle>` (Nitter mirror, Tavily fallback; best effort) |
| `tokens` | array[string] | no | restrict to these symbols; default every cashtag / known name found |
| `hours` | integer | no | look-back window, default 24, max 336 |

`data`: `window_hours`, `since`, `method`, `voices[]{id,status,messages,fetched|error}`,
`tokens[]{symbol, voices, voice_count, mentions, sentiment_mean, sentiment_samples,
conviction_mean, urgency_max, direction, converging, coverage, first_seen, last_seen, samples[]}`.
`converging` is true when at least two voices carry a non-zero sentiment of the same sign.
`availability` is per voice.

## `news_verify`

Count independent sources for a claim and attach the token's RYO market read.

| arg | type | required | notes |
|---|---|---|---|
| `claim` | string | yes | one sentence |
| `symbol` | string | no | attaches `analyze_token` evidence when a RYO source is configured |
| `max_results` | integer | no | default 6, max 20 |

`data`: `claim`, `method.search` (`tavily` | `venice_web_search`), `sources[]{title,url,domain,score,published_date,snippet}`,
`distinct_domains`, `domains`, `top_score`, `verdict` (`corroborated` >= 3 domains scoring >= 0.5, `weak` 1-2,
`unverified` 0, `null` when search failed), `thresholds`, `market_context{...}`.
`availability`: `search` (primary), `market`.

Search backend: Tavily when `TAVILY_API_KEY` is set, otherwise Venice web search through the same
`OPENAI_API_KEY` the council uses (`arena/skills/sources.py::search_backend`). Venice returns no
relevance scores and usually no dates, so the envelope carries a warning that corroboration is
not time-bound; `score` and `published_date` stay `null` rather than being invented.

Headline pass: before the search backend, `news_verify` reads the RSS feeds of CoinDesk,
Cointelegraph, The Block and Decrypt (`method.headlines = rss_headlines`), scoring each item by
the share of claim keywords it contains and keeping its `pubDate`, so corroboration from that
pass is dated and deterministic. Feeds that fail are listed in `warnings`; parsing uses
`defusedxml`.

Sentiment method is `vader_3.3.2+crypto_lexicon_v2`: VADER (MIT) with the crypto lexicon added
at +/-2.0, so negation ("not bullish") and intensity ("very bullish!!") are handled. A text with
no lexicon word at all stays `null`. `x:` voices are read through a Nitter mirror (unofficial,
flagged in `warnings`) and fall back to Tavily when configured.

## `price_crosscheck`

Independent spot prices next to RYO's read, never instead of it.

| arg | type | required | notes |
|---|---|---|---|
| `symbol` | string | yes | e.g. SOL |
| `reference_price` | number | no | RYO's price to compare against |
| `reference_path` | string | no | where the reference came from |

`data`: `sources[]{name,price_usd,as_of,status,error}` (CoinGecko, Coinbase, Kraken; no keys),
`median_usd`, `spread_pct`, `sources_ok`, `reference{price_usd,path,deviation_pct}`,
`fear_greed{value,classification,as_of,source,reference_value,delta}` (alternative.me, optional
`reference_fear_greed` arg; a 10-point gap becomes a warning), `thresholds`.
A deviation of 2% or more becomes a warning. `availability` is per exchange. The council's
Technician sees this section as `price_check`; the calibration step uses the median only when
RYO cannot supply a price, and records that in the outcome.

## `technicals_crosscheck`

RYO's indicators recomputed from an independent source so a reader can audit them.

| arg | type | required | notes |
|---|---|---|---|
| `symbol` | string | yes | e.g. SOL |
| `reference_rsi_14` | number | no | RYO's `technicals.rsi_14` |
| `reference_atr_14` | number | no | RYO's `technicals.atr_14` (USD) |
| `days` | integer | no | look-back, default 30 (15..90) |

`data`: `method{indicators: wilder, period: 14, candles}`, `daily_candles`, `as_of` (last candle),
`close`, `rsi_14`, `atr_14`, `atr_pct`, `performance_pct{1d,7d,30d}`,
`reference{rsi_14, atr_14, rsi_diff_points, atr_diff_pct}`, `thresholds` (10 RSI points, 25% ATR).
Source: CoinGecko `/coins/{id}/ohlc` (no key; 4-hour candles for up to 30 days, aggregated to UTC
days here). Fewer than 15 daily candles means `rsi_14`/`atr_14` are `null` with a warning.
`availability.ohlc` is the primary section.

## Calling them

```bash
uv run arena skill spec                               # definitions as JSON
uv run arena skill run narrative_convergence '{"voices":["tg:WatcherGuru"],"hours":24}'
uv run arena skill run news_verify '{"claim":"SOL ETF approved","symbol":"SOL"}'
```

Inside the council, `--voices tg:a,tg:b` adds a `narrative_signal` section and `--news`
adds `news_check`; the Narrative agent reads both and must cite them by path like any other
evidence.
