"""A local web app and HTTP API: Scrape one page, Crawl a site (as a background job
with live progress) or Map a site's URLs, and see the clean text, the RAG chunks and
every keep/drop decision.

    uv run --extra web pagequorum serve        # then open http://127.0.0.1:8000

It binds to 127.0.0.1 by default. It fetches whatever URL it is given, so do not
expose it on a public network. Requests must name this machine in their Host header
(localhost, 127.0.0.1 or [::1]), which stops a web page you visit from reaching the
API through DNS rebinding.

    pagequorum serve --public                  # a hosted demo (see render.yaml)

Public mode is for the open internet: private-network addresses are refused (also
through redirects and DNS rebinding), every visitor and the server as a whole are rate
limited, only a few requests run at once, pages and sitemaps are size-capped, crawls are
small and user-supplied regular expressions are not accepted. ``PublicLimits`` holds
the numbers.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from importlib import resources
from typing import Callable, Iterable, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
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
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
_ANY_INTERFACE = ("0.0.0.0", "::", "")



@dataclass(frozen=True)
class PublicLimits:
    """What one public server allows. Per-visitor limits are a sliding window per IP
    address; the global ones protect the server (and the sites it reads) from everyone
    at once."""

    scrapes_per_minute: int = 10  # per visitor, pasted HTML included
    maps_per_minute: int = 4  # per visitor
    crawls_per_hour: int = 6  # per visitor
    requests_per_minute: int = 60  # scrapes + maps + crawls, all visitors together
    concurrent: int = 2  # scrapes or maps in progress at once; more get "busy"
    crawls_running: int = 2  # all visitors together; one per visitor
    crawl_pages: int = 10
    crawl_depth: int = 2
    map_urls: int = 500
    page_bytes: int = 5_000_000  # fetched pages and sitemaps
    html_chars: int = 2_000_000  # pasted HTML
    body_bytes: int = 8_000_000  # a request body, read before anything else is checked
    timeout_s: float = 15.0


class RateLimiter:
    """At most ``limit`` events per ``window`` seconds for each key (a sliding window)."""

    MAX_KEYS = 10_000

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self._clock = limit, window, clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self._hits.get(key, deque())  # a check alone never stores a key
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def wait(self, key: str) -> float:
        """Seconds until ``key`` may act again; 0 if it may now."""
        with self._lock:
            now = self._clock()
            q = self._recent(key, now)
            return 0.0 if len(q) < self.limit else q[0] + self.window - now

    def add(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            q = self._recent(key, now)
            q.append(now)
            self._hits[key] = q
            if len(self._hits) > self.MAX_KEYS:  # forget visitors with nothing in the window
                for k in [k for k, q in self._hits.items() if not q or q[-1] <= now - self.window]:
                    del self._hits[k]


class BodyLimit:
    """ASGI middleware: refuse request bodies over ``max_bytes`` (413) before they are read
    into memory, whether or not they declare a Content-Length."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            return await self._too_large(send)
        # read the body here (at most max_bytes of it), then hand it on in one piece
        chunks, seen = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":  # the client went away
                return
            chunks.append(message.get("body", b""))
            seen += len(chunks[-1])
            if seen > self.max_bytes:
                return await self._too_large(send)
            if not message.get("more_body"):
                break
        replay = {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        async def once():
            nonlocal replay
            if replay is not None:
                message, replay = replay, None
                return message
            return await receive()

        await self.app(scope, once, send)

    async def _too_large(self, send):
        body = json.dumps({"detail": f"Request body over {self.max_bytes:,} bytes."}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def public_fetcher(limits: PublicLimits) -> Fetcher:
    """A fetcher for URLs from strangers: no private addresses, smaller caps, quicker timeout."""
    return Fetcher(allow_private=False, max_bytes=limits.page_bytes, max_sitemap_bytes=limits.page_bytes,
                   timeout=limits.timeout_s, retries=1)


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
    visitor: str = ""

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


def create_app(fetcher: Fetcher | None = None, fetcher_factory: Callable[[], Fetcher] | None = None,
               allowed_hosts: Iterable[str] = LOCAL_HOSTS, public: PublicLimits | None = None,
               cors_origins: Iterable[str] = (), client_ip_header: str | None = None) -> FastAPI:
    """``fetcher`` serves Scrape and Map; each crawl job gets its own from ``fetcher_factory``.

    Only requests whose Host header is one of ``allowed_hosts`` are answered (``"*"``
    turns the check off). ``cors_origins`` may call the API from their web pages.

    ``public`` turns on public mode with these limits: every request gets a fresh fetcher
    from ``fetcher_factory`` (by default :func:`public_fetcher`), so nothing a visitor
    fetched is shared or kept. Visitors are told apart by their address, taken from the
    ``client_ip_header`` the hosting proxy sets when given, else from the connection.
    """
    if fetcher_factory is None:
        fetcher_factory = (lambda: public_fetcher(public)) if public else Fetcher
    app = FastAPI(
        title="pagequorum",
        version=__version__,
        summary="Accurate web-page extraction for RAG.",
        description="Scrape a page, crawl a site or map its URLs. Several extractors vote on every block of a "
                    "page and only what they agree on is kept, as Markdown and citation-ready chunks.",
        openapi_tags=TAGS,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    if public:
        app.add_middleware(BodyLimit, max_bytes=public.body_bytes)
    if cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(cors_origins), allow_methods=["GET", "POST", "DELETE"],
                           allow_headers=["Content-Type"], max_age=3600)
    app.state.fetcher = None if public else (fetcher or Fetcher())
    jobs: dict[str, CrawlJob] = {}
    jobs_lock = threading.Lock()

    # ---- public mode ------------------------------------------------------------
    if public:
        limiters = {
            "scrape": RateLimiter(public.scrapes_per_minute, 60),
            "map": RateLimiter(public.maps_per_minute, 60),
            "crawl": RateLimiter(public.crawls_per_hour, 3600),
        }
        everyone = RateLimiter(public.requests_per_minute, 60)
        busy = threading.BoundedSemaphore(public.concurrent)

    def visitor(request: Request) -> str:
        if client_ip_header:
            try:  # only a well-formed address counts; anything else falls back to the connection
                return str(ipaddress.ip_address(request.headers.get(client_ip_header, "").strip()))
            except ValueError:
                pass
        return request.client.host if request.client else "?"

    def admit(request: Request, kind: str) -> str:
        """Rate limits for public mode; returns the visitor's key."""
        who = visitor(request)
        if not public:
            return who
        mine, all_ = limiters[kind].wait(who), everyone.wait("*")
        if mine or all_:
            wait = max(mine, all_)
            msg = (f"Too many {kind} requests from you; try again in {math.ceil(wait)} s." if mine else
                   f"The public server is busy; try again in {math.ceil(wait)} s, or run pagequorum yourself.")
            raise HTTPException(status_code=429, detail=msg, headers={"Retry-After": str(math.ceil(wait))})
        limiters[kind].add(who)
        everyone.add("*")
        return who

    class _Slot:
        """At most ``public.concurrent`` fetches at once in public mode; a fresh fetcher each."""

        def __enter__(self) -> Fetcher:
            if not public:
                return app.state.fetcher
            if not busy.acquire(blocking=False):
                raise HTTPException(status_code=503, detail="The public server is busy with other pages; "
                                                            "try again in a few seconds.", headers={"Retry-After": "5"})
            try:
                self.f = fetcher_factory()
            except BaseException:
                busy.release()
                raise
            return self.f

        def __exit__(self, *exc) -> None:
            if public:
                try:
                    self.f.close()
                finally:
                    busy.release()
    page = resources.files("pagequorum").joinpath("static/index.html")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return page.read_text(encoding="utf-8")  # small file; re-read so edits show without a restart

    # ---- scrape ---------------------------------------------------------------
    @app.post("/api/scrape", tags=["scrape"], summary="Scrape one page")
    def api_scrape(req: ScrapeRequest, request: Request) -> dict:
        """Fetch a page (or take its HTML) and return the document, its RAG chunks and the
        keep/drop decision for every block of the page."""
        if public and req.html and len(req.html) > public.html_chars:
            raise HTTPException(status_code=413, detail=f"The public server takes at most {public.html_chars:,} "
                                                        "characters of HTML.")
        admit(request, "scrape")
        opts = ExtractOptions(links=req.links, images=req.images)
        t0 = time.perf_counter()
        if req.html and req.html.strip():
            doc, blocks = extract_explained(req.html, url=req.url, options=opts, text_chars=BLOCK_TEXT_CHARS)
        else:
            try:
                with _Slot() as f:
                    doc, blocks = scrape_explained(req.url, opts, fetcher=f, render="never" if public else req.render,
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
    def api_map(req: MapRequest, request: Request) -> dict:
        """Page URLs from the site's sitemap(s) and the links on the start page, in seconds."""
        if public and req.limit > public.map_urls:
            raise HTTPException(status_code=422, detail=f"The public server lists at most {public.map_urls} URLs.")
        admit(request, "map")
        t0 = time.perf_counter()
        with _Slot() as f:
            urls = map_site(req.url, f, limit=req.limit, use_sitemap=req.use_sitemap, whole_site=req.whole_site)
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
    def api_crawl(req: CrawlRequest, request: Request) -> dict:
        """Start crawling in the background. Poll `GET /api/crawl/{id}` for progress."""
        if public:
            if req.max_pages > public.crawl_pages or req.max_depth > public.crawl_depth:
                raise HTTPException(status_code=422, detail=f"The public server crawls at most {public.crawl_pages} "
                                                            f"pages, {public.crawl_depth} links deep.")
            if req.include or req.exclude:
                raise HTTPException(status_code=422, detail="The public server does not take include/exclude "
                                                            "patterns; run pagequorum yourself to use them.")
        who = visitor(request)
        job = CrawlJob(id=uuid.uuid4().hex, req=req, make_fetcher=fetcher_factory, visitor=who)
        with jobs_lock:
            running = [j for j in jobs.values() if j.status == "running"]
            if public and any(j.visitor == who for j in running):
                raise HTTPException(status_code=429, detail="You already have a crawl running; wait for it or stop it.")
            limit = public.crawls_running if public else 3
            if len(running) >= limit:
                raise HTTPException(status_code=429, detail=f"{limit} crawls are already running; try again soon",
                                    headers={"Retry-After": "30"})
            admit(request, "crawl")
            for old in [k for k, j in jobs.items() if j.status != "running"][:max(0, len(jobs) - MAX_JOBS + 1)]:
                del jobs[old]  # forget the oldest finished crawls; running ones are never dropped
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
        info: dict = {"ok": True, "version": __version__}
        if public:
            info["public"] = asdict(public)
        return info

    return app


def _env_list(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def serve_public(host: str = "0.0.0.0", port: int | None = None) -> None:
    """Public mode for a hosted demo. Read from the environment:

    * ``PORT``: the port to listen on (set by hosts such as Render), default 8000;
    * ``PAGEQUORUM_CORS_ORIGINS``: comma-separated web pages that may call the API;
    * ``PAGEQUORUM_ALLOWED_HOSTS``: host names to answer to, plus ``RENDER_EXTERNAL_HOSTNAME``
      when Render sets it (anything otherwise);
    * ``PAGEQUORUM_CLIENT_IP_HEADER``: the header in which the host's proxy puts the
      visitor's address (on Render: True-Client-IP); otherwise the connection's address.
    """
    import uvicorn

    port = port or int(os.environ.get("PORT", "8000"))
    hosts = _env_list("PAGEQUORUM_ALLOWED_HOSTS") + _env_list("RENDER_EXTERNAL_HOSTNAME")
    app = create_app(allowed_hosts=[*hosts, *LOCAL_HOSTS] if hosts else ["*"], public=PublicLimits(),
                     cors_origins=_env_list("PAGEQUORUM_CORS_ORIGINS"),
                     client_ip_header=os.environ.get("PAGEQUORUM_CLIENT_IP_HEADER") or None)
    print(f"pagequorum public server on {host}:{port}")
    # proxy_headers off: X-Forwarded-For can be set by anyone, so it never names the visitor
    uvicorn.run(app, host=host, port=port, log_level="warning", proxy_headers=False)


def serve(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = False) -> None:
    import uvicorn

    url = f"http://{host}:{port}"
    print(f"pagequorum web app on {url}  (API reference: {url}/redoc, stop with Ctrl+C)")
    if open_browser:
        import webbrowser

        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    # Bound to every interface, the server can be reached under names we cannot know,
    # so the Host check is off; the CLI warns about exposing it.
    hosts = ["*"] if host in _ANY_INTERFACE else [*LOCAL_HOSTS, f"[{host}]" if ":" in host else host.lower()]
    uvicorn.run(create_app(allowed_hosts=hosts), host=host, port=port, log_level="warning")
