"""Heading-aware chunking of a Document's Markdown into retrieval units.

Rules:
  * a new chunk starts at every heading (tiny sections merge forward),
  * code fences and tables are never cut mid-line; an oversized table is split by
    rows with its header repeated, an oversized code block by lines with its fence
    repeated, an oversized paragraph by sentences, an oversized list by items,
  * every chunk records the heading path it sits under and its character span in
    ``Document.markdown``, plus a deep link to the passage on the live page.

Token counts default to a conservative estimate (UTF-8 bytes / 4), which
over-counts for non-Latin scripts rather than under-counting. Pass
``token_counter`` (e.g. a tiktoken encoder's ``lambda s: len(enc.encode(s))``) for
exact counts.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote

from .models import Chunk, Document

_FENCE = re.compile(r"^(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_LIST_ITEM = re.compile(r"^([-*+]|\d+[.)])\s")
_SENTENCE_END = re.compile(r"(?<=[.!?।॥。！？])\s+")


def approx_tokens(text: str) -> int:
    return max(1, math.ceil(len(text.encode("utf-8")) / 4))


@dataclass
class _Unit:
    kind: str  # heading | code | table | text
    text: str
    start: int
    end: int
    level: int = 0


def _units(md: str) -> list[_Unit]:
    lines = md.splitlines(keepends=True)
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    units: list[_Unit] = []
    i = 0

    def make(kind: str, a: int, b: int, level: int = 0) -> _Unit:
        start = offsets[a]
        end = offsets[b - 1] + len(lines[b - 1].rstrip("\n"))
        return _Unit(kind, md[start:end], start, end, level)

    while i < len(lines):
        s = lines[i].rstrip("\n")
        if not s.strip():
            i += 1
            continue
        fence = _FENCE.match(s)
        if fence:
            char, n = fence.group(1)[0], len(fence.group(1))
            j = i + 1
            while j < len(lines) and not re.match(rf"^{re.escape(char)}{{{n},}}\s*$", lines[j].rstrip("\n")):
                j += 1
            j = min(j + 1, len(lines))
            units.append(make("code", i, j))
            i = j
            continue
        heading = _HEADING.match(s)
        if heading:
            units.append(make("heading", i, i + 1, len(heading.group(1))))
            i += 1
            continue
        if s.lstrip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            units.append(make("table", i, j))
            i = j
            continue
        j = i
        while j < len(lines):
            t = lines[j].rstrip("\n")
            if not t.strip() or _FENCE.match(t) or _HEADING.match(t) or (j > i and t.lstrip().startswith("|")):
                break
            j += 1
        units.append(make("text", i, j))
        i = j
    return units


def _pieces_of(text: str, start: int, parts: list[str]) -> list[_Unit]:
    """Locate consecutive substrings of ``text`` and return them as units with offsets."""
    out, cursor = [], 0
    for part in parts:
        k = text.find(part, cursor)
        if k < 0:
            k = cursor
        out.append(_Unit("text", part, start + k, start + k + len(part)))
        cursor = k + len(part)
    return out


def _split(unit: _Unit, max_tokens: int, count: Callable[[str], int]) -> list[_Unit]:
    if count(unit.text) <= max_tokens:
        return [unit]
    if unit.kind == "table":
        lines = unit.text.split("\n")
        header, rows = lines[:2], lines[2:]
        out, batch = [], []
        for row in rows:
            if batch and count("\n".join(header + batch + [row])) > max_tokens:
                out.append(batch)
                batch = []
            batch.append(row)
        if batch:
            out.append(batch)
        units, cursor = [], unit.start + len("\n".join(header)) + 1
        for batch in out:
            body = "\n".join(batch)
            units.append(_Unit("table", "\n".join(header + batch), cursor, cursor + len(body)))
            cursor += len(body) + 1
        return units
    if unit.kind == "code":
        lines = unit.text.split("\n")
        opening, closing, body = lines[0], lines[-1], lines[1:-1]
        units, batch, cursor = [], [], unit.start + len(opening) + 1
        for line in body + [None]:
            if line is None or (batch and count("\n".join([opening, *batch, line, closing])) > max_tokens):
                if batch:
                    chunk = "\n".join(batch)
                    units.append(_Unit("code", "\n".join([opening, chunk, closing]), cursor, cursor + len(chunk)))
                    cursor += len(chunk) + 1
                batch = []
            if line is not None:
                batch.append(line)
        return units
    lines = unit.text.split("\n")
    if sum(1 for ln in lines if _LIST_ITEM.match(ln)) >= 2:
        items, current = [], []
        for ln in lines:
            if _LIST_ITEM.match(ln) and current:
                items.append("\n".join(current))
                current = []
            current.append(ln)
        if current:
            items.append("\n".join(current))
        parts = items
    else:
        parts = [p for p in _SENTENCE_END.split(unit.text) if p]
    pieces = _pieces_of(unit.text, unit.start, parts)
    out: list[_Unit] = []
    for p in pieces:
        if count(p.text) <= max_tokens:
            out.append(p)
            continue
        words, batch = p.text.split(" "), []  # last resort: split on spaces
        for w in words:
            if batch and count(" ".join(batch + [w])) > max_tokens:
                out.extend(_pieces_of(unit.text, unit.start, [" ".join(batch)]))
                batch = []
            batch.append(w)
        if batch:
            out.extend(_pieces_of(unit.text, unit.start, [" ".join(batch)]))
    return out


_MD_LINKS = [
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),
]
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>])")
_MD_EMPHASIS = re.compile(r"(\*\*|__|\*)")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")


def _strip_inline(s: str) -> str:
    """Remove emphasis and link syntax; backslash-escaped characters are literal text."""
    literal: list[str] = []

    def hold(m: re.Match) -> str:
        literal.append(m.group(1))
        return f"\x00{len(literal) - 1}\x00"

    s = _MD_ESCAPE.sub(hold, s)
    for pattern, repl in _MD_LINKS:
        s = pattern.sub(repl, s)
    s = _MD_EMPHASIS.sub("", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: literal[int(m.group(1))], s)


def _plain_line(line: str) -> str:
    """The text a reader sees for one Markdown line. Code spans are kept verbatim:
    a ``*`` inside ``json.dump(obj, *, **kw)`` is part of the page text."""
    line = re.sub(r"^\s*(>\s*)*(([-*+]|\d+[.)])\s+)?", "", line)
    out, pos = [], 0
    for m in _CODE_SPAN.finditer(line):
        out.append(_strip_inline(line[pos:m.start()]))
        out.append(m.group(2).strip())
        pos = m.end()
    out.append(_strip_inline(line[pos:]))
    return "".join(out).strip()


def citation_url(url: str | None, chunk_text: str, words: int = 6) -> str | None:
    """Deep link to the passage via a text fragment (``#:~:text=``), supported by
    Chrome, Edge, Safari and Firefox. Uses the first words of the chunk's first prose
    line, which lie inside one element on the page."""
    if not url:
        return None
    base = url.split("#", 1)[0]
    in_code = False
    for raw in chunk_text.split("\n"):
        if _FENCE.match(raw):
            in_code = not in_code
            continue
        if in_code or not raw.strip() or _HEADING.match(raw) or raw.lstrip().startswith("|"):
            continue
        text = _plain_line(raw)
        snippet = " ".join(text.split()[:words])
        if len(snippet) >= 8:
            return f"{base}#:~:text={quote(snippet, safe='').replace('-', '%2D')}"
    return base


def chunk_document(doc: Document, max_tokens: int = 512, overlap_tokens: int = 0,
                   min_tokens: int | None = None,
                   token_counter: Callable[[str], int] | None = None) -> list[Chunk]:
    count = token_counter or approx_tokens
    min_tokens = max_tokens // 8 if min_tokens is None else min_tokens
    units = _units(doc.markdown)
    chunks: list[Chunk] = []
    path: list[tuple[int, str]] = []
    cur: list[_Unit] = []
    cur_path: list[str] = []
    n_carried = 0  # leading units of `cur` that are overlap copied from the previous chunk

    def body_tokens(us: list[_Unit]) -> int:
        return sum(count(u.text) for u in us if u.kind != "heading")

    def only_headings() -> bool:
        return all(u.kind == "heading" for u in cur)

    def close(carry: bool) -> None:
        nonlocal cur, cur_path, n_carried
        n_carried = 0
        if not cur or only_headings():
            return
        text = "\n\n".join(u.text for u in cur)
        start, end = cur[0].start, cur[-1].end
        cid = hashlib.sha256(f"{doc.url}|{start}|{end}|{text}".encode()).hexdigest()[:16]
        chunks.append(Chunk(id=cid, url=doc.url or "", index=len(chunks), text=text,
                            heading_path=list(cur_path), tokens=count(text), start=start, end=end,
                            title=doc.title, citation_url=citation_url(doc.url, text)))
        tail: list[_Unit] = []
        if carry and overlap_tokens > 0:
            for u in reversed(cur):
                if u.kind != "text" or count(u.text) + body_tokens(tail) > overlap_tokens:
                    break
                tail.insert(0, u)
        cur = tail
        n_carried = len(tail)

    def fits(piece: _Unit) -> bool:
        return count("\n\n".join(u.text for u in cur + [piece])) <= max_tokens

    for unit in units:
        if unit.kind == "heading":
            title = _plain_line(unit.text.lstrip("#").strip())
            path = [(lv, t) for lv, t in path if lv < unit.level] + [(unit.level, title)]
            if body_tokens(cur) >= min_tokens:
                close(carry=False)
                cur = []
            if not cur or only_headings():
                cur_path = [t for _, t in path]
            cur.append(unit)
            continue
        # Headings waiting at the top of the chunk take part of the budget.
        reserve = count("\n\n".join(u.text for u in cur)) + 1 if cur and only_headings() else 0
        for piece in _split(unit, max(16, max_tokens - reserve), count):
            if cur and not fits(piece):
                if len(cur) > n_carried and body_tokens(cur[n_carried:]):
                    close(carry=True)
                    cur_path = [t for _, t in path]
                while cur and n_carried and not fits(piece):  # overlap must never crowd out new text
                    cur.pop(0)
                    n_carried -= 1
            if not cur or only_headings():
                cur_path = [t for _, t in path]
            cur.append(piece)
    close(carry=False)
    return chunks
