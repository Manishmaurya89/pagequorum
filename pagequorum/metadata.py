"""Page metadata: title, author, date, language, canonical URL, description."""

from __future__ import annotations

from urllib.parse import urljoin

import trafilatura.metadata
from lxml import html as lxml_html

from .text import normalize_ws


def base_url(tree: lxml_html.HtmlElement, url: str | None) -> str | None:
    """The URL relative links resolve against: ``<base href>`` if present, else the page URL."""
    for base in tree.xpath("//head/base[@href]"):
        href = (base.get("href") or "").strip()
        if href:
            return urljoin(url, href) if url else href
    return url


def _meta(tree, *names: str) -> str | None:
    for name in names:
        for el in tree.xpath(f'//meta[@property="{name}" or @name="{name}"]'):
            value = normalize_ws(el.get("content") or "")
            if value:
                return value
    return None


def _language(tree) -> str | None:
    lang = tree.get("lang") or tree.get("xml:lang") or tree.get("{http://www.w3.org/XML/1998/namespace}lang")
    if not lang:
        for el in tree.xpath('//meta[translate(@http-equiv,"CONTENT-LANGUAGE","content-language")="content-language"]'):
            lang = el.get("content")
            break
    if not lang:
        lang = _meta(tree, "og:locale")
    if not lang:
        return None
    return lang.strip().replace("_", "-").split("-")[0].split(",")[0].lower() or None


def extract_metadata(html: str, tree: lxml_html.HtmlElement, url: str | None) -> dict:
    """Metadata from the head; uses trafilatura's metadata module for title/author/date."""
    meta = None
    try:
        meta = trafilatura.metadata.extract_metadata(html, default_url=url)
    except Exception:  # noqa: BLE001 - metadata must never break extraction
        meta = None

    canonical = None
    for link in tree.xpath('//link[@rel="canonical"][@href]'):
        href = (link.get("href") or "").strip()
        if href:
            canonical = urljoin(url, href) if url else href
            break

    title = getattr(meta, "title", None) or _meta(tree, "og:title")
    if not title:
        t = tree.find(".//title")
        title = normalize_ws(t.text_content()) if t is not None else None
    return {
        "title": normalize_ws(title) if title else None,
        "author": getattr(meta, "author", None),
        "date": getattr(meta, "date", None),
        "description": getattr(meta, "description", None) or _meta(tree, "description", "og:description"),
        "site_name": getattr(meta, "sitename", None) or _meta(tree, "og:site_name"),
        "language": _language(tree),
        "canonical_url": canonical,
    }
