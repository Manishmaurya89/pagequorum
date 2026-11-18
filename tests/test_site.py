"""The static online demo: recordings in site/demo must match the web page's examples."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_site", ROOT / "scripts" / "build_site.py")
build_site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_site)

PAGE = (ROOT / "pagequorum" / "static" / "index.html").read_text(encoding="utf-8")
DEMO = ROOT / "site" / "demo"
MANIFEST = json.loads((DEMO / "manifest.json").read_text(encoding="utf-8"))


def test_examples_are_read_from_the_page():
    examples = build_site.page_examples(PAGE)
    assert set(examples) == {"scrape", "crawl", "map"}
    assert all(u.startswith("https://") for urls in examples.values() for u in urls)


def test_every_example_has_a_recording_of_exactly_what_the_page_sends():
    # change an example (or a default setting) without re-recording and this fails:
    # run  uv run --extra web python scripts/build_site.py --record
    wanted = build_site.requests_for(build_site.page_examples(PAGE))
    for kind in ("scrape", "crawl", "map"):
        assert [e["request"] for e in MANIFEST[kind]] == wanted[kind], kind


def test_crawl_defaults_match_the_form():
    for field, value in build_site.CRAWL_DEFAULTS.items():
        assert re.search(rf'<input id="c_{field}"[^>]*\bvalue="{value}"', PAGE), field


def test_recordings_exist_and_have_the_shape_the_page_reads():
    for entry in MANIFEST["scrape"]:
        data = json.loads((DEMO / entry["file"]).read_text(encoding="utf-8"))
        assert {"document", "chunks", "blocks", "voters"} <= set(data)
        assert data["document"]["markdown"] and data["chunks"]
    for entry in MANIFEST["map"]:
        data = json.loads((DEMO / entry["file"]).read_text(encoding="utf-8"))
        assert data["count"] == len(data["urls"]) > 0
    for entry in MANIFEST["crawl"]:
        data = json.loads((DEMO / entry["file"]).read_text(encoding="utf-8"))
        status, pages = data["status"], data["pages"]
        assert status["status"] == "done" and status["id"] == "demo"
        ok = [p for p in status["pages"] if p["status"] == "ok"]
        assert ok and all(str(p["n"]) in pages for p in ok)
        lines = (DEMO / entry["chunks"]).read_text(encoding="utf-8").splitlines()
        assert len(lines) == status["chunks"]
        assert (DEMO / entry["documents"]).exists()


def test_page_switches_to_the_demo_without_a_server():
    assert 'new URL("demo/", location.href)' in PAGE and "manifest.json" in PAGE
    assert 'id="demo-note" hidden' in PAGE  # only shown when no server answers


def test_build_can_name_a_live_server():
    page = build_site.with_api(PAGE, "https://pagequorum.onrender.com/")
    assert '<meta name="pagequorum-api" id="pq-api" content="https://pagequorum.onrender.com">' in page
    assert page.index('name="viewport"') < page.index('id="pq-api"') < page.index("<title>")
    assert 'id="pq-api"' not in PAGE  # the page served by `pagequorum serve` never names one
    for bad in ("http://pagequorum.onrender.com", "javascript:alert(1)", 'https://x.com/"><script>'):
        with pytest.raises(SystemExit):
            build_site.with_api(PAGE, bad)
