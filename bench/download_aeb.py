"""Fetch the article-extraction-benchmark (MIT licence) into bench/data/aeb.

Shallow git clone of https://github.com/scrapinghub/article-extraction-benchmark
(about 100 MB on disk). This script runs no code from the benchmark repository;
bench/aeb.py reads only ground-truth.json and html/*.html.gz, and bench/official.py
runs the repository's evaluate.py (standard library only) as an independent check.
"""

import subprocess
import sys
from pathlib import Path

DEST = Path(__file__).resolve().parent / "data" / "aeb"
REPO = "https://github.com/scrapinghub/article-extraction-benchmark"

if __name__ == "__main__":
    if (DEST / "ground-truth.json").exists():
        print(f"already present: {DEST}")
        sys.exit(0)
    DEST.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--depth", "1", REPO, str(DEST)], check=True)
    print(f"downloaded to {DEST}")
