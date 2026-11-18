"""Crawl one site section breadth-first and extract every page.

Scope: same site (``www.`` ignored) and under the start URL's directory, so
crawling ``https://docs.example.com/guide/intro`` stays inside ``/guide/``.
Pages are discovered from the sitemap(s) listed in robots.txt (or /sitemap.xml)
and from links on fetched pages. Links inside a page's extracted content are
followed before links elsewhere on the page (menus, sidebars, footers), so a small
``max_pages`` budget goes to related pages rather than "About" and "Help". Duplicates are skipped by normalised URL, by
redirect target, by ``<link rel=canonical>`` and by identical extracted text.
"""

from __future__ import annotations

import hashlib
import re
import zlib
from collections import deque
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urljoin, urlsplit

import httpx
from lxml import etree

from . import dom
from .extract import ExtractOptions, extract
from .fetch import Fetcher, read_capped
from .metadata import base_url
from .models import Document
from .urls import in_scope, looks_like_page, normalize_url, rebase_scope, related_site, scope_prefix

_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=False)


@dataclass
class CrawlEvent:
    url: str
    depth: int
    document: Document | None = None
    skipped: str | None = None
    error: str | None = None


def page_links(html: str, url: str) -> list[str]:
    tree = dom.parse_html(html)
    base = base_url(tree, url) or url
    out = []
    for a in tree.xpath("//a[@href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        out.append(urljoin(base, href))
    return out


def _gunzip_capped(body: bytes, cap: int) -> bytes | None:
    """Decompress a gzip sitemap, giving up (None) if it is corrupt, truncated or would
    expand past ``cap`` bytes, so a small "zip bomb" cannot exhaust memory."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(body, cap + 1)
    except zlib.error:
        return None
    if len(out) > cap or d.unconsumed_tail or not d.eof:
        return None
    return out


def _sitemap_urls(fetcher: Fetcher, start_url: str, limit: int) -> list[str]:
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(start_url))
    queue = list(fetcher.robots.sitemaps(start_url)) or [origin + "/sitemap.xml"]
    seen_maps: set[str] = set()
    found: list[str] = []
    while queue and len(seen_maps) < 20 and len(found) < limit:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        if fetcher.respect_robots and not fetcher.robots.allowed(sm):
            continue
        try:
            fetcher._wait_turn(sm)
            r, body, too_big = read_capped(fetcher.client, sm, fetcher.max_sitemap_bytes)
        except httpx.HTTPError:
            continue
        if r.status_code >= 400 or too_big:
            continue
        if body[:2] == b"\x1f\x8b":  # a .xml.gz file (not HTTP Content-Encoding, which httpx undoes)
            body = _gunzip_capped(body, fetcher.max_sitemap_bytes)
            if body is None:
                continue
        try:
            root = etree.fromstring(body, parser=_XML_PARSER)
        except etree.XMLSyntaxError:
            continue
        if root is None:
            continue
        locs = [(el.text or "").strip() for el in root.iter() if isinstance(el.tag, str) and el.tag.endswith("loc")]
        if root.tag.endswith("sitemapindex"):
            queue.extend(loc for loc in locs if loc)
        else:
            found.extend(loc for loc in locs if loc)
    return found[:limit]


def crawl(
    start_url: str,
    max_pages: int = 50,
    max_depth: int = 3,
    fetcher: Fetcher | None = None,
    options: ExtractOptions | None = None,
    use_sitemap: bool = True,
    include: str | None = None,
    exclude: str | None = None,
    content_links_only: bool = False,
) -> Iterator[CrawlEvent]:
    """Crawl breadth-first from ``start_url``. ``content_links_only`` follows only links
    found in the extracted main content, never menu/sidebar/footer links.

    The crawl stays on the start URL's site, under its folder. If the start page
    redirects (to another host, or a section that moved), that boundary follows it.
    """
    own = fetcher is None
    f = fetcher or Fetcher()
    scope_url, prefix = start_url, scope_prefix(start_url)
    inc = re.compile(include) if include else None
    exc = re.compile(exclude) if exclude else None

    def wanted(url: str) -> bool:
        return (in_scope(url, scope_url, prefix) and looks_like_page(url)
                and (not inc or inc.search(url)) and not (exc and exc.search(url)))

    queue: deque[tuple[str, int]] = deque([(start_url, 0)])
    seen = {normalize_url(start_url)}
    done: dict[str, str] = {}  # normalised url / canonical -> url it was extracted from
    hashes: dict[str, str] = {}
    pages = attempts = 0
    try:
        while queue and pages < max_pages and attempts < max_pages * 3:
            url, depth = queue.popleft()
            attempts += 1
            res = f.fetch(url)
            if attempts == 1:  # the start page: follow it if it moved, then queue the sitemap
                if (res.ok and res.final_url and normalize_url(res.final_url) != normalize_url(start_url)
                        and related_site(start_url, res.final_url)):
                    scope_url, prefix = res.final_url, rebase_scope(res.final_url, prefix)
                if use_sitemap:
                    for loc in _sitemap_urls(f, scope_url, limit=max_pages * 5):
                        n = normalize_url(loc)
                        if n not in seen and wanted(loc):
                            seen.add(n)
                            queue.append((loc, 1))
            if not res.ok:
                yield CrawlEvent(url, depth, skipped=res.skipped, error=res.error)
                continue
            final = res.final_url or url
            n_final = normalize_url(final)
            if n_final in done and done[n_final] != url:
                yield CrawlEvent(url, depth, skipped=f"redirects to already crawled {done[n_final]}")
                continue
            doc = extract(res.html, url=final, options=options, fetch=res.info())
            canon = normalize_url(doc.canonical_url) if doc.canonical_url else None
            if canon and canon != n_final and canon in done:
                yield CrawlEvent(url, depth, skipped=f"canonical duplicate of {done[canon]}")
                continue
            digest = hashlib.sha256(doc.text.encode("utf-8")).hexdigest()
            if doc.text and digest in hashes:
                yield CrawlEvent(url, depth, skipped=f"same content as {hashes[digest]}")
                continue
            done[n_final] = url
            if canon:
                done[canon] = url
                seen.add(canon)
            if doc.text:
                hashes[digest] = url
            pages += 1
            yield CrawlEvent(url, depth, document=doc)
            if depth < max_depth:
                content = [l["url"] for l in doc.links]
                rest = [] if content_links_only else page_links(res.html, final)
                for link in content + rest:  # content links first
                    n = normalize_url(link)
                    if n not in seen and wanted(link):
                        seen.add(n)
                        queue.append((link, depth + 1))
    finally:
        if own:
            f.close()


@dataclass
class MappedUrl:
    url: str
    source: str  # "sitemap" or "page"


def map_site(start_url: str, fetcher: Fetcher | None = None, limit: int = 500, use_sitemap: bool = True,
             whole_site: bool = True) -> list[MappedUrl]:
    """List a site's page URLs quickly, without extracting them: the sitemap(s) from
    robots.txt (or /sitemap.xml) plus the links on the start page.

    ``whole_site=False`` keeps only URLs under the start URL's directory, the same
    scope :func:`crawl` uses. Non-HTML files (PDF, images, ...) are left out.
    """
    own = fetcher is None
    f = fetcher or Fetcher()
    scope_url, prefix = start_url, ("/" if whole_site else scope_prefix(start_url))
    seen: set[str] = set()
    out: list[MappedUrl] = []

    def add(url: str, source: str) -> None:
        url = url.split("#", 1)[0]
        n = normalize_url(url)
        if n in seen or not in_scope(url, scope_url, prefix) or not looks_like_page(url):
            return
        seen.add(n)
        out.append(MappedUrl(url, source))

    try:
        res = f.fetch(start_url)
        final = (res.final_url or start_url) if res.ok else start_url
        if normalize_url(final) != normalize_url(start_url) and related_site(start_url, final):  # it moved
            scope_url = final
            if not whole_site:
                prefix = rebase_scope(final, prefix)
        add(final, "page")
        if use_sitemap:
            for loc in _sitemap_urls(f, scope_url, limit=limit * 2):
                add(loc, "sitemap")
                if len(out) >= limit:
                    return out[:limit]
        if res.ok:
            for link in page_links(res.html, final):
                add(link, "page")
                if len(out) >= limit:
                    break
        return out[:limit]
    finally:
        if own:
            f.close()
