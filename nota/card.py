"""Receipt card: a 1200x630 PNG for link previews (Open Graph / X cards / Discord embeds).

Everything drawn comes from the receipt itself; no number is invented for the picture.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from nota.receipt import Receipt
from nota.risk import PracticeTrade

W, H = 1200, 630
BG, FG, DIM, LINE = (15, 17, 21), (230, 232, 238), (139, 147, 167), (38, 44, 56)
COLOUR = {"long": (62, 207, 142), "short": (255, 107, 107), "no_trade": (139, 147, 167)}


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


def render_card(r: Receipt) -> bytes:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    accent = COLOUR.get(r.verdict.action, DIM)
    d.rectangle([0, 0, 14, H], fill=accent)
    d.text((48, 40), "NOTA · DECISION RECEIPT", font=_font(24), fill=DIM)
    d.text((W - 48, 40), r.symbol, font=_font(56), fill=FG, anchor="ra")
    y = 100
    for line in _wrap(d, r.headline, _font(46), W - 96, 2):
        d.text((48, y), line, font=_font(46), fill=FG)
        y += 58
    y += 10
    d.text((48, y), f"{r.verdict.action.upper()}   p_up_7d {r.verdict.p_up_7d:.2f}", font=_font(36), fill=accent)
    y += 56
    t = r.trade
    if isinstance(t, PracticeTrade):
        d.text((48, y), f"entry {t.entry_price:g}   stop {t.stop_price:g}   target {t.target_price:g}   size {t.size_usd:.0f} USD   ATR {t.atr:g}",
               font=_font(28), fill=FG)
    else:
        # the headline already says a trade was blocked, so this line carries what the reader does
        # not have yet: why the judge held back, or the risk it named when the reason only restates
        # the verdict. A card is read out of context and cannot afford to say one thing three times.
        reason = t.reason
        if reason.startswith("judge decided"):
            risks = r.verdict.key_risks or []
            reason = risks[0] if risks else "the council did not reach a tradeable edge"
        d.text((48, y), f"why: {reason}"[:96], font=_font(28), fill=FG)
    y += 50
    council = "   ".join(f"{o.role} {o.stance} {o.p_up_7d:.2f}" for o in r.opinions)
    d.text((48, y), council, font=_font(26), fill=DIM)
    y += 44
    degraded = [k for k, s in r.availability.items() if s != "ok"]
    avail = f"evidence: {len(r.availability)} sections" + (f", degraded: {', '.join(degraded)}" if degraded else ", all ok")
    for line in _wrap(d, avail, _font(26), W - 96, 2):
        d.text((48, y), line, font=_font(26), fill=(245, 181, 63) if degraded else DIM)
        y += 34
    d.line([48, H - 110, W - 48, H - 110], fill=LINE, width=2)
    footer = f"receipt {r.id} · evidence {r.pack_hash[:12]} · source {r.source} · model {r.model}"
    d.text((48, H - 94), _wrap(d, footer, _font(24), W - 96, 1)[0], font=_font(24), fill=DIM)
    d.text((48, H - 58), "Research on read-only RYO evidence. No order was placed. Not financial advice.", font=_font(24), fill=DIM)
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
