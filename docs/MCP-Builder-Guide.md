# RYO Builder MCP Guide

Build agents and applications with RYO's authenticated, read-only market research
tools. This guide covers setup, discovery, request formats, the public JSON contract,
the six available tools, and reliable integration patterns.

**Last updated:** August 13, 2026  
**MCP endpoint:** `https://app-ryochan.com/api/mcp`  
**Protocol version:** `2024-11-05`

---

## Access and setup

Store the endpoint and builder credential on your server or development machine:

```bash
export RYO_MCP_URL="https://app-ryochan.com/api/mcp"
export RYO_MCP_KEY="ryo_mcp_your_private_key"
```

Recommended `.env.example`:

```dotenv
RYO_MCP_URL=https://app-ryochan.com/api/mcp
RYO_MCP_KEY=
```

Never commit a live credential. A browser application should call RYO through its
own server route so the key is not included in client-side JavaScript.

### Check server health

Health does not require authentication:

```bash
curl -s "$RYO_MCP_URL/health" | jq
```

Expected structure:

```json
{
  "status": "ok",
  "protocol_version": "2024-11-05",
  "server": "ryo-chan",
  "tools": 6
}
```

### Check your credential and quota

```bash
curl -s "$RYO_MCP_URL/whoami" \
  -H "Authorization: Bearer $RYO_MCP_KEY" | jq
```

This returns the key identifier, label, scope, expiry, current quota, and published
tool names. Reading `whoami` does not consume tool-call quota.

### Discover the live catalog

```bash
curl -s "$RYO_MCP_URL/tools" \
  -H "Authorization: Bearer $RYO_MCP_KEY" | jq
```

The catalog is authoritative. Each entry includes its name, public description, and
JSON input schema. Read it at startup instead of hard-coding assumptions.

---

## Six independent research tools

Every tool is independently callable. A builder does not need to call
`market_overview` first, and no tool depends on the output or state of another tool.
Choose only the calls that the product needs.

| Tool | Required input | Optional input | Best used for |
|---|---|---|---|
| `market_overview` | None | None | Market regime, totals, sentiment, breadth, and movers |
| `scan_market` | None | `chain`, `theme`, `top_n` | Producing a ranked research shortlist |
| `analyze_token` | `symbol` | None | Fast token market and technical analysis |
| `deep_analysis` | `symbol` | `include_perp` | A comprehensive evidence pack for one token |
| `compare_tokens` | `symbols` | `intent` | Comparing two to four assets on common factors |
| `monitor_market_sentiment_shift` | None | `time_window` fixed to `7d` | Checking a seven-day sentiment change |

The surface is intentionally read-only:

- It cannot create or access a RYO wallet.
- It cannot read user balances, positions, or portfolio state.
- It cannot place, approve, or execute a trade.
- Token analysis accepts asset symbols, not wallet addresses.
- It does not publish a portfolio-analysis or symbol-only safety tool.

Your application owns its users, state, decisions, and execution logic.

---

## Make a request

### REST

REST is the fastest way to test a tool:

```bash
curl -s -X POST "$RYO_MCP_URL/tools/analyze_token/call" \
  -H "Authorization: Bearer $RYO_MCP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"SOL"}' | jq
```

Use the bare argument object shown above.

### MCP JSON-RPC

Initialize:

```bash
curl -s -X POST "$RYO_MCP_URL" \
  -H "Authorization: Bearer $RYO_MCP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | jq
```

List tools:

```bash
curl -s -X POST "$RYO_MCP_URL" \
  -H "Authorization: Bearer $RYO_MCP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | jq
```

Call a tool:

```bash
curl -s -X POST "$RYO_MCP_URL" \
  -H "Authorization: Bearer $RYO_MCP_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "deep_analysis",
      "arguments": {"symbol": "SOL", "include_perp": true}
    }
  }' | jq
```

A successful MCP result contains a text content block. Its `text` value is a JSON
string containing the public result envelope. Parse it once before reading fields.
Always check `result.isError`; a tool execution error follows MCP behavior and can be
returned with HTTP `200` and `isError: true`.

### Generic remote MCP configuration

Many clients accept a configuration similar to this:

```json
{
  "mcpServers": {
    "ryo": {
      "url": "https://app-ryochan.com/api/mcp",
      "headers": {
        "Authorization": "Bearer ${RYO_MCP_KEY}"
      }
    }
  }
}
```

Configuration syntax varies by client. Keep the credential outside tracked files.

---

## Public response contract

Every successful call uses the same top-level fields:

| Field | Meaning |
|---|---|
| `schema_version` | Version of the public builder envelope |
| `tool` | Tool that produced the result |
| `status` | `ok`, `partial`, or `unavailable` |
| `data_mode` | `live`, `mixed`, `simulated`, or `unknown` |
| `as_of` | Observation or generation time |
| `request` | Normalized arguments used for the call |
| `data` | Tool-specific structured evidence |
| `summary` | Short headline and key points |
| `availability` | Availability by result section |
| `warnings` | Important limitations for this result |

Use `data` for application logic. `summary` is designed for display and quick agent
orientation; it is not a replacement for structured fields.

Status meanings:

- `ok`: all primary evidence required for that tool is available.
- `partial`: one or more primary sections are incomplete.
- `unavailable`: the tool could not produce enough primary evidence.

Optional evidence has its own availability and warning. An unavailable optional
profile does not downgrade otherwise complete market and technical evidence. Never
convert an unavailable or `null` measurement to zero.

---

## Tool reference

### `market_overview`

```json
{}
```

Returns market regime, totals, dominance, sentiment, breadth, and top gainers and
losers. Breadth and movers are calculated from one current broad-market listing
snapshot, excluding stable assets from the advancing-versus-declining calculation.

