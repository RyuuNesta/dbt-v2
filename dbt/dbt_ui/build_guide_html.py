#!/usr/bin/env python
"""
Render DATA_TEAM_GUIDE.md into a self-contained, print-ready HTML file.

Stdlib only, on purpose - the project ships with no pip dependencies, so the
docs build should not add one either. This covers the markdown this guide uses:
headings, paragraphs, lists (nested), tables, fenced code, inline code, bold,
italics, links, blockquotes, and horizontal rules.

The output is one HTML file that:
  - opens in any browser and prints cleanly to PDF (File > Print > Save as PDF)
  - imports directly into Google Docs (Docs converts .html on upload)
"""

from __future__ import annotations

import html
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
GUIDE = HERE.parent / "DATA_TEAM_GUIDE.md"
OUT = HERE.parent / "DATA_TEAM_GUIDE.html"

INLINE_CODE = re.compile(r"`([^`]+)`")
BOLD = re.compile(r"\*\*([^*]+)\*\*")
ITALIC = re.compile(r"(?<!\*)\*(?!\s)([^*]+?)\*(?!\*)")
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def inline(text: str) -> str:
    """Inline markdown -> HTML, escaping first so user text can't inject tags."""
    # Protect inline code spans from further formatting.
    spans: list[str] = []

    def stash(m: re.Match) -> str:
        spans.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(spans) - 1}\x00"

    text = INLINE_CODE.sub(stash, text)
    text = html.escape(text)
    text = BOLD.sub(r"<strong>\1</strong>", text)
    text = ITALIC.sub(r"<em>\1</em>", text)
    text = LINK.sub(r'<a href="\2">\1</a>', text)

    def unstash(m: re.Match) -> str:
        return spans[int(m.group(1))]

    return re.sub(r"\x00(\d+)\x00", unstash, text)


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9\s-]", "", text.lower())
    return re.sub(r"\s+", "-", s.strip())


