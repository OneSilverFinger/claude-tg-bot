"""Render Markdown pipe-tables to PNG, because Telegram can't display tables
readably. Detection is fence-aware (tables inside ``` code blocks are ignored).
"""

import io
import logging
import textwrap

log = logging.getLogger(__name__)

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_WRAP = 42          # wrap cell text to this many chars
_PAD = 12
_FONT_SIZE = 26


def _is_sep(line: str) -> bool:
    """A table separator row like |---|:--:|---|."""
    s = line.strip()
    return bool(s) and "|" in s and "-" in s and set(s) <= set("|:- ")


def _find_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """(start, end_exclusive) line ranges of pipe-tables, skipping code fences."""
    blocks = []
    i, n = 0, len(lines)
    in_fence = False
    while i < n:
        stripped = lines[i].lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            i += 1
            continue
        if (not in_fence and "|" in lines[i]
                and i + 1 < n and _is_sep(lines[i + 1])):
            j = i + 2
            while j < n and "|" in lines[j] and lines[j].strip():
                j += 1
            blocks.append((i, j))
            i = j
        else:
            i += 1
    return blocks


def segments(md: str) -> list[tuple[str, str]]:
    """Split the text into ordered ('text', str) / ('table', table_md) segments,
    so a table is delivered exactly where it appears (text before → table image →
    text after), rather than all tables dumped at the end."""
    lines = md.split("\n")
    blocks = _find_blocks(lines)
    if not blocks:
        return [("text", md)] if md.strip() else []
    segs: list[tuple[str, str]] = []
    cur = 0
    for s, e in blocks:
        pre = "\n".join(lines[cur:s]).strip("\n")
        if pre.strip():
            segs.append(("text", pre))
        segs.append(("table", "\n".join(lines[s:e])))
        cur = e
    post = "\n".join(lines[cur:]).strip("\n")
    if post.strip():
        segs.append(("text", post))
    return segs


def _parse(table_md: str) -> list[list[str]]:
    rows = []
    for idx, line in enumerate([l for l in table_md.split("\n") if l.strip()]):
        if idx == 1 and _is_sep(line):
            continue  # separator row
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(cells)
    return rows


def _load_font(size: int):
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def render_png(table_md: str) -> bytes | None:
    """Render one markdown table to a PNG (bytes), or None on failure."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    rows = _parse(table_md)
    if not rows:
        return None
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    wrapped = [[textwrap.wrap(c, _WRAP) or [""] for c in r] for r in rows]

    font = _load_font(_FONT_SIZE)
    asc, desc = font.getmetrics()
    line_h = asc + desc + 4

    def _w(s: str) -> int:
        try:
            return int(font.getlength(s))
        except Exception:
            return len(s) * (_FONT_SIZE // 2)

    col_w = [0] * ncols
    for r in wrapped:
        for ci, cell in enumerate(r):
            col_w[ci] = max(col_w[ci], max((_w(l) for l in cell), default=0))
    col_w = [w + 2 * _PAD for w in col_w]
    row_h = [max(len(cell) for cell in r) * line_h + 2 * _PAD for r in wrapped]

    W, H = sum(col_w) + 1, sum(row_h) + 1
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    y = 0
    for ri, r in enumerate(wrapped):
        if ri == 0:
            d.rectangle([0, y, W, y + row_h[ri]], fill=(228, 234, 246))
        x = 0
        for ci, cell in enumerate(r):
            d.rectangle([x, y, x + col_w[ci], y + row_h[ri]], outline=(170, 175, 185))
            ty = y + _PAD
            for line in cell:
                d.text((x + _PAD, ty), line, fill=(25, 27, 33), font=font)
                ty += line_h
            x += col_w[ci]
        y += row_h[ri]

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
