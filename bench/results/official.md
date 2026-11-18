Scored by the benchmark's own evaluate.py (bench/official.py). Values are F1, precision, recall and exact match, each ± its bootstrap standard deviation.

## All 181 pages

```
system                   F1             precision      recall         exact match
pagequorum               0.970 ± 0.006  0.969 ± 0.006  0.970 ± 0.007  0.436 ± 0.037
rs_trafilatura           0.970 ± 0.004  0.951 ± 0.006  0.990 ± 0.003  0.287 ± 0.033
AutoExtract              0.970 ± 0.005  0.984 ± 0.003  0.956 ± 0.010  0.470 ± 0.036
trafilatura_2_3          0.960 ± 0.005  0.942 ± 0.008  0.978 ± 0.006  0.293 ± 0.034
trafilatura              0.958 ± 0.006  0.938 ± 0.009  0.978 ± 0.006  0.293 ± 0.034
Diffbot                  0.951 ± 0.010  0.958 ± 0.008  0.944 ± 0.013  0.348 ± 0.035
readability_js           0.947 ± 0.005  0.914 ± 0.008  0.982 ± 0.003  0.166 ± 0.027
crawl4ai_fit_markdown    0.805 ± 0.012  0.685 ± 0.017  0.976 ± 0.008  0.006 ± 0.006
crawl4ai_raw_markdown    0.666 ± 0.016  0.502 ± 0.018  0.990 ± 0.002  0.000 ± 0.000
```

## Held-out test pages (86)

```
system                   F1             precision      recall         exact match
pagequorum               0.963 ± 0.012  0.962 ± 0.012  0.964 ± 0.013  0.430 ± 0.052
rs_trafilatura           0.974 ± 0.004  0.958 ± 0.007  0.989 ± 0.004  0.302 ± 0.051
AutoExtract              0.972 ± 0.008  0.986 ± 0.003  0.958 ± 0.016  0.488 ± 0.054
trafilatura_2_3          0.963 ± 0.008  0.947 ± 0.012  0.978 ± 0.008  0.337 ± 0.052
trafilatura              0.959 ± 0.009  0.941 ± 0.015  0.978 ± 0.008  0.337 ± 0.049
Diffbot                  0.950 ± 0.015  0.957 ± 0.014  0.943 ± 0.019  0.291 ± 0.050
readability_js           0.947 ± 0.008  0.914 ± 0.012  0.983 ± 0.005  0.163 ± 0.039
crawl4ai_fit_markdown    0.799 ± 0.018  0.678 ± 0.025  0.974 ± 0.012  0.012 ± 0.011
crawl4ai_raw_markdown    0.659 ± 0.023  0.494 ± 0.026  0.989 ± 0.003  0.000 ± 0.000
```

## Differences on the held-out pages (paired bootstrap)

Mean difference ± standard deviation; about 2 standard deviations or more is unlikely to be noise.

```
pagequorum minus trafilatura_2_3
  f1         0.001 ± 0.008
  precision  0.015 ± 0.007
  recall     -0.014 ± 0.013
pagequorum minus rs_trafilatura
  f1         -0.011 ± 0.012
  precision  0.004 ± 0.012
  recall     -0.026 ± 0.012
AutoExtract minus pagequorum
  f1         0.009 ± 0.015
  precision  0.024 ± 0.012
  recall     -0.007 ± 0.021

pagequorum minus trafilatura_2_3
  exact match +0.093, 95% interval [+0.023, +0.174]
```
