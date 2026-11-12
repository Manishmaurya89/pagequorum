"""Fetching and crawling against an in-memory site (httpx.MockTransport): no network."""

import httpx
import pytest

import pagequorum as pq
from pagequorum.fetch import Fetcher
from pagequorum.urls import in_scope, normalize_url


def article(title: str, links: str = "", extra: str = "") -> str:
    body = "".join(
        f"<p>{title} paragraph {i} explains the topic in plenty of words so that it reads as content.</p>"
        for i in range(5)
    )
    return (f"<html><head><title>{title}</title>{extra}</head><body><nav>{links}</nav>"
            f"<article><h1>{title}</h1>{body}</article></body></html>")


def site(routes: dict, robots: tuple[int, str] = (404, ""), calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(robots[0], text=robots[1])
        if path in routes:
            spec = routes[path]
            if callable(spec):
                return spec(request)
            status, ctype, body = spec
            content = body if isinstance(body, bytes) else body.encode("utf-8")
            return httpx.Response(status, headers={"content-type": ctype}, content=content)
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


def fetcher(transport, **kw) -> Fetcher:
    return Fetcher(transport=transport, delay=0, **kw)


HTML = "text/html; charset=utf-8"


def test_robots_disallow_is_respected():
    t = site({"/private/x": (200, HTML, article("X"))}, robots=(200, "User-agent: *\nDisallow: /private/"))
    res = fetcher(t).fetch("https://ex.com/private/x")
    assert res.skipped == "disallowed by robots.txt" and res.html is None


def test_robots_404_allows_and_5xx_blocks():
    page = {"/a": (200, HTML, article("A"))}
    assert fetcher(site(page, robots=(404, ""))).fetch("https://ex.com/a").ok
    assert "robots.txt returned HTTP 503" in fetcher(site(page, robots=(503, ""))).fetch("https://ex.com/a").skipped
    assert fetcher(site(page, robots=(503, "")), respect_robots=False).fetch("https://ex.com/a").ok


def test_non_html_is_skipped():
    t = site({"/file": (200, "application/pdf", b"%PDF-1.7")})
    res = fetcher(t).fetch("https://ex.com/file")
    assert res.skipped == "not HTML (content-type application/pdf)"


def test_retries_on_503_then_succeeds():
    attempts = []

    def flaky(request):
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, headers={"retry-after": "1"})
        return httpx.Response(200, headers={"content-type": HTML}, text=article("Flaky"))

    res = fetcher(site({"/f": flaky}), retries=2).fetch("https://ex.com/f")
    assert res.ok and len(attempts) == 3


def test_gives_up_after_retries():
    res = fetcher(site({"/down": (502, HTML, "bad gateway")}), retries=1).fetch("https://ex.com/down")
    assert not res.ok and res.error == "HTTP 502"


def test_oversized_body_is_refused():
    big = "<html><body>" + "x" * 5000 + "</body></html>"
    res = fetcher(site({"/big": (200, HTML, big)}), max_bytes=1000).fetch("https://ex.com/big")
    assert res.skipped and "larger than" in res.skipped


def test_wrong_charset_header_is_corrected():
    body = "<html><body><p>It’s working — हिन्दी</p></body></html>".encode("utf-8")
    res = fetcher(site({"/u": (200, "text/html; charset=ISO-8859-1", body)})).fetch("https://ex.com/u")
    assert "It’s working — हिन्दी" in res.html and res.encoding == "utf-8"


def test_cache_round_trip(tmp_path):
    calls = []
    t = site({"/c": (200, HTML, article("Cached"))}, calls=calls)
    f = fetcher(t, cache_dir=tmp_path)
    first = f.fetch("https://ex.com/c")
    n = len(calls)
    second = f.fetch("https://ex.com/c")
    assert first.ok and second.ok and second.from_cache and len(calls) == n
    assert second.html == first.html


def test_scrape_raises_on_failure():
    t = site({}, robots=(404, ""))
    with pytest.raises(pq.ScrapeError, match="HTTP 404"):
        pq.scrape("https://ex.com/missing", fetcher=fetcher(t), render="never")


def test_normalize_url():
    assert normalize_url("HTTPS://Ex.com:443/a/./b/../c?utm_source=x&b=2&a=1#frag") == "https://ex.com/a/c?a=1&b=2"
    assert normalize_url("http://ex.com") == "http://ex.com/"


def test_scope():
    assert in_scope("https://www.ex.com/docs/a", "https://ex.com/docs/intro")
    assert not in_scope("https://ex.com/blog/a", "https://ex.com/docs/intro")
    assert not in_scope("https://other.com/docs/a", "https://ex.com/docs/intro")


def test_crawl_follows_links_in_scope_and_dedupes():
    nav = ('<a href="/docs/a">A</a> <a href="/docs/b?utm_source=nav">B</a> <a href="/docs/b">B again</a>'
           ' <a href="/blog/post">Blog</a> <a href="https://other.com/docs/x">Other</a>'
           ' <a href="/docs/file.pdf">PDF</a> <a href="/docs/a-copy">Copy</a>')
    routes = {
        "/docs/": (200, HTML, article("Index", nav)),
        "/docs/a": (200, HTML, article("Alpha", nav)),
        "/docs/b": (200, HTML, article("Beta", nav)),
        "/docs/a-copy": (200, HTML, article("Alpha copy", nav, '<link rel="canonical" href="https://ex.com/docs/a">')),
        "/blog/post": (200, HTML, article("Blog", nav)),
    }
    calls = []
    events = list(pq.crawl("https://ex.com/docs/", max_pages=10, fetcher=fetcher(site(routes, calls=calls))))
    crawled = {e.url for e in events if e.document}
    assert crawled == {"https://ex.com/docs/", "https://ex.com/docs/a", "https://ex.com/docs/b?utm_source=nav"}
    copy = next(e for e in events if e.url.endswith("a-copy"))
    assert copy.document is None and "canonical duplicate" in copy.skipped
    fetched = [u for u in calls if "robots" not in u and "sitemap" not in u]
    assert not any("blog" in u or "other.com" in u or u.endswith(".pdf") for u in fetched)
    assert sum(1 for u in fetched if "/docs/b" in u) == 1  # utm-tagged and clean URL fetched once


