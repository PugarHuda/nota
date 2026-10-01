# Decisions

The choices that shape Nota, why each was made, and what was given up.

1. **Receipts that replay instead of logs.** A decision is stored together with its evidence pack
   and the model's cached answers, so `nota replay` can rebuild it byte for byte.
   *Given up:* ledger size. Every pack is stored in full.
2. **Citations are paths, not numbers.** The model names a dotted path into RYO's answer. Code
   reads the value back from that path and drops any path that does not exist.
   *Given up:* fluent prose. The model cannot quote a figure it was not given.
3. **Null stays null.** A source that fails becomes `unavailable` with its reason; it never
   becomes 0 and never becomes a plausible default. The council refuses to trade without
   `deep_analysis`.
   *Given up:* on a bad RYO day there are no trades at all (26–28 Sep 2026).
4. **Audit RYO, do not just consume it.** The scorecard locks RYO's own plan before the outcome
   is known, and OKX settles it, so RYO never grades itself.
   *Given up:* the scorecard needs weeks of data before its contrasts can be read.
5. **A gate before the council.** RYO derivatives fields that repeat across unrelated tokens are
   withheld in code, and the agents are never asked to notice them.
   *Given up:* fewer derivatives inputs on the days the gate fires.
6. **Scored against the base rate, not a coin flip.** Each role's Brier score is compared with
   how often the token rose anyway. Weights move by n/(n+20) of the way, so a few calls cannot
   swing them.
7. **SQLite plus a committed snapshot, not a hosted database.** The ledger is one file that CI
   commits, OpenTimestamps anchors, and Sigstore attests. Only backings, which visitors write,
   live in Postgres.
   *Given up:* the hosted demo is up to an hour behind.
8. **Skills in RYO's own shape.** They use the same envelope and the same `/api/skills/{name}/invoke`
   path as RYO, and are also served over MCP and A2A. `contrib/positioning_check/` has no Nota
   imports, so RYO can include it as is.
9. **Read-only by design.** Nota holds no wallet and places no order. Practice trades are rows in
   the ledger.
10. **A live council that is not stored.** The hosted demo can convene the council on fresh
    evidence, but the receipt it returns does not enter the snapshot. The snapshot holds only
    what the scheduled cycle recorded, so no visitor can add rows to it.
