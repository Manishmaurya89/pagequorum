"""Build the static demo site: the web page plus recorded API responses.

    uv run --extra web python scripts/build_site.py --record   # re-record (fetches the example pages)
    python3 scripts/build_site.py                              # assemble site/ (no network, stdlib only)

The web page (pagequorum/static/index.html) normally talks to a pagequorum server.
On a static host there is none, so the page replays the responses recorded here for
its example URLs (``site/demo/``) and tells visitors how to run pagequorum for
anything else. ``site/`` can then be published on any free static host.

Recording goes through the real HTTP API in-process, so the files are exactly what
``pagequorum serve`` returns for the same requests.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "pagequorum" / "static" / "index.html"
SITE = ROOT / "site"
DEMO = SITE / "demo"

# What the page sends for an example with its default settings (see currentRequest()).
CRAWL_DEFAULTS = {"max_pages": 10, "max_depth": 2}


def page_examples(html: str) -> dict[str, list[str]]:
    """The ``EXAMPLES`` object of the page's script, so recordings always match the page."""
    m = re.search(r"const EXAMPLES = (\{.*?\});", html, re.S)
    if not m:
        raise SystemExit(f"no EXAMPLES object found in {PAGE}")
    obj = re.sub(r"(\w+):\s*\[", r'"\1": [', m.group(1))  # quote the keys
    obj = re.sub(r",\s*([\]}])", r"\1", obj)  # drop trailing commas
    return json.loads(obj)


def requests_for(examples: dict[str, list[str]]) -> dict[str, list[dict]]:
    return {
        "scrape": [{"url": u} for u in examples["scrape"]],
        "map": [{"url": u} for u in examples["map"]],
        "crawl": [{"url": u, **CRAWL_DEFAULTS} for u in examples["crawl"]],
    }


def slug(url: str) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "-", re.sub(r"^https?://", "", url)).strip("-")[:50] or "page"
    return f"{base}-{hashlib.sha1(url.encode('utf-8')).hexdigest()[:8]}"


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def record() -> None:
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise SystemExit("recording needs the web extra: uv run --extra web python scripts/build_site.py --record")
    sys.path.insert(0, str(ROOT))
    from pagequorum import __version__
    from pagequorum.web import create_app

    wanted = requests_for(page_examples(PAGE.read_text(encoding="utf-8")))
    client = TestClient(create_app(), base_url="http://localhost")
    if DEMO.exists():
        shutil.rmtree(DEMO)
    manifest = {"version": __version__, "recorded_at": datetime.date.today().isoformat(),
                "scrape": [], "map": [], "crawl": []}

    for kind in ("scrape", "map"):
        for req in wanted[kind]:
            print(f"{kind:6} {req['url']}", file=sys.stderr)
            r = client.post(f"/api/{kind}", json=req)
            if r.status_code != 200:
                raise SystemExit(f"  failed: HTTP {r.status_code} {r.text[:300]}")
            name = f"{kind}/{slug(req['url'])}.json"
            write_json(DEMO / name, r.json())
            manifest[kind].append({"request": req, "file": name})

    for req in wanted["crawl"]:
        print(f"crawl  {req['url']}", file=sys.stderr)
        r = client.post("/api/crawl", json=req)
        if r.status_code != 202:
            raise SystemExit(f"  failed: HTTP {r.status_code} {r.text[:300]}")
        job = r.json()["id"]
        while (status := client.get(f"/api/crawl/{job}").json())["status"] == "running":
            time.sleep(1)
        status = client.get(f"/api/crawl/{job}?since=0").json()
        status["id"] = "demo"
        pages = {str(p["n"]): client.get(f"/api/crawl/{job}/pages/{p['n']}").json()
                 for p in status["pages"] if p["status"] == "ok"}
        print(f"  {status['status']}: {status['pages_done']} pages, {status['chunks']} chunks", file=sys.stderr)
        base = f"crawl/{slug(req['url'])}"
        write_json(DEMO / f"{base}.json", {"status": status, "pages": pages})
        for part in ("chunks", "documents"):
            path = DEMO / f"{base}.{part}.jsonl"
            path.write_text(client.get(f"/api/crawl/{job}/{part}.jsonl").text, encoding="utf-8")
        manifest["crawl"].append({"request": req, "file": f"{base}.json",
                                  "chunks": f"{base}.chunks.jsonl", "documents": f"{base}.documents.jsonl"})

    write_json(DEMO / "manifest.json", manifest)
    size = sum(p.stat().st_size for p in DEMO.rglob("*") if p.is_file())
    print(f"recorded into {DEMO} ({size / 1e6:.1f} MB)", file=sys.stderr)


def build() -> None:
    if not (DEMO / "manifest.json").exists():
        raise SystemExit(f"no recordings in {DEMO}: run with --record first")
    shutil.copyfile(PAGE, SITE / "index.html")
    print(f"site ready in {SITE} (preview: python3 -m http.server -d site 8001)", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--record", action="store_true", help="re-record the examples (needs network and the web extra)")
    args = ap.parse_args()
    if args.record:
        record()
    build()


if __name__ == "__main__":
    main()
