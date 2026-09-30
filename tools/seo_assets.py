"""Generated meta assets: SVG summary badges and OpenGraph preview cards.

* ``render_badge_svg``: a shields-style two-part badge (``radar-badge.svg``), e.g.
  "python radar | 142 hiring signals". Embeddable in READMEs, gists and articles.
* ``render_og_svg``: a 1200×630 SVG card with the headline metrics.
* ``render_og_png``: the same card as PNG, via Pillow (optional: ``pip install '.[images]'``).
  Most social networks don't render SVG ``og:image``s, so pages point at the PNG when it exists.

"Hiring signals" is the number of open roles tracked: each posting counts once.
"""

from __future__ import annotations

import io
from typing import Any
from xml.sax.saxutils import escape, quoteattr

FONT = "Verdana,Geneva,DejaVu Sans,sans-serif"
OG_W, OG_H = 1200, 630


def _text_width(text: str, size: float = 11.0) -> int:
    """Approximate Verdana advance widths, enough to size badge halves without a font engine."""
    narrow, wide = set("fijlrtI.,:;'| !"), set("mwMW@%")
    units = sum(0.35 if c in narrow else 0.95 if c in wide else 0.62 for c in text)
    return int(units * size) + 10


def render_badge_svg(label: str, value: str, color: str = "#2e7d32") -> str:
    lw, vw = _text_width(label), _text_width(value)
    w = lw + vw
    label_x, value_x = lw / 2, lw + vw / 2
    title = f"{label}: {value}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" role="img" aria-label={quoteattr(title)}>'
        f"<title>{escape(title)}</title>"
        f'<linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/>'
        f'<stop offset="1" stop-opacity=".1"/></linearGradient>'
        f'<clipPath id="r"><rect width="{w}" height="20" rx="3" fill="#fff"/></clipPath>'
        f'<g clip-path="url(#r)"><rect width="{lw}" height="20" fill="#555"/>'
        f'<rect x="{lw}" width="{vw}" height="20" fill={quoteattr(color)}/><rect width="{w}" height="20" fill="url(#s)"/></g>'
        f'<g fill="#fff" text-anchor="middle" font-family="{FONT}" font-size="11">'
        f'<text x="{label_x:.1f}" y="15" fill="#010101" fill-opacity=".3">{escape(label)}</text>'
        f'<text x="{label_x:.1f}" y="14">{escape(label)}</text>'
        f'<text x="{value_x:.1f}" y="15" fill="#010101" fill-opacity=".3">{escape(value)}</text>'
        f'<text x="{value_x:.1f}" y="14">{escape(value)}</text></g></svg>\n'
    )


def badge_for(label: str, metrics: dict[str, Any]) -> str:
    roles = metrics.get("roles") or 0
    color = "#2e7d32" if roles >= 50 else "#f9a825" if roles >= 10 else "#9e9e9e"
    return render_badge_svg(label, f"{roles} hiring signals", color)


def og_lines(title: str, metrics: dict[str, Any], price: str) -> list[tuple[str, int, str]]:
    """(text, font size, colour) rows shared by the SVG and PNG cards."""
    rows = [(title[:52] + ("…" if len(title) > 52 else ""), 54, "#ffffff")]
    stats = []
    if metrics.get("companies"):
        stats.append(f"{metrics['companies']} companies")
    if metrics.get("roles"):
        stats.append(f"{metrics['roles']} hiring signals")
    if metrics.get("high_urgency"):
        stats.append(f"{metrics['high_urgency']} high-urgency")
    if stats:
        rows.append((" · ".join(stats), 36, "#9be7a0"))
    signals = metrics.get("signals") or []
    if signals:
        rows.append(("Top signals: " + ", ".join(f"{s} ({n})" for s, n in signals[:3]), 28, "#d0d0d0"))
    rows.append((f"Company-level tech stack intel · {price}", 28, "#d0d0d0"))
    return rows


def render_og_svg(title: str, metrics: dict[str, Any], price: str) -> str:
    y = 170
    texts = []
    for text, size, color in og_lines(title, metrics, price):
        texts.append(f'<text x="80" y="{y}" font-size="{size}" fill="{color}" font-family="{FONT}" '
                     f'font-weight="{700 if size > 40 else 400}">{escape(text)}</text>')
        y += int(size * 1.9)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{OG_W}" height="{OG_H}" viewBox="0 0 {OG_W} {OG_H}">'
            f'<rect width="{OG_W}" height="{OG_H}" fill="#101418"/><rect x="0" y="0" width="16" height="{OG_H}" fill="#2e7d32"/>'
            + "".join(texts) + "</svg>\n")


def _font(size: int):
    from PIL import ImageFont

    for name in ("DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "Arial.ttf",
                 "/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def render_og_png(title: str, metrics: dict[str, Any], price: str) -> bytes | None:
    """PNG OpenGraph card, or None when Pillow isn't installed."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    img = Image.new("RGB", (OG_W, OG_H), "#101418")
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 15, OG_H], fill="#2e7d32")
    y = 110
    for text, size, color in og_lines(title, metrics, price):
        draw.text((80, y), text, font=_font(size), fill=color)
        y += int(size * 1.9)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
