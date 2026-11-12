"""Parse HTML, remove what a reader never sees, and split the page into blocks.

A *block* is one block-level element together with the text it owns directly
(its own text plus the text of inline descendants, but not the text of nested
block elements, which are blocks of their own). Code (``<pre>``) and data tables
are atomic: one block each, never split. Blocks are what the extractors vote on,
and the Markdown renderer walks the same tree, so a decision about a block maps
exactly onto the structure that gets rendered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import html as lxml_html
from lxml.etree import ParserError

from .text import normalize_ws, tokenize

HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# Inline (phrasing) elements: their text belongs to the nearest block ancestor.
# Anything not listed here, including unknown and custom elements, is treated as a
# block boundary, which is the safer default for div-soup and web components.
INLINE_TAGS = {
    "a", "abbr", "acronym", "b", "bdi", "bdo", "big", "br", "cite", "code", "data",
    "del", "dfn", "em", "font", "i", "img", "ins", "kbd", "label", "mark", "math",
    "nobr", "output", "picture", "q", "rp", "rt", "ruby", "s", "samp", "small",
    "span", "strike", "strong", "sub", "sup", "time", "tt", "u", "var", "wbr",
}

# Removed outright: never visible text, or interactive controls.
DROP_TAGS = {
    "script", "style", "noscript", "template", "svg", "canvas", "iframe", "object",
    "embed", "audio", "video", "source", "track", "link", "meta", "input", "button",
    "select", "textarea", "option", "optgroup", "datalist", "dialog",
}

# Classes used for visually hidden, screen-reader-only text.
_SR_ONLY = {"sr-only", "visually-hidden", "visuallyhidden", "screen-reader-text", "screenreader-only"}
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)
# Text of in-page anchors that are navigation, not content: permalinks next to
# headings, and "back to the reference" links after footnotes.
_PERMALINK_GLYPHS = {"", "¶", "#", "§", "🔗", "⚓", "↩", "↑", "^"}
# Removed by class: MediaWiki's "[edit | edit source]" links next to headings, and
# the "v · t · e" (view/talk/edit) navbars inside Wikipedia tables and navboxes.
_DROP_CLASSES = {"mw-editsection", "navbar"}
# Bibliography / citation lists (MediaWiki, DPUB-ARIA). Not article text for RAG.
_REFERENCE_CLASSES = {"references", "reflist", "refbegin", "mw-references-wrap", "mw-references-columns"}
_REFERENCE_ROLES = {"doc-bibliography", "doc-endnotes"}

# Class/id hints, in the spirit of Mozilla Readability's candidate patterns.
NEGATIVE_HINT = re.compile(
    r"(^|[-_\s])(comment|comments|sidebar|side-bar|footer|related|share|sharing|social|"
    r"promo|advert|ads?|sponsor|cookie|consent|newsletter|subscribe|signup|popup|modal|"
    r"breadcrumbs?|menu|nav|navbar|widget|recommend|outbrain|taboola|paywall|banner|"
    r"masthead|toolbar|pagination|pager|tags?|author-bio|byline)([-_\s]|$)",
    re.I,
)
POSITIVE_HINT = re.compile(
    r"(^|[-_\s])(article|articlebody|article-body|story|story-body|entry|entry-content|"
    r"post|post-content|post-body|content|main|main-content|body-text|text|prose|markdown|doc|docs|documentation)([-_\s]|$)",
    re.I,
)


@dataclass(eq=False)
class Block:
    index: int
    el: lxml_html.HtmlElement
    tag: str
    kind: str  # heading | code | table | item | def | caption | quote | cell | para
    # An element's own text is split wherever a nested block interrupts it:
    # <div>A <p>B</p> C</div> gives div segment 0 = "A", p = "B", div segment 1 = "C",
    # so blocks stay in true document order. The renderer counts segments the same way.
    segment: int = 0
    parts: list[str] = field(default_factory=list)
    link_chars: int = 0
    text: str = ""
    tokens: list[str] = field(default_factory=list)
    flags: frozenset[str] = frozenset()
    votes: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    kept: bool = False
    reason: str = ""

    def add(self, s: str, in_link: bool) -> None:
        if s:
            self.parts.append(s)
            if in_link:
                self.link_chars += len(s.strip())

    def finalize(self) -> None:
        self.text = normalize_ws("".join(self.parts))
        self.tokens = tokenize(self.text)

    @property
    def n_tokens(self) -> int:
        return len(self.tokens)

    @property
    def link_density(self) -> float:
        return self.link_chars / len(self.text) if self.text else 0.0

    @property
    def heading_level(self) -> int:
        return int(self.tag[1]) if self.tag in HEADINGS else 0


def parse_html(text: str) -> lxml_html.HtmlElement:
    """Parse a whole HTML document into an lxml tree (never raises on junk input)."""
    text = _XML_DECL.sub("", text.replace("\x00", ""), count=1)
    if not text.strip():
        text = "<html><body></body></html>"
    try:
        return lxml_html.document_fromstring(text)
    except (ParserError, ValueError):
        return lxml_html.document_fromstring("<html><body></body></html>")


def body_of(tree: lxml_html.HtmlElement) -> lxml_html.HtmlElement:
    body = tree.find("body")
    return body if body is not None else tree


def _is_hidden(el: lxml_html.HtmlElement) -> bool:
    # Inactive tab panels (role=tabpanel + hidden) carry real content such as the
    # "yarn" variant of an install command, so they are kept.
    if el.get("hidden") is not None and el.get("role") != "tabpanel":
        return True
    if (el.get("aria-hidden") or "").strip().lower() == "true":
        return True
    style = el.get("style")
    if style and _HIDDEN_STYLE.search(style):
        return True
    cls = el.get("class")
    if cls and _SR_ONLY.intersection(cls.lower().split()):
        return True
    return False


def _is_permalink(el: lxml_html.HtmlElement) -> bool:
    """A heading's "link to this section" anchor: ``<a href="#id">¶</a>`` (Sphinx,
    MkDocs), ``#`` (VitePress, Docusaurus) or an icon, or a footnote's ``↩`` back
    link. Its glyph is not content."""
    href = (el.get("href") or "").strip()
    if not href.startswith("#"):
        return False
    text = el.text_content()
    for ch in "​︎️":
        text = text.replace(ch, "")
    if text.strip() not in _PERMALINK_GLYPHS:
        return False
    return not any(d.tag == "img" for d in el.iter() if isinstance(d.tag, str))


def clean(body: lxml_html.HtmlElement) -> None:
    """Remove scripts, styles, form controls, hidden elements, permalink anchors
    and wiki edit links, in place."""
    doomed = []
    for el in body.iter():
        if el is body:
            continue
        tag = el.tag
        if not isinstance(tag, str):  # comments / processing instructions
            doomed.append(el)
        elif tag.lower() in DROP_TAGS or _is_hidden(el):
            doomed.append(el)
        elif tag.lower() == "a" and _is_permalink(el):
            doomed.append(el)
        elif _DROP_CLASSES.intersection((el.get("class") or "").split()):
            doomed.append(el)
    for el in doomed:
        if el.getparent() is not None:
            el.drop_tree()  # keeps the element's tail text


def is_data_table(table: lxml_html.HtmlElement) -> bool:
    """Data table vs layout table, following Mozilla Readability's rules."""
    role = (table.get("role") or "").lower()
    if role in ("presentation", "none") or table.get("datatable") == "0":
        return False
    if table.get("summary"):
        return True
    if table.find("caption") is not None and (table.find("caption").text_content() or "").strip():
        return True
    if table.xpath(".//table"):
        return False
    if table.xpath("./thead|./tfoot|./colgroup|./col|.//th"):
        return True
    rows = table.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr")
    if not rows:
        return False
    cols = max(len([c for c in r if c.tag in ("td", "th")]) for r in rows)
    if cols <= 1:
        return False
    return len(rows) >= 10 or cols > 4 or len(rows) * cols > 10


