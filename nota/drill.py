"""Failure drills: the real pipeline run offline with one fault injected, next to a healthy run.

Real: `RyoClient` (retries, Retry-After, pacing, error mapping), `gather`, the council's citation check,
`council_without_primary`, the risk gates, `build_receipt`, and in `llm_down` the real `OpenAICompatLLM`
retry loop. Stood in for: the network (an httpx.MockTransport serving RYO's recorded answers from
fixtures/recorded), the model (DrillLLM: fixed, labelled output), the exchanges (DrillExchanges echo
RYO's recorded price), the clock (waits are counted, never slept) and the ledger (":memory:", thrown
away). A drill receipt is labelled source "drill" and is never stored anywhere.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from nota import paths
from nota.council import Citation, Opinion, Verdict
from nota.decide import decide
from nota.evidence import SECTIONS, first_present, ryo_args
from nota.ledger import Ledger
from nota.llm import OpenAICompatLLM
from nota.ryo_client import RecordedRyoClient, RyoClient
from nota.skills.contract import SourceUnavailable
from nota.skills.price_check import PRICE_LEGS, price_crosscheck

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "recorded"
SYMBOL = "SOL"  # ponytail: the one token every recorded tool has, compare included
MODEL = "drill-fixed-output"
DRILL_TEXT = "DRILL: fixed output, no model was called."

# scenario -> (the real event it mirrors, a real receipt of it or None)
SCENARIOS: dict[str, tuple[str, str | None]] = {
    "ryo_down": ("Every RYO tool answers 503 until the client gives up, as in a full upstream outage.", None),
    "ryo_401": ("RYO refuses the key with 401 UNAUTHENTICATED, as it did to this deployment on 2026-09-10.", "2ae531ec2c9b"),
    "deep_analysis_missing": ("deep_analysis fails with VALIDATION_FAILED while the other tools answer, "
                              "as in RYO's three-day deep_analysis outage from 2026-09-27.", "c60acf3ba3b1"),
    "rate_limited": ("Every tool's first call is refused with RYO's real fan-out 429 (Retry-After: 60) and "
                     "deep_analysis times out once, as when several runs share one key.", None),
    "exchange_down": ("All five independent exchange prices fail, as when an ISP DNS-blocks Coinbase and Kraken.", None),
    "llm_down": ("The model provider answers 503 on every retry.", None),
    "partial": ("RYO answers status partial with RSI, ATR and Fear & Greed null, as its optional lanes often do.", None),
}
NULLED = ("deep_analysis.data.trade_plan.atr_14_usd", "deep_analysis.data.technical_analysis.atr_14_pct",
          "deep_analysis.data.technical_analysis.rsi_14", "analyze_token.data.technical_analysis.atr_14_pct",
          "analyze_token.data.technical_analysis.rsi_14", "market_overview.data.sentiment.fear_greed_index")
CITES = (paths.PRICE_USD[0], paths.RSI_14[0], paths.ATR_14[0], paths.FEAR_GREED[0])
SYNTHETIC = ["model output: DrillLLM returns fixed opinions and a fixed judge verdict; no model is called",
             "exchange prices: each exchange 'answers' with RYO's recorded price; no exchange is called",
             "time: Retry-After and pacing waits are added to a counter, not slept"]
TOOLS = {tool: key for key, tool in SECTIONS.items()}


class _Clock:
    def __init__(self) -> None:
        self.t = self.waited = 0.0
        self._lock = threading.Lock()

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        with self._lock:
            self.t += s
            self.waited += s


class DrillLLM:
    """Fixed, labelled council output. Every role cites the same four paths, so the code that checks
    citations against the evidence is what decides which survive."""

    model = MODEL
    last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "usd": 0.0}

    def complete_json(self, system: str, user: str, schema: Any) -> Any:
        if schema is Verdict:
            return Verdict(action="long", p_up_7d=0.62, rationale=f"{DRILL_TEXT} The judge always answers long at 0.62 "
                           "in a drill, so every block below comes from code, not from the model.",
                           agreed_with=["macro", "technician", "narrative"])
        # role: run_council stamps the real one
        return Opinion(role="drill", stance="bullish", p_up_7d=0.62, confidence="medium", thesis=DRILL_TEXT,
                       citations=[Citation(path=p, note="drill citation") for p in CITES], invalidation=DRILL_TEXT)


class DrillExchanges:
    """The five exchange legs of `price_crosscheck`: RYO's recorded price, or the error `get_json` raises
    for a refused request. Fear & Greed is never asked in a drill."""

    def __init__(self, price: float | None, down: bool):
        self.price, self.down = price, down

    def fear_greed(self) -> tuple[int, str, str | None]:
        raise SourceUnavailable("alternative.me: not called in a drill")


def _leg(name: str) -> Any:
    def get(self: DrillExchanges, symbol: str) -> tuple[float, str | None]:
        if self.down or self.price is None:
            raise SourceUnavailable(f"{name}: HTTP 503")
        return self.price, None
    return get


for _n in PRICE_LEGS:
    setattr(DrillExchanges, _n, _leg(_n))


def _err(status: int, code: str, message: str, retry_after: str | None = None) -> httpx.Response:
    return httpx.Response(status, json={"code": code, "message": message, "trace_id": f"drill-{status}"},
                          headers={"Retry-After": retry_after} if retry_after else None)


def _null(env: dict[str, Any], tool: str) -> dict[str, Any]:
    """`partial`: RYO's own shape for a thin answer - status partial, the field present and null, a warning."""
    key = TOOLS[tool]
    hit = False
    for p in NULLED:
        sec, _, rest = p.partition(".data.")
        if sec != key:
            continue
        *parents, leaf = rest.split(".")
        node = env["data"]
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = None
        hit = True
    if hit:
        env["status"] = "partial"
        env["warnings"] = [*env.get("warnings", []), "drill: some inputs were unavailable for this snapshot"]
    return env


