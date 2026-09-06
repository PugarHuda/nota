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
| `voices` | array[string] | yes | `tg:<channel>` (Telegram public preview, free) or `x:<handle>` (via Tavily, best effort) |
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

`data`: `claim`, `sources[]{title,url,domain,score,published_date,snippet}`, `distinct_domains`,
`domains`, `top_score`, `verdict` (`corroborated` >= 3 domains scoring >= 0.5, `weak` 1-2,
`unverified` 0, `null` when search failed), `thresholds`, `market_context{...}`.
`availability`: `search` (primary), `market`.

## Calling them

```bash
uv run arena skill spec                               # definitions as JSON
uv run arena skill run narrative_convergence '{"voices":["tg:WatcherGuru"],"hours":24}'
uv run arena skill run news_verify '{"claim":"SOL ETF approved","symbol":"SOL"}'
```

Inside the council, `--voices tg:a,tg:b` adds a `narrative_signal` section and `--news`
adds `news_check`; the Narrative agent reads both and must cite them by path like any other
evidence.
