# Nota skills: contract and definitions (Track 3)

Seven research tools RYO does not have. All of them follow the published tool specification:
the definition shape is RYO's own `SkillDefinition` (`name`, `description`, `args[]`,
`requires_guard`, `xp`), and the response is RYO's public builder envelope, field for field:

```
schema_version · tool · status (ok|partial|unavailable) · data_mode (live|mixed|simulated|unknown)
as_of · request · data · summary{headline,key_points} · availability · warnings
```

Honesty convention as implemented:

- Each section's availability uses RYO's words: `available`, `partial`, `unavailable`, `error`,
  and `outlier` for a price source excluded from the median. A failed dependency becomes
  `unavailable` plus a warning that names the cause. `status` is derived from the primary
  sections' availability (`nota/skills/contract.py`), never set by hand: `ok` when every primary
  section is available, `unavailable` when every one failed, else `partial`. A context section
  failing (fear/greed next to prices, DVOL next to venue positioning) is a warning, not a
  downgrade. Receipts stored before 2026-09-19 say `ok` for available and still render and replay.
- A measurement that cannot be made is `null`. Sentiment with no sentiment-bearing words is
  `null`, not `0`. Timestamps the source does not supply are `null` and a warning says so.
- The method is named in the output (`data.method`), and thresholds are echoed
  (`data.thresholds`) so a reader can audit every verdict.
- Every skill is read-only, `requires_guard: false`, and never touches wallets or orders.
- Arguments are checked against the definitions below before a skill runs (`nota/skills/__init__.py`).

## Definitions

Generated from the code by `scripts/skill_spec_md.py`; `tests/test_skill_spec.py` fails when this
block and the code differ.

<!-- generated:start -->

### `narrative_convergence`

Monitor up to 20 user-selected voices (Telegram public channels, Bluesky and X handles) and report which tokens they mention, lexicon-scored sentiment per token, conviction and urgency, and whether the voices converge: at least two voices carry a non-zero net sentiment on the token and every one of them has the same sign.

| arg | type | required | enum | description |
|---|---|---|---|---|
| `voices` | array[string] | yes |  | Up to 20 ids like tg:WatcherGuru, bs:handle.bsky.social or x:handle |
| `tokens` | array[string] | no |  | Symbols to track; default = every cashtag, ticker or name of a known major found |
| `hours` | integer | no |  | Look-back window in hours (default 24) |

| availability key | primary | covers |
|---|---|---|
| `<voice id>` | yes | one key per voice, e.g. `tg:WatcherGuru`, `bs:alice.bsky.social`, `x:handle` |

`status`: `ok` when every primary key (`<voice id>`) is `available`, `unavailable` when every one failed, else `partial`. Read-only, `requires_guard: false`.

### `news_verify`

