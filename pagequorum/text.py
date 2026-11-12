"""Text normalisation and tokenisation shared by voting, chunking and evaluation.

The tokenizer is Unicode-aware on purpose. Python's ``\\w`` does not match the
combining vowel signs of Indic scripts (Devanagari matras are category Mc/Mn), so
``re.findall(r"\\w+", "हिन्दी")`` splits one word into fragments. Here a token is a
whitespace-separated run with punctuation and symbols stripped from its ends, which
keeps Hindi, Arabic, Cyrillic and Latin words whole. CJK text has no spaces, so CJK
characters become one token each.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_WS = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    """Collapse every run of whitespace to one space and trim the ends."""
    return _WS.sub(" ", text).strip()


@lru_cache(maxsize=4096)
def _is_edge_char(ch: str) -> bool:
    # Punctuation (P*), symbols (S*), separators (Z*) and control characters (Cc).
    cat = unicodedata.category(ch)
    return cat[0] in "PSZ" or cat == "Cc"


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF  # CJK unified ideographs
        or 0x3400 <= cp <= 0x4DBF  # extension A
        or 0x3040 <= cp <= 0x30FF  # hiragana + katakana
        or 0xF900 <= cp <= 0xFAFF  # compatibility ideographs
        or 0x20000 <= cp <= 0x2A6DF  # extension B
    )


def tokenize(text: str) -> list[str]:
    """Split text into case-folded word tokens, ignoring punctuation-only runs."""
    tokens: list[str] = []
    for raw in unicodedata.normalize("NFKC", text).casefold().split():
        start, end = 0, len(raw)
        while start < end and _is_edge_char(raw[start]):
            start += 1
        while end > start and _is_edge_char(raw[end - 1]):
            end -= 1
        if start == end:
            continue
        tok = raw[start:end]
        if any(_is_cjk(c) for c in tok):
            run = ""
            for c in tok:
                if _is_cjk(c):
                    if run:
                        tokens.append(run)
                        run = ""
                    tokens.append(c)
                elif not _is_edge_char(c):
                    run += c
            if run:
                tokens.append(run)
        else:
            tokens.append(tok)
    return tokens


def ngrams(tokens: list[str], n: int) -> list[tuple[str, ...]]:
    """All contiguous n-grams; a sequence shorter than n yields itself once."""
    if len(tokens) < n:
        return [tuple(tokens)] if tokens else []
    return [tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def word_count(text: str) -> int:
    return len(tokenize(text))
