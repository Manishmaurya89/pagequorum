"""URL normalisation and crawl scope."""

from __future__ import annotations

import ipaddress
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


def _local_host(host: str) -> bool:
    """IP addresses and names that point inside a network, never somewhere a public page should send a crawl."""
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
        return True
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True  # any IP literal: a redirect to one is not "the same site moved"
    except ValueError:
        return False


def related_site(start_url: str, final_url: str) -> bool:
    """Whether a redirected start page stayed on the same site in the broad sense: the same
    host, a subdomain or parent of it (docs.pydantic.dev -> pydantic.dev), or a sibling
    under the same parent domain. Never an IP address or a local name, so a page cannot
    redirect a crawl into a private network."""
    a, b = site_key(start_url), site_key(final_url)
    if a == b:
        return True
    if not a or not b or _local_host(b):
        return False
    if a.endswith("." + b) or b.endswith("." + a):
        return True
    pa, pb = a.split(".", 1), b.split(".", 1)
    return len(pa) == 2 == len(pb) and pa[1] == pb[1] and "." in pa[1]


def rebase_scope(final_url: str, prefix: str) -> str:
    """The scope prefix after the start page redirected to ``final_url``.

    * Same section, new host or scheme (http -> https, example.com -> www...): kept.
    * A moved section keeps its folder name: /latest/concepts/ that now lands on
      /docs/validation/latest/concepts/models/ becomes /docs/validation/latest/concepts/.
    * Otherwise: the folder the start page landed in.
    """
    path = urlsplit(final_url).path or "/"
    if path.startswith(prefix):
        return prefix
    name = prefix.rstrip("/").rsplit("/", 1)[-1]
    if name:
        i = path.rfind(f"/{name}/")
        if i >= 0:
            return path[: i + len(name) + 2]
    return path.rstrip("/").rsplit("/", 1)[0] + "/"


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
