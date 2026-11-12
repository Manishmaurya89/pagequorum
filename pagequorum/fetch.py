"""Polite, correct fetching of HTML pages.

* robots.txt is honoured per RFC 9309: a 4xx robots.txt means everything is
  allowed, a 5xx or unreachable one means nothing is (until it can be read).
  ``Crawl-delay`` is respected.
* one request per host at a time, at least ``delay`` seconds apart,
* retries on network errors, 429 and 5xx with exponential backoff, honouring
  ``Retry-After`` (capped),
* bodies over ``max_bytes`` are refused rather than silently truncated,
* non-HTML responses are reported as skipped, never fed to the extractor,
* bytes are decoded the way a browser would (see ``encoding.py``),
* an optional on-disk cache makes re-runs reproducible and free.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from protego import Protego

from . import __version__
from .encoding import decode_html

DEFAULT_USER_AGENT = f"pagequorum/{__version__} (python; web-page extraction for RAG)"
HTML_TYPES = ("text/html", "application/xhtml+xml")
RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str
    final_url: str | None = None
    status: int | None = None
    content_type: str | None = None
    html: str | None = None
    encoding: str | None = None
    encoding_source: str | None = None
    fetched_at: float | None = None
    from_cache: bool = False
    error: str | None = None  # network/HTTP failure
    skipped: str | None = None  # deliberately not fetched/used (robots, content type, size)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.html is not None and not self.error and not self.skipped

    def info(self) -> dict:
        d = asdict(self)
        d.pop("html")
        d.pop("headers")
        return d


class RobotsCache:
    def __init__(self, client: httpx.Client, user_agent: str):
        self._client = client
        self._ua = user_agent
        self._rules: dict[str, Protego | bool] = {}
        self._why: dict[str, str] = {}  # origin -> why everything is disallowed

    def _origin(self, url: str) -> str:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}"

    def rules(self, url: str) -> Protego | bool:
        """Parsed robots.txt, True (allow all) or False (disallow all)."""
        origin = self._origin(url)
        if origin not in self._rules:
            try:
                r = self._client.get(origin + "/robots.txt", follow_redirects=True, timeout=15)
                if r.status_code >= 500:
                    self._rules[origin] = False
                    self._why[origin] = (f"the site's robots.txt returned HTTP {r.status_code}; "
                                         "pages are not fetched until it can be read (RFC 9309)")
                elif r.status_code >= 400:
                    self._rules[origin] = True
                else:
                    self._rules[origin] = Protego.parse(r.text)
            except httpx.HTTPError as exc:
                self._rules[origin] = False
                self._why[origin] = f"could not reach the site ({type(exc).__name__}: {exc})"[:300]
        return self._rules[origin]

    def why_not(self, url: str) -> str:
        """Why ``url`` may not be fetched (call after :meth:`allowed` returned False)."""
        return self._why.get(self._origin(url), "disallowed by robots.txt")

    def allowed(self, url: str) -> bool:
        rules = self.rules(url)
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(url, self._ua)

    def crawl_delay(self, url: str) -> float | None:
        rules = self.rules(url)
        if isinstance(rules, bool):
            return None
        delay = rules.crawl_delay(self._ua)
        return float(delay) if delay is not None else None

    def sitemaps(self, url: str) -> list[str]:
        rules = self.rules(url)
        return list(rules.sitemaps) if not isinstance(rules, bool) else []


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
            return max(0.0, when.timestamp() - time.time())
        except (TypeError, ValueError):
            return None


class Fetcher:
    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        respect_robots: bool = True,
        delay: float = 1.0,
        timeout: float = 20.0,
        retries: int = 2,
        max_bytes: int = 15_000_000,
        cache_dir: str | Path | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.user_agent = user_agent
        self.respect_robots = respect_robots
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.max_bytes = max_bytes
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en;q=0.9,*;q=0.5",
            },
            follow_redirects=True,
            max_redirects=10,
            timeout=timeout,
            transport=transport,
        )
        self.robots = RobotsCache(self.client, user_agent)
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- cache -------------------------------------------------------------
    def _cache_paths(self, url: str) -> tuple[Path, Path] | None:
        if not self.cache_dir:
            return None
        key = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / f"{key}.json", self.cache_dir / f"{key}.body"

    def _cache_get(self, url: str) -> FetchResult | None:
        paths = self._cache_paths(url)
        if not paths or not paths[0].exists() or not paths[1].exists():
            return None
        meta = json.loads(paths[0].read_text(encoding="utf-8"))
        body = paths[1].read_bytes()
        html, enc, src = decode_html(body, meta.get("content_type"))
        return FetchResult(url=url, final_url=meta.get("final_url"), status=meta.get("status"),
                           content_type=meta.get("content_type"), html=html, encoding=enc,
                           encoding_source=src, fetched_at=meta.get("fetched_at"), from_cache=True)

    def _cache_put(self, url: str, result: FetchResult, body: bytes) -> None:
        paths = self._cache_paths(url)
        if not paths:
            return
        paths[0].parent.mkdir(parents=True, exist_ok=True)
        meta = {k: getattr(result, k) for k in ("final_url", "status", "content_type", "fetched_at")}
        paths[1].write_bytes(body)
        paths[0].write_text(json.dumps(meta), encoding="utf-8")

    # ---- politeness ----------------------------------------------------------
    def _wait_turn(self, url: str) -> None:
        host = urlsplit(url).netloc
        delay = self.delay
        if self.respect_robots:
            robots_delay = self.robots.crawl_delay(url)
            if robots_delay:
                delay = max(delay, min(robots_delay, 60.0))
        with self._lock:
            wait = self._last.get(host, 0.0) + delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    # ---- fetching ------------------------------------------------------------
    def fetch(self, url: str) -> FetchResult:
        if urlsplit(url).scheme not in ("http", "https"):
            return FetchResult(url=url, skipped="only http(s) URLs can be fetched")
        cached = self._cache_get(url)
        if cached:
            return cached
        if self.respect_robots and not self.robots.allowed(url):
            return FetchResult(url=url, skipped=self.robots.why_not(url))

        last_error = None
        for attempt in range(self.retries + 1):
            self._wait_turn(url)
            try:
                result, body = self._get(url)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    time.sleep(min(30.0, 2 ** attempt + random.random()))
                continue
            if result.status in RETRY_STATUS and attempt < self.retries:
                pause = _retry_after(result.headers.get("retry-after"))
                time.sleep(min(30.0, pause if pause is not None else 2 ** attempt + random.random()))
                continue
            if result.ok and body is not None:
                self._cache_put(url, result, body)
            return result
        return FetchResult(url=url, error=last_error or "request failed")

    def _get(self, url: str) -> tuple[FetchResult, bytes | None]:
        with self.client.stream("GET", url) as r:
            ctype = r.headers.get("content-type")
            res = FetchResult(url=url, final_url=str(r.url), status=r.status_code, content_type=ctype,
                              fetched_at=time.time(), headers=dict(r.headers))
            if r.status_code >= 400:
                res.error = f"HTTP {r.status_code}"
                return res, None
            declared = int(r.headers.get("content-length") or 0)
            if declared > self.max_bytes:
                res.skipped = f"response larger than {self.max_bytes} bytes"
                return res, None
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > self.max_bytes:
                    res.skipped = f"response larger than {self.max_bytes} bytes"
                    return res, None
                chunks.append(chunk)
        body = b"".join(chunks)
        mime = (ctype or "").split(";")[0].strip().lower()
        if mime and mime not in HTML_TYPES:
            res.skipped = f"not HTML (content-type {mime})"
            return res, None
        if not mime and b"<html" not in body[:4096].lower() and b"<!doctype html" not in body[:4096].lower():
            res.skipped = "no content-type and the body does not look like HTML"
            return res, None
        res.html, res.encoding, res.encoding_source = decode_html(body, ctype)
        return res, body
