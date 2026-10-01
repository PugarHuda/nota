# Limitations

What Nota does not do, or does only partly. Each item says where it shows.

## Keys and the hosted demo
- A council decision needs one LLM key, and live RYO evidence needs the builder key. Verifying a
  receipt, the scorecard and most skills need neither.
- The hosted demo reads a ledger snapshot (`data/demo.db`) that the ledger cycle commits, because a
  serverless filesystem cannot be written. It is as fresh as the last cycle: at most an hour old for
  settlements, and twice a day for new decisions.
- "Run the council live" on the hosted demo does not store its receipt in that snapshot. Its roles
  are weighted 1.0 rather than by their record, and the response says so. Each address may run it
  3 times an hour, and all visitors together 20 times an hour.
- Backings go to Postgres (Neon, free tier) on the hosted demo. In a plain clone with no
  `DATABASE_URL`, they go to the ledger's own table.

## Sources
- `x:` voices are best effort. X's public syndication endpoint has answered `HTTP 429` to ordinary
  connections since 2026-09-10. The fallbacks and how much each one covers are listed in
  `docs/skills/SKILL-SPEC.md`, and every fallback a run takes is named in that run's warnings.
  `tg:` and `bs:` voices answer directly.
- RYO's optional token-profile lane is often `partial` or `unavailable`. Nota keeps it as RYO
  reports it and never fills it in.
- Some RYO derivatives fields carry the same value across unrelated tokens on the same day. The
  gate withholds them, so on those days the technician sees less derivatives evidence.
- Some exchanges (Coinbase, Kraken) are blocked from some networks. When fewer exchanges answer,
  `price_crosscheck` reports fewer, and an answer is never invented for one that failed.

## Statistics
- Brier weights move only once calls reach their seven-day horizon, and then only by n/(n+20) of
  the way. Every table beside them shows how many of its calls come from independent weeks.
- The scorecard began on 18 September 2026 and so far covers one market regime. Its contrasts read
  "not yet distinguishable" until ten lock days exist on both sides.
- In settlement, a stop and a target touched within the same minute stay `ambiguous` rather than
  being ordered by guess. A lock whose RYO price is more than 2% away from OKX's is kept but never
  settled.
- The "What changed" ranking is a documented heuristic, not a model.

## Scope
- Read-only. Nota never places an order, and practice trades exist only in its ledger.
- This is not financial advice. The trades are practice trades.
