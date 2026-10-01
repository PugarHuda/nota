# move_base_rate: a drop-in skill for RYO

One folder, no Nota imports. Needs Python 3.10+, `httpx`, and `fastapi` only if you use the router.

## The gap it fills

`analyze_token` and `deep_analysis` give ATR(14) and a verdict, but no probability. "SOL cautious, ATR
4.1%" does not say how often a 1.5-ATR drop within three days happens anyway. This skill counts it: in
the token's own last ~400 UTC days on OKX, on days in the same volatility tercile as today, how often did
price touch (or close beyond) `k` ATRs away within `h` days. With `k = 0, event = close` it is the plain
"higher after h days" rate, the number any `p_up` forecast has to beat.

Pass RYO's `atr_14_pct` and the distance is measured in RYO's ATR: the skill converts `k` into OKX-ATR
units with today's ratio of the two and prints that ratio (RYO's ATR ran 0.91-0.98 of OKX's Wilder ATR
across the tokens checked on 2026-09-18). A time-split holdout (older three quarters against the newest
quarter) reports whether the rate has drifted; the fit never sees the holdout.

## Ship it in 10 minutes

1. Copy `contrib/move_base_rate/` into the backend as a package, e.g. `app/skills_contrib/move_base_rate/`.
2. Register the routes:
   ```python
   from app.skills_contrib.move_base_rate.fastapi_router import router as base_rate_router
   app.include_router(base_rate_router)   # GET /api/skills/move_base_rate, POST /api/skills/move_base_rate/invoke
   ```
   If you'd rather use your existing skill registry, `SKILL_DEFINITION` is a plain dict in your
   `SkillDefinition` shape and `invoke(args) -> dict` returns your `SkillCallResponse`.
3. Optional: have your router fill `atr_14_pct` from `analyze_token` when the caller leaves it out, so the
   distance is always in the ATR your users see. Without it, `k` is in OKX's own ATR.
4. Run `pytest -q path/to/move_base_rate`. The tests run offline with `httpx.MockTransport`, no network.

Auth, rate limiting and XP are left to your app: the router adds none. `xp` is 0 and `requires_guard`
is false because the skill only reads public candles and never touches a wallet or an order.

## Example (a real call, 2026-10-01)

```
POST /api/skills/move_base_rate/invoke
{"name": "move_base_rate", "args": {"symbol": "SOL", "k": 1.5, "horizon_days": 3, "direction": "down",
  "event": "touch", "atr_14_pct": 4.1}, "conversation_id": null}
```

OKX was unreachable from the machine that ran this (its TLS was intercepted on the way), so the
response shows the failure path, unedited:

```json
{"name": "move_base_rate", "status": "error", "latency_ms": 130, "xp": 0, "guard_decision": null,
 "result": {"schema_version": "nota-skill-1", "tool": "move_base_rate", "status": "unavailable", "data_mode": "unknown",
  "as_of": "2026-10-01T18:11:22+00:00",
  "request": {"symbol": "SOL", "k": 1.5, "horizon_days": 3, "direction": "down", "event": "touch", "atr_14_pct": 4.1, "as_of": null},
  "data": {"symbol": "SOL", "p": null, "event": "touch", "direction": "down", "k": 1.5, "horizon_days": 3,
           "method": "okx_1Dutc_wilder_atr14_same_tercile_count", "atr_scale": null},
  "summary": {"headline": "SOL: base rate unavailable", "key_points": []},
  "availability": {"okx_daily": "unavailable"}, "warnings": ["okx: network error ConnectError"]}}
```

When OKX answers, `data` also carries `p`, `hits`, `n_days`, `n_independent` (overlapping `h`-day windows
share their future), `tercile` and its cuts, `atr_14_pct_okx_today`, `k_in_okx_atr`,
`atr_scale.ryo_over_okx`, `holdout {fit_p, fit_days, recent_p, recent_days, recent_from}` and `history`
(first and last day used), and the headline has the form
`SOL: <p>% of <n_days> <tercile>-volatility days touch -1.5 ATR below within 3d`.

## Failure behaviour

- OKX failing (network, non-200, an error code such as an unlisted pair, a malformed candle) gives
  `availability.okx_daily = "unavailable"`, a warning naming the cause, `p: null`, `result.status =
  "unavailable"`, `data_mode = "unknown"`, and `SkillCallResponse.status = "error"`. No number is put in
  its place.
- Too little history (under 60 usable days, e.g. a new listing) or an `as_of` before every candle gives
  `status = "partial"`, `p: null` and a warning that says why. The response status stays `success`.
- A drift of more than 10 points between the fit and the holdout rate is a warning, not a downgrade.
- RYO's ATR more than 2x or under 0.5x OKX's is a warning: the two may not measure the same asset.
- Bad arguments (unknown arg, a symbol that isn't 1-15 letters/digits, `k` outside 0-10, `horizon_days`
  not an integer 1-14, a direction or event outside its enum, a non-positive ATR, an `as_of` that is not a
  real YYYY-MM-DD date, or `k = 0` with `event = touch`, which every day trivially satisfies) raise
  `ValueError`, which the router turns into a 422, before any request goes out.

## Sources

One public source, no key: OKX `GET /api/v5/market/history-candles` (`bar=1Dutc`, 100 candles per page,
up to 4 pages per call; only confirmed candles are used). The rate limit is 20 requests per 2 s per IP as
recalled when this was written (2026-10-01), not re-read from OKX's docs; confirm it. Daily candles only
change once a day, so cache per symbol for an hour if you serve heavy traffic. OKX data is covered by
OKX's API terms, not an open-data licence: check them before you redistribute raw candles.