Verify a crypto news claim: count independent domains reporting it (four outlets' RSS headlines plus Tavily or Venice web search), count those whose headline leans the opposite way, and attach the token's RYO analyze_token evidence so the story and the market read sit side by side.

| arg | type | required | enum | description |
|---|---|---|---|---|
| `claim` | string | yes |  | The story to verify, in one sentence |
| `symbol` | string | no |  | Token symbol to attach market evidence for |
| `max_results` | integer | no |  | Search results to inspect (default 6, max 20) |

| availability key | primary | covers |
|---|---|---|
| `headlines` | yes | RSS pass over CoinDesk, Cointelegraph, The Block and Decrypt |
| `search` | yes | Tavily, or Venice web search when no Tavily key is set |
| `market` | no | RYO `analyze_token` for `symbol` |

`status`: `ok` when every primary key (`headlines`, `search`) is `available`, `unavailable` when every one failed, else `partial`. Read-only, `requires_guard: false`.

### `price_crosscheck`

Fetch independent USD spot prices for a symbol from CoinGecko, Coinbase, Kraken, Binance and DefiLlama (no keys), drop a source that disagrees with the rest as an outlier, report median and spread, and flag how far a reference price (e.g. RYO's) deviates from them.

| arg | type | required | enum | description |
|---|---|---|---|---|
| `symbol` | string | yes |  | Token symbol, e.g. SOL |
| `reference_price` | number | no |  | Price to compare against, e.g. RYO deep_analysis price |
| `reference_path` | string | no |  | Where the reference price came from |
| `reference_fear_greed` | number | no |  | A Fear & Greed value to compare with alternative.me |

| availability key | primary | covers |
|---|---|---|
| `coingecko` | yes | spot price leg; `outlier` when excluded from the median |
| `coinbase` | yes | spot price leg; `outlier` when excluded from the median |
| `kraken` | yes | spot price leg; `outlier` when excluded from the median |
| `binance` | yes | spot price leg; `outlier` when excluded from the median |
| `defillama` | yes | spot price leg; `outlier` when excluded from the median |
| `fear_greed` | no | alternative.me Fear & Greed index |

`status`: `ok` when every primary key (`coingecko`, `coinbase`, `kraken`, `binance`, `defillama`) is `available`, `unavailable` when every one failed, else `partial`. Read-only, `requires_guard: false`.

### `technicals_crosscheck`

Recompute RSI(14) and ATR(14) (Wilder smoothing) from 200 closed UTC-day candles (OKX, else Binance, else CoinGecko 4h OHLC aggregated to days) plus 1d/7d/30d performance, and report how far reference values (e.g. RYO's) deviate from the independent calculation.

| arg | type | required | enum | description |
|---|---|---|---|---|
| `symbol` | string | yes |  | Token symbol, e.g. SOL |
| `reference_rsi_14` | number | no |  | RSI(14) to compare against |
| `reference_atr_14` | number | no |  | ATR(14) in USD to compare against |
| `days` | integer | no |  | lookback for performance, 7..90 (indicators always use 200 closed daily candles) |

| availability key | primary | covers |
|---|---|---|
| `ohlc` | yes | closed daily candles: OKX, then Binance, then CoinGecko |

`status`: `ok` when every primary key (`ohlc`) is `available`, `unavailable` when every one failed, else `partial`. Read-only, `requires_guard: false`.

### `positioning_check`

Decide, field by field, whether RYO's deep_analysis derivatives block can be cited as evidence about this token (identical values across other tokens, sign conflicts with OKX coin-terms open interest), and report OKX perp positioning: premium over spot on OKX and Hyperliquid, 24 h open-interest change in coins, long/short account ratio and its 100-hour percentile, and for BTC/ETH Deribit's DVOL implied 7-day move as a check on stop distance.

| arg | type | required | enum | description |
|---|---|---|---|---|
| `symbol` | string | yes |  | Token symbol, e.g. SOL |
| `reference_derivatives` | object | no |  | RYO deep_analysis data.derivatives for this symbol; fetched from RYO when omitted and a RYO key is set |
| `peer_derivatives` | array[object] | no |  | Same-day RYO derivatives blocks for other symbols: [{symbol, funding_rate_bps, open_interest_change_24h_pct}]; taken from the day's scorecard locks when omitted |
| `ryo_btc_funding_bps` | number | no |  | Latest BTC funding from RYO monitor_market_sentiment_shift (evidence.funding.latest_bps) |
| `atr_stop_pct` | number | no |  | Stop distance in % of price (e.g. RYO's 1.5 x atr_14_pct), checked against the DVOL implied 7-day move |

| availability key | primary | covers |
|---|---|---|
| `okx_premium` | yes | OKX perp premium over spot and funding |
| `okx_open_interest` | yes | OKX coin-terms open interest, 25 hourly points |
| `okx_long_short` | yes | OKX long/short account ratio, 100 hourly points |
| `hyperliquid_premium` | yes | Hyperliquid perp premium over its oracle |
| `ryo_reference` | no | RYO `deep_analysis` derivatives, fetched when none is passed |
| `ledger_peers` | no | same-day scorecard locks as the peer cross-section |
| `deribit_dvol` | no | Deribit DVOL for BTC/ETH; `unavailable` for every other token |

`status`: `ok` when every primary key (`okx_premium`, `okx_open_interest`, `okx_long_short`, `hyperliquid_premium`) is `available`, `unavailable` when every one failed, else `partial`. Read-only, `requires_guard: false`.

### `move_base_rate`

Empirical probability that a token moves k ATRs (up or down, touched or closed beyond) within h days, counted from ~400 days of OKX daily candles on days in the same ATR tercile as today, with a time-split holdout. Pass RYO's atr_14_pct to measure the distance in RYO's ATR; k=0 with event=close gives the plain 'higher after h days' rate.

| arg | type | required | enum | description |
|---|---|---|---|---|
| `symbol` | string | yes |  | Token symbol, e.g. SOL |
| `k` | number | no |  | Distance in ATRs (default 1) |
| `horizon_days` | integer | no |  | 1 to 14 (default 3) |
| `direction` | string | no | `up`, `down` | Default up |
| `event` | string | no | `touch`, `close` | Default touch |
| `atr_14_pct` | number | no |  | RYO's ATR(14) as % of price (analyze_token or deep_analysis) |
| `as_of` | string | no |  | YYYY-MM-DD: use only candles closed before this day (scoring a past call) |

| availability key | primary | covers |
|---|---|---|
| `okx_daily` | yes | about 400 closed OKX UTC-day candles |

`status`: `ok` when every primary key (`okx_daily`) is `available`, `unavailable` when every one failed, else `partial`. Read-only, `requires_guard: false`.

### `verdict_track_record`

How RYO's own deep_analysis plans have fared: locked daily for 25 majors and settled on OKX hourly candles (stop or +1R target first, 24 h or 72 h). Returns counts by verdict and by confluence state, plans whose verdict leans against their own direction, and the latest locked verdict. Omit the symbol for all tokens.

| arg | type | required | enum | description |
|---|---|---|---|---|
| `symbol` | string | no |  | Token symbol, e.g. SOL; omit for every token |
| `horizon_hours` | integer | no | `24`, `72` | 24 (default) or 72 |

| availability key | primary | covers |
|---|---|---|
| `ledger` | yes | the scorecard's locks and settlements |

`status`: `ok` when every primary key (`ledger`) is `available`, `unavailable` when every one failed, else `partial`. Read-only, `requires_guard: false`.

<!-- generated:end -->

## Outputs and methods

### `narrative_convergence`

`data`: `window_hours`, `since`, `method{sentiment, lexicon_sizes}`, `voices[]{id,status,messages,fetched|error}`,
`tokens[]{symbol, voices, voice_count, mentions, sentiment_mean, sentiment_samples,
conviction_mean, urgency_max, direction, converging, coverage, first_seen, last_seen, samples[]}`.

Tokens are cashtags, bare uppercase tickers and names (`bitcoin`, `solana`, ...) of known majors:
the scorecard's 25 plus the tokens `price_crosscheck` maps to CoinGecko. A cashtag outside that set
(`$HODL`) is dropped unless it is in `tokens`. A token's sentiment reads only the clauses that name
it (split on `. ! ? ; newline` and on `while`, `but`, `whereas`, `although`, `though`), so
"BTC pumping while ETH dumping" is bullish on BTC and bearish on ETH.

`converging` is true when at least two voices carry a non-zero net sentiment on the token and every
one of those voices has the same sign; one dissenting voice is enough to make it false.

Sentiment method is `vader_3.3.2+crypto_lexicon_v3`: VADER (MIT) with a crypto lexicon added at
+/-2.0, so negation ("not bullish") and intensity ("very bullish!!") are handled. Chart vocabulary
(`top`, `short`, `long`, `support`, `resistance`, `exit`, `higher`, `lower`) carries no stance. A text
with no lexicon word at all stays `null`. Conviction and urgency phrases match whole words only
("now" does not fire inside "know").

`x:` voices are read through X's own public syndication endpoint, the one that serves embedded
timelines (unofficial and rate limited, and throttled hardest on data-centre addresses, so a hosted
deployment usually reports these voices `unavailable`; flagged in `warnings`) and fall back to
Tavily when configured. `bs:` voices use Bluesky's public AppView (`app.bsky.feed.getAuthorFeed`).

### `news_verify`

`data`: `method{search, headlines, stance}`, `sources[]{title,url,domain,score,published_date,snippet,stance}`,
`distinct_domains` and `domains` (domains reporting the claim), `contradicting_domains`,
`supports`, `contradicts` (distinct-domain counts), `claim_sentiment`, `top_score`, `verdict`,
`thresholds`, `market_context{...}`.

`verdict`: `disputed` when at least one relevant domain leans the opposite way and those domains
are at least as many as the supporting ones; otherwise `corroborated` (3 or more supporting
domains scoring >= 0.5), `weak` (1-2), `unverified` (0); `null` only when neither the headlines nor
the search pass could run. A headline *contradicts* when the claim's and the headline's lexicon
sentiment (the narrative lexicon above) have opposite signs outside VADER's +/-0.05 neutral band.

Headline pass: `news_verify` reads the RSS feeds of CoinDesk, Cointelegraph, The Block and Decrypt
(`method.headlines = rss_headlines`), keeping each item's `pubDate`. Its score is the share of the
claim's distinctive terms the item contains: asset tickers and names, and generic words (`price`,
`today`, `this`, `week`, `crypto`, `market`, `news`), are removed first, and an item needs at least
one distinctive match. A claim with nothing but an asset name ("SOL news this week") is a topic
query and matches on the asset's ticker and name (SOL, solana). Feeds that fail are listed in `warnings`; parsing uses `defusedxml`.

Search pass: Tavily when `TAVILY_API_KEY` is set, otherwise Venice web search through the same
`OPENAI_API_KEY` the council uses (`nota/skills/sources.py::search_backend`). `method.search`
names the backend that answered, `null` when none did; the headlines still count then, and the
status is `partial`. Venice returns no relevance scores and usually no dates, so the envelope
carries a warning that its hits are not time-bound; `score` and `published_date` stay `null`.

### `price_crosscheck`

`data`: `sources[]{name,price_usd,as_of,status,error}` (CoinGecko, Coinbase, Kraken, Binance's
`data-api.binance.vision` USDT pair, DefiLlama `coins.llama.fi` at confidence 0.9 or more; no keys),
`coingecko_id` (search hits with the symbol ranked by market-cap rank, unranked last),
`median_usd`, `spread_pct`, `sources_ok`, `reference{price_usd,path,deviation_pct}`,
`fear_greed{value,classification,as_of,source,reference_value,delta}` (alternative.me, optional
`reference_fear_greed` arg; a 10-point gap becomes a warning), `thresholds`.
A deviation of 2% or more becomes a warning. When the sources spread more than 2% and at
least three independent ones answered, the one farthest from the median of the others is marked
`outlier`, shown but left out of the median, with both prices in a warning. DefiLlama is looked up
by CoinGecko's id, so it does not vote and is excluded with CoinGecko. A CoinGecko 429 is retried
once after its Retry-After (at most 10 s); `COINGECKO_API_KEY` sends a demo key. The council's
Technician sees this section as `price_check`; the calibration step uses the median only when
RYO cannot supply a price, and records that in the outcome.

### `technicals_crosscheck`

`data`: `method{indicators: wilder, period: 14, candles}`, `candle_source`, `warmup_candles`,
`daily_candles`, `as_of` (close of the last closed candle), `close`, `rsi_14`, `atr_14`, `atr_pct`,
`performance_pct{1d,7d,30d,<days>d}`, `reference{rsi_14, atr_14, rsi_diff_points, atr_diff_pct}`,
`thresholds` (10 RSI points, 25% ATR).
Source: 200 closed UTC-day candles from OKX `history-candles` `1Dutc` (`method.candles =
okx_1Dutc`); if OKX fails, Binance `data-api.binance.vision` `klines` `1d` (`binance_1d`); if both
fail, CoinGecko `/coins/{id}/ohlc?days=30` 4-hour candles aggregated to UTC days
(`coingecko_ohlc_4h_to_utc_daily`, with the warning that fewer than 100 candles do not converge).
The day in progress is always dropped. Wilder smoothing forgets its seed geometrically, so 100
closes already agree with 200 to 0.1 RSI point. Fewer than 15 daily candles means
`rsi_14`/`atr_14` are `null` with a warning.

### `positioning_check`

`data`: `symbol`, `gate[]{field, path, ryo_value, verdict, why, same_as?}`, `withheld_paths`,
`okx{premium_bps, funding_rate_bps_8h, interest_bps_8h, premium_state, oi_change_24h_pct_coin,
long_short_ratio, long_short_percentile_100h, long_short_hours}`, `hyperliquid{premium_bps,
premium_state}`, `premium_consensus`, `plain{en, ja}`, `peers_compared`,
`implied_vol{index, dvol, implied_7d_move_pct, as_of}`, `stop_check{atr_stop_pct,
half_implied_7d_move_pct, inside_noise}`, `thresholds`.

Gate verdicts per RYO derivatives field, using only tests that need no definition of RYO's units:
`not_token_specific` (the same value on two or more other symbols the same day),
`conflicts_with_venue` (RYO's and OKX's 24 h OI change have opposite signs, both at least 3%),
`conflicts_with_ryo` (BTC funding exactly 0 while RYO's own sentiment tool reports a value),
`citable` (a venue agrees in sign), `unverified` (nothing to check against), `absent` (RYO
answered null), `not_provided` (no block passed and none fetched). Without
`reference_derivatives`, a public call asks RYO's `deep_analysis` itself when `RYO_MCP_KEY` is set,
and without `peer_derivatives` it takes today's scorecard locks from the ledger.

The headline's premium clause comes from both venues: "OKX -2.6 bps / Hyperliquid +4.2 bps: venues
disagree" when they disagree, and a side is called eager only when every venue that answered puts
the perp on the same side of spot.

DVOL: for BTC and ETH, Deribit's `public/get_volatility_index_data` (daily, last 3 days); the latest
close is the 30-day implied volatility, annualised, and `implied_7d_move_pct = dvol / sqrt(365) x
sqrt(7)`. With `atr_stop_pct`, a stop under half that move warns "stop inside normal 7-day noise".
Deribit publishes no index for other tokens, so for them `deribit_dvol` is `unavailable` with the
warning "no DVOL index for <SYM>" and nothing is estimated.

### `move_base_rate`

`data`: `symbol`, `p`, `hits`, `n_days`, `n_independent`, `tercile`, `tercile_cuts_atr_pct`,
`atr_14_pct_okx_today`, `k_in_okx_atr`, `atr_scale{ryo_atr_14_pct, okx_atr_14_pct, ryo_over_okx}`,
`holdout{fit_p, fit_days, recent_p, recent_days, recent_from}`, `history`, `method`.
Counted from about 400 closed OKX UTC-day candles, on days in the same Wilder ATR(14) tercile as
today. The holdout splits the history in time at three quarters: the tercile cuts are ranked over
the older part only, and an older day counts toward `fit_p` only when its `h`-day window closed
before the split, so the fit never sees the holdout. A gap of more than 10 points between
`fit_p` and `recent_p` becomes a warning.

### `verdict_track_record`

`data`: `symbol`, `horizon_hours`, `lock_days`, `settled`, `open`, `target_first{hits, of_decided}`,
`by_verdict`, `by_confluence_state`, `verdict_against_plan`, `latest`, `method`.
`verdict_against_plan` counts plans whose verdict leans against their own direction: a long
bracket under `cautious`, `bearish`, `avoid` or `negative`, or a short bracket under `constructive`,
`bullish`, `positive` or `accumulate`. A symbol outside the 25-major universe is `unavailable`;
one with nothing settled yet is `partial` and gives no rate.

## Calling them

```bash
uv run nota skill spec                               # definitions as JSON
uv run nota skill run narrative_convergence '{"voices":["tg:WatcherGuru"],"hours":24}'
uv run nota skill run news_verify '{"claim":"SOL ETF approved","symbol":"SOL"}'
uv run nota skill run positioning_check '{"symbol":"BTC","atr_stop_pct":2.5}'
```

Over HTTP: `GET /api/skills`, `POST /api/skills/<name>/invoke` with `{"args": {...}}`, or MCP
`tools/call` on `POST /mcp`.

Inside the council, `--voices tg:a,tg:b` adds a `narrative_signal` section and `--news`
adds `news_check`; the Narrative agent reads both and must cite them by path like any other
evidence.
