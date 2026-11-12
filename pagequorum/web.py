"""A local web app and HTTP API: Scrape one page, Crawl a site (as a background job
with live progress) or Map a site's URLs, and see the clean text, the RAG chunks and
every keep/drop decision.

    uv run --extra web pagequorum serve        # then open http://127.0.0.1:8000

It binds to 127.0.0.1 by default. It fetches whatever URL it is given, so do not
expose it on a public network.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from importlib import resources
from typing import Callable, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from . import __version__
from .crawl import crawl, map_site
from .extract import ExtractOptions, extract_explained
from .fetch import Fetcher
from .scrape import ScrapeError, scrape_explained

MAX_HTML_CHARS = 15_000_000
BLOCK_TEXT_CHARS = 400
MAX_JOBS = 20

TAGS = [
    {"name": "scrape", "description": "Extract one page: clean Markdown, RAG chunks and every keep/drop decision."},
    {"name": "crawl", "description": "Crawl a site section in the background and collect every page's chunks."},
    {"name": "map", "description": "List a site's page URLs from its sitemap and links, without extracting them."},
]


def _normalise_url(url: str | None) -> str | None:
    if url is None:
        return None
    url = url.strip()
    if url and "://" not in url:
        url = "https://" + url
    return url or None


class ScrapeRequest(BaseModel):
    url: str | None = Field(None, description="Page to fetch. Also used to resolve links when `html` is given.",
                            examples=["https://docs.python.org/3/library/json.html"])
    html: str | None = Field(None, description="Raw HTML to extract instead of fetching `url`.",
                             max_length=MAX_HTML_CHARS)
    max_tokens: int = Field(512, ge=32, le=8192, description="Chunk size in tokens.")
    overlap: int = Field(0, ge=0, le=2048, description="Overlap between consecutive chunks, in tokens.")
    links: bool = Field(False, description="Keep links as [text](url).")
    images: bool = Field(False, description="Keep images as ![alt](src).")
    render: Literal["auto", "never"] = Field("auto", description="Render JavaScript pages with Playwright if installed.")

    @field_validator("url")
    @classmethod
    def clean_url(cls, v: str | None) -> str | None:
        return _normalise_url(v)

    @model_validator(mode="after")
    def _check(self) -> "ScrapeRequest":
        if not self.url and not (self.html or "").strip():
            raise ValueError("give a url or some html")
        if self.overlap >= self.max_tokens:
            raise ValueError("overlap must be smaller than max_tokens")
        return self


class MapRequest(BaseModel):
    url: str = Field(..., examples=["https://www.python-httpx.org/"])
    limit: int = Field(500, ge=1, le=5000, description="Maximum number of URLs to return.")
    use_sitemap: bool = Field(True, description="Read the sitemap(s) listed in robots.txt, or /sitemap.xml.")
    whole_site: bool = Field(True, description="All paths of the site, or only under the start URL's directory.")

    @field_validator("url")
    @classmethod
    def clean_url(cls, v: str) -> str:
        url = _normalise_url(v)
        if not url:
            raise ValueError("give a url")
        return url


class CrawlRequest(BaseModel):
    url: str = Field(..., examples=["https://www.python-httpx.org/"])
    max_pages: int = Field(10, ge=1, le=500, description="Stop after this many extracted pages.")
    max_depth: int = Field(2, ge=0, le=10, description="How many links away from the start page to follow.")
    include: str | None = Field(None, description="Only crawl URLs matching this regular expression.")
    exclude: str | None = Field(None, description="Skip URLs matching this regular expression.")
    use_sitemap: bool = True
    content_links_only: bool = Field(False, description="Follow only links inside the extracted content, never "
                                                        "menu, sidebar or footer links (those come last otherwise).")
    max_tokens: int = Field(512, ge=32, le=8192)
    overlap: int = Field(0, ge=0, le=2048)

    @field_validator("url")
    @classmethod
    def clean_url(cls, v: str) -> str:
        url = _normalise_url(v)
        if not url:
            raise ValueError("give a url")
        return url

    @field_validator("include", "exclude")
    @classmethod
    def _regex(cls, v: str | None) -> str | None:
        if v:
            try:
                re.compile(v)
            except re.error as exc:
                raise ValueError(f"not a valid regular expression: {exc}") from exc
        return v or None


@dataclass
class CrawlJob:
    id: str
    req: CrawlRequest
    make_fetcher: Callable[[], Fetcher] = Fetcher
    status: str = "running"  # running | done | stopped | failed
    started: float = field(default_factory=time.time)
    finished: float | None = None
    error: str | None = None
    pages: list[dict] = field(default_factory=list)  # one summary per crawl event
    documents: dict[int, dict] = field(default_factory=dict)  # page number -> document dict
    chunks: list[dict] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def run(self) -> None:
        opts = ExtractOptions()
        try:
            with self.make_fetcher() as f:
                for ev in crawl(self.req.url, max_pages=self.req.max_pages, max_depth=self.req.max_depth,
                                fetcher=f, options=opts, use_sitemap=self.req.use_sitemap,
                                include=self.req.include, exclude=self.req.exclude,
                                content_links_only=self.req.content_links_only):
                    with self.lock:
                        n = len(self.pages)
                        row = {"n": n, "url": ev.url, "depth": ev.depth}
                        if ev.document:
                            doc = ev.document
                            chunks = [c.to_dict() for c in doc.chunks(self.req.max_tokens, self.req.overlap)]
                            row.update(status="ok", title=doc.title, words=doc.word_count, chunks=len(chunks),
                                       agreement=doc.extraction["agreement"])
                            self.documents[n] = doc.to_dict()
                            self.chunks.extend(chunks)
                        else:
                            row.update(status="error" if ev.error else "skipped", reason=ev.error or ev.skipped)
                        self.pages.append(row)
                    if self.stop.is_set():
                        self.status = "stopped"
                        break
            if self.status == "running":
                self.status = "done"
        except Exception as exc:  # noqa: BLE001 - report it to the UI instead of dying silently
            self.status, self.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            self.finished = time.time()

    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            ok = sum(1 for p in self.pages if p["status"] == "ok")
            return {
                "id": self.id,
                "url": self.req.url,
                "status": self.status,
                "error": self.error,
                "max_pages": self.req.max_pages,
                "pages_done": ok,
                "pages_skipped": len(self.pages) - ok,
                "chunks": len(self.chunks),
                "words": sum(p.get("words", 0) for p in self.pages),
                "elapsed_s": round((self.finished or time.time()) - self.started, 1),
                "pages": self.pages[since:],
            }


def create_app(fetcher: Fetcher | None = None, fetcher_factory: Callable[[], Fetcher] = Fetcher) -> FastAPI:
    """``fetcher`` serves Scrape and Map; each crawl job gets its own from ``fetcher_factory``."""
    app = FastAPI(
        title="pagequorum",
        version=__version__,
        summary="Accurate web-page extraction for RAG.",
        description="Scrape a page, crawl a site or map its URLs. Several extractors vote on every block of a "
                    "page and only what they agree on is kept, as Markdown and citation-ready chunks.",
        openapi_tags=TAGS,
    )
    app.state.fetcher = fetcher or Fetcher()
    jobs: dict[str, CrawlJob] = {}
    jobs_lock = threading.Lock()
    page = resources.files("pagequorum").joinpath("static/index.html")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return page.read_text(encoding="utf-8")  # small file; re-read so edits show without a restart

    # ---- scrape ---------------------------------------------------------------
    @app.post("/api/scrape", tags=["scrape"], summary="Scrape one page")
    def api_scrape(req: ScrapeRequest) -> dict:
        """Fetch a page (or take its HTML) and return the document, its RAG chunks and the
        keep/drop decision for every block of the page."""
        opts = ExtractOptions(links=req.links, images=req.images)
        t0 = time.perf_counter()
        if req.html and req.html.strip():
            doc, blocks = extract_explained(req.html, url=req.url, options=opts, text_chars=BLOCK_TEXT_CHARS)
        else:
            try:
                doc, blocks = scrape_explained(req.url, opts, fetcher=app.state.fetcher, render=req.render,
                                               text_chars=BLOCK_TEXT_CHARS)
            except ScrapeError as exc:
                raise HTTPException(status_code=422, detail=f"Could not use this page: {exc.reason}") from exc
        chunks = [c.to_dict() for c in doc.chunks(req.max_tokens, req.overlap)]
        return {
            "document": doc.to_dict(),
            "chunks": chunks,
            "blocks": blocks,
            "voters": list(opts.voters),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        }

    # Original name of the scrape endpoint, kept for existing clients.
    app.add_api_route("/api/extract", api_scrape, methods=["POST"], include_in_schema=False)

    # ---- map ------------------------------------------------------------------
    @app.post("/api/map", tags=["map"], summary="List a site's URLs")
    def api_map(req: MapRequest) -> dict:
        """Page URLs from the site's sitemap(s) and the links on the start page, in seconds."""
        t0 = time.perf_counter()
        urls = map_site(req.url, app.state.fetcher, limit=req.limit, use_sitemap=req.use_sitemap,
                        whole_site=req.whole_site)
        return {
            "url": req.url,
            "count": len(urls),
            "urls": [{"url": u.url, "source": u.source} for u in urls],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        }

    # ---- crawl ----------------------------------------------------------------
    def job_or_404(job_id: str) -> CrawlJob:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="no such crawl job (jobs live until the server restarts)")
        return job

    @app.post("/api/crawl", tags=["crawl"], summary="Start a crawl", status_code=202)
    def api_crawl(req: CrawlRequest) -> dict:
        """Start crawling in the background. Poll `GET /api/crawl/{id}` for progress."""
        job = CrawlJob(id=uuid.uuid4().hex[:12], req=req, make_fetcher=fetcher_factory)
        with jobs_lock:
            if sum(1 for j in jobs.values() if j.status == "running") >= 3:
                raise HTTPException(status_code=429, detail="three crawls are already running; wait or stop one")
            while len(jobs) >= MAX_JOBS:
                jobs.pop(next(iter(jobs)))
            jobs[job.id] = job
        threading.Thread(target=job.run, name=f"crawl-{job.id}", daemon=True).start()
        return {"id": job.id, "status": job.status}

    @app.get("/api/crawl/{job_id}", tags=["crawl"], summary="Crawl progress")
    def api_crawl_status(job_id: str, since: int = 0) -> dict:
        """Status, counters and the pages handled so far (from page number `since`)."""
        return job_or_404(job_id).snapshot(since=max(0, since))

    @app.delete("/api/crawl/{job_id}", tags=["crawl"], summary="Stop a crawl")
    def api_crawl_stop(job_id: str) -> dict:
        job = job_or_404(job_id)
        job.stop.set()
        return {"id": job.id, "status": "stopping" if job.status == "running" else job.status}

    @app.get("/api/crawl/{job_id}/pages/{n}", tags=["crawl"], summary="One crawled page")
    def api_crawl_page(job_id: str, n: int) -> dict:
        job = job_or_404(job_id)
        with job.lock:
            doc = job.documents.get(n)
        if not doc:
            raise HTTPException(status_code=404, detail="no extracted page with that number")
        return doc

    @app.get("/api/crawl/{job_id}/chunks.jsonl", tags=["crawl"], summary="All chunks (JSONL)",
             response_class=PlainTextResponse)
    def api_crawl_chunks(job_id: str) -> PlainTextResponse:
        job = job_or_404(job_id)
        with job.lock:
            body = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in job.chunks)
        return PlainTextResponse(body, media_type="application/x-ndjson",
                                 headers={"Content-Disposition": f'attachment; filename="crawl-{job.id}.chunks.jsonl"'})

    @app.get("/api/crawl/{job_id}/documents.jsonl", tags=["crawl"], summary="All documents (JSONL)",
             response_class=PlainTextResponse)
    def api_crawl_documents(job_id: str) -> PlainTextResponse:
        job = job_or_404(job_id)
        with job.lock:
            body = "".join(json.dumps(d, ensure_ascii=False) + "\n" for _, d in sorted(job.documents.items()))
        return PlainTextResponse(body, media_type="application/x-ndjson",
                                 headers={"Content-Disposition": f'attachment; filename="crawl-{job.id}.documents.jsonl"'})

    @app.get("/api/health", include_in_schema=False)
    def health() -> dict:
        return {"ok": True, "version": __version__}

    return app


def serve(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = False) -> None:
    import uvicorn

    url = f"http://{host}:{port}"
    print(f"pagequorum web app on {url}  (API reference: {url}/redoc, stop with Ctrl+C)")
    if open_browser:
        import webbrowser

        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
