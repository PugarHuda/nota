"""Receipt card: a 1200x630 PNG for link previews (Open Graph / X cards / Discord embeds).

Everything drawn comes from the receipt itself; no number is invented for the picture.
"""

from __future__ import annotations

import math
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from nota.receipt import Receipt
from nota.skills.contract import OK
from nota.risk import PracticeTrade

W, H = 1200, 630
# the same receipt the site draws: a white slip on a blue-grey desk, indigo ink, a vermilion seal
DESK, PAPER, FG, DIM, LINE = (237, 240, 245), (255, 255, 255), (30, 43, 94), (88, 96, 116), (200, 208, 228)
SEAL, WARN = (210, 63, 41), (122, 83, 6)
COLOUR = {"long": (35, 113, 79), "short": (156, 42, 60), "no_trade": DIM}
TEXT_W = 820  # the seal owns the right-hand column


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)  # Pillow's bundled scalable font: no system font dependency


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and len(" ".join(lines)) < len(text):
        lines[-1] = lines[-1][:-1].rstrip() + "…"
    return lines


def _seal(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, pack_hash: str, p: float) -> None:
    """The site's hanko: 64 ticks, one per hex digit of the evidence hash, and the judge's probability
    as an arc and as the number in the middle."""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=SEAL, width=5)
    for i, ch in enumerate(pack_hash[:64]):
        a = math.radians(i * 360 / 64 - 90)
        r1, r2 = r * .70, r * .73 + int(ch, 16) * r * .012
        d.line([cx + r1 * math.cos(a), cy + r1 * math.sin(a), cx + r2 * math.cos(a), cy + r2 * math.sin(a)], fill=SEAL, width=3)
    ra = r * .86
    d.arc([cx - ra, cy - ra, cx + ra, cy + ra], -90, -90 + 360 * p, fill=SEAL, width=12)
    ri = r * .5
    d.ellipse([cx - ri, cy - ri, cx + ri, cy + ri], outline=SEAL, width=4)
    d.text((cx, cy), f"{p:.2f}", font=_font(46), fill=SEAL, anchor="mm")


def render_card(r: Receipt) -> bytes:
    img = Image.new("RGB", (W, H), DESK)
    d = ImageDraw.Draw(img)
    d.rectangle([24, 24, W - 24, H - 24], fill=PAPER, outline=LINE, width=2)
    accent = COLOUR.get(r.verdict.action, DIM)
    d.text((60, 52), "RECEIPT", font=_font(34), fill=FG)
    d.text((W - 60, 60), f"{r.symbol}   {r.id}", font=_font(26), fill=DIM, anchor="ra")
    d.line([60, 102, W - 60, 102], fill=FG, width=2)
    d.line([60, 108, W - 60, 108], fill=FG, width=2)
    y = 128
    for line in _wrap(d, r.headline, _font(42), TEXT_W, 2):
        d.text((60, y), line, font=_font(42), fill=FG)
        y += 54
    y += 10
    d.text((60, y), f"{r.verdict.action.replace('_', ' ').upper()}   p_up_7d {r.verdict.p_up_7d:.2f}", font=_font(34), fill=accent)
    y += 54
    t = r.trade
    if isinstance(t, PracticeTrade):
        line = f"entry {t.entry_price:g}  stop {t.stop_price:g}  target {t.target_price:g}  {t.size_usd:.0f} USD"
    else:
        # the headline already says a trade was blocked, so this line carries what the reader does
        # not have yet: why the judge held back, or the risk it named when the reason only restates
        # the verdict. A card is read out of context and cannot afford to say one thing three times.
        reason = t.reason
        if reason.startswith("judge decided"):
            risks = r.verdict.key_risks or []
            reason = risks[0] if risks else "the council did not reach a tradeable edge"
        line = f"why: {reason}"
    for part in _wrap(d, line, _font(27), TEXT_W, 2):
        d.text((60, y), part, font=_font(27), fill=FG)
        y += 36
    y += 8
    council = "   ".join(f"{o.role} {o.stance} {o.p_up_7d:.2f}" for o in r.opinions)
    d.text((60, y), _wrap(d, council, _font(24), TEXT_W, 1)[0], font=_font(24), fill=DIM)
    y += 40
    degraded = [k for k, s in r.availability.items() if s not in OK]
    avail = f"evidence: {len(r.availability)} sections" + (f", degraded: {', '.join(degraded)}" if degraded else ", all ok")
    for line in _wrap(d, avail, _font(24), TEXT_W, 2):
        d.text((60, y), line, font=_font(24), fill=WARN if degraded else DIM)
        y += 32
    _seal(d, 1020, 330, 112, r.pack_hash, r.verdict.p_up_7d)
    d.line([60, H - 116, W - 60, H - 116], fill=LINE, width=2)
    footer = f"evidence {r.pack_hash[:12]} · source {r.source} · model {r.model}"
    d.text((60, H - 100), _wrap(d, footer, _font(22), W - 120, 1)[0], font=_font(22), fill=DIM)
    d.text((60, H - 66), "Research on read-only RYO evidence. No order was placed. Not financial advice.", font=_font(22), fill=DIM)
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
