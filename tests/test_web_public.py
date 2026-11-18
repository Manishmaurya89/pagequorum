"""Public mode (`pagequorum serve --public`): limits for a server on the open internet."""

import time

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pagequorum.fetch import Fetcher  # noqa: E402
from pagequorum.web import PublicLimits, RateLimiter, create_app  # noqa: E402

ORIGIN = "https://manishmaurya89.github.io"


def site(article_html):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, html=article_html)
    return handler


def public_client(handler=None, factory=None, **limits) -> TestClient:
    transport = httpx.MockTransport(handler or (lambda req: httpx.Response(404)))
    app = create_app(public=PublicLimits(**limits), cors_origins=[ORIGIN], client_ip_header="True-Client-IP",
                     fetcher_factory=factory or (lambda: Fetcher(transport=transport, delay=0)),
                     allowed_hosts=["pagequorum.onrender.com", "localhost"])
    return TestClient(app, base_url="http://pagequorum.onrender.com")


def test_private_network_addresses_are_refused():
    # the real public fetcher: these are refused before any connection is made
    c = TestClient(create_app(public=PublicLimits(), allowed_hosts=["*"]), base_url="http://localhost")
    for url in ("http://127.0.0.1:8000/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
                "http://10.0.0.1/", "http://192.168.1.1/"):
        r = c.post("/api/scrape", json={"url": url})
        assert r.status_code == 422 and "refused" in r.json()["detail"], url


def test_scrapes_are_rate_limited_per_visitor(article_html):
    c = public_client(site(article_html), scrapes_per_minute=2)
    me, other = {"True-Client-IP": "203.0.113.5"}, {"True-Client-IP": "198.51.100.7"}
    assert c.post("/api/scrape", json={"url": "https://ex.com/a"}, headers=me).status_code == 200
    assert c.post("/api/scrape", json={"html": article_html}, headers=me).status_code == 200  # pasted HTML counts
    r = c.post("/api/scrape", json={"url": "https://ex.com/a"}, headers=me)
    assert r.status_code == 429 and 0 < int(r.headers["retry-after"]) <= 60
    assert c.post("/api/scrape", json={"url": "https://ex.com/a"}, headers=other).status_code == 200


def test_the_whole_server_has_a_limit(article_html):
    c = public_client(site(article_html), requests_per_minute=2)
    for ip in ("203.0.113.1", "203.0.113.2"):
        assert c.post("/api/scrape", json={"html": article_html}, headers={"True-Client-IP": ip}).status_code == 200
    r = c.post("/api/scrape", json={"html": article_html}, headers={"True-Client-IP": "203.0.113.3"})
    assert r.status_code == 429 and "busy" in r.json()["detail"]


def test_too_many_fetches_at_once_get_busy(article_html):
    c = public_client(site(article_html), concurrent=0)
    r = c.post("/api/scrape", json={"url": "https://ex.com/a"})
    assert r.status_code == 503 and r.headers["retry-after"] == "5"


def test_each_request_gets_its_own_fetcher_and_closes_it(article_html):
    made = []

    def factory():
        f = Fetcher(transport=httpx.MockTransport(site(article_html)), delay=0)
        made.append(f)
        return f

    c = public_client(factory=factory)
    for _ in range(2):
        assert c.post("/api/scrape", json={"url": "https://ex.com/a"}).status_code == 200
    assert len(made) == 2 and all(f.client.is_closed for f in made)


def test_size_and_crawl_caps():
    c = public_client(html_chars=100)
    assert c.post("/api/scrape", json={"html": "<p>" + "x" * 200 + "</p>"}).status_code == 413
    assert c.post("/api/crawl", json={"url": "https://ex.com/", "max_pages": 11}).status_code == 422
    assert c.post("/api/crawl", json={"url": "https://ex.com/", "max_depth": 3}).status_code == 422
    r = c.post("/api/crawl", json={"url": "https://ex.com/", "include": "(a+)+$"})  # no regex from strangers
    assert r.status_code == 422 and "include/exclude" in r.json()["detail"]
    assert c.post("/api/map", json={"url": "https://ex.com/", "limit": 501}).status_code == 422


