"""Wrappers around the established extractors that act as voters.

Each voter gets the original HTML and returns the lines of text it considers
main content (or raises). pagequorum never trusts any single one of them: their
outputs are only used to vote on the blocks of pagequorum's own DOM.

Voters:
  trafilatura  - strongest single open-source extractor on public benchmarks
  readability  - python port of Mozilla Readability (Firefox reader view)
  justext      - paragraph classifier using stopword and link density
  resiliparse  - fast main-content extractor from the Webis group (ChatNoir)
  jsonld       - the publisher's own ``articleBody`` from schema.org JSON-LD, when present
"""

from __future__ import annotations

import json
import logging
from typing import Callable

import justext
import trafilatura
from lxml import html as lxml_html
from readability import Document as ReadabilityDocument
from resiliparse.extract.html2text import extract_plain_text
from resiliparse.parse.html import HTMLTree

from . import dom
from .text import normalize_ws

for _name in ("readability", "readability.readability", "trafilatura", "htmldate", "justext", "courlan"):
    logging.getLogger(_name).setLevel(logging.CRITICAL)

# ISO 639-1 -> jusText stoplist name, for the languages most likely to be scraped.
_JUSTEXT_LANG = {
    "en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian",
    "pt": "Portuguese", "nl": "Dutch", "sv": "Swedish", "da": "Danish", "no": "Norwegian_Bokmal",
    "nb": "Norwegian_Bokmal", "fi": "Finnish", "pl": "Polish", "cs": "Czech", "sk": "Slovak",
    "ru": "Russian", "uk": "Ukrainian", "tr": "Turkish", "el": "Greek", "hu": "Hungarian",
    "ro": "Romanian", "bg": "Bulgarian", "hr": "Croatian", "sr": "Serbian", "sl": "Slovenian",
    "hi": "Hindi", "mr": "Marathi", "bn": "Bengali", "ta": "Tamil", "te": "Telugu",
    "gu": "Gujarati", "kn": "Kannada", "ml": "Malayalam", "pa": "Punjabi", "ur": "Urdu",
    "ne": "Nepali", "ar": "Arabic", "fa": "Persian", "he": "Hebrew", "id": "Indonesian",
    "ms": "Malay", "vi": "Vietnamese", "ca": "Catalan", "et": "Estonian", "lv": "Latvian",
    "lt": "Lithuanian", "sw": "Swahili", "tl": "Tagalog",
}
_ALL_STOPWORDS: frozenset[str] | None = None


def _stoplist(lang: str | None) -> frozenset[str]:
    global _ALL_STOPWORDS
    name = _JUSTEXT_LANG.get((lang or "").lower())
    if name and name in justext.get_stoplists():
        return justext.get_stoplist(name)
    # Unknown language: union of all stoplists, the same approach trafilatura uses.
    if _ALL_STOPWORDS is None:
        words: set[str] = set()
        for n in justext.get_stoplists():
            words.update(justext.get_stoplist(n))
        _ALL_STOPWORDS = frozenset(words)
    return _ALL_STOPWORDS


def run_trafilatura(html: str, url: str | None, lang: str | None) -> list[str]:
    out = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=True,
        output_format="txt",
        deduplicate=False,
    )
    return out.splitlines() if out else []


def run_readability(html: str, url: str | None, lang: str | None) -> list[str]:
    summary = ReadabilityDocument(html, url=url).summary(html_partial=True)
    if not summary:
        return []
    tree = lxml_html.fragment_fromstring(summary, create_parent="div")
    return [b.text for b in dom.segment(tree) if b.text]


def run_justext(html: str, url: str | None, lang: str | None) -> list[str]:
    paragraphs = justext.justext(html, _stoplist(lang))
    return [p.text for p in paragraphs if not p.is_boilerplate]


def run_resiliparse(html: str, url: str | None, lang: str | None) -> list[str]:
    text = extract_plain_text(
        HTMLTree.parse(html),
        preserve_formatting=True,
        main_content=True,
        list_bullets=False,
        alt_texts=False,
        links=False,
        form_fields=False,
        noscript=False,
        comments=False,
    )
    return text.splitlines() if text else []


_ARTICLE_TYPES = {
    "article", "newsarticle", "blogposting", "reportagenews", "techarticle",
    "scholarlyarticle", "analysisnewsarticle", "opinionnewsarticle", "report",
    "backgroundnewsarticle", "reviewnewsarticle", "socialmediaposting", "liveblogposting",
}


def _walk_jsonld(node, found: list[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_jsonld(item, found)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        body = node.get("articleBody")
        if isinstance(body, str) and any(str(t).lower() in _ARTICLE_TYPES for t in types):
            found.append(body)
        for value in node.values():
            if isinstance(value, (list, dict)):
                _walk_jsonld(value, found)


def jsonld_article_body(tree: lxml_html.HtmlElement) -> list[str]:
    """``articleBody`` text from schema.org JSON-LD blocks (read before cleaning)."""
    found: list[str] = []
    for script in tree.xpath('//script[@type="application/ld+json"]'):
        raw = script.text_content() or ""
        try:
            data = json.loads(raw, strict=False)
        except ValueError:
            continue
        _walk_jsonld(data, found)
    if not found:
        return []
    body = max(found, key=len)
    if "<" in body and ">" in body:  # some publishers put HTML in articleBody
        frag = lxml_html.fragment_fromstring(body, create_parent="div")
        return [b.text for b in dom.segment(frag) if b.text]
    return [normalize_ws(line) for line in body.splitlines() if line.strip()]


HTML_VOTERS: dict[str, Callable[[str, str | None, str | None], list[str]]] = {
    "trafilatura": run_trafilatura,
    "readability": run_readability,
    "justext": run_justext,
    "resiliparse": run_resiliparse,
}
