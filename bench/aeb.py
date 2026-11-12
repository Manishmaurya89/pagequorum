"""Evaluate pagequorum on the Zyte/ScrapingHub article-extraction-benchmark.

    uv run python bench/download_aeb.py      # once: fetch the benchmark into bench/data/aeb
    uv run python bench/aeb.py --tune        # grid search on the DEV half only
    uv run python bench/aeb.py               # report DEV, TEST (held out) and ALL

Pages are split deterministically by their id: ids whose first 8 hex digits are
even form DEV (used for every tuning decision), the rest form TEST, which is only
looked at to report results.
"""

from __future__ import annotations

import argparse
import gzip
import itertools
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import metrics  # noqa: E402

from pagequorum.extract import ALL_VOTERS, ExtractOptions, analyze, build_document, decide  # noqa: E402

DATA = HERE / "data" / "aeb"
RESULTS = HERE / "results"


def load(limit: int | None = None):
    truth = json.loads((DATA / "ground-truth.json").read_text(encoding="utf-8"))
    keys = sorted(truth)[:limit] if limit else sorted(truth)
    for key in keys:
        raw = gzip.decompress((DATA / "html" / f"{key}.html.gz").read_bytes())
        yield key, truth[key]["url"], truth[key].get("articleBody", ""), raw


def split_of(key: str) -> str:
    return "dev" if int(key[:8], 16) % 2 == 0 else "test"


def fast_text(an, opts) -> str:
    """Kept block texts in document order: close to the rendered text, much faster (tuning only)."""
    kept, *_ = decide(an, opts)
    return "\n".join(b.text for b in an.blocks if b.index in kept)


def score(pages, predict) -> dict:
    triples, accs = [], []
    for page in pages:
        pred = predict(page)
        triples.append(metrics.tp_fp_fn(page["truth"], pred))
        accs.append(metrics.accuracy(page["truth"], pred))
    out = metrics.aggregate(triples)
    out["accuracy"] = sum(accs) / len(accs)
    out["_triples"] = triples
    return out


def analyse_all(limit=None):
    pages = []
    t0 = time.time()
    for i, (key, url, truth, raw) in enumerate(load(limit)):
        an = analyze(raw, url, ALL_VOTERS)
        pages.append({"key": key, "url": url, "truth": truth, "an": an, "split": split_of(key)})
        if (i + 1) % 30 == 0:
            print(f"  analysed {i + 1} pages ({time.time() - t0:.0f}s)", file=sys.stderr)
    return pages


def tune(pages) -> None:
    dev = [p for p in pages if p["split"] == "dev"]
    print(f"tuning on {len(dev)} DEV pages")
    grid = []
    for thr, wt, wr, wj, wres, wld, smooth in itertools.product(
        [0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0], [1, 1.5, 2], [1, 1.5], [0, 0.5, 1], [0.5, 1], [0, 1], [True, False]
    ):
        weights = {"trafilatura": wt, "readability": wr, "justext": wj, "resiliparse": wres, "jsonld": wld}
        opts = ExtractOptions(weights=weights, threshold=thr, smoothing=smooth)
        r = score(dev, lambda p, o=opts: fast_text(p["an"], o))
        grid.append((r["f1"], r["precision"], r["recall"], thr, weights, smooth))
    grid.sort(key=lambda x: -x[0])
    for f1, p, r, thr, w, s in grid[:15]:
        print(f"F1 {f1:.4f}  P {p:.4f}  R {r:.4f}  thr={thr} smooth={s} {w}")


def report(pages, opts: ExtractOptions, write: bool) -> None:
    systems = {"pagequorum": None, **{n: n for n in ALL_VOTERS if n != "jsonld"}}
    rows = {}
    per_page = {p["key"]: {"url": p["url"], "split": p["split"]} for p in pages}

    def predict_pq(page):
        kept, method, warnings, agreement = decide(page["an"], opts)
        doc = build_document(page["an"], kept, method, warnings, agreement, opts)
        page["doc"] = doc
        return doc.text

    for name, voter in systems.items():
        rows[name] = {}
        if voter is None:
            fn = predict_pq
        else:
            fn = lambda page, v=voter: "\n".join(page["an"].voter_lines.get(v, []))  # noqa: E731
        for split in ("dev", "test", "all"):
            subset = [p for p in pages if split == "all" or p["split"] == split]
            r = score(subset, fn)
            triples = r.pop("_triples")
            if split == "all":
                r.update(metrics.bootstrap_std(triples))
                for p, t in zip(subset, triples):
                    per_page[p["key"]][name] = [round(x, 4) for x in metrics.page_scores(*t)]
            rows[name][split] = r

    order = sorted(rows, key=lambda n: -rows[n]["all"]["f1"])
    lines = [
        f"Article-extraction-benchmark ({len(pages)} pages: "
        f"{sum(p['split'] == 'dev' for p in pages)} dev / {sum(p['split'] == 'test' for p in pages)} test)",
        "",
        "| system | F1 (all) | precision (all) | recall (all) | F1 dev | F1 test (held out) | exact match |",
        "|---|---|---|---|---|---|---|",
    ]
    for n in order:
        a = rows[n]["all"]
        lines.append(
            f"| {n} | {a['f1']:.3f} ± {a['f1_std']:.3f} | {a['precision']:.3f} ± {a['precision_std']:.3f} "
            f"| {a['recall']:.3f} ± {a['recall_std']:.3f} | {rows[n]['dev']['f1']:.3f} | {rows[n]['test']['f1']:.3f} "
            f"| {a['accuracy']:.3f} |"
        )
    table = "\n".join(lines)
    print(table)

    # Does the agreement score flag the bad pages? (pages with F1 < 0.8 vs the rest)
    agreements = [(p["doc"].extraction["agreement"], per_page[p["key"]]["pagequorum"]) for p in pages]
    low = [a for a, (pr, rc) in agreements if (2 * pr * rc / (pr + rc) if pr + rc else 0) < 0.8]
    high = [a for a, (pr, rc) in agreements if (2 * pr * rc / (pr + rc) if pr + rc else 0) >= 0.8]
    note = (f"\nmean agreement on pages with F1 < 0.8: {sum(low) / max(1, len(low)):.3f} (n={len(low)}); "
            f"on the rest: {sum(high) / max(1, len(high)):.3f} (n={len(high)})")
    print(note)

    if write:
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / "aeb.md").write_text(table + "\n" + note + "\n", encoding="utf-8")
        (RESULTS / "aeb.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        with (RESULTS / "aeb_pages.jsonl").open("w", encoding="utf-8") as f:
            for key, row in per_page.items():
                f.write(json.dumps({"key": key, **row}) + "\n")
        print(f"\nwrote {RESULTS / 'aeb.md'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tune", action="store_true", help="grid search on the DEV split")
    ap.add_argument("--limit", type=int, help="only the first N pages (smoke test)")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()
    if not (DATA / "ground-truth.json").exists():
        sys.exit("benchmark data missing: run  uv run python bench/download_aeb.py")
    pages = analyse_all(args.limit)
    if args.tune:
        tune(pages)
    else:
        report(pages, replace(ExtractOptions()), write=not args.no_write)


if __name__ == "__main__":
    main()
