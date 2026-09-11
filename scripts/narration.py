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
     "This is Nota. It turns an AI trading opinion into something you can check, instead of "
     "something you have to trust."),
    ("mark", "#mark",
     "The mark is not decoration. Its ticks are the evidence hash of a real receipt, and the arc is "
     "the probability that receipt's judge stated. The same evidence always draws the same figure."),
    ("verify", "#verify",
     "The headline claims a decision can be re-run. So let's press the button and check it, live."),
    ("verified", "#verify-out",
     "Identical, true. That rebuilt the decision from the stored evidence and the model output cached "
     "beside it. No key, no network call to any model."),
    ("audit", ".audit",
     "Nota also audits the data it is given. It recomputes RYO's own RSI and ATR from public candles "
     "with Wilder's method. Four point three four seven against RYO's four point three seven six: "
     "zero point six seven percent apart."),
    ("broken", "#broken-panel",
     "And here is the day it broke. On the tenth of September the builder key started answering "
     "unauthenticated, and all five RYO sections failed at once. Nothing was patched for it. The "
     "three independent skills kept answering, a value that could not be fetched stayed null rather "
     "than becoming zero, and the judge refused to size anything without primary evidence. This "
     "panel is read from the ledger as the page loads. It is a record, not an illustration."),
    ("ledger", "#ledger",
     "The ledger's receipts are drawn here, each one from its own hash. Nothing on this page is "
     "typed in: the marks, this caption and the button you just watched all come from the "
     "deployment's own API."),
    ("dashboard", None,
     "Now the instrument itself."),
    ("summary", "#summary",
     "Every receipt opens on a thirty second summary. What was decided, what changed most since the "
     "previous receipt, where the practice position stands, and what happens next."),
    ("changed", "#detail h2",
     "Each receipt is diffed against the one before it for the same symbol, and ranked by impact, so "
     "the row that mattered is the row on top."),
    ("council", None,
     "Three specialists argue. Each cites dotted paths into the evidence, and the value shown is read "
     "back out of that evidence rather than retyped by the model. A citation pointing at something "
     "absent is dropped in code, and the opinion is downgraded for it."),
    ("skills", "#skills",
     "Four research skills run from this panel, and the same four are served over the Model Context "
     "Protocol, so Claude Desktop or Cursor can call them directly."),
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
