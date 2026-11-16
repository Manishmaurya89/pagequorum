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


def streamed(body: bytes, encoding: str | None = None, seen: list | None = None):
    """A route served as a real stream (as from the network), optionally compressed."""
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.headers.get("accept-encoding"))
        headers = {"content-type": HTML} | ({"content-encoding": encoding} if encoding else {})
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))
    return handler


def test_compressed_pages_are_decoded():
    import gzip
    import zlib

    page = article("Compressed").encode()
    raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    routes = {
        "/gzip": streamed(gzip.compress(page), "gzip"),
        "/deflate": streamed(zlib.compress(page), "deflate"),
        "/raw-deflate": streamed(raw.compress(page) + raw.flush(), "deflate"),
        "/plain": streamed(page),
    }
    f = fetcher(site(routes))
    for path in routes:
        res = f.fetch(f"https://ex.com{path}")
        assert res.ok and "Compressed paragraph 4" in res.html, path


def test_zip_bomb_page_is_stopped_at_the_size_limit():
    import tracemalloc
    import zlib

    gz = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    zeros = b"\0" * 1_000_000
    bomb = b"".join(gz.compress(zeros) for _ in range(200)) + gz.flush()  # ~200 KB -> 200 MB
    f = fetcher(site({"/bomb": streamed(bomb, "gzip")}), max_bytes=1_000_000)
    tracemalloc.start()
    res = f.fetch("https://ex.com/bomb")
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert res.skipped == "response larger than 1000000 bytes"
    assert peak < 20_000_000  # inflated in bounded steps, never all 200 MB


def test_unsupported_or_corrupt_encoding_is_an_error_not_a_crash():
    import gzip

    seen: list = []
    routes = {"/br": streamed(b"\x1b\x00\x00", "br", seen),
              "/bad": streamed(gzip.compress(article("X").encode())[:40] + b"garbage" * 20, "gzip")}
    f = fetcher(site(routes))
    br = f.fetch("https://ex.com/br")
    assert not br.ok and "unsupported content-encoding" in br.error
    assert seen == ["gzip, deflate"]  # brotli is never asked for
    bad = f.fetch("https://ex.com/bad")
    assert not bad.ok and "could not decode" in bad.error


def test_rebase_scope_follows_a_moved_start_page():
    from pagequorum.urls import rebase_scope

    assert rebase_scope("https://www.ex.com/docs/intro/", "/docs/") == "/docs/"  # new host/scheme only
    assert rebase_scope("https://new.ex.com/docs/v/latest/concepts/models/", "/latest/concepts/") == \
        "/docs/v/latest/concepts/"  # the section moved and kept its name
    assert rebase_scope("https://ex.com/guide/start.html", "/manual/") == "/guide/"  # landed elsewhere
    assert rebase_scope("https://ex.com/", "/old/") == "/"


def moved_site():
    """old.ex.com/latest/concepts/ now redirects to a section on new.ex.com (like docs that moved)."""
    base = "/docs/v/latest/concepts/"
    links = (f'<a href="{base}types/">Types</a> <a href="{base}fields/">Fields</a> '
             '<a href="/docs/other/">Other section</a>')
    pages = {f"{base}models/": article("Models", links), f"{base}types/": article("Types"),
             f"{base}fields/": article("Fields"), "/docs/other/": article("Other")}

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if host == "old.ex.com" and path == "/latest/concepts/":
            return httpx.Response(301, headers={"location": f"https://new.ex.com{base}models/"})
        if host == "new.ex.com" and path in pages:
            return httpx.Response(200, headers={"content-type": HTML}, text=pages[path])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_crawl_follows_a_start_page_that_redirects_to_another_site():
    events = list(pq.crawl("https://old.ex.com/latest/concepts/", max_pages=10, fetcher=fetcher(moved_site())))
    done = sorted(ev.document.url for ev in events if ev.document)
    assert done == ["https://new.ex.com/docs/v/latest/concepts/fields/",
                    "https://new.ex.com/docs/v/latest/concepts/models/",
                    "https://new.ex.com/docs/v/latest/concepts/types/"]  # not /docs/other/


def test_map_follows_a_start_page_that_redirects_to_another_site():
    from pagequorum.crawl import map_site

    urls = [u.url for u in map_site("https://old.ex.com/latest/concepts/", fetcher=fetcher(moved_site()),
                                    whole_site=False)]
    assert urls[0] == "https://new.ex.com/docs/v/latest/concepts/models/"
    assert "https://new.ex.com/docs/v/latest/concepts/types/" in urls
    assert not any("/docs/other/" in u for u in urls)
    whole = [u.url for u in map_site("https://old.ex.com/latest/concepts/", fetcher=fetcher(moved_site()))]
    assert "https://new.ex.com/docs/other/" in whole


