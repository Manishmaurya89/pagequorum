"""Score pagequorum with the benchmark's OWN evaluate.py, as an independent check of
bench/aeb.py and bench/compare.py.

    uv run python bench/download_aeb.py            # once
    uv run python bench/official.py                # writes bench/results/official.md

pagequorum's output (and Crawl4AI's, if bench/run_crawl4ai.py was run) is written in the
benchmark's format next to the outputs saved in its repository, for all 181 pages and for
the 86 held-out test pages, and the repository's evaluate.py scores each folder. That
script (standard library only, part of the MIT-licensed benchmark) is run unchanged; only
its random generator is seeded so the bootstrap ± values are the same on every run. The
paired bootstrap then tests whether differences on the held-out pages are larger than noise.

Note: this prints held-out test numbers, so running it is a look at the test split.
"""

from __future__ import annotations

import io
import json
import os
import random
import re
import runpy
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import aeb  # noqa: E402
from compare import markdown_to_text  # noqa: E402

from pagequorum.extract import ExtractOptions, build_document, decide  # noqa: E402

DATA = HERE / "data" / "aeb"
CRAWL4AI = HERE / "data" / "crawl4ai_aeb.json"
OUT = HERE / "results" / "official.md"
SHOWN = ["pagequorum", "rs_trafilatura", "AutoExtract", "trafilatura_2_3", "trafilatura", "Diffbot",
         "readability_js", "crawl4ai_fit_markdown", "crawl4ai_raw_markdown"]
PAIRS = [("pagequorum", "trafilatura_2_3"), ("pagequorum", "rs_trafilatura"), ("pagequorum", "AutoExtract")]


def run_evaluate(folder: Path, *args: str) -> str:
    """The benchmark's evaluate.py, unchanged, run in ``folder`` with a seeded random."""
    old_cwd, old_argv = Path.cwd(), sys.argv
    buf = io.StringIO()
    try:
        os.chdir(folder)
        sys.argv = ["evaluate.py", "--n-bootstrap", "1000", *args]
        random.seed(0)
        with redirect_stdout(buf):
            runpy.run_path(str(folder / "evaluate.py"), run_name="__main__")
    finally:
        os.chdir(old_cwd)
        sys.argv = old_argv
    return buf.getvalue()


def ours(pages: list[dict], truth: dict) -> dict[str, dict[str, str]]:
    opts = replace(ExtractOptions())
    systems = {"pagequorum": {}, "trafilatura_2_3": {}}
    for p in pages:
        kept, method, warnings, agreement = decide(p["an"], opts)
        systems["pagequorum"][p["key"]] = build_document(p["an"], kept, method, warnings, agreement, opts).text
        systems["trafilatura_2_3"][p["key"]] = "\n".join(p["an"].voter_lines.get("trafilatura", []))
    if CRAWL4AI.exists():
        c4a = json.loads(CRAWL4AI.read_text(encoding="utf-8"))
        for kind in ("fit_markdown", "raw_markdown"):
            systems[f"crawl4ai_{kind}"] = {k: markdown_to_text(c4a[kind].get(k, "")) for k in truth}
    return systems


def build(folder: Path, keys: set[str], truth: dict, systems: dict, only: list[str] | None = None) -> None:
    (folder / "output").mkdir(parents=True)
    shutil.copy(DATA / "evaluate.py", folder)
    # the benchmark's own page order: the seeded bootstrap then draws the same pages every run
    (folder / "ground-truth.json").write_text(json.dumps({k: v for k, v in truth.items() if k in keys}),
                                              encoding="utf-8")
    for f in sorted((DATA / "output").glob("*.json")):
        if only is not None and f.stem not in only:
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        wrapped = isinstance(data, dict) and set(data) == {"version", "output"}
        inner = data["output"] if wrapped else data
        if set(inner) != set(truth):
            continue  # an incomplete saved output: the script would refuse it
        sub = {k: inner[k] for k in truth if k in keys}
        (folder / "output" / f.name).write_text(
            json.dumps({"version": data["version"], "output": sub} if wrapped else sub), encoding="utf-8")
    for name, preds in systems.items():
        if only is None or name in only:
            (folder / "output" / f"{name}.json").write_text(
                json.dumps({k: {"articleBody": preds[k]} for k in truth if k in keys}), encoding="utf-8")