def is_block_element(el) -> bool:
    tag = el.tag
    return isinstance(tag, str) and tag not in INLINE_TAGS


def pre_text(el: lxml_html.HtmlElement) -> str:
    """Text of a code block with whitespace kept, ``<br>`` as newlines, line numbers removed."""
    out: list[str] = []

    def walk(node, skip: bool) -> None:
        cls = (node.get("class") or "").lower()
        skip_this = skip or any(k in cls for k in ("linenos", "lineno", "line-number", "line-numbers", "gutter"))
        if node.tag == "br":
            out.append("\n")
        elif not skip_this and node.text:
            out.append(node.text)
        for child in node:
            if isinstance(child.tag, str):
                walk(child, skip_this)
            if child.tail and not skip_this:
                out.append(child.tail)

    walk(el, False)
    return "".join(out).strip("\n")


def _flags_for(el: lxml_html.HtmlElement) -> frozenset[str]:
    flags: set[str] = set()
    for anc in [el, *el.iterancestors()]:
        tag = anc.tag if isinstance(anc.tag, str) else ""
        role = (anc.get("role") or "").lower()
        if tag == "nav" or role in ("navigation", "menu", "menubar"):
            flags.add("nav")
        elif tag == "footer" or role == "contentinfo":
            flags.add("footer")
        elif tag == "aside" or role == "complementary":
            flags.add("aside")
        elif tag == "header" or role == "banner":
            flags.add("header")
        elif tag == "main" or role == "main":
            flags.add("main")
        elif tag == "article" or role == "article":
            flags.add("article")
        elif tag == "form":
            flags.add("form")
        if role in _REFERENCE_ROLES or _REFERENCE_CLASSES.intersection((anc.get("class") or "").split()):
            flags.add("references")
        hint = f"{anc.get('class') or ''} {anc.get('id') or ''}"
        if hint.strip():
            if NEGATIVE_HINT.search(hint):
                flags.add("negative")
            if POSITIVE_HINT.search(hint):
                flags.add("positive")
    return frozenset(flags)


