"""Run Crawl4AI on the article-extraction-benchmark pages, for bench/compare.py.

Crawl4AI is not a dependency of pagequorum; run this in a throwaway environment:

    uv run --no-project --with crawl4ai==0.9.5 python bench/run_crawl4ai.py

Each page's saved HTML is given to Crawl4AI as raw HTML (no browser, no network),
exactly as ``AsyncWebCrawler.arun("raw:...")`` would process it. Two settings are run:
the documented ``PruningContentFilter`` (threshold 0.48) for ``fit_markdown``, and the
same plus Crawl4AI's ``excluded_tags`` clean-up options. Output goes to
``bench/data/crawl4ai_aeb.json`` (git-ignored with the rest of the benchmark data).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
import time
import warnings
from pathlib import Path

from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, __version__
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

warnings.filterwarnings("ignore", category=DeprecationWarning)

DATA = Path(__file__).resolve().parent / "data"
AEB = DATA / "aeb"
OUT = DATA / "crawl4ai_aeb.json"


def configs() -> dict[str, CrawlerRunConfig]:
    def pruning():
        return DefaultMarkdownGenerator(content_filter=PruningContentFilter(threshold=0.48))

    return {
        "pruning": CrawlerRunConfig(markdown_generator=pruning()),
        "pruning+excluded_tags": CrawlerRunConfig(
            excluded_tags=["nav", "header", "footer", "aside", "form"], word_count_threshold=10,
            markdown_generator=pruning()),
    }


async def main() -> None:
    if not (AEB / "ground-truth.json").exists():
        sys.exit("benchmark data missing: run  uv run python bench/download_aeb.py")
    truth = json.loads((AEB / "ground-truth.json").read_text(encoding="utf-8"))
    crawler = AsyncWebCrawler()  # never started: raw HTML needs no browser
    out = {"version": getattr(__version__, "__version__", str(__version__)),
           "raw_markdown": {}, "fit_markdown": {}, "fit_markdown_excluded_tags": {}}
    t0 = time.time()
    for i, key in enumerate(sorted(truth)):
        html = gzip.decompress((AEB / "html" / f"{key}.html.gz").read_bytes()).decode("utf-8", errors="replace")
        url = truth[key]["url"]
        for name, cfg in configs().items():
            result = await crawler.aprocess_html(
                url=url, html=html, extracted_content=None, config=cfg, screenshot_data=None,
                pdf_data=None, verbose=False, is_raw_html=True, redirected_url=url, original_scheme="https")
            md = result.markdown
            if name == "pruning":
                out["raw_markdown"][key] = md.raw_markdown or ""
                out["fit_markdown"][key] = md.fit_markdown or ""
            else:
                out["fit_markdown_excluded_tags"][key] = md.fit_markdown or ""
        if (i + 1) % 45 == 0:
            print(f"  {i + 1} pages ({time.time() - t0:.0f}s)", file=sys.stderr)
    OUT.write_text(json.dumps(out), encoding="utf-8")
    print(f"wrote {OUT} (crawl4ai {out['version']})")


if __name__ == "__main__":
    asyncio.run(main())