def test_one_running_crawl_per_visitor(article_html):
    import threading

    gate = threading.Event()

    def slow(req):
        gate.wait(5)
        return site(article_html)(req)

    c = public_client(slow)
    me = {"True-Client-IP": "203.0.113.9"}
    first = c.post("/api/crawl", json={"url": "https://ex.com/", "max_pages": 2}, headers=me)
    assert first.status_code == 202
    again = c.post("/api/crawl", json={"url": "https://ex.com/", "max_pages": 2}, headers=me)
    assert again.status_code == 429 and "already have a crawl" in again.json()["detail"]
    assert c.post("/api/crawl", json={"url": "https://ex.com/", "max_pages": 2},
                  headers={"True-Client-IP": "203.0.113.10"}).status_code == 202
    third = c.post("/api/crawl", json={"url": "https://ex.com/", "max_pages": 2},
                   headers={"True-Client-IP": "203.0.113.11"})
    assert third.status_code == 429  # two crawls at once, server-wide
    gate.set()
    job = first.json()["id"]
    for _ in range(250):
        if c.get(f"/api/crawl/{job}").json()["status"] != "running":
            break
        time.sleep(0.02)
    assert c.get(f"/api/crawl/{job}").json()["status"] == "done"


def test_health_reports_the_limits_and_cors_allows_only_the_demo_page():
    c = public_client()
    h = c.get("/api/health").json()
    assert h["ok"] and h["public"]["crawl_pages"] == 10
    pre = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
    ok = c.options("/api/scrape", headers={"Origin": ORIGIN, **pre})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == ORIGIN
    bad = c.options("/api/scrape", headers={"Origin": "https://evil.example", **pre})
    assert "access-control-allow-origin" not in bad.headers
    assert c.get("/api/health", headers={"host": "attacker.example"}).status_code == 400


def test_errors_are_readable_cross_origin():
    c = public_client(html_chars=10)
    r = c.post("/api/scrape", json={"html": "<p>much too long</p>"}, headers={"Origin": ORIGIN})
    assert r.status_code == 413 and r.headers["access-control-allow-origin"] == ORIGIN


def test_rate_limiter_window_slides():
    now = [0.0]
    rl = RateLimiter(2, 60, clock=lambda: now[0])
    rl.add("a"); now[0] = 10; rl.add("a")  # noqa: E702
    assert rl.wait("a") == 50 and rl.wait("b") == 0
    now[0] = 60.5
    assert rl.wait("a") == 0  # the first hit left the window


def test_rate_limiter_forgets_idle_visitors():
    now = [0.0]
    rl = RateLimiter(1, 60, clock=lambda: now[0])
    rl.MAX_KEYS = 3
    for k in "abcd":
        rl.add(k)
    now[0] = 100
    rl.add("e")
    assert set(rl._hits) == {"e"}


def test_local_mode_is_unchanged():
    c = TestClient(create_app(), base_url="http://localhost")
    h = c.get("/api/health").json()
    assert "public" not in h
    assert "access-control-allow-origin" not in c.get("/api/health", headers={"Origin": ORIGIN}).headers


def test_huge_bodies_are_refused_before_they_are_read():
    c = public_client(body_bytes=1000)
    big = '{"html": "' + "x" * 5000 + '"}'
    r = c.post("/api/scrape", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413

    def chunks():  # no Content-Length: counted while it streams in
        yield b'{"html": "'
        for _ in range(10):
            yield b"x" * 500
        yield b'"}'

    r = c.post("/api/scrape", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert c.post("/api/scrape", json={"html": "<p>small</p>"}).status_code == 200


def test_rate_limiter_checks_store_nothing():
    rl = RateLimiter(1, 60)
    for i in range(100):
        rl.wait(f"visitor-{i}")
    assert rl._hits == {}


def test_a_malformed_client_ip_header_is_not_a_visitor_key(article_html):
    c = public_client(site(article_html), scrapes_per_minute=1)
    assert c.post("/api/scrape", json={"html": article_html}, headers={"True-Client-IP": "junk-1"}).status_code == 200
    # a different junk value is the same visitor (the connection's address), so no fresh allowance
    r = c.post("/api/scrape", json={"html": article_html}, headers={"True-Client-IP": "junk-2"})
    assert r.status_code == 429
