"""The web app's API, run in-process (no network)."""

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pagequorum.fetch import Fetcher  # noqa: E402
from pagequorum.web import create_app  # noqa: E402


def client(handler=None) -> TestClient:
    transport = httpx.MockTransport(handler or (lambda req: httpx.Response(404)))
    return TestClient(create_app(Fetcher(transport=transport, delay=0),
                                 fetcher_factory=lambda: Fetcher(transport=transport, delay=0)))


def test_index_page_is_served():
    r = client().get("/")
    assert r.status_code == 200 and "pagequorum" in r.text and all(p in r.text for p in ("/api/scrape", "/api/crawl", "/api/map"))


def test_extract_from_html_returns_document_chunks_and_decisions(article_html):
    r = client().post("/api/scrape", json={"html": article_html, "url": "https://example.com/solar", "max_tokens": 128})
    assert r.status_code == 200
    j = r.json()
    assert j["document"]["title"] == "How Solar Panels Work"
    assert j["chunks"] and all(c["tokens"] <= 128 for c in j["chunks"])
    assert j["chunks"][0]["citation_url"].startswith("https://example.com/solar#:~:text=")
    kept = [b for b in j["blocks"] if b["kept"]]
    removed = [b for b in j["blocks"] if not b["kept"]]
    assert kept and removed
    assert any("Related stories" in b["text"] for b in removed)
    assert set(j["voters"]) >= {"trafilatura", "readability", "resiliparse"}


def test_extract_fetches_url(article_html):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, html=article_html)

    r = client(handler).post("/api/extract", json={"url": "example.com/solar"})
    assert r.status_code == 200
    assert r.json()["document"]["url"] == "https://example.com/solar"


def test_fetch_problems_are_reported():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        return httpx.Response(200, html="<p>x</p>")

    r = client(handler).post("/api/extract", json={"url": "https://example.com/private"})
    assert r.status_code == 422 and "robots.txt" in r.json()["detail"]


def test_request_needs_url_or_html():
    assert client().post("/api/extract", json={}).status_code == 422
    assert client().post("/api/extract", json={"html": "<p>x</p>", "max_tokens": 64, "overlap": 64}).status_code == 422


def test_old_extract_route_still_works(article_html):
    assert client().post("/api/extract", json={"html": article_html}).status_code == 200


def _site(article_html):
    def page(title):
        paras = "".join(f"<p>Paragraph {i} of {title} explains the topic in enough words to be content.</p>"
                        for i in range(5))
        return (f"<html><head><title>{title}</title></head><body><nav><a href='/a'>A</a> <a href='/b'>B</a></nav>"
                f"<article><h1>{title}</h1>{paras}</article></body></html>")

    def handler(req):
        path = req.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\nSitemap: https://ex.com/sitemap.xml")
        if path == "/sitemap.xml":
            return httpx.Response(200, text="<urlset><url><loc>https://ex.com/a</loc></url>"
                                            "<url><loc>https://ex.com/b</loc></url>"
                                            "<url><loc>https://ex.com/file.pdf</loc></url></urlset>")
        if path in ("/", "/a", "/b"):
            return httpx.Response(200, html=page({"/": "Home page", "/a": "Page A", "/b": "Page B"}[path]))
        return httpx.Response(404)

    return handler


def test_map_lists_sitemap_and_page_links(article_html):
    r = client(_site(article_html)).post("/api/map", json={"url": "ex.com/"})
    assert r.status_code == 200
    urls = {u["url"]: u["source"] for u in r.json()["urls"]}
    assert urls == {"https://ex.com/": "page", "https://ex.com/a": "sitemap", "https://ex.com/b": "sitemap"}


def test_crawl_job_runs_in_background_and_collects_chunks(article_html):
    import time

    c = client(_site(article_html))
    r = c.post("/api/crawl", json={"url": "https://ex.com/", "max_pages": 5})
    assert r.status_code == 202
    job_id = r.json()["id"]
    for _ in range(200):
        state = c.get(f"/api/crawl/{job_id}").json()
        if state["status"] != "running":
            break
        time.sleep(0.02)
    assert state["status"] == "done"
    assert state["pages_done"] == 3 and state["chunks"] >= 3
    titles = sorted(p["title"] for p in state["pages"] if p["status"] == "ok")
    assert titles == ["Home page", "Page A", "Page B"]
    assert c.get(f"/api/crawl/{job_id}?since=2").json()["pages"][0]["n"] == 2
    page = c.get(f"/api/crawl/{job_id}/pages/0").json()
    assert page["markdown"].startswith("# ")
    lines = c.get(f"/api/crawl/{job_id}/chunks.jsonl").text.strip().splitlines()
    assert len(lines) == state["chunks"]
    assert c.get("/api/crawl/nope").status_code == 404


def test_crawl_rejects_bad_regex():
    r = client().post("/api/crawl", json={"url": "https://ex.com/", "include": "(unclosed"})
    assert r.status_code == 422 and "regular expression" in r.text
