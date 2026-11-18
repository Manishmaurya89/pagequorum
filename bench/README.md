# Benchmark method and decision log

## Data and scoring

- **Data:** [scrapinghub/article-extraction-benchmark](https://github.com/scrapinghub/article-extraction-benchmark)
  (MIT), 181 news/blog pages with human-labelled `articleBody`. Fetched by
  `download_aeb.py` into `bench/data/aeb` (git-ignored, about 100 MB). Only the HTML
  and `ground-truth.json` are read; none of the benchmark's code is executed.
- **Scoring:** `metrics.py` re-implements the benchmark's `evaluate.py`: token
  4-gram shingles matched as multisets, TP/FP/FN normalised per page,
  precision and recall averaged over pages, F1 from the averages, ±
  bootstrap standard deviation (1000 resamples). Sanity check: trafilatura
  scores 0.960 here, against 0.958 for trafilatura 2.0 in the benchmark's README.
- **Split:** pages whose id starts with an even 8-hex-digit number are **dev**
  (95), the rest **test** (86). Every design choice and parameter below was made
  on dev.
- **Disclosure:** the report prints the test column on every run, so test
  numbers were *seen* seven times during development (0.945, 0.958, 0.960, 0.963,
  then 0.963 twice more after the docs-page and Wikipedia fixes, against
  trafilatura's 0.963), and a seventh time (0.963, unchanged) when comparing with
  other tools in `compare.py`. An eighth look, on 2026-11-17, re-verified everything
  from a fresh clone and a fresh benchmark download, and scored it with the
  benchmark's own `evaluate.py` (`official.py`): unchanged. No choice was made on them. Every comparison between
  alternatives below used dev only, and the last changes were prompted by live
  pages (Python json docs, Hindi Wikipedia), not by the benchmark.

Run: `uv run python bench/aeb.py` (report, writes `results/`) or `--tune` (dev grid).

**Other tools:** `compare.py` scores the outputs saved in the benchmark repository
(Zyte AutoExtract, Diffbot, rs-trafilatura, trafilatura, Readability.js) with the same
scorer and split; this reproduces the benchmark's published F1 for each of them, which
checks `metrics.py`. Crawl4AI is run by `run_crawl4ai.py` in its own environment
(`uv run --no-project --with crawl4ai==0.9.5 python bench/run_crawl4ai.py`), and its
Markdown is turned into plain text before scoring. Results: `results/compare.md`.

## Decision log (dev F1 unless stated)

| step | dev F1 | note |
|---|---|---|
| equal-weight majority of 5 voters, order-free trigram-set matching | 0.939 | below trafilatura alone (0.957) |
| fixed a renderer bug (cache keyed by `id()` of lxml proxies) | — | page with paragraphs inside a `<span>` rendered empty |
| grid search: require trafilatura + readability + resiliparse to agree | 0.965 | set matching credits menu items and captions that repeat article words |
| **in-order alignment** (difflib) instead of set matching | 0.971 | the largest single gain |
| own text of a block split into segments around nested blocks | 0.972 | `<div>text <p>…</p> more text</div>` was out of document order |
| smoothing limited to code and tables | ±0 | rescuing headings and captions mostly brought back "Related post" headings and image captions |
| "inside the agreed region" fill (2-of-3 between unanimous blocks) | 0.969–0.970 | **rejected**: lowered F1 at every threshold |
| jusText as a voter | lower | kept available, off by default |
| **outlier voter set aside** (covers < 50% of the others' median) | 0.976 | motivated by Readability keeping 557 of ~3500 words on a Sphinx docs page; also helps news |
| permalink anchors (`¶`, `#`) removed; Sphinx API signatures rendered as one code span | 0.976 | rendering only; found on the Python json docs page (`## Basic Usage¶`, `***kw*`) |
| **two-voter tie-break**: with a voter set aside, a run one remaining voter chose between kept blocks is kept | 0.976 | never fires on dev (no news page drops to two voters), so the benchmark cannot confirm it; on the json docs page it restores the `raw_decode()` entry that trafilatura skipped |
| admonition labels joined to their paragraph (`**Note:** …`); footnotes kept when kept text links to them (`role=doc-noteref`) | 0.976 | rendering/docs only: the footnote rule fires on 0 of 181 benchmark pages and the label change alters no scored tokens, so results were not re-run |
| wiki "[edit]" links and v·t·e navbars removed; reference/notes lists (`ol.references`, `doc-bibliography`) never kept; picture cells not repeated across rowspan | 0.976 | found on Hindi Wikipedia (भारत): 104 citations were kept because all three extractors chose them; fires on no dev page |
| heading rescue v1: heading with a majority vote whose section has kept text | 0.9729 | **rejected**: on news it brought back standfirsts, duplicate titles and "Subscribe" headings (precision 0.9757 → 0.9699, 14 pages) |
| heading rescue v2: also needs kept text before it, an agreed heading of the same level on the page, not H1 | 0.9759 | fires on 1 dev page, restoring a labelled heading; restores 19 section headings resiliparse missed on Hindi Wikipedia |
| list/table/code announced by a kept sentence ending in ":" kept if any voter chose it | 0.9759 | fires on no dev page; restores e.g. the climate-type list on Hindi Wikipedia |
| kept H2–H6 with an empty section dropped | 0.9760 | dev: removes "You may also like:", "More:", and wrongly one "Score 4.5 out of 5" |

The voting weights barely matter once the rule is "the main voters must agree".
Many weightings tie at the top of the grid, so the simplest one (all equal) was kept.

## Results (final)

See `results/aeb.md` (table), `results/aeb.json` (all splits) and
`results/aeb_pages.jsonl` (per-page precision/recall for every system, useful for
error analysis).
