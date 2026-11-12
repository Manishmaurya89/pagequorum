from pagequorum.encoding import decode_html, normalize_label
from pagequorum.text import tokenize


def test_tokenize_keeps_hindi_words_whole():
    # \w+ would split these at the vowel signs (matras); our tokenizer must not.
    assert tokenize("हिन्दी भाषा, बहुत सुंदर है।") == ["हिन्दी", "भाषा", "बहुत", "सुंदर", "है"]


def test_tokenize_strips_punctuation_and_casefolds():
    assert tokenize("Hello, World! (it's) — fine...") == ["hello", "world", "it's", "fine"]


def test_tokenize_splits_cjk_characters():
    assert tokenize("東京タワー tower") == ["東", "京", "タ", "ワ", "ー", "tower"]


def test_bom_wins():
    text, enc, src = decode_html("﻿héllo".encode("utf-8"), "text/html; charset=iso-8859-1")
    assert (text, enc, src) == ("héllo", "utf-8", "bom")


def test_header_charset_is_used():
    body = "<p>café “quoted”</p>".encode("cp1252")
    text, enc, src = decode_html(body, "text/html; charset=windows-1252")
    assert "café “quoted”" in text and enc == "cp1252" and src == "http-header"


def test_meta_charset_is_used_without_header():
    body = '<html><head><meta charset="windows-1252"></head><body>naïve</body></html>'.encode("cp1252")
    text, enc, src = decode_html(body, "text/html")
    assert "naïve" in text and src == "meta"


def test_latin1_label_means_windows_1252_like_browsers():
    assert normalize_label("ISO-8859-1") == "cp1252"
    assert normalize_label("us-ascii") == "cp1252"
    assert normalize_label("gb2312") == "gb18030"
    assert normalize_label("no-such-charset") is None


def test_wrong_latin1_header_on_utf8_body_is_corrected():
    body = "<p>It’s a Hindi word: हिन्दी</p>".encode("utf-8")
    text, enc, src = decode_html(body, "text/html; charset=ISO-8859-1")
    assert "It’s" in text and "हिन्दी" in text
    assert enc == "utf-8" and src == "http-header-corrected-to-utf8"


def test_undeclared_utf8_is_detected():
    body = "<p>नमस्ते दुनिया</p>".encode("utf-8")
    text, enc, src = decode_html(body, None)
    assert "नमस्ते" in text and enc == "utf-8"