def _kind_for(tag: str) -> str:
    if tag in HEADINGS:
        return "heading"
    if tag == "li":
        return "item"
    if tag in ("dt", "dd"):
        return "def"
    if tag in ("figcaption", "caption"):
        return "caption"
    if tag == "blockquote":
        return "quote"
    if tag in ("td", "th"):
        return "cell"
    return "para"


def segment(root: lxml_html.HtmlElement) -> list[Block]:
    """Split a (cleaned) tree into blocks in document order."""
    blocks: list[Block] = []

    def new_block(el, kind: str, seg: int = 0) -> Block:
        b = Block(index=len(blocks), el=el, tag=el.tag, kind=kind, segment=seg)
        blocks.append(b)
        return b

    def walk(el, cur: Block, in_link: bool) -> Block:
        """Add el's content to the current segment; return the segment active afterwards."""
        cur.add(el.text or "", in_link)
        for child in el:
            cur = visit(child, cur, in_link)
            cur.add(child.tail or "", in_link)
        return cur

    def visit(el, cur: Block, in_link: bool) -> Block:
        tag = el.tag
        if not isinstance(tag, str):
            return cur
        if tag == "br":
            cur.add("\n", in_link)
            return cur
        if tag == "img":
            return cur
        if tag in INLINE_TAGS:
            return walk(el, cur, in_link or tag == "a")
        if tag == "pre":
            new_block(el, "code").add(pre_text(el), in_link)
        elif tag == "table" and is_data_table(el):
            b = new_block(el, "table")
            for row in el.xpath(".//tr"):
                cells = [normalize_ws(c.text_content()) for c in row if c.tag in ("td", "th")]
                b.add(" ".join(cells) + "\n", in_link)
        else:
            walk(el, new_block(el, _kind_for(tag)), in_link)
        # A nested block ended: the owner's following text starts a new segment.
        return new_block(cur.el, cur.kind, cur.segment + 1)

    walk(root, new_block(root, "para"), False)
    flag_cache: dict = {}
    for b in blocks:
        b.finalize()
        if b.el not in flag_cache:
            flag_cache[b.el] = _flags_for(b.el)
        b.flags = flag_cache[b.el]
    return blocks