def _ryo_transport(scenario: str, recorded: RecordedRyoClient, log: list[tuple[str, int]]) -> httpx.MockTransport:
    seen: Counter[str] = Counter()

    def handle(request: httpx.Request) -> httpx.Response:
        tool = request.url.path.split("/")[-2]
        seen[tool] += 1  # one tool per gather thread, so no two threads touch the same key
        if scenario == "ryo_down":
            resp = _err(503, "SERVICE_UNAVAILABLE", "drill: RYO is down", retry_after="2")
        elif scenario == "ryo_401":
            resp = _err(401, "UNAUTHENTICATED", "Invalid or expired credential.")
        elif scenario == "deep_analysis_missing" and tool == "deep_analysis":
            resp = _err(422, "VALIDATION_FAILED", f"Current USD market data is unavailable for {SYMBOL}.")
        elif scenario == "rate_limited" and seen[tool] == 1 and tool == "deep_analysis":
            log.append((tool, 0))
            raise httpx.ReadTimeout("drill: no answer in time", request=request)
        elif scenario == "rate_limited" and seen[tool] == 1:
            resp = _err(429, "RATE_LIMITED", "Rate limit exceeded (6/min for mcp_fanout).", retry_after="60")
        else:
            env = recorded.call(tool, json.loads(request.content or b"{}")).model_dump(mode="json")
            resp = httpx.Response(200, json={"result": _null(env, tool) if scenario == "partial" else env})
        log.append((tool, resp.status_code))
        return resp

    return httpx.MockTransport(handle)


def _llm_down() -> OpenAICompatLLM:
    """The real OpenAI-compatible client, retrying a provider that answers 503 every time."""
    llm = OpenAICompatLLM(model=MODEL, base_url="http://llm.drill/v1", api_key="drill")
    llm.http = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(503, json={"error": "drill: the model provider is down"})))
    llm.sleep = lambda s: None
    return llm


