"""The MCP server, driven in-process by the MCP client (no network)."""

import anyio
import httpx
import pytest

pytest.importorskip("mcp")
from mcp import Client  # noqa: E402

from pagequorum.fetch import Fetcher  # noqa: E402
from pagequorum.mcp_server import build_server  # noqa: E402

HTML = "text/html; charset=utf-8"
NAV = "<nav><a href='/'>Home</a> · <a href='/about'>About us</a> · <a href='/subscribe'>Subscribe</a></nav>"


def page(title: str, links: str = "") -> str:
    body = "".join(f"<p>{title} paragraph {i} explains solar power in enough words to read as content.</p>"
                   for i in range(5))
    return f"<html><head><title>{title}</title></head><body>{NAV}<article><h1>{title}</h1>{body}{links}</article></body></html>"


def site(handler):
    transport = httpx.MockTransport(handler)
    return build_server(lambda: Fetcher(transport=transport, delay=0, allow_private=False))


def docs_site(request: httpx.Request) -> httpx.Response:
    pages = {"/docs/": page("Guide", '<p>Next: <a href="/docs/install/">Install</a></p>'),
             "/docs/install/": page("Install")}
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    if request.url.path in pages:
        return httpx.Response(200, headers={"content-type": HTML}, text=pages[request.url.path])
    return httpx.Response(404)


def call(server, name: str, args: dict):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(name, args)
    result = anyio.run(go)
    return result.is_error, "".join(getattr(b, "text", "") for b in result.content)


def test_tools_are_listed_as_read_only():
    async def go():
        async with Client(build_server()) as c:
            return (await c.list_tools()).tools
    tools = {t.name: t for t in anyio.run(go)}
    assert set(tools) == {"scrape", "crawl", "map_site", "extract_html"}
    assert all(t.annotations.read_only_hint for t in tools.values())


def test_scrape_returns_clean_markdown_with_the_agreement():
    error, text = call(site(docs_site), "scrape", {"url": "https://ex.com/docs/"})
    assert not error
    assert text.startswith("Source: https://ex.com/docs/ | Guide") and "extractor agreement" in text
    assert "# Guide" in text and "Subscribe" not in text


def test_crawl_returns_every_page_and_checks_its_limits():
    error, text = call(site(docs_site), "crawl", {"url": "https://ex.com/docs/", "max_pages": 5})
    assert not error and "Crawled 2 page(s)" in text and "# Install" in text
    error, text = call(site(docs_site), "crawl", {"url": "https://ex.com/docs/", "max_pages": 500})
    assert error and "between 1 and 50" in text
    error, text = call(site(docs_site), "crawl", {"url": "https://ex.com/docs/", "include": "(["})
    assert error


def test_private_network_addresses_are_refused_even_after_a_redirect():
    def handler(request):
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, headers={"content-type": HTML}, text=page("Secret"))

    for url in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8000/", "https://public.example/"):
        error, text = call(site(handler), "scrape", {"url": url})
        assert error and "private-network address" in text and "Secret" not in text, url


def test_extract_html_needs_no_network_and_long_output_is_cut():
    error, text = call(build_server(), "extract_html", {"html": page("Offline")})
    assert not error and text.startswith("Source: (the HTML you gave) | Offline") and "# Offline" in text
    error, text = call(site(docs_site), "scrape", {"url": "https://ex.com/docs/", "max_chars": 40})
    assert "cut at 40 characters" in text
