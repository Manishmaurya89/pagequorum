"""Decode HTML bytes the way a browser would, plus one pragmatic correction.

Order follows the WHATWG HTML spec: byte-order mark, then the HTTP ``charset``,
then a ``<meta>`` declaration, then sniffing. Labels are mapped the way browsers
map them (``iso-8859-1`` and ``ascii`` really mean windows-1252, ``gb2312`` means
gb18030, and so on).

The correction: many servers send ``charset=ISO-8859-1`` by default for pages that
are really UTF-8. Valid multi-byte UTF-8 almost never occurs by accident in
windows-1252 text, so when a single-byte Western label is declared but the bytes
are valid UTF-8 containing non-ASCII characters, UTF-8 wins. That avoids the
classic mojibake (``â€™`` instead of ``'``) that silently corrupts RAG text.
"""

from __future__ import annotations

import codecs
import re

from charset_normalizer import from_bytes

# WHATWG encoding labels whose Python name differs or that browsers remap.
_ALIASES = {
    "iso-8859-1": "cp1252",
    "iso8859-1": "cp1252",
    "iso_8859-1": "cp1252",
    "latin1": "cp1252",
    "latin-1": "cp1252",
    "l1": "cp1252",
    "cp819": "cp1252",
    "ascii": "cp1252",
    "us-ascii": "cp1252",
    "windows-1252": "cp1252",
    "x-cp1252": "cp1252",
    "iso-8859-9": "cp1254",
    "latin5": "cp1254",
    "iso-8859-11": "cp874",
    "tis-620": "cp874",
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "x-gbk": "gb18030",
    "euc-kr": "cp949",
    "ks_c_5601-1987": "cp949",
    "shift_jis": "cp932",
    "shift-jis": "cp932",
    "sjis": "cp932",
    "x-sjis": "cp932",
    "ms_kanji": "cp932",
    "windows-31j": "cp932",
    "utf8": "utf-8",
    "unicode-1-1-utf-8": "utf-8",
}

# Single-byte Western encodings that UTF-8 validity is allowed to override.
_WESTERN_SINGLE_BYTE = {"cp1252", "iso8859-1", "iso8859-15", "ascii"}

_HEADER_CHARSET = re.compile(r"charset\s*=\s*[\"']?([^;\"'\s]+)", re.I)
# Matches both <meta charset="x"> and <meta http-equiv=... content="...; charset=x">.
_META_CHARSET = re.compile(rb"<meta[^>]+?charset\s*=\s*[\"']?\s*([a-zA-Z0-9_:.+-]+)", re.I)
_META_SCAN_BYTES = 16384


def normalize_label(label: str | None) -> str | None:
    """Map an encoding label to a Python codec name, or None if unknown."""
    if not label:
        return None
    key = label.strip().strip("\"'").lower()
    key = _ALIASES.get(key, key)
    try:
        return codecs.lookup(key).name
    except LookupError:
        return None


def _valid_utf8_non_ascii(data: bytes) -> bool:
    if data.isascii():
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def decode_html(data: bytes, content_type: str | None = None) -> tuple[str, str, str]:
    """Return ``(text, encoding, source)``; ``source`` says how the encoding was chosen."""
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode("utf-8", "replace"), "utf-8", "bom"
    if data.startswith(codecs.BOM_UTF16_LE):
        return data[2:].decode("utf-16-le", "replace"), "utf-16-le", "bom"
    if data.startswith(codecs.BOM_UTF16_BE):
        return data[2:].decode("utf-16-be", "replace"), "utf-16-be", "bom"

    candidates: list[tuple[str, str]] = []
    if content_type:
        m = _HEADER_CHARSET.search(content_type)
        enc = normalize_label(m.group(1)) if m else None
        if enc:
            candidates.append((enc, "http-header"))
    m = _META_CHARSET.search(data[:_META_SCAN_BYTES])
    if m:
        enc = normalize_label(m.group(1).decode("ascii", "ignore"))
        if enc:
            # The spec treats a <meta> claim of UTF-16 as UTF-8 (the bytes were ASCII-compatible
            # enough for us to read the tag, so they cannot really be UTF-16).
            if enc.startswith("utf-16"):
                enc = "utf-8"
            candidates.append((enc, "meta"))

    if candidates:
        enc, source = candidates[0]
        if enc in _WESTERN_SINGLE_BYTE and _valid_utf8_non_ascii(data):
            return data.decode("utf-8"), "utf-8", f"{source}-corrected-to-utf8"
        return data.decode(enc, "replace"), enc, source

    try:
        return data.decode("utf-8"), "utf-8", "utf8-valid"
    except UnicodeDecodeError:
        pass
    best = from_bytes(data[:200_000]).best()
    if best is not None and best.encoding:
        enc = normalize_label(best.encoding) or best.encoding
        return data.decode(enc, "replace"), enc, "detected"
    return data.decode("cp1252", "replace"), "cp1252", "fallback"
