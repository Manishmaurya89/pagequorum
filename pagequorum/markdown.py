"""Render the kept blocks of a page as Markdown (or plain text), keeping structure.

The renderer walks the same tree the segmenter walked. An element's own text is
emitted only if its block was kept; nested blocks decide for themselves. Headings,
nested lists, block quotes, code fences (with language) and GitHub-style tables
(rowspan values repeated so every row stands on its own) are preserved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from lxml import html as lxml_html

from .dom import HEADINGS, Block, is_block_element, pre_text
from .text import normalize_ws

_WS = re.compile(r"[ \t\r\f\v\n]+")
_LANG_CLASS = re.compile(r"(?:^|\s)(?:language|lang|highlight-source|highlight)-([\w+#.-]+)", re.I)
_NEEDS_ESCAPE = re.compile(r"^(#|>|[-+*]\s|\d+[.)]\s|\||[-=*_]{3,}\s*$)")
_LIST_START = re.compile(r"^([-+*]|\d+[.)]) ")
# Admonitions/callouts: Sphinx and MkDocs (div.admonition > p.admonition-title),
# GitHub (div.markdown-alert > p.markdown-alert-title), Docusaurus (admonitionHeading_*).
_ADMONITION = re.compile(r"admonition|markdown-alert|callout", re.I)
_ADMONITION_TITLE = re.compile(r"(^|\s)(admonition-title|markdown-alert-title|callout-title)(\s|$)|admonitionHeading", re.I)
# A rendered part that starts with block syntax cannot take a label in front of it.
_BLOCK_SYNTAX = re.compile(r"^(```|~~~|\||[-+*] |\d+[.)] |#|>)")


@dataclass
class RenderOptions:
    links: bool = False  # [text](url) instead of plain anchor text
    images: bool = False  # ![alt](src) for images inside kept text
    formatting: bool = True  # **bold**, *italic*, `code`
    plain: bool = False  # plain text: no Markdown syntax at all


@dataclass
class Link:
    text: str
    url: str


@dataclass
class Renderer:
    block_of: dict
    kept: set[int]
    base_url: str | None
    opts: RenderOptions
    links: list[Link] = field(default_factory=list)
    _force: int = 0
    _block_desc_cache: dict = field(default_factory=dict)

    # ---- helpers -------------------------------------------------------------
    def _is_kept(self, el, seg: int = 0) -> bool:
        if self._force:
            return True
        b: Block | None = self.block_of.get((el, seg))
        return b is not None and b.index in self.kept

    def _atomic_kind(self, el) -> str | None:
        b = self.block_of.get((el, 0))
        if el.tag == "pre":
            return "code"
        if b is not None and b.kind == "table":
            return "table"
        if self._force and el.tag == "table":
            return "table"
        return None

    def _has_block_desc(self, el) -> bool:
        # Keyed by the element itself, not id(el): lxml recreates proxy objects on
        # access, so ids of dead proxies get reused for other elements. Holding the
        # element as a key keeps its proxy alive and the mapping stable.
        if el not in self._block_desc_cache:
            self._block_desc_cache[el] = any(
                is_block_element(d) for d in el.iterdescendants() if isinstance(d.tag, str)
            )
        return self._block_desc_cache[el]

    def _abs(self, href: str) -> str:
        return urljoin(self.base_url, href) if self.base_url else href

    @staticmethod
    def _space(s: str) -> str:
        return _WS.sub(" ", s)

    def _wrap(self, inner: str, marker: str) -> str:
        if self.opts.plain or not self.opts.formatting:
            return inner
        m = re.match(r"^(\s*)(.*?)(\s*)$", inner, re.S)
        lead, core, trail = m.group(1), m.group(2), m.group(3)
        if not core or "\n" in core:
            return inner
        return f"{lead}{marker}{core}{marker}{trail}"

    def _finish(self, raw: str) -> str:
        """Turn an inline buffer into one or more paragraphs (``<br><br>`` splits them)."""
        paragraphs, current = [], []
        for line in raw.split("\n"):
            line = re.sub(r" {2,}", " ", line).strip()
            if line:
                if not self.opts.plain and _NEEDS_ESCAPE.match(line):
                    line = "\\" + line
                current.append(line)
            elif current:
                paragraphs.append("\n".join(current))
                current = []
        if current:
            paragraphs.append("\n".join(current))
        return "\n\n".join(paragraphs)

    # ---- inline --------------------------------------------------------------
    def _inline(self, el) -> str:
        tag = el.tag
        if tag == "br":
            return "\n"
        if tag == "img":
            if self.opts.images and not self.opts.plain and el.get("src"):
                alt = normalize_ws(el.get("alt") or "")
                return f"![{alt}]({self._abs(el.get('src'))})"
            return ""
        if tag in ("code", "kbd", "samp", "tt"):
            code = el.text_content()
            if self.opts.plain or not self.opts.formatting or not code.strip() or "\n" in code.strip():
                return self._space(code)
            fence = "``" if "`" in code else "`"
            pad = " " if code.startswith("`") or code.endswith("`") else ""
            return f"{fence}{pad}{code.strip()}{pad}{fence}"
        inner = self._space(el.text or "")
        for child in el:
            if isinstance(child.tag, str):
                inner += self._inline(child)
            inner += self._space(child.tail or "")
        if tag in ("strong", "b"):
            return self._wrap(inner, "**")
        if tag in ("em", "i"):
            return self._wrap(inner, "*")
        if tag == "a":
            href = (el.get("href") or "").strip()
            text = normalize_ws(inner)
            if href and not href.lower().startswith(("javascript:", "#")):
                url = self._abs(href)
                if text:
                    self.links.append(Link(normalize_ws(el.text_content()), url))
                if self.opts.links and not self.opts.plain and text:
                    return f"[{text}]({url})"
        return inner

    # ---- blocks --------------------------------------------------------------
    def flow(self, el) -> list[str]:
        """Own inline content of ``el`` (if kept) interleaved with its child blocks.

        ``seg`` counts nested blocks exactly as ``dom.segment`` does, so each run
        of own text is rendered only if that particular segment was kept.
        """
        out: list[str] = []
        buf: list[str] = []
        seg = 0
        mark = len(self.links)  # links collected from the current run of own text start here

        def flush() -> None:
            nonlocal mark
            if buf:
                text = self._finish("".join(buf))
                buf.clear()
                if text and self._is_kept(el, seg):
                    out.append(text)
                else:
                    del self.links[mark:]  # a dropped segment's links are not content links
            mark = len(self.links)

        def walk(node) -> None:
            nonlocal seg, mark
            buf.append(self._space(node.text or ""))
            for child in node:
                if not isinstance(child.tag, str):
                    pass
                elif child.tag == "br":
                    buf.append("\n")
                elif is_block_element(child):
                    flush()
                    out.extend(self.block(child))
                    seg += 1
                    mark = len(self.links)
                elif self._has_block_desc(child):
                    walk(child)  # inline wrapper around blocks: treat as transparent
                else:
                    buf.append(self._inline(child))
                buf.append(self._space(child.tail or ""))

        walk(el)
        flush()
        return out

    def block(self, el) -> list[str]:
        tag = el.tag
        atomic = self._atomic_kind(el)
        if atomic == "code":
            return [self._code(el)] if self._is_kept(el) else []
        if atomic == "table":
            return self._table(el) if self._is_kept(el) else []
        if tag in ("ul", "ol"):
            return self._list(el)
        if tag in HEADINGS:
            parts = self.flow(el)
            if not parts:
                return []
            if self._is_kept(el) and not self.opts.plain:
                return ["#" * int(tag[1]) + " " + " ".join(p.replace("\n", " ") for p in parts).lstrip("\\")]
            return parts
        if tag == "blockquote":
            parts = self.flow(el)
            if not parts or self.opts.plain:
                return parts
            body = "\n\n".join(parts)
            return ["\n".join(f"> {ln}" if ln else ">" for ln in body.split("\n"))]
        if tag == "dt" and "sig" in (el.get("class") or "").split() and not self._has_block_desc(el):
            # A Sphinx API signature: every parameter is wrapped in <em>, and ``*``/``**kw``
            # are literal, so inline formatting would produce broken Markdown. Render the
            # signature as one code span instead.
            sig = normalize_ws(el.text_content())
            if not sig or not self._is_kept(el):
                return []
            if self.opts.plain or not self.opts.formatting:
                return [sig]
            fence = "``" if "`" in sig else "`"
            return [f"{fence}{sig}{fence}"]
        labelled = self._label_child(el)
        if labelled:
            return self._with_label(el, *labelled)
        if tag == "dt":
            parts = self.flow(el)
            if parts and self._is_kept(el):
                return [self._wrap(" ".join(parts), "**")]
            return parts
        return self.flow(el)

    def _label_child(self, el) -> tuple[str, object] | None:
        """The label of an admonition ("Note") or a footnote ("[1]"), if ``el`` is one."""
        cls = el.get("class") or ""
        if el.get("role") == "doc-footnote" or "footnote" in cls.split():
            for child in el:
                if isinstance(child.tag, str) and "label" in (child.get("class") or "").split():
                    return "footnote", child
            return None
        if _ADMONITION.search(cls):
            for child in el:
                if isinstance(child.tag, str) and _ADMONITION_TITLE.search(child.get("class") or ""):
                    return "admonition", child
        return None

    def _with_label(self, el, kind: str, label_el) -> list[str]:
        """Put the label in front of the first paragraph ("**Note:** The term ...") so
        it is not left on a line of its own, where a chunk boundary could separate them."""
        parts = self.flow(el)
        title = normalize_ws(label_el.text_content())
        if len(parts) < 2 or not title or parts[0].lstrip("\\") != title:
            return parts
        head = title
        if kind == "admonition":
            if title[-1] not in ".:!?":
                head += ":"
            if not self.opts.plain and self.opts.formatting:
                head = f"**{head}**"
        rest = parts[1:]
        if _BLOCK_SYNTAX.match(rest[0]):
            return [head, *rest]
        return [f"{head} {rest[0]}", *rest[1:]]

    def _list(self, el) -> list[str]:
        ordered = el.tag == "ol"
        try:
            n = int(el.get("start", "1"))
        except ValueError:
            n = 1
        items: list[str] = []
        loose: list[str] = []
        for child in el:
            if not isinstance(child.tag, str):
                continue
            if child.tag != "li":
                loose.extend(self.block(child))
                continue
            content = self.flow(child)
            if not content:
                continue
            own = self.block_of.get((child, 0))
            own_text_dropped = own is not None and own.tokens and not self._is_kept(child)
            if own_text_dropped:
                # The item's own text was dropped but something nested was kept:
                # emit the nested content without inventing an empty bullet.
                items.extend(content)
                continue
            marker = "-" if self.opts.plain else (f"{n}." if ordered else "-")
            n += 1
            indent = " " * (len(marker) + 1)
            # A nested list follows its parent item directly (tight list); other
            # blocks inside an item (paragraphs, code) are separated by a blank line.
            joined = content[0]
            for part in content[1:]:
                joined += ("\n" if _LIST_START.match(part) else "\n\n") + part
            lines = joined.split("\n")
            text = marker + " " + lines[0]
            for ln in lines[1:]:
                text += "\n" + (indent + ln if ln else "")
            items.append(text)
        out = ["\n".join(items)] if items else []
        return out + loose

    def _code(self, el) -> str:
        code = pre_text(el)
        if self.opts.plain:
            return code
        lang = ""
        for node in [el, *el.iter("code")]:
            m = _LANG_CLASS.search(node.get("class") or "")
            if m:
                lang = m.group(1).lower()
                break
        fence = "```"
        while fence in code:
            fence += "`"
        return f"{fence}{lang}\n{code}\n{fence}"

    def _cell_text(self, cell) -> str:
        self._force += 1
        try:
            text = " ".join(p.replace("\n", " ") for p in self.flow(cell))
        finally:
            self._force -= 1
        return normalize_ws(text).replace("|", "\\|") if not self.opts.plain else normalize_ws(text)

    def _table(self, el) -> list[str]:
        rows: list[list[str]] = []
        pending: dict[int, list] = {}  # column -> [rows_left, text]
        for tr in el.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr"):
            cells = [c for c in tr if c.tag in ("td", "th")]
            row: list[str] = []
            col = ci = 0
            while ci < len(cells) or col in pending:
                if col in pending:
                    left, text = pending[col]
                    row.append(text)
                    if left <= 1:
                        del pending[col]
                    else:
                        pending[col][0] = left - 1
                    col += 1
                    continue
                cell = cells[ci]
                ci += 1
                text = self._cell_text(cell)
                span = _int_attr(cell, "colspan", 1, 50)
                rspan = _int_attr(cell, "rowspan", 1, 1000)
                # Rowspan values are repeated so every row stands on its own, except a
                # picture cell (photo + caption beside the whole table): repeating its
                # caption on every row would only add noise.
                repeat = "" if cell.xpath(".//img") else text
                for k in range(span):
                    row.append(text if k == 0 else "")
                    if rspan > 1:
                        pending[col] = [rspan - 1, repeat if k == 0 else ""]
                    col += 1
            if any(c for c in row):
                rows.append(row)
        if not rows:
            return []
        caption_el = el.find("caption")
        caption = normalize_ws(caption_el.text_content()) if caption_el is not None else ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        if self.opts.plain:
            body = "\n".join(" | ".join(c for c in r if c) for r in rows)
            return [caption, body] if caption else [body]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join([" --- "] * width) + "|"]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        table = "\n".join(lines)
        return [caption, table] if caption else [table]


def _int_attr(el, name: str, default: int, cap: int) -> int:
    try:
        return max(1, min(cap, int((el.get(name) or "").strip() or default)))
    except ValueError:
        return default


def render(root: lxml_html.HtmlElement, blocks: list[Block], kept: set[int], base_url: str | None,
           opts: RenderOptions) -> tuple[str, list[Link]]:
    renderer = Renderer({(b.el, b.segment): b for b in blocks}, kept, base_url, opts)
    parts = renderer.flow(root) if root.tag not in HEADINGS else renderer.block(root)
    text = "\n\n".join(p for p in parts if p.strip())
    return text, renderer.links
