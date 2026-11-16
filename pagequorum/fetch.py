"""Polite, correct fetching of HTML pages.

* robots.txt is honoured per RFC 9309: a 4xx robots.txt means everything is
  allowed, a 5xx or unreachable one means nothing is (until it can be read).
  ``Crawl-delay`` is respected.
* one request per host at a time, at least ``delay`` seconds apart,
* retries on network errors, 429 and 5xx with exponential backoff, honouring
  ``Retry-After`` (capped),
* bodies over ``max_bytes`` are refused rather than silently truncated,
  and robots.txt is read up to 500 KiB (the minimum RFC 9309 requires);
  compressed bodies are inflated a bounded step at a time, so a small
  "zip bomb" is stopped at the limit instead of filling memory,
* non-HTML responses are reported as skipped, never fed to the extractor,
* bytes are decoded the way a browser would (see ``encoding.py``),
* an optional on-disk cache makes re-runs reproducible and free.
"""

from __future__ import annotations

import email.utils
import hashlib
import ipaddress
import json
import random
import socket
import threading
import time
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpcore
import httpx
from protego import Protego

from . import __version__
from .encoding import decode_html

DEFAULT_USER_AGENT = f"pagequorum/{__version__} (python; web-page extraction for RAG)"
HTML_TYPES = ("text/html", "application/xhtml+xml")
RETRY_STATUS = {429, 500, 502, 503, 504}
ROBOTS_MAX_BYTES = 500 * 1024  # RFC 9309 2.5: parse at least 500 KiB; anything after it is ignored


# Only encodings decoded below are asked for: httpx would also offer brotli (when it is
# installed) and inflate every network chunk in one unbounded step.
ACCEPT_ENCODING = "gzip, deflate"
_GZIP_WBITS = 16 + zlib.MAX_WBITS


class BlockedAddress(httpx.RequestError):
    """A request to a private-network address, refused because ``allow_private=False``."""


def private_address(url: str) -> str | None:
    """Why ``url`` points into a private network (loopback, 10/8, 192.168/16, link-local
    such as cloud metadata at 169.254.169.254, ...), or None. Host names are resolved;
    one that does not resolve is let through, since its request fails anyway."""
    host = urlsplit(url).hostname
    if not host:
        return None
    try:
        addresses = _resolve(host)
    except (OSError, UnicodeError, ValueError):
        return None
    return _private_reason(host, addresses)


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """The host's addresses, in the resolver's order (an IP literal is its own address)."""
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    found = []
    for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP):
        address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if address not in found:
            found.append(address)
    return found


def _private_reason(host: str, addresses) -> str | None:
    for address in addresses:
        plain = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address
        if not plain.is_global or plain.is_multicast:
            return f"{host} is a private-network address" + ("" if str(plain) == host else f" ({plain})")
    return None