def test_crawl_uses_sitemap_from_robots():
    sitemap = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               "<url><loc>https://ex.com/docs/hidden</loc></url></urlset>")
    routes = {
        "/docs/": (200, HTML, article("Index")),
        "/docs/hidden": (200, HTML, article("Only in sitemap")),
        "/sm.xml": (200, "application/xml", sitemap),
    }
    t = site(routes, robots=(200, "User-agent: *\nAllow: /\nSitemap: https://ex.com/sm.xml"))
    events = list(pq.crawl("https://ex.com/docs/", max_pages=5, fetcher=fetcher(t)))
    assert "https://ex.com/docs/hidden" in {e.url for e in events if e.document}


def test_crawl_respects_max_pages():
    nav = "".join(f'<a href="/p{i}">p{i}</a>' for i in range(20))
    routes = {f"/p{i}": (200, HTML, article(f"Page {i}", nav)) for i in range(20)}
    routes["/"] = (200, HTML, article("Home", nav))
    events = list(pq.crawl("https://ex.com/", max_pages=4, fetcher=fetcher(site(routes)), use_sitemap=False))
    assert sum(1 for e in events if e.document) == 4


def test_unreachable_site_is_reported_as_unreachable():
    def handler(req):
        raise httpx.ConnectError("name or service not known")

    res = fetcher(httpx.MockTransport(handler)).fetch("https://no-such-host.invalid/a")
    assert res.skipped.startswith("could not reach the site (ConnectError")


def test_crawl_follows_content_links_before_menu_links():
    def page(title, extra=""):
        body = "".join(f"<p>Paragraph {i} of {title} explains the subject in enough words to be content.</p>" for i in range(5))
        return (f"<html><body><nav><a href='/wiki/help'>Help</a> <a href='/wiki/about'>About</a></nav>"
                f"<article><h1>{title}</h1>{body}{extra}</article></body></html>")

    routes = {
        "/wiki/start": (200, HTML, page("Start", "<p>Related: <a href='/wiki/related'>Related topic</a> explained.</p>")),
        "/wiki/related": (200, HTML, page("Related")),
        "/wiki/help": (200, HTML, page("Help")),
        "/wiki/about": (200, HTML, page("About")),
    }
    t = site(routes)
    urls = [ev.url for ev in pq.crawl("https://ex.com/wiki/start", max_pages=2, fetcher=fetcher(t), use_sitemap=False)]
    assert urls == ["https://ex.com/wiki/start", "https://ex.com/wiki/related"]  # not the menu's /wiki/help
    only = [ev.url for ev in pq.crawl("https://ex.com/wiki/start", max_pages=5, fetcher=fetcher(t), use_sitemap=False,
                                   content_links_only=True)]
    assert only == ["https://ex.com/wiki/start", "https://ex.com/wiki/related"]


def test_robots_txt_is_read_only_up_to_500_kib():
    from pagequorum.fetch import ROBOTS_MAX_BYTES

    rules = ("User-agent: *\nDisallow: /private/\n" + "# filler line\n" * (ROBOTS_MAX_BYTES // 14 + 10)
             + "Disallow: /late/\n")
    t = site({"/private/x": (200, HTML, article("P")), "/late/x": (200, HTML, article("L"))}, robots=(200, rules))
    f = fetcher(t)
    assert f.fetch("https://ex.com/private/x").skipped == "disallowed by robots.txt"
    assert f.fetch("https://ex.com/late/x").ok  # the rule after the limit is not read


def test_oversized_and_zip_bomb_sitemaps_are_skipped(monkeypatch):
    import gzip
    import importlib

    crawl_mod = importlib.import_module("pagequorum.crawl")
    monkeypatch.setattr(crawl_mod, "_MAX_SITEMAP_BYTES", 10_000)

    def urlset(loc: str, pad: int = 0) -> bytes:
        return (f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{loc}</loc></url>'
                f'<!--{"x" * pad}--></urlset>').encode()

    bomb = gzip.compress(urlset("https://ex.com/bomb", pad=2_000_000))  # ~2 KB that expands to 2 MB
    assert len(bomb) < 10_000
    routes = {
        "/bomb.xml.gz": (200, "application/gzip", bomb),
        "/big.xml": (200, "application/xml", urlset("https://ex.com/big", pad=20_000)),
        "/cut.xml.gz": (200, "application/gzip", gzip.compress(urlset("https://ex.com/cut"))[:-12]),
        "/ok.xml.gz": (200, "application/gzip", gzip.compress(urlset("https://ex.com/good"))),
    }
    robots = "User-agent: *\nAllow: /\n" + "".join(f"Sitemap: https://ex.com{p}\n" for p in routes)
    found = crawl_mod._sitemap_urls(fetcher(site(routes, robots=(200, robots))), "https://ex.com/", 100)
    assert found == ["https://ex.com/good"]
