Article-extraction-benchmark (181 pages: 95 dev / 86 test)

| system | F1 (all) | precision (all) | recall (all) | F1 dev | F1 test (held out) | exact match |
|---|---|---|---|---|---|---|
| pagequorum | 0.970 ± 0.006 | 0.969 ± 0.006 | 0.970 ± 0.007 | 0.976 | 0.963 | 0.436 |
| trafilatura | 0.960 ± 0.005 | 0.942 ± 0.008 | 0.978 ± 0.006 | 0.957 | 0.963 | 0.293 |
| readability | 0.954 ± 0.007 | 0.935 ± 0.008 | 0.974 ± 0.007 | 0.958 | 0.950 | 0.298 |
| resiliparse | 0.927 ± 0.005 | 0.879 ± 0.008 | 0.981 ± 0.004 | 0.927 | 0.928 | 0.033 |
| justext | 0.823 ± 0.015 | 0.858 ± 0.016 | 0.791 ± 0.024 | 0.832 | 0.813 | 0.039 |

mean agreement on pages with F1 < 0.8: 0.747 (n=5); on the rest: 0.885 (n=176)
