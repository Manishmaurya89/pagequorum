"""Fetch + extract one URL."""

from __future__ import annotations

from .extract import ExtractOptions, extract_explained
from .fetch import Fetcher
from .models import Document
from .render import RenderUnavailable, looks_js_rendered, render_html


class ScrapeError(RuntimeError):
    def __init__(self, url: str, reason: str):
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason


def scrape(url: str, options: ExtractOptions | None = None, fetcher: Fetcher | None = None,
           render: str = "auto") -> Document:
    """Fetch ``url`` and return its main content as a Document.

    ``render``: "auto" renders with Playwright only when the static HTML looks like an
    empty JavaScript app shell (and Playwright is installed), "always" or "never".
    """
    return scrape_explained(url, options, fetcher, render)[0]


def scrape_explained(url: str, options: ExtractOptions | None = None, fetcher: Fetcher | None = None,
                     render: str = "auto", text_chars: int = 120) -> tuple[Document, list[dict]]:
    """Like :func:`scrape`, plus the per-block keep/drop decisions (see ``explain``)."""
    if render not in ("auto", "always", "never"):
        raise ValueError("render must be 'auto', 'always' or 'never'")
    own = fetcher is None
    f = fetcher or Fetcher()
    try:
        res = f.fetch(url)
    finally:
        if own:
            f.close()
    if not res.ok:
        raise ScrapeError(url, res.skipped or res.error or "fetch failed")
    doc, rows = extract_explained(res.html, url=res.final_url or url, options=options, fetch=res.info(),
                                  text_chars=text_chars)

    wants = render == "always" or (
        render == "auto"
        and looks_js_rendered(res.html, doc.extraction["page_words"], doc.extraction["kept_words"])
    )
    if wants:
        try:
            html, final = render_html(res.final_url or url, user_agent=f.user_agent)
        except RenderUnavailable as exc:
            if render == "always":
                raise ScrapeError(url, str(exc)) from exc
            doc.extraction["warnings"].append(str(exc))
            return doc, rows
        info = res.info() | {"rendered": True, "final_url": final}
        doc, rows = extract_explained(html, url=final, options=options, fetch=info, text_chars=text_chars)
    return doc, rows
