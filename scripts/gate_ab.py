"""Controlled comparison: the same evidence, with and without the positioning_check gate.

    uv run python scripts/gate_ab.py <receipt id>

Takes a stored receipt's evidence pack, drops only the `positioning_check` section, and runs the
council on that copy (four fresh model calls: a different pack hash is a different cache key). Writes
both sides to docs/gate-ab/<id>.json and prints what changed. The result is published whatever it
shows, including no difference: that is the point of running it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from nota.calibration import role_scores, role_weights
from nota.cli import _ledger, _llm
from nota.council import run_council
from nota.evidence import EvidencePack
from nota.receipt import Receipt

load_dotenv()


def side(opinions: list, verdict) -> dict:
    return {"verdict": {"action": verdict.action, "p_up_7d": verdict.p_up_7d},
            "opinions": {o.role: {"stance": o.stance, "p_up_7d": o.p_up_7d, "confidence": o.confidence,
                                  "cited": [f"{c.path} = {c.value}" for c in o.citations]} for o in opinions}}


def main(receipt_id: str) -> None:
    import os

    led = _ledger()
    r = Receipt.model_validate_json(led.get_decision(receipt_id))
    pack = EvidencePack.model_validate_json(led.get_pack(r.pack_hash))
    if "positioning_check" not in pack.sections:
        raise SystemExit(f"{receipt_id} was decided without positioning_check; nothing to compare")
    ungated = pack.model_copy(deep=True)
    del ungated.sections["positioning_check"]
    council = run_council(ungated, _llm(os.environ.get("NOTA_LLM", "anthropic")), led,
                          weights=role_weights(role_scores(led)), prompt_version=r.prompt_version)
    out = {"receipt": receipt_id, "symbol": r.symbol, "model": r.model, "prompt_version": r.prompt_version,
           "withheld": pack.withheld(), "gated_pack_hash": r.pack_hash, "ungated_pack_hash": ungated.pack_hash(),
           "with_gate": side(r.opinions, r.verdict), "without_gate": side(council.opinions, council.verdict)}
    path = Path("docs/gate-ab") / f"{receipt_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    withheld = set(out["withheld"])
    for role in out["with_gate"]["opinions"]:
        a, b = out["with_gate"]["opinions"][role], out["without_gate"]["opinions"][role]
        leaked = [c for c in b["cited"] if c.split(" = ")[0] in withheld]
        print(f"{role:10} gated p={a['p_up_7d']:.2f} {a['stance']:8} | ungated p={b['p_up_7d']:.2f} {b['stance']:8}"
              + (f" | ungated cites withheld: {leaked}" if leaked else ""))
    print(f"verdict    gated {out['with_gate']['verdict']} | ungated {out['without_gate']['verdict']}")
    print(f"written to {path}")


if __name__ == "__main__":
    main(sys.argv[1])
