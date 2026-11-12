"""Renderer behaviour, checked with every block forced to 'kept'."""

from pagequorum import dom
from pagequorum.markdown import RenderOptions, render


def md(body_html: str, **opts) -> str:
    tree = dom.parse_html(f"<html><body>{body_html}</body></html>")
    body = dom.body_of(tree)
    dom.clean(body)
    blocks = dom.segment(body)
    text, _ = render(body, blocks, {b.index for b in blocks}, "https://example.com/a/", RenderOptions(**opts))
    return text


def test_table_rowspan_is_repeated_and_colspan_padded():
    out = md("""<table><tr><th>Region</th><th>Q1</th><th>Q2</th></tr>
        <tr><td rowspan="2">North</td><td>1</td><td>2</td></tr>
        <tr><td>3</td><td>4</td></tr>
        <tr><td colspan="2">Total</td><td>10</td></tr></table>""")
    assert out == ("| Region | Q1 | Q2 |\n| --- | --- | --- |\n| North | 1 | 2 |\n"
                   "| North | 3 | 4 |\n| Total |  | 10 |")


def test_pipes_in_cells_are_escaped():
    out = md("<table><tr><th>Op</th><th>Meaning</th></tr><tr><td>a|b</td><td>or</td></tr>"
             "<tr><td>x</td><td>y</td></tr><tr><td>p</td><td>q</td></tr><tr><td>m</td><td>n</td></tr>"
             "<tr><td>s</td><td>t</td></tr></table>")
    assert "| a\\|b | or |" in out


def test_nested_lists_and_loose_items():
    out = md("<ul><li>One<ul><li>One A</li><li>One B</li></ul></li><li><p>Two as paragraph</p></li></ul>")
    assert out == "- One\n  - One A\n  - One B\n- Two as paragraph"


def test_ordered_list_respects_start():
    assert md('<ol start="4"><li>four</li><li>five</li></ol>') == "4. four\n5. five"


def test_code_fence_longer_than_backticks_inside_and_line_numbers_dropped():
    out = md('<pre class="language-md"><span class="linenos">1</span>use ```code``` here\n'
             '<span class="linenos">2</span>second line</pre>')
    assert out == "````md\nuse ```code``` here\nsecond line\n````"


def test_pre_with_br_line_breaks():
    assert md("<pre>line one<br>line two</pre>") == "```\nline one\nline two\n```"


def test_inline_formatting_and_spacing():
    out = md("<p>This is <b> bold </b>and <em>italic</em> with <code>x = 1</code>.</p>")
    assert out == "This is **bold** and *italic* with `x = 1`."


def test_formatting_can_be_disabled():
    assert md("<p>a <b>b</b> <code>c</code></p>", formatting=False) == "a b c"


def test_blockquote_and_headings():
    out = md("<h2>Title here</h2><blockquote><p>Quoted line one.</p><p>Quoted two.</p></blockquote>")
    assert out == "## Title here\n\n> Quoted line one.\n>\n> Quoted two."


def test_leading_markdown_characters_are_escaped():
    out = md("<p># not a heading</p><p>1. not a list</p><p>- not a bullet</p>")
    assert out == "\\# not a heading\n\n\\1. not a list\n\n\\- not a bullet"


def test_double_br_splits_paragraphs():
    assert md("<div>first line<br>second line<br><br>new paragraph</div>") == \
        "first line\nsecond line\n\nnew paragraph"


def test_links_and_images_options():
    html = '<p>See <a href="/docs">the docs</a> <img src="i.png" alt="diagram"></p>'
    assert md(html) == "See the docs"
    assert md(html, links=True, images=True) == \
        "See [the docs](https://example.com/docs) ![diagram](https://example.com/a/i.png)"


def test_layout_table_is_not_rendered_as_a_table():
    out = md('<table role="presentation"><tr><td><p>Left column text.</p></td><td><p>Right column.</p></td></tr></table>')
    assert "|" not in out and "Left column text." in out and "Right column." in out


