"""HTML version of the owner's emails (Phase 110).

The daily report is written as plain text (it reads fine in any mail app and in the audit log).
``text_to_html`` turns it into a tidy HTML alternative: blocks separated by blank lines become
sections, a block's first line ending in ":" becomes its heading, "- " lines become a list, and
links are clickable. Everything is escaped; nothing is loaded from the internet (no images, no
tracking), so it renders the same offline.
"""

from __future__ import annotations

import html
import re

_URL = re.compile(r"https?://[^\s<>\"')]+")


def _inline(text: str) -> str:
    out, pos = [], 0
    for m in _URL.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        url = m.group(0).rstrip(".,;:")
        out.append(f'<a href="{html.escape(url)}">{html.escape(url)}</a>{html.escape(m.group(0)[len(url):])}')
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", "".join(out))


def text_to_html(text: str, title: str = "") -> str:
    parts = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if not lines:
            continue
        head = ""
        if len(lines) > 1 and lines[0].rstrip().endswith(":") and not lines[0].startswith("- "):
            head, lines = lines[0].rstrip(), lines[1:]
        section = f"<h3>{_inline(head)}</h3>" if head else ""
        items = [line for line in lines if line.startswith("- ")]
        if items and len(items) == len(lines):
            section += "<ul>" + "".join(f"<li>{_inline(line[2:])}</li>" for line in items) + "</ul>"
        else:
            section += "<p>" + "<br>".join(_inline(line) for line in lines) + "</p>"
        parts.append(section)
    heading = f"<h2>{html.escape(title)}</h2>" if title else ""
    return ('<!doctype html><html><body style="font:15px/1.5 -apple-system,Segoe UI,sans-serif;color:#111;max-width:640px;'
            'margin:0 auto;padding:12px">' + heading + "".join(parts) + "</body></html>")