def test_related_site_allows_moved_docs_but_not_private_or_unrelated_hosts():
    from pagequorum.urls import related_site

    assert related_site("https://docs.pydantic.dev/x/", "https://pydantic.dev/docs/x/")  # parent domain
    assert related_site("https://ex.com/", "https://docs.ex.com/")  # subdomain
    assert related_site("https://old.ex.com/a/", "https://new.ex.com/a/")  # sibling
    assert related_site("http://ex.com/", "https://www.ex.com/")  # scheme / www only
    assert not related_site("https://ex.com/", "https://other.org/")
    assert not related_site("https://ex.com/", "http://127.0.0.1:8000/admin/")
    assert not related_site("https://ex.com/", "http://192.168.1.1/")
    assert not related_site("https://ex.com/", "http://[::1]/")
    assert not related_site("https://ex.com/", "http://router.local/")


def test_a_start_page_that_redirects_into_a_private_network_does_not_widen_the_crawl():
    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if host == "evil.example":
            return httpx.Response(302, headers={"location": "http://192.168.1.1/admin/"})
        if host == "192.168.1.1":
            links = '<a href="/admin/users/">Users</a> <a href="/admin/wifi/">Wi-Fi</a>'
            return httpx.Response(200, headers={"content-type": HTML}, text=article("Router", links))
        return httpx.Response(404)

    events = list(pq.crawl("https://evil.example/start/", max_pages=10, fetcher=fetcher(httpx.MockTransport(handler))))
    assert [ev.url for ev in events] == ["https://evil.example/start/"]  # nothing on the router is followed


def test_allow_private_false_refuses_private_addresses_and_redirects_into_them():
    from pagequorum.fetch import private_address

    for url in ("http://127.0.0.1/", "http://localhost:8000/", "http://169.254.169.254/", "http://10.0.0.1/",
                "http://192.168.1.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://100.64.0.1/"):
        assert private_address(url), url
    assert private_address("https://ex.com/") is None

    def handler(request):
        if request.url.host == "ex.com":
            return httpx.Response(302, headers={"location": "http://192.168.1.1/admin"})
        return httpx.Response(200, headers={"content-type": HTML}, text=article("Router"))

    f = fetcher(httpx.MockTransport(handler), allow_private=False, respect_robots=False)
    assert f.fetch("https://ex.com/").skipped.startswith("refused: 192.168.1.1")
    assert f.fetch("http://192.168.1.1/admin").skipped.startswith("refused:")
    assert fetcher(httpx.MockTransport(handler), respect_robots=False).fetch("http://192.168.1.1/admin").ok  # default: allowed


def test_a_name_that_turns_private_between_the_check_and_the_connection_is_refused(monkeypatch):
    # DNS rebinding: the name looks public while it is checked, then answers with the cloud
    # metadata address when the connection resolves it. The connection is what counts.
    import socket

    from pagequorum import fetch as fetch_mod

    answers = iter(["93.184.215.14", "93.184.215.14"])  # the early check and the request hook

    def rebinding(host, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers, "169.254.169.254"), 0))]

    monkeypatch.setattr(fetch_mod.socket, "getaddrinfo", rebinding)
    monkeypatch.setattr(fetch_mod.httpcore.SyncBackend, "connect_tcp",
                        lambda *a, **kw: pytest.fail("connected to an unchecked address"))
    f = fetch_mod.Fetcher(allow_private=False, respect_robots=False, retries=0)
    assert isinstance(f.client._transport._pool._network_backend, fetch_mod._PublicOnlyBackend)
    res = f.fetch("http://rebind.test/")
    assert res.skipped == "refused: rebind.test is a private-network address (169.254.169.254)"


def test_the_guard_connects_to_the_address_it_checked_and_fails_closed(monkeypatch):
    import socket

    import httpcore

    from pagequorum import fetch as fetch_mod

    calls, opened = [], []

    def once(host, *a, **kw):
        calls.append(host)
        if host == "gone.test":
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 0))]

    class Inner:
        def connect_tcp(self, host, port, **kw):
            opened.append((host, port))
            return "stream"

    monkeypatch.setattr(fetch_mod.socket, "getaddrinfo", once)
    guard = fetch_mod._PublicOnlyBackend(Inner())
    assert guard.connect_tcp("ex.com", 443) == "stream"
    assert opened == [("93.184.215.14", 443)] and calls == ["ex.com"]  # resolved once, that IP used
    with pytest.raises(httpcore.ConnectError):
        guard.connect_tcp("gone.test", 443)  # unresolvable: no connection, not a pass
    with pytest.raises(fetch_mod.BlockedAddress):
        guard.connect_tcp("127.0.0.1", 80)
    with pytest.raises(fetch_mod.BlockedAddress):
        guard.connect_unix_socket("/var/run/docker.sock")
    assert fetch_mod.Fetcher().client._transport.__class__ is httpx.HTTPTransport  # default: unchanged
