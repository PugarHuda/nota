"""The spoken script for the walkthrough, and the neural voice that reads it.

    uv run --with edge-tts python scripts/narration.py

Writes one mp3 per beat into `docs/demo/audio/` plus `beats.json` carrying each beat's real
measured duration. The recorder reads that file and holds every step for exactly as long as the
sentence takes to say, so the picture follows the voice instead of a guessed timer.

Voice: Microsoft's `en-US-AndrewNeural` through edge-tts, which needs no key. Nothing here is
synthesised text: every claim in the script is a number this deployment can produce on demand.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import edge_tts

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "demo" / "audio"
VOICE = "en-US-AndrewNeural"
RATE = "+6%"          # a touch quicker than default: this is a walkthrough, not an audiobook

# (id, target selector or None, spoken text). `target` is what the cursor moves to and the box frames.
BEATS: list[tuple[str, str | None, str]] = [
    ("open", None,
     "This is Nota. It turns an AI trading opinion on RYO's market evidence into a receipt you can "
     "check, instead of something you have to trust."),
    ("slip", ".slip dl",
     "Every decision is a receipt. This is the newest one in the ledger: the token, what the council "
     "decided, the probability the judge stated, and where the evidence came from."),
    ("mark", "#mark",
     "The seal is drawn from the receipt itself. Its ticks are the evidence hash and its arc is that "
     "probability, so the same evidence always presses the same seal."),
    ("verify", "#verify",
     "The headline says a decision can be re-run. Let's check that, live."),
    ("verified", ".kiritori",
     "Identical, true. The decision was rebuilt from the stored evidence and the model output cached "
     "beside it, with no key and no call to any model, and the receipt is stamped as checked."),
    ("audit", ".audit",
     "Nota also audits the data it is given. It recomputes RYO's own RSI and ATR from public candles, "
     "and checks RYO's price against three exchanges before anything is sized."),
    ("broken", "#broken-panel",
     "And this is what a failing source looks like. Each section keeps the status its source returned, "
     "a value that could not be fetched stays null instead of becoming zero, and the judge is told what "
     "it does not have. It is read from the ledger as the page loads."),
    ("scorecard", "#scorecard",
     "It also keeps RYO's own score. Every day Nota locks RYO's verdict and trade plan for twenty five "
     "major tokens, and later settles each plan on OKX's hourly candles."),
    ("answer", "#answer",
     "RYO never reports what became of its plans. Here it is recorded. Seven of the fifty plans locked "
     "so far point one way while RYO's own verdict leans the other."),
    ("plans", "#open-t",
     "Each row is RYO's own answer, pinned with its trace id. When a plan settles, the page records "
     "whether the stop or the target was touched first, and compares verdicts day by day, with an "
     "interval, not a headline number."),
    ("dashboard", None,
     "Now the dashboard."),
    ("summary", "#summary",
     "Every receipt opens on a thirty second summary: what was decided, what changed most since the "
     "previous receipt, where the practice position stands, and what happens next."),
    ("changed", "#detail h2",
     "Each receipt is diffed against the one before it for the same token, and ranked by impact, so "
     "the row that mattered is the row on top."),
    ("gate", "#detail .withheld + .withheld",
     "The council is not allowed to cite a derivatives number that is not about this token. On this "
     "receipt, RYO reported the same open interest change for Ethereum as for six unrelated tokens "
     "that day. The gate withheld it, and the receipt says why."),
    ("council", ".mitome",
     "Three specialists argue, citing dotted paths into the evidence, and every value shown is read "
     "back out of that evidence rather than retyped by the model. Each one signs the receipt, or "
     "dissents on it."),
    ("scores", "#scores .vs-base",
     "Every agent is scored against what actually happened, and against the token's own base rate: "
     "how often it rose anyway. So far only the macro agent beats that base rate. Five scored calls is "
     "far too few to trust, and the page says so."),
    ("skills", "#skills",
     "Seven research skills run from this panel in RYO's own envelope, and the same seven are served "
     "over the Model Context Protocol, so Claude or Cursor can call them directly."),
    ("close", None,
     "Read-only research on RYO evidence. No order is ever placed. Not financial advice."),
]


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    beats = []
    for index, (name, target, text) in enumerate(BEATS):
        path = OUT / f"{index:02d}-{name}.mp3"
        await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(path))
        secs = duration(path)
        beats.append({"id": name, "target": target, "text": text,
                      "audio": path.name, "seconds": round(secs, 3)})
        print(f"{index:02d} {name:10} {secs:6.2f}s  {text[:56]}...")
    total = sum(b["seconds"] for b in beats)
    (OUT / "beats.json").write_text(json.dumps({"voice": VOICE, "rate": RATE,
                                                "total_seconds": round(total, 3),
                                                "beats": beats}, indent=1), encoding="utf-8")
    print(f"\n{len(beats)} beats, {total:.1f}s of narration -> {OUT / 'beats.json'}")


if __name__ == "__main__":
    asyncio.run(main())