def render_table(rows: list[str]) -> str:
    """rows: markdown table lines including the header separator."""
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    header = cells[0]
    body = cells[2:]  # skip the --- separator row

    out = ["<table>", "<thead><tr>"]
    for h in header:
        out.append(f"<th>{inline(h)}</th>")
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>")
        for c in row:
            align = ' style="text-align:center"' if c in ("✅", "❌", "✓", "—") else ""
            out.append(f"<td{align}>{inline(c)}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def convert(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # fenced code
        if line.startswith("```"):
            lang = line[3:].strip()
            buf = []
            i += 1
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # closing fence
            code = html.escape("\n".join(buf))
            out.append(f'<pre class="code" data-lang="{html.escape(lang)}"><code>{code}</code></pre>')
            continue

        # table (a line with | followed by a |---| separator)
        if "|" in line and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|", lines[i + 1]) and "-" in lines[i + 1]:
            block = [line]
            i += 1
            block.append(lines[i])  # separator
            i += 1
            while i < n and "|" in lines[i] and lines[i].strip():
                block.append(lines[i])
                i += 1
            out.append(render_table(block))
            continue

        # headings
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            text = m.group(2)
            anchor = slugify(re.sub(r"^\d+\.\s*", "", text))
            out.append(f'<h{level} id="{anchor}">{inline(text)}</h{level}>')
            i += 1
            continue

        # horizontal rule
        if re.match(r"^---+\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        # blockquote (possibly multi-line)
        if line.startswith(">"):
            buf = []
            while i < n and lines[i].startswith(">"):
                buf.append(lines[i].lstrip(">").strip())
                i += 1
            # join, blank line inside quote -> paragraph break
            paras = "\n".join(buf).split("\n\n")
            inner = "".join(f"<p>{inline(p.replace(chr(10), ' '))}</p>" for p in paras if p.strip())
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        # lists (ordered / unordered, with 2-space nesting)
        if re.match(r"^\s*([-*]|\d+\.)\s+", line):
            block = []
            while i < n and (re.match(r"^\s*([-*]|\d+\.)\s+", lines[i]) or (lines[i].startswith("  ") and lines[i].strip())):
                block.append(lines[i])
                i += 1
            out.append(render_list(block))
            continue

        # blank
        if not line.strip():
            i += 1
            continue

        # paragraph (gather until blank / block starter)
        buf = [line]
        i += 1
        while i < n and lines[i].strip() and not re.match(
            r"^(#{1,6}\s|```|>|\s*([-*]|\d+\.)\s|---+\s*$)", lines[i]
        ) and not ("|" in lines[i] and i + 1 < n and "-" in (lines[i + 1] if i + 1 < n else "")):
            buf.append(lines[i])
            i += 1
        out.append(f"<p>{inline(' '.join(buf))}</p>")

    return "\n".join(out)


def render_list(block: list[str]) -> str:
    """Render a (possibly nested) list block. Two-space indent = one level."""
    root_ordered = bool(re.match(r"^\s*\d+\.", block[0]))
    tag = "ol" if root_ordered else "ul"
    out = [f"<{tag}>"]
    depth = 0
    stack = [tag]

    for raw in block:
        indent = len(raw) - len(raw.lstrip(" "))
        level = indent // 2
        m = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)$", raw)
        if not m:
            # continuation line of the previous item
            if out and out[-1].endswith("</li>"):
                out[-1] = out[-1][:-5] + " " + inline(raw.strip()) + "</li>"
            continue
        item = inline(m.group(1))
        ordered = bool(re.match(r"^\s*\d+\.", raw))
        sub = "ol" if ordered else "ul"

        while level > depth:
            out.append(f"<{sub}>")
            stack.append(sub)
            depth += 1
        while level < depth:
            out.append(f"</{stack.pop()}>")
            depth -= 1

        out.append(f"<li>{item}</li>")

    while stack:
        out.append(f"</{stack.pop()}>")
    return "\n".join(out)


CSS = """
:root { --fg:#1a1d23; --muted:#5b6472; --line:#e2e6ec; --accent:#2b6cb0;
        --code-bg:#f5f7fa; --quote-bg:#f0f6ff; --quote-bar:#2b6cb0; }
* { box-sizing: border-box; }
body { font: 15px/1.65 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       color: var(--fg); max-width: 860px; margin: 40px auto; padding: 0 28px; }
h1 { font-size: 30px; margin: 0 0 6px; letter-spacing: -0.02em; }
h2 { font-size: 22px; margin: 34px 0 10px; padding-top: 14px; border-top: 2px solid var(--line); letter-spacing: -0.01em; }
h3 { font-size: 17px; margin: 24px 0 8px; }
h4 { font-size: 15px; margin: 18px 0 6px; color: var(--muted); }
p { margin: 10px 0; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
code { font: 13px "SF Mono", "Cascadia Code", Consolas, monospace;
       background: var(--code-bg); padding: 1px 5px; border-radius: 4px; }
pre.code { background: var(--code-bg); border: 1px solid var(--line); border-radius: 8px;
           padding: 14px 16px; overflow-x: auto; margin: 12px 0; }
pre.code code { background: none; padding: 0; font-size: 12.5px; line-height: 1.55; }
table { border-collapse: collapse; width: 100%; margin: 14px 0; font-size: 13.5px; }
th, td { border: 1px solid var(--line); padding: 7px 11px; text-align: left; vertical-align: top; }
th { background: #f0f3f7; font-weight: 600; }
tr:nth-child(even) td { background: #fafbfc; }
blockquote { background: var(--quote-bg); border-left: 4px solid var(--quote-bar);
             margin: 14px 0; padding: 4px 16px; border-radius: 0 6px 6px 0; }
blockquote p { margin: 8px 0; }
hr { border: 0; border-top: 1px solid var(--line); margin: 26px 0; }
ul, ol { margin: 10px 0; padding-left: 26px; }
li { margin: 4px 0; }
@media print {
  body { max-width: none; margin: 0; padding: 0; font-size: 11pt; }
  h2 { page-break-before: auto; }
  h2, h3, h4 { page-break-after: avoid; }
  pre.code, table, blockquote { page-break-inside: avoid; }
  a { color: var(--fg); }
}
"""


def main() -> int:
    if not GUIDE.exists():
        print(f"Not found: {GUIDE}", file=sys.stderr)
        return 1
    md = GUIDE.read_text(encoding="utf-8")
    body = convert(md)
    doc = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>ASG Data Platform - Data Team Guide</title>"
        f"<style>{CSS}</style></head><body>\n{body}\n</body></html>\n"
    )
    OUT.write_text(doc, encoding="utf-8")
    print(f"Wrote {OUT} ({len(doc):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
