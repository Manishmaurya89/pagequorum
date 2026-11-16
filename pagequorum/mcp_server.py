"""pagequorum as an MCP server, so AI assistants (Claude Desktop, Claude Code, Cursor,
VS Code, ...) can read web pages as clean Markdown: scrape a page, crawl a site section,
list a site's URLs, or extract HTML they already have.

    pagequorum mcp        # speaks MCP over stdin/stdout; the assistant starts it (see README)

The URLs come from a model, which may have just read a page that tells it to fetch
something it should not. So private-network addresses (localhost, 10.x, 192.168.x, cloud
metadata at 169.254.169.254, ...) are refused, including redirects into them. Set
``PAGEQUORUM_ALLOW_PRIVATE=1`` in the server's environment to allow them, e.g. to read
your own development server. robots.txt is respected as usual.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .crawl import crawl as _crawl
from .crawl import map_site as _map_site
from .extract import ExtractOptions, extract as _extract
from .fetch import Fetcher
from .models import Document
from .scrape import ScrapeError, scrape as _scrape

MAX_CRAWL_PAGES = 50
INSTRUCTIONS = (
    "pagequorum reads web pages for you. Several extractors vote on every block of a page and only "
    "what they agree on is kept, so menus, ads, cookie banners and 'related stories' are gone while "
    "headings, tables, code and lists survive as Markdown. Use `scrape` for one page, `crawl` for a "
    "documentation section, `map_site` to see what pages a site has first, and `extract_html` when "
    "you already hold the HTML. Each result starts with the extractors' agreement: below about 0.8, "
    "the page was hard to read and the text may miss or include something."
)
READS_THE_WEB = ToolAnnotations(read_only_hint=True, open_world_hint=True, idempotent_hint=True)


def allow_private() -> bool:
    return os.environ.get("PAGEQUORUM_ALLOW_PRIVATE", "").strip().lower() in ("1", "true", "yes")


def _header(doc: Document) -> str:
    x = doc.extraction
    agreement = x.get("agreement", 0.0)
    level = "high" if agreement >= 0.85 else "medium" if agreement >= 0.7 else "low, check the page yourself"
    lines = [f"Source: {doc.url or '(the HTML you gave)'}" + (f" | {doc.title}" if doc.title else ""),
             f"Kept {x.get('kept_words', 0):,} of {x.get('page_words', 0):,} words | "
             f"extractor agreement {agreement:.2f} ({level})"]
    lines += [f"Note: {w}" for w in x.get("warnings", [])]
    return "\n".join(lines)


def _cut(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + f"\n\n[... cut at {max_chars:,} characters; raise max_chars to see more]"


def build_server(make_fetcher: Callable[[], Fetcher] | None = None) -> MCPServer:
    """The MCP server. ``make_fetcher`` is for tests; by default each call gets a polite
    fetcher that refuses private-network addresses (unless PAGEQUORUM_ALLOW_PRIVATE is set)."""
    make = make_fetcher or (lambda: Fetcher(allow_private=allow_private()))
    server = MCPServer(name="pagequorum", version=__version__, instructions=INSTRUCTIONS,
                       website_url="https://github.com/Manishmaurya89/pagequorum")

    @server.tool(annotations=READS_THE_WEB)
    def scrape(url: str, include_links: bool = False, max_chars: int = 100_000) -> str:
        """Fetch one web page and return its main content as clean Markdown, without menus,
        ads, cookie banners or footers. Headings, tables, code blocks and lists are kept.

        Args:
            url: the page to read (http or https).
            include_links: keep hyperlinks as [text](url) in the Markdown.
            max_chars: cut the Markdown after this many characters.
        """
        with make() as fetcher:
            try:
                doc = _scrape(url, ExtractOptions(links=include_links), fetcher=fetcher, render="never")
            except ScrapeError as exc:
                raise ToolError(f"Could not read {url}: {exc.reason}") from exc
        return f"{_header(doc)}\n\n---\n\n{_cut(doc.markdown, max_chars)}"

    @server.tool(annotations=READS_THE_WEB)
    def crawl(url: str, max_pages: int = 10, max_depth: int = 2, include: str | None = None,
              exclude: str | None = None, max_chars: int = 100_000) -> str:
        """Crawl a site section (same site, under the start URL's folder) and return every
        page's main content as Markdown. Links in each page's content are followed before
        menu links; robots.txt and a one-second delay per site are respected, so expect
        about a second per page.

        Args:
            url: where to start, e.g. a documentation section.
            max_pages: pages to extract (at most 50).
            max_depth: how many links away from the start page to go.
            include: only follow URLs matching this regular expression.
            exclude: never follow URLs matching this regular expression.
            max_chars: cut the combined Markdown after this many characters.
        """
        if not 1 <= max_pages <= MAX_CRAWL_PAGES:
            raise ToolError(f"max_pages must be between 1 and {MAX_CRAWL_PAGES}")
        parts, skipped = [], []
        with make() as fetcher:
            try:
                for ev in _crawl(url, max_pages=max_pages, max_depth=max_depth, fetcher=fetcher,
                                 include=include, exclude=exclude):
                    if ev.document:
                        parts.append(f"{_header(ev.document)}\n\n{ev.document.markdown.strip()}")
                    else:
                        skipped.append(f"- {ev.url}: {ev.skipped or ev.error}")
            except (re.error, ValueError) as exc:  # a bad include/exclude pattern
                raise ToolError(str(exc)) from exc
        if not parts:
            raise ToolError("No page could be extracted.\n" + "\n".join(skipped[:20]))
        summary = f"Crawled {len(parts)} page(s) from {url}" + (f"; skipped {len(skipped)}:\n" + "\n".join(skipped[:20])
                                                                if skipped else ".")
        return _cut(summary + "\n\n===\n\n" + "\n\n===\n\n".join(parts), max_chars)

    @server.tool(annotations=READS_THE_WEB)
    def map_site(url: str, limit: int = 200, whole_site: bool = True) -> str:
        """List a site's page URLs from its sitemap and the start page's links, without
        extracting them. Use it to choose what to scrape or crawl.

        Args:
            url: any page of the site.
            limit: at most this many URLs.
            whole_site: False keeps only URLs under the start URL's folder.
        """
        with make() as fetcher:
            urls = _map_site(url, fetcher=fetcher, limit=max(1, min(limit, 5000)), whole_site=whole_site)
        if not urls:
            raise ToolError(f"No URLs found for {url} (the site may be unreachable or disallow robots).")
        return f"{len(urls)} URL(s) ({sum(u.source == 'sitemap' for u in urls)} from the sitemap):\n" + \
            "\n".join(u.url for u in urls)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True))
    def extract_html(html: str, url: str | None = None, include_links: bool = False) -> str:
        """Extract the main content of HTML you already have (no network access) as clean
        Markdown.

        Args:
            html: the page's HTML.
            url: the page's address, used to resolve relative links.
            include_links: keep hyperlinks as [text](url) in the Markdown.
        """
        doc = _extract(html, url=url, options=ExtractOptions(links=include_links))
        return f"{_header(doc)}\n\n---\n\n{doc.markdown}"

    return server


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)  # not one log line per request
    build_server().run("stdio")
