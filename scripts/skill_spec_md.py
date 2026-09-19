"""Regenerate the per-skill reference in docs/skills/SKILL-SPEC.md from the skill definitions.

    uv run python scripts/skill_spec_md.py          # rewrite the block between the markers
    uv run python scripts/skill_spec_md.py --check  # exit 1 when the doc has drifted from the code

The argument tables come straight from `nota.skills.SKILLS`, so the spec cannot describe an argument
the code does not take. The availability keys are listed here, next to the generator, because they
are decided inside each skill's body; tests/test_skill_spec.py fails when this block and the doc
differ, so a change to either shows up in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nota.skills import SKILLS  # noqa: E402
from nota.skills.price_check import PRICE_LEGS  # noqa: E402

DOC = ROOT / "docs" / "skills" / "SKILL-SPEC.md"
START, END = "<!-- generated:start -->", "<!-- generated:end -->"

# (key, primary, what it covers). Primary keys decide `status`; the others are context and only warn.
AVAILABILITY: dict[str, list[tuple[str, bool, str]]] = {
    "narrative_convergence": [("<voice id>", True, "one key per voice, e.g. `tg:WatcherGuru`, `bs:alice.bsky.social`, `x:handle`")],
    "news_verify": [("headlines", True, "RSS pass over CoinDesk, Cointelegraph, The Block and Decrypt"),
                    ("search", True, "Tavily, or Venice web search when no Tavily key is set"),
                    ("market", False, "RYO `analyze_token` for `symbol`")],
    "price_crosscheck": [(leg, True, "spot price leg; `outlier` when excluded from the median") for leg in PRICE_LEGS]
    + [("fear_greed", False, "alternative.me Fear & Greed index")],
    "technicals_crosscheck": [("ohlc", True, "closed daily candles: OKX, then Binance, then CoinGecko")],
    "positioning_check": [("okx_premium", True, "OKX perp premium over spot and funding"),
                          ("okx_open_interest", True, "OKX coin-terms open interest, 25 hourly points"),
                          ("okx_long_short", True, "OKX long/short account ratio, 100 hourly points"),
                          ("hyperliquid_premium", True, "Hyperliquid perp premium over its oracle"),
                          ("ryo_reference", False, "RYO `deep_analysis` derivatives, fetched when none is passed"),
                          ("ledger_peers", False, "same-day scorecard locks as the peer cross-section"),
                          ("deribit_dvol", False, "Deribit DVOL for BTC/ETH; `unavailable` for every other token")],
    "move_base_rate": [("okx_daily", True, "about 400 closed OKX UTC-day candles")],
    "verdict_track_record": [("ledger", True, "the scorecard's locks and settlements")],
}


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render() -> str:
    out = [START, ""]
    for name, (d, _) in SKILLS.items():
        out += [f"### `{name}`", "", _cell(d.description), ""]
        out += ["| arg | type | required | enum | description |", "|---|---|---|---|---|"]
        for a in d.args:
            kind = f"array[{(a.items or {}).get('type', 'string')}]" if a.type == "array" else a.type
            enum = ", ".join(f"`{e}`" for e in a.enum) if a.enum else ""
            out.append(f"| `{a.name}` | {kind} | {'yes' if a.required else 'no'} | {enum} | {_cell(a.description)} |")
        out += ["", "| availability key | primary | covers |", "|---|---|---|"]
        for key, primary, what in AVAILABILITY[name]:
            out.append(f"| `{key}` | {'yes' if primary else 'no'} | {_cell(what)} |")
        primary = [f"`{k}`" for k, p, _ in AVAILABILITY[name] if p]
        out += ["", f"`status`: `ok` when every primary key ({', '.join(primary)}) is `available`, `unavailable` when every "
                    "one failed, else `partial`. Read-only, `requires_guard: false`.", ""]
    out.append(END)
    return "\n".join(out)


def splice(doc: str) -> str:
    head, _, rest = doc.partition(START)
    _, _, tail = rest.partition(END)
    return head + render() + tail


if __name__ == "__main__":
    current = DOC.read_text(encoding="utf-8")
    wanted = splice(current)
    if "--check" in sys.argv:
        sys.exit(0 if current == wanted else 1)
    DOC.write_text(wanted, encoding="utf-8", newline="\n")
