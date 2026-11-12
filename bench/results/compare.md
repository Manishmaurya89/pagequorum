Same 181 pages, same scorer, same split as bench/aeb.py; sorted by held-out F1.

| system | F1 test (held out) | F1 (all) | precision (all) | recall (all) | exact match |
|---|---|---|---|---|---|
| rs-trafilatura (saved output) | 0.974 | 0.970 ± 0.004 | 0.951 | 0.990 | 0.287 |
| Zyte AutoExtract (commercial; saved Nov 2019 output) | 0.972 | 0.970 ± 0.005 | 0.984 | 0.956 | 0.470 |
| **pagequorum** | 0.963 | 0.970 ± 0.006 | 0.969 | 0.970 | 0.436 |
| trafilatura 2.0 (saved output) | 0.959 | 0.958 ± 0.006 | 0.938 | 0.978 | 0.293 |
| Diffbot (commercial; saved Nov 2019 output) | 0.950 | 0.951 ± 0.010 | 0.958 | 0.944 | 0.348 |
| Mozilla Readability.js (saved output) | 0.947 | 0.947 ± 0.005 | 0.914 | 0.982 | 0.166 |
| Crawl4AI 0.9.5 `fit_markdown` (our run) | 0.799 | 0.805 ± 0.012 | 0.685 | 0.976 | 0.006 |
| Crawl4AI 0.9.5 `fit_markdown` + `excluded_tags` (our run) | 0.794 | 0.800 ± 0.013 | 0.678 | 0.976 | 0.006 |
| Crawl4AI 0.9.5 `raw_markdown` (our run) | 0.659 | 0.666 ± 0.015 | 0.502 | 0.990 | 0.000 |
