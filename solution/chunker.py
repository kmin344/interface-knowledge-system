"""Turn one page of crawled markdown into retrievable chunks.

Rules that matter for this data:
- A table row is its own chunk, carrying the table's caption, the section heading and the
  column names. "60 months | 5.39%" means nothing without "2024-27 model years" and "APR".
- The whole table is also kept as one chunk, for "list all certificate rates" questions.
- Paragraphs and bullet lists are grouped under their heading, a few hundred characters each.
- Images are dropped; link text is kept and link targets are remembered separately so a
  "what link should I use" question can be answered with a real URL.
- Pages repeat blocks (desktop and mobile layouts); identical chunk text is kept once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from textutil import norm_space

IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]*)[^)]*\)")
HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
EFFECTIVE_RE = re.compile(r"effective (?:as of )?(\d{1,2})/(\d{1,2})/(\d{4})", re.I)


@dataclass
class Chunk:
    text: str                 # what we show the member
    search: str               # what we index (text + heading context + title)
    url: str
    kind: str                 # "row" | "table" | "para"
    heading: str = ""
    table_id: int = -1        # rows of the same table share this id (unique per page)
    links: list[str] = field(default_factory=list)
    effective: str = ""       # "2026-06-15" when the block states an effective date


def _clean_inline(s: str, links: list[str]) -> str:
    s = IMG_RE.sub("", s)

    def repl(m):
        txt, url = m.group(1), m.group(2)
        if url.startswith("http") or url.startswith("/"):
            links.append(url)
        return txt
    s = LINK_RE.sub(repl, s)
    s = s.replace("\\\\", " ").replace("\\r\\n", " ").replace("\\n", " ").replace("\\", "")
    s = s.replace("**", "").replace("__", "")
    return norm_space(s)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _effective(text: str) -> str:
    m = EFFECTIVE_RE.search(text)
    if not m:
        return ""
    mo, d, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def chunk_page(url: str, title: str, markdown: str) -> list[Chunk]:
    lines = (markdown or "").splitlines()
    chunks: list[Chunk] = []
    heads: list[str] = []          # heading stack, index = level-1
    last_caption = ""             # last short plain line (tables are captioned this way)
    buf: list[str] = []
    buf_links: list[str] = []
    seen: set[str] = set()
    table_no = 0

    def heading_path() -> str:
        return " > ".join(h for h in heads if h)

    def emit(text: str, kind: str, links: list[str], extra: str = "", table_id: int = -1):
        text = norm_space(text)
        if len(text) < 3:
            return
        key = kind + "|" + text
        if key in seen:
            return
        seen.add(key)
        hp = heading_path()
        chunks.append(Chunk(text=text, search=f"{title} {hp} {extra} {text}", url=url, kind=kind,
                            heading=hp, table_id=table_id, links=list(dict.fromkeys(links)),
                            effective=_effective(text)))

    def flush():
        nonlocal buf, buf_links
        if buf:
            text = " ".join(buf)
            # long runs of prose are split into ~700 char pieces on sentence boundaries
            while len(text) > 900:
                cut = text.rfind(". ", 0, 700)
                cut = cut + 1 if cut > 200 else 700
                emit(text[:cut], "para", buf_links)
                text = text[cut:]
            emit(text, "para", buf_links)
        buf, buf_links = [], []

    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i].rstrip()
        line = raw.strip()
        hm = HEAD_RE.match(line)
        if hm:
            flush()
            level = len(hm.group(1))
            txt = _clean_inline(hm.group(2), [])
            heads[:] = heads[: level - 1] + [""] * max(0, level - 1 - len(heads))
            heads.append(txt)
            heads[:] = heads[:level]
            last_caption = ""
            i += 1
            continue
        if line.startswith("|"):
            flush()
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            rows = [r for r in rows if not re.fullmatch(r"\|[\s:\-|]+\|?", r)]
            if not rows:
                continue
            links: list[str] = []
            header = [_clean_inline(c, links) for c in _cells(rows[0])]
            body = [[_clean_inline(c, links) for c in _cells(r)] for r in rows[1:]]
            table_no += 1
            cap = last_caption
            row_texts = []
            for cells in body:
                parts = []
                for j, c in enumerate(cells):
                    h = header[j] if j < len(header) else ""
                    if not c:
                        continue
                    parts.append(f"{h}: {c}" if h and h.lower() != c.lower() else c)
                rt = "; ".join(parts)
                row_texts.append(rt)
                ctx = f"{cap}" if cap else ""
                emit((f"{cap} — " if cap else "") + rt, "row", links, extra=ctx, table_id=table_no)
            if not body:   # a one-line table is just a row
                emit(" | ".join(header), "para", links)
            else:
                emit((f"{cap}: " if cap else "") + " / ".join(row_texts), "table", links,
                     extra=" ".join(header), table_id=table_no)
            continue
        if not line or line in ("* * *", "---"):
            # Short lines (hours, addresses, labels) separated by blank lines belong together.
            if line or not buf or len(buf[-1]) >= 100 or sum(map(len, buf)) > 500:
                flush()
            i += 1
            continue
        links = []
        txt = _clean_inline(line.lstrip("-*+ ").strip() if re.match(r"^[-*+]\s", line) else line, links)
        if txt:
            if len(txt) < 60 and not txt.endswith(".") and not re.match(r"^[-*+]\s", line):
                last_caption = txt
            if buf and not buf[-1].endswith((".", "!", "?", ":", ";")) and \
                    (re.match(r"^[-*+]\s", line) or len(buf[-1]) < 100):
                buf[-1] = buf[-1] + ";"
            buf.append(txt)
            buf_links.extend(links)
        i += 1
    flush()
    return chunks