def run(scenario: str, symbol: str = SYMBOL, root: Path = FIXTURES) -> dict[str, Any]:
    """One drill run. `scenario` is a key of SCENARIOS, or "healthy" for the baseline."""
    if scenario != "healthy" and scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; one of healthy, {', '.join(SCENARIOS)}")
    recorded = RecordedRyoClient(root)
    clock, log = _Clock(), []
    src = RyoClient(base_url="http://ryo.drill/api/mcp", key="drill", transport="rest", sleep=clock.sleep, clock=clock.now,
                    http=httpx.Client(transport=_ryo_transport(scenario, recorded, log)))
    # It serves RYO's recorded answers, which is what lets the risk gates treat it like a recorded run; the
    # receipt is relabelled "drill" below and never reaches a ledger, a score or a feed.
    src.name = "recorded"
    ryo_price = recorded.call("deep_analysis", ryo_args(symbol)["deep_analysis"]).get("market.price_usd")

    def price_check(sym: str, pack: Any) -> Any:
        path, price = first_present(pack, paths.PRICE_USD)
        return price_crosscheck(sym, reference_price=price, reference_path=path,
                                exchanges=DrillExchanges(ryo_price, scenario == "exchange_down"))

    ledger = Ledger(":memory:")
    llm = _llm_down() if scenario == "llm_down" else DrillLLM()
    receipt, error = None, None
    try:
        receipt = decide(symbol, src, llm, ledger, extras={"price_check": price_check}).model_copy(update={"source": "drill"})
    except Exception as exc:  # what a failing run surfaces is the point of the drill
        error = f"{type(exc).__name__}: {exc}"
    count = lambda t: ledger.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    out: dict[str, Any] = {
        "scenario": scenario, "symbol": symbol, "source": "drill", "stored": False,
        "mirrors": SCENARIOS.get(scenario, ("RYO and every source answer as recorded.", None))[0],
        "real_receipt": SCENARIOS.get(scenario, ("", None))[1],
        "outcome": "error" if receipt is None else receipt.trade.kind,
        "error": error, "receipt": receipt.model_dump(mode="json") if receipt else None,
        "ryo_calls": {"attempts": len(log), "by_status": dict(Counter(str(s or "timeout") for _, s in log)),
                      "simulated_wait_s": round(clock.waited, 1)},
        "throwaway_ledger": {"evidence_packs": count("evidence"), "decisions": count("decisions")},
        "synthetic": SYNTHETIC,
    }
    out["what_happened"] = _story(out, receipt)
    return out


def _story(out: dict[str, Any], r: Any) -> list[str]:
    calls = out["ryo_calls"]
    by = calls["by_status"]
    lines = [f"RYO was asked {calls['attempts']} times for {len(SECTIONS)} tools: "
             + ", ".join(f"{n} x {'timeout' if s == 'timeout' else 'HTTP ' + s}" for s, n in sorted(by.items())) + "."]
    if any(s in by for s in ("429", "503", "timeout")):
        lines.append(f"The client retried: it honoured Retry-After and RYO's six-calls-a-minute pacing, "
                     f"{calls['simulated_wait_s']:g} s of waiting in a real run (counted here, not slept).")
    if "401" in by or "422" in by:
        lines.append("A 401 or 422 is not retried: a revoked key or a refused argument does not fix itself.")
    if r is None:
        lines.append(f"The run stopped with {out['error']}")
        lines.append(f"No receipt and no verdict were produced; nothing was filled in. The evidence pack was kept "
                     f"({out['throwaway_ledger']['evidence_packs']} pack, {out['throwaway_ledger']['decisions']} decisions), "
                     "so a rerun with a working model decides on the same evidence.")
        return lines
    bad = {k: v for k, v in r.availability.items() if v != "ok"}
    lines.append("Every section came back ok." if not bad else
                 "Sections not ok: " + ", ".join(f"{k} {v}" for k, v in bad.items()) + "; each keeps the reason it gave.")
    if not r.opinions:
        lines.append("deep_analysis is missing, so the council was not convened: no_trade, zero model calls.")
    else:
        dropped = sum(o.dropped_citations for o in r.opinions)
        if dropped:
            lines.append(f"{dropped} citations pointed at values that are null in the evidence and were dropped in code; "
                         "null stayed null, nothing became 0.")
    t = r.trade
    if t.kind == "trade" and bad.get("price_check"):
        lines.append("The exchange cross-check is marked unavailable and its median stays null; exchange prices never "
                     "replace RYO's, so the trade is still sized from RYO's own price.")
    lines.append(f"Practice trade: {t.side} {t.size_usd:g} USD at {t.entry_price:g}." if t.kind == "trade"
                 else f"No trade: {t.reason}.")
    return lines


def compare(scenario: str) -> dict[str, Any]:
    """A drill next to the healthy baseline on the same recording, which is what the /drill page shows."""
    d, base = run(scenario), run("healthy")
    if d["receipt"] and d["receipt"]["id"] == base["receipt"]["id"]:
        d["what_happened"].append(f"Receipt {d['receipt']['id']} is the healthy run's own: same evidence hash, same "
                                  "decision. The failure cost time, not correctness.")
    return {"drill": d, "baseline": base}
