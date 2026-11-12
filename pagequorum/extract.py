"""Turn one HTML page into a Document: analyse, vote, decide, render."""

from __future__ import annotations

import re

from dataclasses import dataclass, field

from lxml import html as lxml_html

from . import dom, vote
from .encoding import decode_html
from .extractors import HTML_VOTERS, jsonld_article_body
from .markdown import RenderOptions, render
from .metadata import base_url, extract_metadata
from .models import Document
from .text import tokenize

ALL_VOTERS = ("trafilatura", "readability", "justext", "resiliparse", "jsonld")
DEFAULT_VOTERS = ("trafilatura", "readability", "resiliparse", "jsonld")

# Chosen on the DEV half of the article-extraction benchmark only (see bench/README.md).
# With threshold 0.7 and equal weights: trafilatura, readability and resiliparse must all
# agree; when the page has a JSON-LD articleBody, 3 of the 4 voters must agree; when a
# voter abstains (returns nothing), the remaining ones must all agree.
DEFAULT_WEIGHTS = {"trafilatura": 1.0, "readability": 1.0, "resiliparse": 1.0, "jsonld": 1.0, "justext": 1.0}
DEFAULT_THRESHOLD = 0.7  # fraction of the active voters' total weight a block needs


@dataclass
class ExtractOptions:
    voters: tuple[str, ...] = DEFAULT_VOTERS
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    threshold: float = DEFAULT_THRESHOLD
    # With 3+ voters, a voter covering less than this fraction of the median of the
    # others is treated as failed on that page (see _active). 0 disables.
    outlier_ratio: float = 0.5
    smoothing: bool = True
    # When only two voters remain (one set aside), a run of blocks one of them chose
    # that sits between kept blocks is kept (see _fill_two_voter_gaps).
    two_voter_fill: bool = True
    min_words: int = 20  # fewer kept words than this triggers the fallbacks
    drop_title: bool = True  # the page's H1 duplicates Document.title, so it leaves the body
    links: bool = False
    images: bool = False
    formatting: bool = True


@dataclass
class Analysis:
    """Everything computed before deciding; deciding again with other options is cheap."""

    url: str | None
    html: str
    tree: lxml_html.HtmlElement
    body: lxml_html.HtmlElement
    base_url: str | None
    blocks: list[dom.Block]
    meta: dict
    voter_lines: dict[str, list[str]]
    voter_errors: dict[str, str]
    indices: dict[str, vote.VoterIndex]
    outliers: dict[str, str] = field(default_factory=dict)  # set by decide()


def analyze(html: str | bytes, url: str | None = None, voters: tuple[str, ...] = DEFAULT_VOTERS,
            content_type: str | None = None) -> Analysis:
    if isinstance(html, bytes):
        html = decode_html(html, content_type)[0]
    html = html.replace("\x00", "")
    tree = dom.parse_html(html)
    meta = extract_metadata(html, tree, url)
    voter_lines: dict[str, list[str]] = {}
    voter_errors: dict[str, str] = {}
    if "jsonld" in voters:
        try:
            voter_lines["jsonld"] = jsonld_article_body(tree)
        except Exception as exc:  # noqa: BLE001
            voter_errors["jsonld"] = f"{type(exc).__name__}: {exc}"
    for name in voters:
        if name in HTML_VOTERS:
            try:
                voter_lines[name] = HTML_VOTERS[name](html, url, meta.get("language"))
            except Exception as exc:  # noqa: BLE001 - one broken voter must not break the page
                voter_errors[name] = f"{type(exc).__name__}: {exc}"[:300]

    body = dom.body_of(tree)
    dom.clean(body)
    blocks = dom.segment(body)
    indices = {n: vote.build_index(n, lines) for n, lines in voter_lines.items()}
    vote.cast_votes(blocks, list(indices.values()))
    return Analysis(url, html, tree, body, base_url(tree, url), blocks, meta, voter_lines, voter_errors, indices)