def test_permalink_anchors_are_removed_but_in_page_links_kept():
    out = md('<h2>Basic Usage<a class="headerlink" href="#basic-usage" title="Link to this heading">¶</a></h2>'
             '<h3 id="x">Setup <a class="header-anchor" href="#x">#</a></h3>'
             '<p>See <a href="#basic-usage">the usage section</a> first.</p>')
    assert out == "## Basic Usage\n\n### Setup\n\nSee the usage section first."


def test_sphinx_signature_is_one_code_span():
    html = ('<dl class="py function"><dt class="sig sig-object py" id="json.dump">'
            '<span class="sig-prename descclassname"><span class="pre">json.</span></span>'
            '<span class="sig-name descname"><span class="pre">dump</span></span><span class="sig-paren">(</span>'
            '<em class="sig-param"><span class="n"><span class="pre">obj</span></span></em>, '
            '<em class="sig-param"><span class="o"><abbr title="Keyword-only"><span class="pre">*</span></abbr></span></em>, '
            '<em class="sig-param"><span class="o"><span class="pre">**</span></span><span class="n"><span class="pre">kw</span></span></em>'
            '<span class="sig-paren">)</span><a class="headerlink" href="#json.dump">¶</a></dt>'
            '<dd><p>Serialize <em>obj</em>.</p></dd></dl>')
    assert md(html) == "`json.dump(obj, *, **kw)`\n\nSerialize *obj*."
    assert md(html, plain=True) == "json.dump(obj, *, **kw)\n\nSerialize obj."


def test_admonition_label_joins_its_first_paragraph():
    sphinx = ('<div class="admonition note"><p class="admonition-title">Note</p>'
              '<p>The term is ambiguous.</p><p>Second paragraph.</p></div>')
    assert md(sphinx) == "**Note:** The term is ambiguous.\n\nSecond paragraph."
    assert md(sphinx, plain=True) == "Note: The term is ambiguous.\n\nSecond paragraph."
    github = ('<div class="markdown-alert markdown-alert-warning"><p class="markdown-alert-title">'
              '<svg></svg>Warning</p><p>Back up first.</p></div>')
    assert md(github) == "**Warning:** Back up first."
    # A label cannot go in front of a code fence or a list: it stays on its own line.
    code = '<div class="admonition tip"><p class="admonition-title">Tip</p><pre>pip install x</pre></div>'
    assert md(code) == "**Tip:**\n\n```\npip install x\n```"


def test_footnote_label_joins_its_text_and_back_link_is_dropped():
    out = md('<aside class="footnote brackets" id="fn1" role="doc-footnote"><span class="label">'
             '<span class="fn-bracket">[</span><a role="doc-backlink" href="#r1">1</a><span class="fn-bracket">]</span>'
             '</span><p>As noted in the errata. <a href="#r1" role="doc-backlink">↩︎</a></p></aside>')
    assert out == "[1] As noted in the errata."


def test_wiki_edit_links_and_navbars_are_removed():
    out = md('<div class="mw-heading mw-heading2"><h2 id="h">इतिहास</h2><span class="mw-editsection">'
             '<span class="mw-editsection-bracket">[</span><a href="/w/index.php?action=edit">संपादित करें</a>'
             '<span class="mw-editsection-bracket">]</span></span></div><p>पहला अनुच्छेद।</p>'
             '<div class="navbar plainlinks hlist"><ul><li><a href="/t">v</a></li><li><a href="/e">e</a></li></ul></div>')
    assert out == "## इतिहास\n\nपहला अनुच्छेद।"


def test_picture_cell_spanning_rows_is_not_repeated():
    out = md('<table><tr><th>Rank</th><th>City</th><th>Photo</th></tr>'
             '<tr><td>1</td><td>Mumbai</td><td rowspan="3"><img src="m.jpg"> Mumbai skyline</td></tr>'
             '<tr><td>2</td><td>Delhi</td></tr><tr><td>3</td><td>Pune</td></tr></table>')
    assert out.count("Mumbai skyline") == 1
    assert "| 2 | Delhi |  |" in out
