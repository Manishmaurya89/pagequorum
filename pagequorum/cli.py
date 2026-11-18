"""Command line: pagequorum scrape | crawl | extract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .crawl import crawl
from .extract import ExtractOptions, explain, extract
from .fetch import Fetcher
from .models import Document
from .scrape import ScrapeError, scrape


def _slug(url: str) -> str:
    p = urlsplit(url)
    base = re.sub(r"[^A-Za-z0-9]+", "-", f"{p.netloc}{p.path}").strip("-")[:80] or "page"
    return f"{base}-{hashlib.sha256(url.encode()).hexdigest()[:8]}"


def _options(args) -> ExtractOptions:
    return ExtractOptions(links=args.links, images=args.images)


def _emit(doc: Document, args) -> None:
    if args.format == "markdown":
        sys.stdout.write(doc.markdown)
    elif args.format == "text":
        sys.stdout.write(doc.text + "\n")
    elif args.format == "json":
        d = doc.to_dict()
        d["chunks"] = [c.to_dict() for c in doc.chunks(args.max_tokens, args.overlap)]
        json.dump(d, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    elif args.format == "chunks":
        for c in doc.chunks(args.max_tokens, args.overlap):
            sys.stdout.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
    for w in doc.extraction.get("warnings", []):
        print(f"warning: {w}", file=sys.stderr)


def _fetcher(args) -> Fetcher:
    if args.no_robots:
        print("warning: robots.txt is being ignored (--no-robots)", file=sys.stderr)
    kwargs = {"respect_robots": not args.no_robots, "cache_dir": args.cache}
    if getattr(args, "delay", None) is not None:
        kwargs["delay"] = args.delay
    if args.user_agent:
        kwargs["user_agent"] = args.user_agent
    return Fetcher(**kwargs)


def cmd_scrape(args) -> int:
    with _fetcher(args) as f:
        status = 0
        for url in args.urls:
            try:
                doc = scrape(url, _options(args), fetcher=f, render=args.render)
            except ScrapeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                status = 1
                continue
            _emit(doc, args)
    return status


def cmd_extract(args) -> int:
    data = Path(args.file).read_bytes()
    if args.explain:
        for row in explain(data, url=args.url, options=_options(args)):
            mark = "KEEP" if row["kept"] else "drop"
            votes = " ".join(f"{k}={v:.2f}" for k, v in row["votes"].items())
            print(f"{mark} {row['tag']:<10} {row['reason']:<22} {votes}  | {row['text'][:80]}")
        return 0
    _emit(extract(data, url=args.url, options=_options(args)), args)
    return 0


def cmd_crawl(args) -> int:
    out = Path(args.out)
    (out / "pages").mkdir(parents=True, exist_ok=True)
    n_docs = n_chunks = 0
    with _fetcher(args) as f, \
            (out / "documents.jsonl").open("w", encoding="utf-8") as docs_f, \
            (out / "chunks.jsonl").open("w", encoding="utf-8") as chunks_f, \
            (out / "crawl_log.jsonl").open("w", encoding="utf-8") as log_f:
        for ev in crawl(args.url, max_pages=args.max_pages, max_depth=args.max_depth, fetcher=f,
                        options=_options(args), use_sitemap=not args.no_sitemap,
                        include=args.include, exclude=args.exclude,
                        content_links_only=args.content_links_only):
            entry = {"url": ev.url, "depth": ev.depth, "skipped": ev.skipped, "error": ev.error}
            if ev.document:
                doc = ev.document
                slug = _slug(doc.url or ev.url)
                (out / "pages" / f"{slug}.md").write_text(doc.markdown, encoding="utf-8")
                docs_f.write(json.dumps(doc.to_dict(), ensure_ascii=False) + "\n")
                chunks = doc.chunks(args.max_tokens, args.overlap)
                for c in chunks:
                    chunks_f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
                n_docs += 1
                n_chunks += len(chunks)
                entry.update(file=f"pages/{slug}.md", words=doc.word_count, chunks=len(chunks),
                             method=doc.extraction["method"], agreement=doc.extraction["agreement"])
                print(f"[{n_docs}] {ev.url}  ({doc.word_count} words, {len(chunks)} chunks)", file=sys.stderr)
            else:
                print(f"skip {ev.url}: {ev.skipped or ev.error}", file=sys.stderr)
            log_f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"done: {n_docs} pages, {n_chunks} chunks -> {out}", file=sys.stderr)
    return 0 if n_docs else 1


def cmd_serve(args) -> int:
    try:
        from .web import serve, serve_public
    except ImportError:
        print("The web app needs extra packages. Run it with:\n"
              "  uv run --extra web pagequorum serve\n"
              "or install them with:  pip install 'pagequorum[web]'", file=sys.stderr)
        return 2
    if args.public:
        serve_public(host=args.host or "0.0.0.0", port=args.port)
        return 0
    host = args.host or "127.0.0.1"
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"warning: listening on {host}; anyone who can reach it can make this machine "
              "fetch any URL (use --public for a hardened server)", file=sys.stderr)
    serve(host=host, port=args.port or 8000, open_browser=args.open)
    return 0


def cmd_mcp(args) -> int:
    try:
        from .mcp_server import main as run_mcp
    except ImportError:
        print("The MCP server needs the 'mcp' extra:  pip install 'pagequorum[mcp]'\n"
              "or run it with:  uv run --extra mcp pagequorum mcp", file=sys.stderr)
        return 2
    run_mcp()  # stdout carries the MCP protocol from here on: nothing else may print to it
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pagequorum", description="Accurate web-page extraction for RAG.")
    ap.add_argument("--version", action="version", version=f"pagequorum {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, fetching: bool = True):
        p.add_argument("--links", action="store_true", help="keep links as [text](url)")
        p.add_argument("--images", action="store_true", help="keep images as ![alt](src)")
        p.add_argument("--max-tokens", type=int, default=512, help="chunk size (default 512)")
        p.add_argument("--overlap", type=int, default=0, help="chunk overlap in tokens (default 0)")
        if fetching:
            p.add_argument("--no-robots", action="store_true", help="ignore robots.txt (be sure you may)")
            p.add_argument("--cache", help="directory for an on-disk fetch cache")
            p.add_argument("--user-agent", help="override the User-Agent header")

    s = sub.add_parser("scrape", help="fetch URLs and print their main content")
    s.add_argument("urls", nargs="+")
    s.add_argument("-f", "--format", choices=["markdown", "text", "json", "chunks"], default="markdown")
    s.add_argument("--render", choices=["auto", "always", "never"], default="auto",
                   help="JavaScript rendering with Playwright (default: auto)")
    common(s)
    s.set_defaults(func=cmd_scrape)

    e = sub.add_parser("extract", help="extract a local HTML file")
    e.add_argument("file")
    e.add_argument("--url", help="the page's URL (resolves relative links, enables citations)")
    e.add_argument("-f", "--format", choices=["markdown", "text", "json", "chunks"], default="markdown")
    e.add_argument("--explain", action="store_true", help="show every block, its votes and the decision")
    common(e, fetching=False)
    e.set_defaults(func=cmd_extract)

    c = sub.add_parser("crawl", help="crawl a site section into Markdown + JSONL chunks")
    c.add_argument("url")
    c.add_argument("-o", "--out", required=True, help="output directory")
    c.add_argument("--max-pages", type=int, default=50)
    c.add_argument("--max-depth", type=int, default=3)
    c.add_argument("--delay", type=float, default=1.0, help="seconds between requests to a host")
    c.add_argument("--no-sitemap", action="store_true")
    c.add_argument("--include", help="only URLs matching this regex")
    c.add_argument("--exclude", help="skip URLs matching this regex")
    c.add_argument("--content-links-only", action="store_true",
                   help="follow only links inside each page's main content, not menus or footers")
    common(c)
    c.set_defaults(func=cmd_crawl)

    w = sub.add_parser("serve", help="start the local web app (needs the 'web' extra)")
    w.add_argument("--host", help="interface to listen on (default 127.0.0.1; 0.0.0.0 with --public)")
    w.add_argument("--port", type=int, help="port (default 8000; with --public, $PORT if set)")
    w.add_argument("--open", action="store_true", help="open the app in your browser")
    w.add_argument("--public", action="store_true",
                   help="hardened mode for the open internet: no private addresses, rate limits, small "
                        "crawls (settings from the environment, see pagequorum/web.py)")
    w.set_defaults(func=cmd_serve)

    m = sub.add_parser("mcp", help="run as an MCP server for AI assistants (needs the 'mcp' extra)")
    m.set_defaults(func=cmd_mcp)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
