"""A small, dependency-free PDF writer for text documents (the Executive Migration Dossier).

Why not HTML-to-PDF: that needs a browser engine or a third-party binary (wkhtmltopdf,
WeasyPrint's native stack) on the Mac. This writes PDF 1.4 directly with the standard library:
the 14 standard fonts (Helvetica, Helvetica-Bold) that every PDF reader ships, so nothing is
embedded or downloaded; real glyph widths from the Adobe font metrics for wrapping;
Flate-compressed page streams (``zlib``); a correct cross-reference table; "Page n of N" footers.

Text is encoded as WinAnsi (cp1252). Characters outside it are mapped to close equivalents
(``→`` becomes ``->``, curly quotes become straight ones) or ``?``.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Advance widths (1/1000 em) for ASCII 32..126, from the Adobe AFM files.
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556,
         556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833,
         722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556,
         556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334,
         260, 334, 584]
_HELV_BOLD = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556,
              556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611,
              833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556,
              611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500,
              389, 280, 389, 584]
FONTS = {"F1": ("Helvetica", _HELV), "F2": ("Helvetica-Bold", _HELV_BOLD)}
_REPLACE = {"→": "->", "←": "<-", "–": "-", "—": " - ", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "...", "≥": ">=",
            "≤": "<=", "×": "x", "•": "\x95", " ": " "}

PAGE_W, PAGE_H = 612.0, 792.0  # US Letter
MARGIN = 54.0
INK = (0.07, 0.08, 0.1)
MUTED = (0.38, 0.41, 0.45)
ACCENT = (0.12, 0.44, 0.92)


def to_winansi(text: str) -> str:
    return "".join(_REPLACE.get(c, c) for c in str(text))


def width(text: str, size: float, font: str = "F1") -> float:
    table = FONTS[font][1]
    total = 0
    for ch in to_winansi(text):
        o = ord(ch)
        total += table[o - 32] if 32 <= o <= 126 else 556
    return total * size / 1000


def wrap(text: str, size: float, max_width: float, font: str = "F1") -> list[str]:
    lines: list[str] = []
    for para in str(text).split("\n"):
        words, line = para.split(" "), ""
        for w in words:
            candidate = f"{line} {w}" if line else w
            if width(candidate, size, font) <= max_width:
                line = candidate
                continue
            if line:
                lines.append(line)
            while width(w, size, font) > max_width and len(w) > 1:  # hard-break very long tokens (URLs)
                cut = max(1, int(len(w) * max_width / width(w, size, font)) - 1)
                lines.append(w[:cut])
                w = w[cut:]
            line = w
        lines.append(line)
    return lines


def _esc(text: str) -> bytes:
    raw = to_winansi(text).encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)").replace(b"\r", b"").replace(b"\n", b" ")


@dataclass
class _Page:
    ops: list[bytes] = field(default_factory=list)


class PDFDocument:
    def __init__(self, title: str, subtitle: str = "", author: str = "AutoMonetize"):
        self.title, self.subtitle, self.author = title, subtitle, author
        self.pages: list[_Page] = []
        self.y = 0.0
        self._new_page(first=True)

    # -- low level -------------------------------------------------------------------
    def _new_page(self, first: bool = False) -> None:
        self.pages.append(_Page())
        if first:
            self._rect(0, PAGE_H - 96, PAGE_W, 96, ACCENT)
            self._text(MARGIN, PAGE_H - 50, self.title, 20, "F2", (1, 1, 1))
            if self.subtitle:
                self._text(MARGIN, PAGE_H - 72, self.subtitle, 10.5, "F1", (0.9, 0.93, 1))
            self.y = PAGE_H - 96 - 28
        else:
            self._text(MARGIN, PAGE_H - 36, self.title, 8.5, "F1", MUTED)
            self._line(MARGIN, PAGE_H - 42, PAGE_W - MARGIN, PAGE_H - 42, (0.85, 0.87, 0.9))
            self.y = PAGE_H - 64

    def _op(self, data: str | bytes) -> None:
        self.pages[-1].ops.append(data if isinstance(data, bytes) else data.encode("ascii"))

    def _text(self, x: float, y: float, text: str, size: float, font: str = "F1", color: tuple = INK) -> None:
        self._op(f"BT {color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg /{font} {size:.2f} Tf {x:.2f} {y:.2f} Td (".encode()
                 + _esc(text) + b") Tj ET")

    def _rect(self, x: float, y: float, w: float, h: float, color: tuple) -> None:
        self._op(f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")

    def _line(self, x1: float, y1: float, x2: float, y2: float, color: tuple = MUTED, w: float = 0.6) -> None:
        self._op(f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG {w:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    def _need(self, height: float) -> None:
        if self.y - height < MARGIN + 24:
            self._new_page()

    @property
    def content_width(self) -> float:
        return PAGE_W - 2 * MARGIN

    # -- blocks --------------------------------------------------------------------------
    def heading(self, text: str, level: int = 1) -> None:
        size = {1: 14.5, 2: 11.5}.get(level, 10.5)
        self._need(size * 2.6)
        self.y -= size * 0.9
        self._text(MARGIN, self.y, text, size, "F2", ACCENT if level == 1 else INK)
        if level == 1:
            self._line(MARGIN, self.y - 5, PAGE_W - MARGIN, self.y - 5, (0.8, 0.85, 0.95))
        self.y -= size * 0.9

    def paragraph(self, text: str, size: float = 10, font: str = "F1", color: tuple = INK, indent: float = 0) -> None:
        lead = size * 1.38
        for line in wrap(text, size, self.content_width - indent, font):
            self._need(lead)
            self.y -= lead
            self._text(MARGIN + indent, self.y, line, size, font, color)
        self.y -= size * 0.45

    def bullets(self, items: list[str], size: float = 10) -> None:
        lead = size * 1.38
        for item in items:
            lines = wrap(item, size, self.content_width - 16)
            self._need(lead * len(lines))
            for i, line in enumerate(lines):
                self.y -= lead
                if i == 0:
                    self._text(MARGIN + 4, self.y, "\x95", size, "F1", ACCENT)
                self._text(MARGIN + 16, self.y, line, size)
        self.y -= size * 0.45

    def key_values(self, pairs: list[tuple[str, str]], size: float = 10, key_width: float = 150) -> None:
        lead = size * 1.42
        for key, value in pairs:
            lines = wrap(value, size, self.content_width - key_width)
            self._need(lead * len(lines))
            self.y -= lead
            self._text(MARGIN, self.y, key, size, "F2", MUTED)
            for i, line in enumerate(lines):
                if i:
                    self.y -= lead
                self._text(MARGIN + key_width, self.y, line, size)
        self.y -= size * 0.5

    def table(self, headers: list[str], rows: list[list[str]], widths: list[float], size: float = 8.8) -> None:
        total = sum(widths)
        cols = [w / total * self.content_width for w in widths]
        lead = size * 1.35

        def draw_row(cells: list[str], font: str, shade: tuple | None) -> None:
            wrapped = [wrap(c, size, cols[i] - 6, font)[:4] for i, c in enumerate(cells)]
            height = lead * max(len(w) for w in wrapped) + 4
            self._need(height)
            if shade:
                self._rect(MARGIN, self.y - height, self.content_width, height, shade)
            x = MARGIN
            for i, lines in enumerate(wrapped):
                yy = self.y
                for line in lines:
                    yy -= lead
                    self._text(x + 3, yy + 1, line, size, font, INK if font == "F1" else (0.2, 0.22, 0.26))
                x += cols[i]
            self.y -= height
            self._line(MARGIN, self.y, PAGE_W - MARGIN, self.y, (0.88, 0.89, 0.91), 0.4)

        draw_row(headers, "F2", (0.94, 0.95, 0.97))
        for r in rows:
            draw_row([str(c) for c in r], "F1", None)
        self.y -= 8

    def spacer(self, h: float = 6) -> None:
        self.y -= h

    # -- output ----------------------------------------------------------------------------
    def to_bytes(self, created: datetime | None = None) -> bytes:
        created = created or datetime.now(timezone.utc)
        n = len(self.pages)
        objects: list[bytes] = []
        # 1 catalog, 2 pages, 3-4 fonts, 5 info, then (page, content) pairs
        page_ids = [6 + 2 * i for i in range(n)]
        objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        objects.append(f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {n} >>".encode())
        for name in ("F1", "F2"):
            objects.append(f"<< /Type /Font /Subtype /Type1 /BaseFont /{FONTS[name][0]} /Encoding /WinAnsiEncoding >>".encode())
        stamp = created.astimezone(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
        objects.append(b"<< /Title (" + _esc(self.title) + b") /Author (" + _esc(self.author) + b") /Producer (AutoMonetize pdf_writer)"
                       + f" /CreationDate ({stamp}) >>".encode())
        for i, page in enumerate(self.pages):
            footer = f"BT {MUTED[0]:.3f} {MUTED[1]:.3f} {MUTED[2]:.3f} rg /F1 8 Tf {PAGE_W - MARGIN - 60:.2f} 30 Td (Page {i + 1} of {n}) Tj ET"
            stream = zlib.compress(b"\n".join(page.ops + [footer.encode()]))
            objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W:.0f} {PAGE_H:.0f}] "
                           f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {page_ids[i] + 1} 0 R >>".encode())
            objects.append(f"<< /Length {len(stream)} /Filter /FlateDecode >>\nstream\n".encode() + stream + b"\nendstream")
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for num, body in enumerate(objects, 1):
            offsets.append(len(out))
            out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
        out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 5 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return bytes(out)
