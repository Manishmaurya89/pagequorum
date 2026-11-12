"""Compare pagequorum with other extractors on the same pages, the same scorer and the
same dev/test split as bench/aeb.py.

    uv run python bench/compare.py

* Commercial and other extractors: the outputs saved in the benchmark repository
  (``bench/data/aeb/output``). Scoring them here reproduces the benchmark's own
  published numbers, which checks the scorer.
* Crawl4AI: run ``bench/run_crawl4ai.py`` first (it needs its own environment).
* Markdown output is turned into plain text before scoring (links and images
  reduced to their text, emphasis markers dropped), or link URLs would count as
  wrong words. Every system gets the same treatment.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import metrics  # noqa: E402
from aeb import DATA, RESULTS, analyse_all, split_of  # noqa: E402

from pagequorum.extract import ExtractOptions, build_document, decide  # noqa: E402

SAVED = {  # file in bench/data/aeb/output -> label
    "AutoExtract": "Zyte AutoExtract (commercial; saved Nov 2019 output)",
    "Diffbot": "Diffbot (commercial; saved Nov 2019 output)",
    "rs_trafilatura": "rs-trafilatura (saved output)",
    "trafilatura": "trafilatura 2.0 (saved output)",
    "readability_js": "Mozilla Readability.js (saved output)",
}
CRAWL4AI = HERE / "data" / "crawl4ai_aeb.json"

_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)")
_AUTOLINK = re.compile(r"<(?:https?|mailto):[^>\s]+>")
_EMPHASIS = re.compile(r"(?<!\w)[*_]{1,3}|[*_]{1,3}(?!\w)")  # not the _ inside snake_case


def markdown_to_text(md: str) -> str:
    """Markdown syntax that would otherwise be scored as words (``#``, ``|`` and ``>`` are
    not words to the tokenizer, so they can stay)."""
    return _EMPHASIS.sub(" ", _AUTOLINK.sub(" ", _LINK.sub(r"\1", _IMAGE.sub(" ", md))))


def saved_output(name: str) -> dict[str, str]:
    data = json.loads((DATA / "output" / f"{name}.json").read_text(encoding="utf-8"))
    if isinstance(data.get("output"), dict):  # newer files wrap the results
        data = data["output"]
    return {k: (v or {}).get("articleBody") or "" for k, v in data.items()}


def main() -> None:
    if not (DATA / "ground-truth.json").exists():
        sys.exit("benchmark data missing: run  uv run python bench/download_aeb.py")
    pages = analyse_all()
    opts = replace(ExtractOptions())
    systems: dict[str, dict[str, str]] = {"**pagequorum**": {}}
    for p in pages:
        kept, method, warnings, agreement = decide(p["an"], opts)
        systems["**pagequorum**"][p["key"]] = build_document(p["an"], kept, method, warnings, agreement, opts).text
    for name, label in SAVED.items():
        systems[label] = saved_output(name)
    if CRAWL4AI.exists():
        c4a = json.loads(CRAWL4AI.read_text(encoding="utf-8"))
        v = c4a.get("version", "?")
        systems[f"Crawl4AI {v} `fit_markdown` (our run)"] = {k: markdown_to_text(t) for k, t in c4a["fit_markdown"].items()}
        systems[f"Crawl4AI {v} `fit_markdown` + `excluded_tags` (our run)"] = {
            k: markdown_to_text(t) for k, t in c4a["fit_markdown_excluded_tags"].items()}
        systems[f"Crawl4AI {v} `raw_markdown` (our run)"] = {k: markdown_to_text(t) for k, t in c4a["raw_markdown"].items()}
    else:
        print(f"(no {CRAWL4AI.name}: run bench/run_crawl4ai.py to include Crawl4AI)", file=sys.stderr)

    rows = []
    for label, preds in systems.items():
        r = {}
        for split in ("all", "test"):
            subset = [p for p in pages if split == "all" or p["split"] == split]
            triples = [metrics.tp_fp_fn(p["truth"], preds.get(p["key"], "")) for p in subset]
            r[split] = metrics.aggregate(triples)
            if split == "all":
                r[split].update(metrics.bootstrap_std(triples))
                r[split]["exact"] = sum(metrics.accuracy(p["truth"], preds.get(p["key"], "")) for p in subset) / len(subset)
        rows.append((label, r))
    rows.sort(key=lambda x: -x[1]["test"]["f1"])

    lines = [
        f"Same {len(pages)} pages, same scorer, same split as bench/aeb.py; sorted by held-out F1.",
        "",
        "| system | F1 test (held out) | F1 (all) | precision (all) | recall (all) | exact match |",
        "|---|---|---|---|---|---|",
    ]
    for label, r in rows:
        a = r["all"]
        lines.append(f"| {label} | {r['test']['f1']:.3f} | {a['f1']:.3f} ± {a['f1_std']:.3f} | {a['precision']:.3f} "
                     f"| {a['recall']:.3f} | {a['exact']:.3f} |")
    table = "\n".join(lines)
    print(table)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "compare.md").write_text(table + "\n", encoding="utf-8")
    print(f"\nwrote {RESULTS / 'compare.md'}")


if __name__ == "__main__":
    main()