def rows(text: str) -> dict[str, str]:
    """name -> the four "value ± std" columns of evaluate.py's table (the version column dropped)."""
    out = {}
    for line in text.splitlines():
        name = line.split(" ", 1)[0]
        m = re.search(r"\d\.\d{3} ± .*", line)
        if name in SHOWN and m:
            out[name] = m.group()
    return out


def exact_match_difference(folder: Path, a: str, b: str, n: int = 5000) -> str:
    """Paired bootstrap of the exact-match difference (evaluate.py's own get_accuracy):
    the mean and a 95% interval."""
    ev = runpy.run_path(str(folder / "evaluate.py"), run_name="evaluate")
    truth = json.loads((folder / "ground-truth.json").read_text(encoding="utf-8"))

    def acc(name: str) -> dict[str, float]:
        pred, _ = ev["load_prediction"](folder / "output" / f"{name}.json")
        return {k: ev["get_accuracy"](truth[k].get("articleBody", ""), pred[k].get("articleBody", "")) for k in truth}

    x, y, keys = acc(a), acc(b), sorted(truth)
    rng = random.Random(0)
    boots = sorted(sum(x[k] - y[k] for k in s) / len(s) for s in ([rng.choice(keys) for _ in keys] for _ in range(n)))
    mean = sum(x[k] - y[k] for k in keys) / len(keys)
    return (f"{a} minus {b}\n  exact match {mean:+.3f}, 95% interval "
            f"[{boots[int(0.025 * n)]:+.3f}, {boots[int(0.975 * n) - 1]:+.3f}]")


def main() -> None:
    if not (DATA / "evaluate.py").exists():
        sys.exit("benchmark data missing: run  uv run python bench/download_aeb.py")
    truth = json.loads((DATA / "ground-truth.json").read_text(encoding="utf-8"))
    pages = aeb.analyse_all()
    systems = ours(pages, truth)
    test = {k for k in truth if aeb.split_of(k) == "test"}
    lines = ["Scored by the benchmark's own evaluate.py (bench/official.py). Values are "
             "F1, precision, recall and exact match, each ± its bootstrap standard deviation.", ""]
    with tempfile.TemporaryDirectory() as tmp:
        for title, keys in (("All 181 pages", set(truth)), ("Held-out test pages (86)", test)):
            folder = Path(tmp) / title.split()[0].lower()
            build(folder, keys, truth, systems)
            got = rows(run_evaluate(folder))
            lines += [f"## {title}", "", "```", f"{'system':<24} F1             precision      recall         exact match"]
            lines += [f"{n:<24} {got[n]}" for n in SHOWN if n in got] + ["```", ""]
        folder = Path(tmp) / "pairs"
        build(folder, test, truth, systems, only=sorted({n for pair in PAIRS for n in pair}))
        diff = run_evaluate(folder, "--bootstrap-differences")
        lines += ["## Differences on the held-out pages (paired bootstrap)", "",
                  "Mean difference ± standard deviation; about 2 standard deviations or more is unlikely "
                  "to be noise.", "", "```"]
        blocks = diff.split("Comparison: ")[1:]
        for a, b in PAIRS:
            for blk in blocks:
                head = blk.splitlines()[0]
                if head in (f"{a} minus {b}", f"{b} minus {a}"):
                    lines.append(f"{head}")
                    lines += ["  " + ln for ln in blk.splitlines()[1:] if ln.strip()]
        lines += ["", exact_match_difference(folder, "pagequorum", "trafilatura_2_3"), "```"]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
