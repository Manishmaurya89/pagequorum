"""URL normalisation and crawl scope."""

from __future__ import annotations

import posixpath
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that only track the visitor and never change the content.
TRACKING_PARAMS = {
    "gclid", "fbclid", "msclkid", "dclid", "yclid", "igshid", "mc_cid", "mc_eid",
    "_ga", "_gl", "ref_src", "spm", "mkt_tok",
}

NON_HTML_EXT = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tiff",
    ".mp3", ".mp4", ".m4a", ".wav", ".avi", ".mov", ".webm", ".zip", ".gz", ".tgz", ".bz2",
    ".7z", ".rar", ".tar", ".exe", ".dmg", ".pkg", ".msi", ".apk", ".css", ".js", ".json",
    ".xml", ".rss", ".atom", ".csv", ".xls", ".xlsx", ".doc", ".docx", ".ppt", ".pptx",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".txt",
}


def normalize_url(url: str) -> str:
    """Canonical form for de-duplication: lower-case scheme/host, no default port,
    no fragment, no tracking parameters, sorted query."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    if parts.username:
        netloc = f"{parts.username}@{netloc}"
    path = parts.path or "/"
    if "/./" in path or "/../" in path or path.endswith(("/.", "/..")):
        trailing = path.endswith("/")
        path = posixpath.normpath(path) + ("/" if trailing else "")
        path = path.replace("//", "/")
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query), doseq=True), ""))


def site_key(url: str) -> str:
    """Host without a leading ``www.``, so www.example.com and example.com match."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def scope_prefix(start_url: str) -> str:
    """The directory of the start URL: crawling /docs/intro.html stays under /docs/."""
    path = urlsplit(start_url).path or "/"
    return path if path.endswith("/") else path.rsplit("/", 1)[0] + "/"


def in_scope(url: str, start_url: str, prefix: str | None = None) -> bool:
    p = urlsplit(url)
    if p.scheme not in ("http", "https"):
        return False
    if site_key(url) != site_key(start_url):
        return False
    return (p.path or "/").startswith(prefix if prefix is not None else scope_prefix(start_url))


def looks_like_page(url: str) -> bool:
    path = urlsplit(url).path.lower()
    ext = posixpath.splitext(path)[1]
    return ext not in NON_HTML_EXT
