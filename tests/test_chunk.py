from urllib.parse import unquote

import pagequorum as pq
from pagequorum.chunk import citation_url
from pagequorum.models import Document


def make_doc(markdown: str, url: str = "https://example.com/page") -> Document:
    return Document(url=url, markdown=markdown, text="", title="Title")


def body(chunk) -> str:
    """Chunk text without the heading lines that open a section."""
    lines = chunk.text.split("\n")
    while lines and (lines[0].startswith("#") or not lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines)


def test_chunks_follow_headings_and_offsets_point_into_markdown(docs_html):
    doc = pq.extract(docs_html, url="https://docs.acme.dev/sdk/retries/")
    chunks = doc.chunks(max_tokens=200)
    assert len(chunks) >= 3
    for c in chunks:
        assert doc.markdown[c.start:c.end] == c.text  # exact span, usable for highlighting
        assert c.heading_path[0] == "Configuring retries"
    paths = [c.heading_path for c in chunks]
    assert ["Configuring retries", "Custom policies", "Per-request overrides"] in paths
    assert len({c.id for c in chunks}) == len(chunks)


def test_no_chunk_exceeds_the_limit_and_no_text_is_lost():
    paras = "\n\n".join(f"Sentence {i} of the body goes here. " * 12 for i in range(10))
    doc = make_doc(f"# Title\n\n## Part\n\n{paras}\n")
    chunks = doc.chunks(max_tokens=120)
    assert all(c.tokens <= 120 for c in chunks)
    joined = " ".join(c.text for c in chunks)
    for i in range(10):
        assert f"Sentence {i} of the body" in joined


def test_large_table_is_split_by_rows_with_header_repeated():
    rows = "\n".join(f"| item {i} | value {i} |" for i in range(80))
    doc = make_doc(f"# Title\n\n| Name | Value |\n| --- | --- |\n{rows}\n")
    chunks = doc.chunks(max_tokens=100)
    assert len(chunks) > 2
    for c in chunks:
        assert c.tokens <= 100
        assert body(c).startswith("| Name | Value |\n| --- | --- |\n| item")
    all_rows = [ln for c in chunks for ln in body(c).split("\n")[2:]]
    assert all_rows == [f"| item {i} | value {i} |" for i in range(80)]


def test_large_code_block_keeps_fences_in_every_piece():
    code = "\n".join(f"print('line {i}')" for i in range(120))
    doc = make_doc(f"# Title\n\n```python\n{code}\n```\n")
    chunks = doc.chunks(max_tokens=100)
    assert len(chunks) > 2
    for c in chunks:
        assert c.tokens <= 100
        assert body(c).startswith("```python\n") and c.text.endswith("\n```")


def test_heading_inside_code_fence_is_not_a_heading():
    doc = make_doc("# Title\n\n```bash\n# install it\npip install x\n```\n\nAfter the code block.\n")
    chunks = doc.chunks()
    assert len(chunks) == 1 and chunks[0].heading_path == ["Title"]


def test_overlap_repeats_the_tail_of_the_previous_chunk():
    paras = "\n\n".join(f"Paragraph {i} " + "word " * 40 for i in range(6))
    doc = make_doc(f"# Title\n\n{paras}\n")
    chunks = doc.chunks(max_tokens=130, overlap_tokens=70)
    assert len(chunks) >= 3
    first_para_of_second = chunks[1].text.split("\n\n")[0]
    assert first_para_of_second in chunks[0].text


def test_citation_url_uses_text_fragment():
    url = citation_url("https://example.com/a#old", "## Heading\n\n- **Solar-power** basics, explained well")
    assert url.startswith("https://example.com/a#:~:text=")
    assert "%2D" in url  # '-' must be escaped inside a text directive
    assert unquote(url.split("=", 1)[1]) == "Solar-power basics, explained well"


def test_contextualized_text_has_title_and_path():
    doc = make_doc("# Title\n\n## Setup\n\n### Linux\n\nInstall the package with apt and verify it.\n")
    c = doc.chunks()[0]
    assert c.contextualized().startswith("Title\nSetup > Linux\n\n")


def test_citation_keeps_literal_characters_inside_code_and_escapes():
    from urllib.parse import unquote
    from pagequorum.chunk import citation_url
    url = citation_url("https://d.example/x", "`json.dump(obj, fp, *, skipkeys=False)`\n\nSerialize *obj*.")
    assert unquote(url.split("#:~:text=")[1]) == "json.dump(obj, fp, *, skipkeys=False)"
    url = citation_url("https://d.example/x", r"Use \*args and **keyword** arguments in calls")
    assert unquote(url.split("#:~:text=")[1]) == "Use *args and keyword arguments in"


def test_citation_link_uses_the_text_a_reader_sees():
    from pagequorum.chunk import citation_url

    url = citation_url("https://ex.com/p", "Source code: Lib/json/\\_\\_init\\_\\_.py is the module file")
    assert url.endswith("#:~:text=Source%20code%3A%20Lib%2Fjson%2F__init__.py%20is%20the%20module")