def _coverage_words(an: Analysis, name: str) -> int:
    return sum(b.n_tokens for b in an.blocks if b.tokens and b.votes.get(name, 0.0) >= vote.COVERAGE)


def _active(an: Analysis, opts: ExtractOptions) -> list[str]:
    """Voters whose opinion counts on this page.

    A voter that produced (almost) nothing is abstaining, not voting "boilerplate".
    With 3+ voters, one that covers far less of the page than the others typically
    picked a single sub-section (e.g. Readability on a long Sphinx docs page). Since
    the quorum already vetoes over-extraction, such an under-extracting outlier is
    set aside instead of being allowed to veto most of the page.
    """
    voters = [n for n in opts.voters if n in an.indices and an.indices[n].n_tokens >= 10]
    an.outliers = {}
    if len(voters) < 3 or opts.outlier_ratio <= 0:
        return voters
    cover = {n: _coverage_words(an, n) for n in voters}
    keep = []
    for n in voters:
        others = sorted(cover[m] for m in voters if m != n)
        median = others[len(others) // 2] if len(others) % 2 else (others[len(others) // 2 - 1] + others[len(others) // 2]) / 2
        if median and cover[n] < opts.outlier_ratio * median:
            an.outliers[n] = f"set aside: covered {cover[n]} words, the other voters a median of {median:.0f}"
            continue
        keep.append(n)
    if len(keep) < 2:
        an.outliers = {}
        return voters
    return keep


def _words(blocks) -> int:
    return sum(b.n_tokens for b in blocks)


def decide(an: Analysis, opts: ExtractOptions) -> tuple[set[int], str, list[str], float]:
    """Return (kept block indices, method, warnings, consensus)."""
    blocks = [b for b in an.blocks if b.tokens]
    active = _active(an, opts)
    total = sum(opts.weights.get(n, 1.0) for n in active)
    warnings: list[str] = []
    for b in an.blocks:
        b.score = vote.score(b, opts.weights, active)
        b.kept = False
        b.reason = ""

    if total > 0:
        need = opts.threshold * total - 1e-9
        for b in blocks:
            if b.score >= need:
                if "references" in b.flags:
                    b.reason = "reference list"
                else:
                    b.kept, b.reason = True, "quorum"
        if opts.smoothing:
            _rescue_headings(blocks, total)
            _rescue_lead_ins(blocks)
            _smooth(blocks)
            _drop_empty_sections(blocks)
        if opts.two_voter_fill and an.outliers and len(active) == 2:
            _fill_two_voter_gaps(blocks)
        _keep_referenced_footnotes(an, blocks)
    method = "quorum"

    kept_blocks = [b for b in blocks if b.kept]
    if _words(kept_blocks) < opts.min_words:
        union = [b for b in blocks if b.score > 0]
        if total > 0 and _words(union) >= opts.min_words:
            for b in union:
                b.kept, b.reason = True, b.reason or "union-fallback"
            method = "union-fallback"
            warnings.append("voters disagreed on everything; kept any block at least one voter chose")
        else:
            for b in _container_fallback(blocks):
                b.kept, b.reason = True, "container-fallback"
            method = "container-fallback"
            warnings.append("no main content found by voters; used the page's main container minus navigation")

    if _words(blocks) < 50:
        warnings.append("very little text in the static HTML; the page may need JavaScript rendering")

    if opts.drop_title:
        _drop_title_heading(blocks, an.meta)

    kept = {b.index for b in blocks if b.kept}
    return kept, method, warnings, _consensus(blocks, active)


def _consensus(blocks: list[dom.Block], active: list[str]) -> float:
    """How much the voters agree about this page: words every active voter chose,
    divided by words any active voter chose (1.0 = identical choices). Low values
    flag pages where the extraction is uncertain and worth a human look."""
    if not active:
        return 0.0
    covered = [[b.votes.get(n, 0.0) >= vote.COVERAGE for n in active] for b in blocks]
    union = sum(b.n_tokens for b, c in zip(blocks, covered) if any(c))
    inter = sum(b.n_tokens for b, c in zip(blocks, covered) if all(c))
    return inter / union if union else 0.0


def _smooth(blocks: list[dom.Block]) -> None:
    """Rescue code blocks and data tables that sit inside kept content.

    Voters format (and sometimes drop) code and tables inconsistently, so one that
    at least one voter kept and that sits directly between two kept blocks is kept.
    Headings and captions are deliberately not rescued: on the dev split that rule
    mostly brought back image captions and "Related post" headings.
    """
    for i, b in enumerate(blocks):
        if b.kept or b.score <= 0 or b.kind not in ("code", "table"):
            continue
        prev_kept = i > 0 and blocks[i - 1].kept
        next_kept = i + 1 < len(blocks) and blocks[i + 1].kept
        if prev_kept and next_kept:
            b.kept, b.reason = True, f"smoothing:{b.kind}"


def _rescue_headings(blocks: list[dom.Block], total: float) -> None:
    """Keep a section heading that most voters chose, when it is clearly one of the
    page's own section headings.

    Voters drop headings for odd reasons (resiliparse skips a Wikipedia heading that
    is followed by a "Main article: ..." note), and a lost heading costs the chunks
    their section path. On news pages a looser rule brought back standfirsts, titles
    and "Subscribe" headings, so a heading is only rescued when it sits inside kept
    text (kept text before it and in its own section) and the voters already agreed
    on another heading of the same level on this page. H1 (the title) is never rescued.
    """
    agreed_tags = {b.tag for b in blocks if b.kept and b.kind == "heading"}
    seen_kept_text = False
    for i, b in enumerate(blocks):
        if b.kept and b.kind != "heading":
            seen_kept_text = True
        if (b.kept or b.kind != "heading" or b.tag == "h1" or b.tag not in agreed_tags
                or b.score <= total / 2 or not seen_kept_text):
            continue
        for nb in blocks[i + 1:]:
            if nb.kind == "heading" and nb.heading_level and nb.heading_level <= b.heading_level:
                break
            if nb.kept and nb.kind != "heading":
                b.kept, b.reason = True, "heading of kept section"
                break


def _drop_empty_sections(blocks: list[dom.Block]) -> None:
    """Drop a kept heading (H2-H6) with no kept text in its section, such as a
    "References" heading whose list was dropped, or a trailing "Related" heading."""
    for i, b in enumerate(blocks):
        if not b.kept or b.kind != "heading" or b.tag == "h1":
            continue
        has_text = False
        for nb in blocks[i + 1:]:
            if nb.kind == "heading" and nb.heading_level and nb.heading_level <= b.heading_level:
                break
            if nb.kept and nb.kind != "heading":
                has_text = True
                break
        if not has_text:
            b.kept, b.reason = False, "empty section"


_LEAD_IN = re.compile(r"[:：]\s*[-–—]?\s*$")


def _rescue_lead_ins(blocks: list[dom.Block]) -> None:
    """Keep the list, table or code block that a kept sentence announces with a colon
    ("... six climate types are found in India:"), when at least one voter chose
    every part of it. Extractors often drop link-heavy lists as navigation."""
    for i, b in enumerate(blocks[:-1]):
        nxt = blocks[i + 1]
        if not b.kept or nxt.kept or not _LEAD_IN.search(b.text):
            continue
        if nxt.kind in ("code", "table"):
            run = [nxt]
        elif nxt.kind == "item" and nxt.el.getparent() is not None and nxt.el.getparent().tag in ("ul", "ol"):
            lst = nxt.el.getparent()
            run = []
            for nb in blocks[i + 1:]:
                if lst not in nb.el.iterancestors():
                    break
                run.append(nb)
        else:
            continue
        if run and all(x.score > 0 and "references" not in x.flags for x in run):
            for x in run:
                x.kept, x.reason = True, "announced by the previous sentence"


_NOT_CONTENT = {"nav", "aside", "footer", "header", "negative"}


def _fill_two_voter_gaps(blocks: list[dom.Block]) -> None:
    """Break ties when a voter was set aside and only two remain.

    With two voters the quorum means both must agree, so a single miss by either
    one drops content (on the Python json docs page trafilatura skips the whole
    ``raw_decode()`` entry while resiliparse keeps it). In that case a run of blocks
    that one voter chose is kept when it sits directly between kept blocks and
    nothing marks it as navigation, sidebar, footer or caption. With three voters
    this rule lowered accuracy (see bench/README.md), so it is limited to this case.
    """
    i = 0
    while i < len(blocks):
        if blocks[i].kept:
            i += 1
            continue
        j = i
        while j < len(blocks) and not blocks[j].kept:
            j += 1
        run = blocks[i:j]
        bounded = i > 0 and j < len(blocks)
        if bounded and all(b.score > 0 and b.kind != "caption" and not (b.flags & _NOT_CONTENT) for b in run):
            for b in run:
                b.kept, b.reason = True, "two-voter-fill"
        i = j


# Footnote references marked up as such: the DPUB-ARIA role (docutils/Sphinx, pandoc)
# or docutils' class. Wikipedia-style "[1]" citation links are not matched.
_NOTEREF = ("//a[starts-with(@href, '#')][@role='doc-noteref' or "
            "contains(concat(' ', normalize-space(@class), ' '), ' footnote-reference ')]")


def _keep_referenced_footnotes(an: Analysis, blocks: list[dom.Block]) -> None:
    """Keep a footnote's text when kept content refers to it.

    Extractors drop footnotes as boilerplate, which leaves a "[1]" marker in the text
    pointing at nothing (and a stray "Footnotes" label). A footnote that kept text
    links to explicitly is part of that text.
    """
    owner: dict = {}
    for b in blocks:
        owner.setdefault(b.el, []).append(b)
    targets = set()
    for a in an.body.xpath(_NOTEREF):
        anc = a.getparent()
        while anc is not None and anc not in owner:
            anc = anc.getparent()
        if anc is None or not any(b.kept for b in owner[anc]):
            continue
        found = an.body.xpath("//*[@id=$i]", i=a.get("href")[1:])
        if found:
            targets.add(found[0])
    if not targets:
        return
    for b in blocks:
        if not b.kept and (b.el in targets or any(anc in targets for anc in b.el.iterancestors())):
            b.kept, b.reason = True, "footnote"


def _container_fallback(blocks: list[dom.Block]) -> list[dom.Block]:
    in_main = [b for b in blocks if "main" in b.flags or "article" in b.flags]
    pool = in_main or blocks
    return [
        b for b in pool
        if not ({"nav", "footer", "aside"} & b.flags) and not ("header" in b.flags and not in_main)
        and b.link_density < 0.5
    ]


_TITLE_SEPARATORS = (" | ", " - ", " – ", " — ", " :: ", " · ", " • ")


def _clean_title(blocks: list[dom.Block], meta: dict) -> None:
    """Prefer the on-page headline over "<headline> | <site name>" from <title>."""
    title = meta.get("title") or ""
    toks = tokenize(title)
    if not toks:
        return
    for b in blocks:
        if b.tag != "h1" or not b.text:
            continue
        exact = title.startswith(b.text) and title[len(b.text):].startswith(_TITLE_SEPARATORS)
        prefix = 2 <= len(b.tokens) < len(toks) and toks[: len(b.tokens)] == b.tokens
        if exact or prefix:
            meta["title"] = b.text
            return
    site = (meta.get("site_name") or "").strip()
    for sep in _TITLE_SEPARATORS:
        if site and title.endswith(sep + site):
            meta["title"] = title[: -len(sep + site)].strip()
            return


def _drop_title_heading(blocks: list[dom.Block], meta: dict) -> None:
    _clean_title(blocks, meta)
    title = tokenize(meta.get("title") or "")
    if not title:
        return
    seen = 0
    for b in blocks:
        if not b.kept:
            continue
        seen += 1
        if seen > 4:
            return
        if b.kind == "heading" and b.heading_level <= 2:
            h = b.tokens
            short = min(len(h), len(title))
            if h == title or (short >= 3 and (title[: len(h)] == h or h[: len(title)] == title)):
                b.kept, b.reason = False, "title"
                if len(h) < len(title):
                    meta["title"] = b.text  # the on-page headline, without " | Site Name"
                return


def build_document(an: Analysis, kept: set[int], method: str, warnings: list[str], agreement: float,
                   opts: ExtractOptions, fetch: dict | None = None) -> Document:
    ropts = RenderOptions(links=opts.links, images=opts.images, formatting=opts.formatting)
    body_md, links = render(an.body, an.blocks, kept, an.base_url, ropts)
    body_txt, _ = render(an.body, an.blocks, kept, an.base_url, RenderOptions(plain=True))
    title = an.meta.get("title")
    markdown = f"# {title}\n\n{body_md}" if title else body_md
    seen: set[str] = set()
    link_dicts = []
    for link in links:
        if link.url not in seen:
            seen.add(link.url)
            link_dicts.append({"text": link.text, "url": link.url})
    active = [n for n in opts.voters if n in an.indices and n not in an.outliers
              and an.indices[n].n_tokens >= 10]
    extraction = {
        "method": method,
        "agreement": round(agreement, 3),
        "voters": {
            n: ("error: " + an.voter_errors[n]) if n in an.voter_errors
            else an.outliers[n] if n in an.outliers
            else ("active" if n in active else "abstained")
            for n in opts.voters
        },
        "kept_blocks": len(kept),
        "total_blocks": sum(1 for b in an.blocks if b.tokens),
        "kept_words": sum(b.n_tokens for b in an.blocks if b.index in kept),
        "page_words": sum(b.n_tokens for b in an.blocks),
        "warnings": warnings,
    }
    return Document(
        url=an.url,
        markdown=markdown.strip() + "\n",
        text=body_txt.strip(),
        title=title,
        author=an.meta.get("author"),
        date=an.meta.get("date"),
        language=an.meta.get("language"),
        description=an.meta.get("description"),
        site_name=an.meta.get("site_name"),
        canonical_url=an.meta.get("canonical_url"),
        links=link_dicts,
        extraction=extraction,
        fetch=fetch or {},
    )


def extract(html: str | bytes, url: str | None = None, options: ExtractOptions | None = None,
            content_type: str | None = None, fetch: dict | None = None) -> Document:
    """Extract the main content of one HTML page as a structured Document."""
    return extract_explained(html, url, options, content_type, fetch)[0]


def extract_explained(html: str | bytes, url: str | None = None, options: ExtractOptions | None = None,
                      content_type: str | None = None, fetch: dict | None = None,
                      text_chars: int = 120) -> tuple[Document, list[dict]]:
    """Like :func:`extract`, plus the per-block decisions of :func:`explain`, from one analysis."""
    opts = options or ExtractOptions()
    an = analyze(html, url, opts.voters, content_type)
    kept, method, warnings, agreement = decide(an, opts)
    return build_document(an, kept, method, warnings, agreement, opts, fetch), _explain_rows(an, text_chars)


def explain(html: str | bytes, url: str | None = None, options: ExtractOptions | None = None) -> list[dict]:
    """Per-block decisions: what each voter said and why the block was kept or dropped."""
    opts = options or ExtractOptions()
    an = analyze(html, url, opts.voters)
    decide(an, opts)
    return _explain_rows(an)


def _explain_rows(an: Analysis, text_chars: int = 120) -> list[dict]:
    rows = []
    for b in an.blocks:
        if not b.tokens:
            continue
        rows.append({
            "index": b.index,
            "tag": b.tag,
            "kind": b.kind,
            "kept": b.kept,
            "reason": b.reason or ("below quorum" if b.score > 0 else "no votes"),
            "score": round(b.score, 2),
            "votes": {n: round(v, 2) for n, v in b.votes.items()},
            "words": b.n_tokens,
            "text": b.text[:text_chars],
        })
    return rows
