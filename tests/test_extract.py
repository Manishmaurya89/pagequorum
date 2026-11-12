import pagequorum as pq
from pagequorum import dom

URL = "https://example.com/solar/how-it-works"


def test_article_boilerplate_removed_and_structure_kept(article_html):
    doc = pq.extract(article_html, url=URL)
    md = doc.markdown
    for boilerplate in ("About us", "cookies", "Related stories", "Battery prices", "All rights reserved", "Privacy"):
        assert boilerplate not in md, boilerplate
    assert md.startswith("# How Solar Panels Work\n")
    assert md.count("How Solar Panels Work") == 1  # the page H1 is not repeated under the title
    assert "## Efficiency" in md and "## Installing them" in md
    assert "| Panel type | Efficiency | Cost per watt |\n| --- | --- | --- |\n| Monocrystalline | 20-23% | $1.00 |" in md
    assert "1. Site survey and shading analysis of the roof\n2. Mounting" in md
    assert "```python\ndef output(kw, hours):\n    return kw * hours  # kWh per day\n```" in md
    assert doc.title == "How Solar Panels Work"
    assert doc.language == "en"
    assert doc.extraction["method"] == "quorum"


def test_docs_page_keeps_code_tabs_tables_and_drops_navigation(docs_html):
    doc = pq.extract(docs_html, url="https://docs.acme.dev/sdk/retries/")
    md = doc.markdown
    assert doc.title == "Configuring retries"
    assert doc.canonical_url == "https://docs.acme.dev/sdk/retries/"
    for nav in ("Quickstart", "Timeouts", "Logging", "GitHub", "Copyright", "Previous:"):
        assert nav not in md, nav
    assert "| `max_attempts` | 3 | Total attempts including the first one |" in md
    assert "```python\nfrom acme import Client, RetryPolicy" in md
    assert '```js\nimport { Client, RetryPolicy } from "acme";' in md  # the hidden tab panel
    assert "### Per-request overrides" in md
    assert "idempotency key" in md


def test_text_is_plain_and_has_no_title(article_html):
    doc = pq.extract(article_html, url=URL)
    assert not doc.text.startswith("How Solar Panels Work")
    assert "|" in doc.text  # table cells separated, but no Markdown table syntax
    assert "---" not in doc.text and "```" not in doc.text and "##" not in doc.text


def test_text_owned_by_a_div_around_a_paragraph_stays_in_order():
    html = """<html><body><article><div>First part of the story, which is long enough to be content.
    <p>Second part sits in its own paragraph and is also long enough to matter here.</p>
    Third part follows the paragraph directly inside the same div element of the page.</div></article></body></html>"""
    blocks = [b for b in dom.segment(dom.body_of(dom.parse_html(html))) if b.tokens]
    assert [b.text.split()[0] for b in blocks] == ["First", "Second", "Third"]
    assert [(b.tag, b.segment) for b in blocks] == [("div", 0), ("p", 0), ("div", 1)]


def test_paragraphs_wrapped_in_inline_span_are_rendered():
    # Regression: CMSs (HubSpot) wrap block content in <span>; it must not vanish.
    paras = "".join(f"<p>Paragraph number {i} talks about the topic in enough words to count as text.</p>"
                    for i in range(6))
    html = f"<html><body><nav><a href='/'>Home</a></nav><article><span class='wrapper'>{paras}</span></article></body></html>"
    doc = pq.extract(html, url=URL)
    for i in range(6):
        assert f"Paragraph number {i}" in doc.markdown


def test_hidden_elements_are_removed_but_tab_panels_kept():
    html = """<html><body><div style="display:none">Secret hidden text here</div>
    <span class="sr-only">Opens in a new window</span>
    <div aria-hidden="true">Decorative duplicate</div>
    <div role="tabpanel" hidden><p>Tab content is real content</p></div></body></html>"""
    body = dom.body_of(dom.parse_html(html))
    dom.clean(body)
    text = body.text_content()
    assert "Secret" not in text and "Opens in a new window" not in text and "Decorative" not in text
    assert "Tab content is real content" in text


def test_links_resolve_against_base_href():
    html = """<html><head><base href="https://cdn.example.org/docs/"></head><body><article>
    <p>Read the <a href="guide.html">full guide</a> for details about configuration of the tool and its options.</p>
    <p>Another paragraph with enough words so that every extractor agrees this is the content.</p>
    <p>A third paragraph keeps the article long enough for the voters to recognise it easily.</p>
    </article></body></html>"""
    doc = pq.extract(html, url="https://example.com/page", options=pq.ExtractOptions(links=True))
    assert "[full guide](https://cdn.example.org/docs/guide.html)" in doc.markdown
    assert {"text": "full guide", "url": "https://cdn.example.org/docs/guide.html"} in doc.links


def test_page_without_content_falls_back_and_warns():
    html = "<html><body><ul><li><a href='/a'>Alpha</a></li><li><a href='/b'>Beta</a></li></ul></body></html>"
    doc = pq.extract(html, url=URL)
    assert doc.extraction["method"] in ("union-fallback", "container-fallback")
    assert any("JavaScript" in w for w in doc.extraction["warnings"])


def test_bytes_input_is_decoded_with_declared_charset():
    html = '<html><head><meta charset="windows-1252"></head><body><article>' + \
        "<p>Café society met every evening to discuss naïve theories about art and politics.</p>" * 4 + \
        "</article></body></html>"
    doc = pq.extract(html.encode("cp1252"), url=URL)
    assert "Café society" in doc.text and "naïve" in doc.text


