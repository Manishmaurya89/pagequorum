"""Scoring identical to the article-extraction-benchmark's evaluate.py.

Re-implemented (not imported) from https://github.com/scrapinghub/article-extraction-benchmark
(MIT licence): token 4-gram "shingles" are matched as multisets, TP/FP/FN are
normalised per page so long articles do not dominate, precision and recall are
averaged over pages, and F1 is computed from those averages.
"""

from __future__ import annotations

import random
import re
import statistics
from collections import Counter

_TOKEN_RE = re.compile(r"\w+", re.UNICODE | re.MULTILINE | re.IGNORECASE | re.DOTALL)


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text or "")


def _shingles(text: str, n: int = 4) -> Counter:
    tokens = _tokenize(text)
    grams = [tuple(tokens[i : i + n]) for i in range(0, max(1, len(tokens) - n + 1))]
    return Counter(g for g in grams if g)


def tp_fp_fn(true: str, pred: str) -> tuple[float, float, float]:
    t, p = _shingles(true), _shingles(pred)
    tp = fp = fn = 0.0
    for key in set(t) | set(p):
        tc, pc = t.get(key, 0), p.get(key, 0)
        tp += min(tc, pc)
        fp += max(0, pc - tc)
        fn += max(0, tc - pc)
    s = tp + fp + fn
    return (tp / s, fp / s, fn / s) if s > 0 else (0.0, 0.0, 0.0)


def _precision(tp, fp, fn):
    if fp == fn == 0:
        return 1.0
    if tp == fp == 0:
        return 0.0
    return tp / (tp + fp)


def _recall(tp, fp, fn):
    if fp == fn == 0:
        return 1.0
    if tp == fn == 0:
        return 0.0
    return tp / (tp + fn)


def page_scores(tp: float, fp: float, fn: float) -> tuple[float, float]:
    return _precision(tp, fp, fn), _recall(tp, fp, fn)


def aggregate(triples: list[tuple[float, float, float]]) -> dict[str, float]:
    precision = statistics.mean([_precision(*x) for x in triples if x[0] + x[1] > 0] or [0.0])
    recall = statistics.mean([_recall(*x) for x in triples if x[0] + x[2] > 0] or [0.0])
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"f1": f1, "precision": precision, "recall": recall}


def accuracy(true: str, pred: str) -> float:
    return float(_tokenize(true) == _tokenize(pred))


def bootstrap_std(triples, n: int = 1000, seed: int = 0) -> dict[str, float]:
    rng = random.Random(seed)
    values: dict[str, list[float]] = {}
    for _ in range(n):
        sample = [triples[rng.randrange(len(triples))] for _ in triples]
        for k, v in aggregate(sample).items():
            values.setdefault(k, []).append(v)
    return {f"{k}_std": statistics.stdev(v) for k, v in values.items()}