class _PublicOnlyBackend(httpcore.NetworkBackend):
    """Opens every TCP connection the way httpcore would, except that the host is resolved
    once, refused if any of its addresses is private, and the socket connects to one of the
    addresses that was checked. Checking a name and then letting the connection resolve it
    again would let a DNS answer change in between (DNS rebinding). TLS still verifies the
    certificate against the host name, which httpcore passes separately."""

    def __init__(self, inner: httpcore.NetworkBackend | None = None):
        self.inner = inner or httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            addresses = _resolve(host)
        except (OSError, UnicodeError, ValueError) as exc:
            raise httpcore.ConnectError(f"could not resolve {host}: {exc}") from exc
        if not addresses:
            raise httpcore.ConnectError(f"could not resolve {host}")
        if why := _private_reason(host, addresses):
            raise BlockedAddress(why)
        error: Exception | None = None
        for address in addresses:
            try:
                return self.inner.connect_tcp(str(address), port, timeout=timeout,
                                              local_address=local_address, socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc
        raise error

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise BlockedAddress("unix sockets are not allowed")

    def sleep(self, seconds: float) -> None:
        self.inner.sleep(seconds)


def _public_only_transport() -> httpx.HTTPTransport:
    """httpx's default transport with every connection going through _PublicOnlyBackend.
    httpx has no public option for the backend, so it is set on the connection pool; the
    assertion keeps a future httpx/httpcore from silently dropping the guard."""
    transport = httpx.HTTPTransport()
    pool = transport._pool
    assert isinstance(pool, httpcore.ConnectionPool) and hasattr(pool, "_network_backend")
    pool._network_backend = _PublicOnlyBackend(pool._network_backend)
    return transport


def _deflate_wbits(head: bytes) -> int:
    """"deflate" should be zlib-wrapped (RFC 9110), but some servers send raw deflate."""
    if len(head) >= 2 and head[0] & 0x0F == 8 and ((head[0] << 8) | head[1]) % 31 == 0:
        return zlib.MAX_WBITS
    return -zlib.MAX_WBITS


def read_body(r: httpx.Response, cap: int) -> tuple[bytes, bool]:
    """Read a streamed response's body, decoded, but never more than ``cap`` bytes of it.

    Returns the body and whether there was more than ``cap``. Content-Encoding is undone
    here rather than by httpx, a bounded step at a time, and the raw size is capped too,
    so neither a huge body nor a tiny one that inflates to gigabytes can exhaust memory.
    Raises ``httpx.DecodingError`` for an encoding it does not handle or corrupt data.
    """
    if r.is_stream_consumed:  # an in-memory response (a test transport): httpx already read it
        body = r.content
        return body[:cap], len(body) > cap
    codings = [c.strip().lower() for c in r.headers.get("content-encoding", "").split(",")]
    codings = [c for c in codings if c and c != "identity"]
    if len(codings) > 1 or (codings and codings[0] not in ("gzip", "x-gzip", "deflate")):
        raise httpx.DecodingError(f"unsupported content-encoding {', '.join(codings)!r}", request=r.request)
    coding = codings[0] if codings else None
    inflater = zlib.decompressobj(_GZIP_WBITS) if coding in ("gzip", "x-gzip") else None
    out = bytearray()
    raw_size = 0
    try:
        for raw in r.iter_raw():
            raw_size += len(raw)
            if raw_size > cap:  # compressed data bigger than the cap: the body is too
                return bytes(out[:cap]), True
            if coding is None:
                out += raw
            else:
                if inflater is None:  # deflate: pick the variant from the first bytes
                    inflater = zlib.decompressobj(_deflate_wbits(raw))
                data = raw
                while data and not inflater.eof:
                    out += inflater.decompress(data, cap + 1 - len(out))  # never 0 = unbounded
                    if len(out) > cap:
                        return bytes(out[:cap]), True
                    data = inflater.unconsumed_tail
            if len(out) > cap:
                return bytes(out[:cap]), True
        if inflater is not None:
            out += inflater.flush()
    except zlib.error as exc:
        raise httpx.DecodingError(f"corrupt {coding} data: {exc}", request=r.request) from exc
    if len(out) > cap:
        return bytes(out[:cap]), True
    return bytes(out), False


def read_capped(client: httpx.Client, url: str, cap: int, **kwargs) -> tuple[httpx.Response, bytes, bool]:
    """GET ``url`` and read at most ``cap`` bytes of its decoded body (see :func:`read_body`).

    Returns the response, the body read and whether it was cut off at ``cap``.
    """
    with client.stream("GET", url, **kwargs) as r:
        body, cut = read_body(r, cap)
    return r, body, cut


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
                r, body, _ = read_capped(self._client, origin + "/robots.txt", ROBOTS_MAX_BYTES,
                                         follow_redirects=True, timeout=15)
                if r.status_code >= 500:
                    self._rules[origin] = False
                    self._why[origin] = (f"the site's robots.txt returned HTTP {r.status_code}; "
                                         "pages are not fetched until it can be read (RFC 9309)")
                elif r.status_code >= 400:
                    self._rules[origin] = True
                else:
                    self._rules[origin] = Protego.parse(body.decode("utf-8", errors="replace"))  # RFC 9309: UTF-8
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


def _refuse_private(request: httpx.Request) -> None:
    if why := private_address(str(request.url)):
        raise BlockedAddress(why, request=request)


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
        allow_private: bool = True,
    ):
        """``allow_private=False`` refuses private-network addresses, including redirects
        into one: use it whenever the URLs come from someone else (a model, a web form)."""
        self.user_agent = user_agent
        self.respect_robots = respect_robots
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.max_bytes = max_bytes
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.allow_private = allow_private
        self.client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en;q=0.9,*;q=0.5",
                "Accept-Encoding": ACCEPT_ENCODING,
            },
            follow_redirects=True,
            max_redirects=10,
            timeout=timeout,
            # private addresses: the request hook gives a clear early refusal (also for every
            # redirect); the transport checks the address each connection really goes to
            transport=transport or (None if allow_private else _public_only_transport()),
            event_hooks={} if allow_private else {"request": [_refuse_private]},
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
        if not self.allow_private and (why := private_address(url)):
            return FetchResult(url=url, skipped=f"refused: {why}")
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
            except BlockedAddress as exc:  # a redirect into a private network: never retried
                return FetchResult(url=url, skipped=f"refused: {exc}")
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
            try:
                body, too_big = read_body(r, self.max_bytes)
            except httpx.DecodingError as exc:  # retrying would not help
                res.error = f"could not decode the response ({exc})"
                return res, None
            if too_big:
                res.skipped = f"response larger than {self.max_bytes} bytes"
                return res, None
        mime = (ctype or "").split(";")[0].strip().lower()
        if mime and mime not in HTML_TYPES:
            res.skipped = f"not HTML (content-type {mime})"
            return res, None
        if not mime and b"<html" not in body[:4096].lower() and b"<!doctype html" not in body[:4096].lower():
            res.skipped = "no content-type and the body does not look like HTML"
            return res, None
        res.html, res.encoding, res.encoding_source = decode_html(body, ctype)
        return res, body
