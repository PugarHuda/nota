# positioning_check: a drop-in skill for RYO

One folder, no Nota imports. Needs Python 3.10+, `httpx`, and `fastapi` only if you use the router.

## The gap it fills

`deep_analysis` returns `derivatives.{funding_rate_bps, open_interest_change_24h_pct, long_short_ratio}`
with `availability.derivatives = "available"`. On 2026-09-18 funding was 0.0 and the ratio was null for
every token we probed, and the 24 h OI change was exactly -10.44 for ETH, SOL, WIF and ONDO in the same
run. An agent reading that block can't tell a real reading from a placeholder.

This skill checks each field with tests that don't depend on RYO's (undocumented) units, venue or window,
and gives each one a verdict: `not_token_specific`, `conflicts_with_venue`, `conflicts_with_ryo`,
`citable`, `unverified`, `absent` or `not_provided`. It also reports independent venue positioning:
the OKX perp premium, 24 h open interest in coins, the long/short account ratio and its 100-hour
percentile, the Hyperliquid premium as a second venue, and for BTC/ETH the 7-day move implied by
Deribit DVOL as a check on stop distance.

## Ship it in 10 minutes

1. Copy `contrib/positioning_check/` into the backend as a package, e.g. `app/skills_contrib/positioning_check/`.
2. Register the routes:
   ```python
   from app.skills_contrib.positioning_check.fastapi_router import router as positioning_router
   app.include_router(positioning_router)   # GET /api/skills/positioning_check, POST /api/skills/positioning_check/invoke
   ```
   If you'd rather use your existing skill registry, `SKILL_DEFINITION` is a plain dict in your
   `SkillDefinition` shape and `invoke(args) -> dict` returns your `SkillCallResponse`.
3. Optional: let the skill gate RYO's own numbers without the caller passing them in:
   ```python
   invoke(args, fetch_reference=lambda sym: deep_analysis(sym)["data"]["derivatives"])
   ```
   Your router layer has to supply `fetch_reference`, `peer_derivatives` (the same day's derivatives
   blocks for other symbols, which comes from RYO's own cache) and `ryo_btc_funding_bps` (from
   `monitor_market_sentiment_shift`). The skill can't fetch these by itself. When they're missing, the
   gate says `not_provided` or `unverified`, and the venue half still runs.
4. Run `pytest -q path/to/positioning_check`. The tests run offline with `httpx.MockTransport`, no network.

Auth, rate limiting and XP are left to your app: the router adds none. `xp` is 0 and `requires_guard`
is false because the skill only reads data and never touches a wallet or an order.

## Example (a real call, 2026-10-01)

```
POST /api/skills/positioning_check/invoke
{"name": "positioning_check", "args": {"symbol": "SOL",
  "reference_derivatives": {"funding_rate_bps": 0.0, "open_interest_change_24h_pct": -10.44, "long_short_ratio": null},
  "peer_derivatives": [{"symbol": "ETH", "open_interest_change_24h_pct": -10.44},
                       {"symbol": "WIF", "open_interest_change_24h_pct": -10.44}]}}
```

OKX was unreachable from the machine that ran this, so the response also shows how the skill fails
(trimmed):

```json
{"name": "positioning_check", "status": "success", "latency_ms": 665, "xp": 0, "guard_decision": null,
 "result": {"schema_version": "nota-skill-1", "tool": "positioning_check", "status": "partial", "data_mode": "live",
  "as_of": "2026-10-01T12:32:37+00:00",
  "data": {"gate": [
     {"field": "funding_rate_bps", "ryo_value": 0.0, "verdict": "unverified"},
     {"field": "open_interest_change_24h_pct", "ryo_value": -10.44, "verdict": "not_token_specific",
      "why": "the same value -10.44 on ETH, WIF the same day", "same_as": ["ETH", "WIF"]},
     {"field": "long_short_ratio", "ryo_value": null, "verdict": "absent", "why": "RYO returned null"}],
   "withheld_paths": ["deep_analysis.data.derivatives.open_interest_change_24h_pct"],
   "okx": {"premium_bps": null, "oi_change_24h_pct_coin": null, "long_short_ratio": null, "...": null},
   "hyperliquid": {"premium_bps": -4.669, "premium_state": "below_spot"},
   "premium_consensus": "below_spot_1_venues", "implied_vol": null},
  "summary": {"headline": "SOL: 1 of 3 RYO derivatives fields withheld; perp below spot (Hyperliquid -4.669 bps only): more demand to be short"},
  "availability": {"okx_premium": "unavailable", "okx_open_interest": "unavailable", "okx_long_short": "unavailable",
                   "deribit_dvol": "unavailable", "hyperliquid_premium": "available"},
  "warnings": ["okx premium: okx: network error ConnectError", "...",
               "no DVOL index for SOL (Deribit publishes BTC and ETH only)",
               "withheld deep_analysis.data.derivatives.open_interest_change_24h_pct: not_token_specific (...)"]}}
```

## Failure behaviour

- A source that fails gets `availability.<section> = "unavailable"` and a warning that names the cause.
  Its fields stay `null` and nothing is put in their place.
- `result.status` comes from the four venue sections (`okx_premium`, `okx_open_interest`,
  `okx_long_short`, `hyperliquid_premium`): `ok` if all are available, `unavailable` if all failed,
  `partial` otherwise. DVOL and `ryo_reference` are context, so if they fail you get a warning but the
  status stays the same.
- If every venue is down, `result.status` is `unavailable`, `data_mode` is `unknown`, and the
  `SkillCallResponse.status` is `error`. In every other case it is `success`.
- Bad arguments (unknown arg, a symbol that isn't 1-15 letters/digits, a non-number, more than 25
  peers) raise `ValueError`, which the router turns into a 422, before any request goes out.
- `null` from RYO stays `null` (`verdict: absent`). If RYO was never asked, the verdict is
  `not_provided`, which is a different thing.

## Sources

All of them are public and need no key. One call makes 3 OKX requests, 1 Hyperliquid request and, for
BTC/ETH only, 1 Deribit request. None of these sources has an open-data licence. Their data is covered
by each venue's API terms, so check those terms before you redistribute raw values. The rate limits
below are per-IP limits as recalled when this was written (2026-10-01), not re-read from each venue's docs; confirm them. If you serve heavy traffic,
cache responses per symbol for about 60 s.

| source | endpoint | limit |
|---|---|---|
| OKX | `GET /api/v5/public/funding-rate` | 20 req / 2 s |
| OKX | `GET /api/v5/rubik/stat/contracts/open-interest-history` | 5 req / 2 s |
| OKX | `GET /api/v5/rubik/stat/contracts/long-short-account-ratio-contract` | 5 req / 2 s |
| Hyperliquid | `POST /info {"type": "metaAndAssetCtxs"}` | 1200 weight / min, this call weighs 20 |
| Deribit | `GET /api/v2/public/get_volatility_index_data` | credit-based, ample for one call per request |