This tool is useful context, but it is never a prerequisite for another call.

### `scan_market`

```json
{
  "chain": "bsc",
  "theme": "news",
  "top_n": 5
}
```

All inputs are optional. `theme` provides context; it is not a strict news filter.
If a project requires verified news matching, combine the scan with the builder's
chosen news source.

### `analyze_token`

```json
{"symbol":"SOL"}
```

Returns current USD market data, multi-window performance, calculated technical
measurements such as RSI(14) and ATR(14), market intelligence, availability, summary,
and warnings. It does not make a symbol-only safety claim.

### `deep_analysis`

```json
{
  "symbol": "SOL",
  "include_perp": true
}
```

Returns market context, performance, technicals, confluence, a deterministic verdict,
optional token-profile evidence, optional derivatives evidence, catalysts, risks, and
an ATR-based preview plan when enough price history is available.

The token profile is optional enrichment. Its own `partial` or `unavailable` status
does not invalidate complete primary market and technical data. Set `include_perp` to
`false` when the product does not need derivatives evidence.

The tool accepts a token symbol such as `SOL`, `BTC`, or `CAKE`. It does not accept a
wallet address and never places a trade.

### `compare_tokens`

```json
{
  "symbols": "SOL, AVAX, BNB",
  "intent": "swing"
}
```

Supply two to four distinct symbols as one comma- or space-separated string. Optional
`intent` values are `swing`, `hold`, and `spot`. The live catalog schema is
authoritative for client-side validation.

Every asset is evaluated from its own fresh read. The comparison uses factors that
are available for every candidate: momentum, market activity, and measured
volatility. Optional token profiles are returned with honest per-token coverage but
do not unfairly score one asset when the same evidence is absent for another.

### `monitor_market_sentiment_shift`

Default call:

```json
{}
```

Explicit call:

```json
{"time_window":"7d"}
```

The comparison window is fixed to seven days. The evidence pack can include sentiment
level and change, market-wide phase, derivatives context, observation dates, coverage,
and explicit gaps.

---

## Integration examples

### Python REST client

```python
import os

import httpx

BASE = os.environ["RYO_MCP_URL"].rstrip("/")
HEADERS = {"Authorization": f"Bearer {os.environ['RYO_MCP_KEY']}"}


def call_ryo(tool: str, arguments: dict | None = None) -> dict:
    response = httpx.post(
        f"{BASE}/tools/{tool}/call",
        headers=HEADERS,
        json=arguments or {},
        timeout=60.0,
    )
    response.raise_for_status()
    return response.json()["result"]


analysis = call_ryo("analyze_token", {"symbol": "SOL"})
print(analysis["status"], analysis["summary"]["headline"])
```

### TypeScript REST client

```typescript
const base = process.env.RYO_MCP_URL!.replace(/\/$/, "");
const key = process.env.RYO_MCP_KEY!;

async function callRyo(
  tool: string,
  arguments_: Record<string, unknown> = {},
) {
  const response = await fetch(`${base}/tools/${tool}/call`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(arguments_),
  });

  if (!response.ok) {
    throw new Error(`RYO ${response.status}: ${await response.text()}`);
  }

  return (await response.json()).result;
}

const comparison = await callRyo("compare_tokens", {
  symbols: "SOL, AVAX",
  intent: "swing",
});
console.log(comparison.summary.headline);
```

### Parse MCP text content

```typescript
function parseRyoMcpResult(response: {
  result: {
    content: Array<{ type: string; text?: string }>;
    isError: boolean;
  };
}) {
  if (response.result.isError) {
    throw new Error(response.result.content[0]?.text ?? "RYO tool failed");
  }

  const text = response.result.content.find((item) => item.type === "text")?.text;
  if (!text) throw new Error("RYO returned no text content");
  return JSON.parse(text);
}
```

---

## Recommended usage patterns

The tools can be chained when a product benefits from a research funnel:

```text
scan_market
     |
analyze_token on selected candidates
     |
deep_analysis on a final shortlist
```

They can also be called directly:

```text
user asks about SOL ──> analyze_token(SOL)
user compares assets ─> compare_tokens(SOL, AVAX)
user wants depth ──────> deep_analysis(SOL)
```

Do not add an unnecessary `market_overview` call merely to unlock another tool. Use
it only when broad-market context improves the builder's own experience.

---

## Rate limits, retries, and errors

Read the current limit with `GET /api/mcp/whoami`. Inspect these response headers:

- `X-RateLimit-Limit`
- `X-RateLimit-Remaining`
- `X-RateLimit-Reset`
- `Retry-After` when rate-limited

Recommended behavior:

- Avoid tight polling loops.
- Cache a result only when that fits the product; tools do not require shared cache.
- Use exponential backoff with jitter for `429`, `503`, and temporary network errors.
- Do not retry invalid arguments or unknown tools without changing the request.
- Use `analyze_token` when a quick read is enough; use `deep_analysis` for added depth.
- Read the tool catalog without spending tool-call quota.

REST errors include an HTTP status, stable code, message, and trace identifier. MCP
protocol errors appear in the top-level `error` member; tool failures return
`result.isError: true`.

When requesting support, provide the tool name, timestamp and timezone, sanitized
arguments, response status, and trace identifier. Never share a builder credential.

---

## Security and responsible use

- Keep the builder credential server-side.
- Commit `.env.example`, never a populated `.env`.
- Report an exposed credential immediately.
- Use only the tools returned by the authenticated catalog.
- Do not probe for wallet, account, or trading capabilities.
- Do not present research output as guaranteed safety, financial advice, or an
  executed trade.
- Make important decisions from structured evidence, not only summary text.

The live catalog at `GET /api/mcp/tools` is the final source of truth if this guide
and the deployed server ever differ.