def test_explain_reports_votes(article_html):
    rows = pq.explain(article_html, url=URL)
    nav = next(r for r in rows if r["text"] == "About us")
    assert not nav["kept"] and nav["reason"] == "no votes"
    para = next(r for r in rows if r["text"].startswith("Solar panels convert"))
    assert para["kept"] and set(para["votes"]) >= {"trafilatura", "readability", "resiliparse"}


def test_consensus_is_reported(article_html):
    doc = pq.extract(article_html, url=URL)
    assert 0.5 < doc.extraction["agreement"] <= 1.0
    assert doc.extraction["voters"]["trafilatura"] == "active"


def test_two_voter_fill_only_bridges_content_between_kept_blocks():
    from pagequorum.extract import _fill_two_voter_gaps

    def blk(i, kept, score, flags=(), kind="para"):
        return dom.Block(index=i, el=None, tag="p", kind=kind, kept=kept, score=score, flags=frozenset(flags))

    blocks = [blk(0, True, 2), blk(1, False, 1), blk(2, False, 1), blk(3, True, 2),  # bridged
              blk(4, False, 1, flags={"aside"}), blk(5, True, 2),                     # sidebar: not bridged
              blk(6, False, 0), blk(7, True, 2),                                      # nobody chose it
              blk(8, False, 1, kind="caption"), blk(9, True, 2),                      # caption
              blk(10, False, 1)]                                                       # trailing: unbounded
    _fill_two_voter_gaps(blocks)
    assert [b.index for b in blocks if b.kept] == [0, 1, 2, 3, 5, 7, 9]
    assert blocks[1].reason == "two-voter-fill"


def test_footnote_referenced_from_kept_text_is_kept():
    paras = "".join(f"<p>Paragraph number {i} talks about the topic in enough words to count as text.</p>"
                    for i in range(5))
    html = f"""<html><body><nav><a href='/'>Home</a> <a href='/about'>About</a></nav><article>
    <p>JSON is inspired by JavaScript object syntax, although it is not a strict subset of it
    <a class="footnote-reference brackets" href="#rfc-errata" role="doc-noteref">[1]</a> as the errata explain.</p>
    {paras}
    <p class="rubric">Footnotes</p>
    <aside class="footnote brackets" id="rfc-errata" role="doc-footnote"><span class="label">[1]</span>
    <p>JSON permits literal U+2028 characters in strings, whereas older JavaScript does not.</p></aside>
    <aside class="footnote" id="orphan" role="doc-footnote"><span class="label">[2]</span>
    <p>A footnote nothing refers to stays out.</p></aside>
    </article></body></html>"""
    md = pq.extract(html, url=URL).markdown
    assert "[1] JSON permits literal U+2028 characters" in md
    assert "nothing refers to" not in md


def _blocks(html):
    body = dom.body_of(dom.parse_html(f"<html><body>{html}</body></html>"))
    dom.clean(body)
    return [b for b in dom.segment(body) if b.tokens]


def test_reference_lists_are_flagged():
    blocks = _blocks('<p>Text of the article.</p><div class="reflist"><ol class="references">'
                     '<li id="cite_note-1">Smith, J. (2010). A book. Archived from the original.</li></ol></div>')
    assert "references" not in blocks[0].flags
    assert "references" in blocks[1].flags


def test_heading_rescue_needs_agreed_sibling_and_kept_text_around():
    from pagequorum.extract import _rescue_headings
    blocks = _blocks("<h2>Subtitle above the article</h2><p>Intro paragraph.</p><h2>History</h2>"
                     "<p>History paragraph.</p><h2>Geography</h2><p>Geography paragraph.</p>"
                     "<h2>Related stories</h2><ul><li>Other story</li></ul>")
    sub, intro, hist, hist_p, geo, geo_p, rel, other = blocks
    for b in (intro, hist_p, geo_p, geo):
        b.kept = True
    for b in (sub, hist, rel):
        b.score = 2.0  # 2 of 3 voters
    _rescue_headings(blocks, total=3.0)
    assert hist.kept and hist.reason == "heading of kept section"
    assert not sub.kept  # above the article: nothing kept before it
    assert not rel.kept  # its section has no kept text


def test_list_announced_by_a_colon_is_kept():
    from pagequorum.extract import _rescue_lead_ins
    blocks = _blocks("<p>Six climate types are found in India:</p><ul><li>Alpine</li><li>Humid subtropical</li></ul>"
                     "<p>Another paragraph.</p><ul><li>Unannounced item</li></ul>")
    lead, a, b, para, other = blocks
    lead.kept = para.kept = True
    a.score = b.score = other.score = 1.0
    _rescue_lead_ins(blocks)
    assert a.kept and b.kept
    assert not other.kept


def test_links_come_only_from_kept_content():
    paras = "".join(f"<p>Paragraph {i} explains solar power in enough words to be content, see "
                    f"<a href='/topic/{i}'><b>topic {i}</b></a> for details.</p>" for i in range(5))
    html = (f"<html><body><nav><a href='/'>Home</a> <a href='/about'>About us</a></nav>"
            f"<article><h1>Solar</h1>{paras}</article><footer><a href='/privacy'>Privacy</a></footer></body></html>")
    doc = pq.extract(html, url=URL)
    assert [l["url"] for l in doc.links] == [f"https://example.com/topic/{i}" for i in range(5)]
    assert doc.links[0]["text"] == "topic 0"  # plain text, no Markdown
