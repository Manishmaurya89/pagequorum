# pagequorum

**Accurate web-page extraction for RAG.** Several proven extractors vote on every
block of a page; only what they agree on is kept. The result keeps its structure
(headings, tables, code, lists) as clean Markdown, split into citation-ready chunks.

```bash
pagequorum scrape https://docs.python.org/3/library/json.html
pagequorum crawl https://www.python-httpx.org/ -o out/ --max-pages 50
pagequorum serve        # local playground + HTTP API: scrape, crawl, map
pagequorum mcp          # let AI assistants (Claude, Cursor, VS Code) read pages with it
```

**Online demo:** https://manishmaurya89.github.io/pagequorum/ (recorded examples; run it
locally to scrape anything else).

## Why

Getting the main text out of a web page is the step most RAG pipelines get
quietly wrong. Navigation menus, cookie banners, "related stories" and footers
leak into the index, while code blocks, tables and headings get flattened or dropped.
Each popular extractor has its own blind spots, and none tells you when it failed.

pagequorum does not add yet another heuristic. It asks several independent
extractors and keeps only the parts they agree on:

| voter | what it is |
|---|---|
| [trafilatura](https://github.com/adbar/trafilatura) | among the strongest open-source extractors on public benchmarks |
| [readability](https://github.com/buriy/python-readability) | Python port of Mozilla Readability (Firefox reader view) |
| [resiliparse](https://github.com/chatnoir-eu/chatnoir-resiliparse) | main-content extraction from the Webis group (ChatNoir search engine) |
| JSON-LD `articleBody` | the publisher's own article text, when the page has it |
| [jusText](https://github.com/miso-belica/jusText) | available, off by default (it lowered accuracy on the dev split) |

## How it works

1. **Fetch** politely: robots.txt (RFC 9309), per-host delay and `Crawl-delay`,
   retries with `Retry-After`, size limit, content-type check, optional cache.
2. **Decode** like a browser: BOM → HTTP charset → `<meta>` → detection, with
   WHATWG label mapping. A `charset=ISO-8859-1` header on a UTF-8 page is
   corrected, so you don't get `â€™` instead of `’`.
3. **Clean and segment** the DOM into blocks: paragraphs, headings, list items;
   code blocks and data tables stay whole. Hidden elements, scripts and form
   controls are removed. Inactive tab panels are kept, since they hold real
   content like the "JavaScript" variant of a code sample.
4. **Vote.** Each extractor's output is *aligned* with the page in document
   order (`difflib.SequenceMatcher` over tokens). A block counts as covered by a
   voter when at least half of its words are matched. Alignment matters: an
   order-free "is this phrase anywhere in the output?" check wrongly credits menu
   items and captions that repeat words of the article.
5. **Decide.** A block needs at least 70% of the active voters' weight: all three
   main voters agree, or 3 of 4 when JSON-LD is present. These safeguards keep this
   strict rule from losing content:
   - A voter that covers less than half of what the others cover is *set aside*
     for that page, because it picked a sub-section (Readability does this on long
     Sphinx docs pages).
   - A code block or table that sits directly between kept blocks is rescued if
     at least one voter kept it.
   - A section heading most voters chose is kept when it sits inside kept text
     and the voters agreed on another heading of the same level; a list, table or
     code block that a kept sentence announces with a colon is kept if a voter
     chose it. Headings left with an empty section are dropped.
   - When a voter was set aside and only two remain, a run of blocks one of them
     chose that sits between kept blocks is kept, unless it is navigation,
     sidebar, footer or a caption. Two voters cannot outvote each other, and
     a single miss would otherwise drop content, such as a whole API entry.
6. **Render** the kept blocks from the same DOM as Markdown: `#` headings, nested
   lists, fenced code with language, GitHub tables (rowspans repeated so each row
   stands alone), block quotes. The page's H1 becomes the title and is not
   repeated in the body. Docs-site details are handled too: permalink `¶` marks
   are removed, API signatures become one code span, "Note"/"Warning" labels stay
   attached to their paragraph, and footnotes that the kept text refers to are kept.
   Wiki pages lose their "[edit]" links and v·t·e navbars, and reference/citation
   lists are left out (they are bibliography, not article text).
7. **Chunk** by headings. A table is never split mid-row (its header repeats in
   each piece) and a code fence never mid-line. Each chunk records its heading
   path, its exact character span in the Markdown, and a `#:~:text=` deep link
   that opens the live page scrolled to the passage.

Every page also reports a **consensus score**: the share of words that every
active voter agreed on. Low consensus marks pages worth a human look.

## Accuracy

Measured on the [article-extraction-benchmark](https://github.com/scrapinghub/article-extraction-benchmark):
181 real news and blog pages with human-labelled article text. The scoring is
identical to the benchmark's own (token 4-gram precision/recall per page). Pages are
split by id into **dev** (95 pages, the only ones used for every design and tuning
decision) and **test** (86 held-out pages).

| system | F1 (all) | **F1 test (held out)** | precision (all) | recall (all) | exact match |
|---|---|---|---|---|---|
| **pagequorum** | **0.970** ± 0.006 | **0.963** | **0.969** ± 0.006 | 0.970 ± 0.007 | **0.436** |
| trafilatura 2.3 | 0.960 ± 0.005 | 0.963 | 0.942 ± 0.008 | 0.978 ± 0.006 | 0.293 |
| readability-lxml 0.9 | 0.954 ± 0.007 | 0.950 | 0.935 ± 0.008 | 0.974 ± 0.007 | 0.298 |
| resiliparse 2.0 | 0.927 ± 0.005 | 0.928 | 0.879 ± 0.008 | 0.981 ± 0.004 | 0.033 |
| jusText 3.0 | 0.823 ± 0.015 | 0.813 | 0.858 ± 0.016 | 0.791 ± 0.024 | 0.039 |

What this does and does not show:

- On held-out pages pagequorum **ties** trafilatura on F1. The "all pages"
  lead includes the dev pages it was tuned on, so don't read it as a win.
- The real gain is **precision**: on held-out pages 0.962 vs 0.947, i.e. less
  boilerplate in your index. The text is also exactly the human-labelled article
  body on **43%** of held-out pages vs 34%. The price is slightly lower recall
  (0.964 vs 0.978). Counting the dev pages too inflates the gap (0.969 vs 0.942,
  44% vs 29%), because pagequorum was tuned on them.
- The consensus score separates failures: mean 0.75 on pages with F1 < 0.8,
  0.89 on the rest.
- The benchmark contains news and blogs only. Documentation pages, a major RAG
  source, were checked by hand: Python docs, MkDocs sites, Wikipedia in English
  and Hindi, and the fixtures in `tests/fixtures/`. There is no labelled
  benchmark for them yet.

Reproduce: `uv run python bench/download_aeb.py && uv run python bench/aeb.py`.

### Compared with other tools

Same pages, same scorer, same split. The commercial and other extractors are scored
from the outputs saved in the benchmark repository (scoring them here reproduces the
benchmark's published numbers exactly, which checks the scorer). Crawl4AI 0.9.5 was
run by us on the same HTML with its documented `PruningContentFilter`, and its
Markdown was turned into plain text before scoring, so link URLs are not counted
against it.

| system | **F1 test (held out)** | F1 (all) | precision (all) | recall (all) | exact match |
|---|---|---|---|---|---|
| rs-trafilatura | **0.974** | 0.970 ± 0.004 | 0.951 | 0.990 | 0.287 |
| Zyte AutoExtract (commercial, 2019) | 0.972 | 0.970 ± 0.005 | **0.984** | 0.956 | **0.470** |
| **pagequorum** | 0.963 | 0.970 ± 0.006 | 0.969 | 0.970 | 0.436 |
| trafilatura 2.0 | 0.959 | 0.958 ± 0.006 | 0.938 | 0.978 | 0.293 |
| Diffbot (commercial, 2019) | 0.950 | 0.951 ± 0.010 | 0.958 | 0.944 | 0.348 |
| Mozilla Readability.js | 0.947 | 0.947 ± 0.005 | 0.914 | 0.982 | 0.166 |
| Crawl4AI 0.9.5 `fit_markdown` | 0.799 | 0.805 ± 0.012 | 0.685 | 0.976 | 0.006 |
| Crawl4AI 0.9.5 `raw_markdown` | 0.659 | 0.666 ± 0.015 | 0.502 | 0.990 | 0.000 |

- pagequorum is **not** the most accurate extractor here. On held-out pages,
  rs-trafilatura and Zyte's commercial AutoExtract are about 0.01 F1 ahead, and
  AutoExtract keeps less boilerplate (precision 0.984 vs 0.969).
- Crawl4AI keeps nearly all of the article (recall 0.976) but also a lot that is not
  article: "you may also like" links, comment boxes and the like. Its median output
  is 797 words where the labelled article has 529 (pagequorum: 539). Adding its
  `excluded_tags` clean-up option did not help (0.794 held out).
- Firecrawl was not run here. An independent run with the same scorer and the same
  Markdown clean-up reported F1 0.785 (precision 0.660) with `onlyMainContent`
  ([robertz.co, October 2026](https://robertz.co/knowledge/computer-science/ai-llm-engineering/web-scraper-extraction-quality/)).
- These are news and blog pages, which suit article extractors. Crawl4AI and
  Firecrawl also render JavaScript and crawl at scale, which this benchmark does
  not measure.

Reproduce: `uv run --no-project --with crawl4ai==0.9.5 python bench/run_crawl4ai.py`,
then `uv run python bench/compare.py`.
[`bench/README.md`](bench/README.md) has the method and a log of everything
tried, including what did not work.

## Install

The quickest way, with the web app (no clone needed):

```bash
pip install "pagequorum[web] @ https://github.com/Manishmaurya89/pagequorum/archive/refs/heads/main.zip"
pagequorum serve --open
```

For development:

```bash
git clone https://github.com/Manishmaurya89/pagequorum && cd pagequorum
uv sync                      # or: pip install -e .
# optional, the local web app:
pip install -e '.[web]'      # or run it with: uv run --extra web pagequorum serve
# optional, for JavaScript-rendered pages:
pip install -e '.[render]' && playwright install chromium
```

Python 3.10+.

## Use

### Command line

```bash
# Markdown to stdout
pagequorum scrape https://example.com/article

# Plain text, full JSON (document + chunks), or one JSON chunk per line
pagequorum scrape URL -f text
pagequorum scrape URL -f json
pagequorum scrape URL -f chunks --max-tokens 400 --overlap 50

# Crawl a site section (same site, under the start URL's directory). Links inside each
# page's main content are followed before menu/sidebar/footer links;
# --content-links-only never follows the latter.
pagequorum crawl https://docs.example.com/guide/ -o out/ --max-pages 200 --delay 1
#   out/pages/*.md  out/documents.jsonl  out/chunks.jsonl  out/crawl_log.jsonl

# A local file, with an explanation of every keep/drop decision
pagequorum extract page.html --url https://example.com/page --explain
```

### Web app and HTTP API

```bash
uv run --extra web pagequorum serve --open     # http://127.0.0.1:8000
```

A local playground with three modes:

| mode | what it does |
|---|---|
| **Scrape** | One page: summary (words kept, chunks, agreement, each extractor's verdict) and tabs for the clean text, the chunks, every kept/removed block with its votes, links, Markdown and the raw JSON |
| **Crawl** | A site section as a background job: live progress, a table of pages (click one to read it), skipped pages with the reason, and one-click `chunks.jsonl` / `documents.jsonl` downloads |
| **Map** | Every page URL from the sitemap and the start page's links in a few seconds, with a filter and a "Scrape →" button per URL |

"Get code" shows the request you are about to make as cURL, Python, JavaScript or
Python-library code. The same API is documented at `/redoc` (reference) and `/docs`
(try requests in the browser):

| endpoint | |
|---|---|
| `POST /api/scrape` | `{"url": ...}` or `{"html": ..., "url": ...}`, plus `max_tokens`, `overlap`, `links`, `images` |
| `POST /api/map` | `{"url": ..., "limit": 500, "use_sitemap": true, "whole_site": true}` |
| `POST /api/crawl` | `{"url": ..., "max_pages": 10, "max_depth": 2, "include": regex, "exclude": regex}` → `{"id"}` |
| `GET /api/crawl/{id}` | status, counters, and the pages done so far (`?since=n` for new ones only) |
| `GET /api/crawl/{id}/chunks.jsonl` | every chunk of the crawl; also `documents.jsonl` and `pages/{n}` |
| `DELETE /api/crawl/{id}` | stop a crawl |

The server listens on 127.0.0.1 only. It fetches any URL it is given, so don't
expose it on a public network. It also answers only requests addressed to
`localhost`, `127.0.0.1` or `[::1]`, so a website you visit can't reach it through
DNS rebinding. Links taken from pages keep only `http`, `https` and `mailto` URLs.
Crawl jobs live in memory until the server stops.

#### Online demo (static hosting)

The same page also works with no server, as a free static site. When `/api/health`
does not answer, it switches to a recorded demo: the example links replay real API
responses saved in `site/demo/`, a crawl replays page by page, and anything else
(another URL, other settings, pasted HTML) explains how to run pagequorum locally.
Code samples then point at `http://127.0.0.1:8000`.

```bash
python3 scripts/build_site.py                              # assemble site/ (no network)
python3 -m http.server -d site 8001                        # preview it
uv run --extra web python scripts/build_site.py --record   # re-record the examples
```

`.github/workflows/pages.yml` publishes it to GitHub Pages on every push to `main`.
Any other static host works too (build command `python3 scripts/build_site.py`,
output folder `site`). Scraping
never runs on the host: the recordings are plain files. Re-record after changing
the examples in the page; `tests/test_site.py` fails until you do.

### AI assistants (MCP)

`pagequorum mcp` is an [MCP](https://modelcontextprotocol.io) server, so assistants
that support MCP (Claude Code, Claude Desktop, Cursor, VS Code and others) can read
web pages with it. It offers four read-only tools:

| tool | what the assistant gets |
|---|---|
| `scrape` | one page as clean Markdown, headed by the extractors' agreement |
| `crawl` | a site section (up to 50 pages), each page as Markdown |
| `map_site` | the site's page URLs, from its sitemap and links |
| `extract_html` | clean Markdown from HTML the assistant already has (no network) |

Setup needs [uv](https://docs.astral.sh/uv/) (`uvx` fetches and runs pagequorum on
demand). For Claude Code:

```bash
claude mcp add pagequorum -- uvx --from "pagequorum[mcp] @ https://github.com/Manishmaurya89/pagequorum/archive/refs/heads/main.zip" pagequorum mcp
```

For Claude Desktop (`claude_desktop_config.json`), Cursor (`~/.cursor/mcp.json`) and
most other clients:

```json
{
  "mcpServers": {
    "pagequorum": {
      "command": "uvx",
      "args": ["--from", "pagequorum[mcp] @ https://github.com/Manishmaurya89/pagequorum/archive/refs/heads/main.zip",
               "pagequorum", "mcp"]
    }
  }
}
```

VS Code (`.vscode/mcp.json`) uses `"servers"` instead of `"mcpServers"`, with
`"type": "stdio"` added. Without uv: `pip install "pagequorum[mcp] @ <the same URL>"`
and use `"command": "pagequorum", "args": ["mcp"]`.

**Safety.** The URLs come from a model, which may have just read a page telling it to
fetch something it should not. So the tools refuse private-network addresses
(localhost, 10.x, 192.168.x, cloud metadata at 169.254.169.254, ...), including
redirects into them and host names that resolve to them. To read your own development
server, set `PAGEQUORUM_ALLOW_PRIVATE=1` in the server's `env`. robots.txt and the
per-site delay apply as usual.

### Python

```python
import pagequorum as pq

doc = pq.scrape("https://example.com/article")      # fetch + extract
doc = pq.extract(html, url="https://example.com/x")  # already have the HTML

doc.title, doc.author, doc.date, doc.language, doc.canonical_url
doc.markdown                    # "# Title" + body as Markdown
doc.text                        # plain body text
doc.links                       # links inside the kept content, absolute
doc.extraction                  # method, agreement (consensus score), each voter's status, warnings

doc, blocks = pq.extract_explained(html, url=...)  # plus every block's keep/drop decision and votes

for chunk in doc.chunks(max_tokens=512, overlap_tokens=0):
    chunk.text                  # == doc.markdown[chunk.start:chunk.end] (tables/code split excepted)
    chunk.heading_path          # ["Title", "Section", "Subsection"]
    chunk.contextualized()      # title + section path + text, ready to embed
    chunk.citation_url          # https://...#:~:text=First%20words%20of%20the%20passage

for event in pq.crawl("https://docs.example.com/guide/", max_pages=100):
    if event.document:
        ...

# Exact token counts for your model (default is a conservative bytes/4 estimate):
import tiktoken
enc = tiktoken.get_encoding("o200k_base")
doc.chunks(max_tokens=512, token_counter=lambda s: len(enc.encode(s)))
```

Options (`pq.ExtractOptions`): `links=True` keeps `[text](url)`, `images=True`
keeps `![alt](src)`, `formatting=False` drops `**bold**`/`*italic*`/`` `code` ``,
`voters=`, `weights=`, `threshold=`, `outlier_ratio=` change the voting.

## Being a good citizen

robots.txt is honoured by default. `--no-robots` exists for sites you own or have
permission to crawl, and it prints a warning. The default delay is 1 second per
host, raised when robots.txt sets a `Crawl-delay`. Check the site's terms before
crawling it.

## Limitations

- Evaluation covers news and blog articles. Documentation, forums, product pages
  and listing pages have no labelled benchmark here yet.
- JavaScript rendering (`--render`) is wired up but has **not been tested**, as
  Playwright is an optional install. Without it, app-shell pages get a warning
  instead of silently empty output.
- HTML only: PDFs and other formats are reported as skipped.
- Footnotes marked up as footnotes (Sphinx, pandoc) are kept when the text refers
  to them. Wikipedia-style citation markers such as `[1]` are kept as on the page,
  but the reference and notes lists are dropped.
- A block only one extractor chose is dropped. On Wikipedia this can lose a
  section heading or paragraph (e.g. one "Culture" heading on Hindi Wikipedia's
  भारत page) and link-heavy lists that no sentence announces.
- CJK text is tokenized per character for voting; this has not been evaluated.
- Speed: about 50 ms per page for extraction on a laptop (four extractors plus
  alignment). The crawl is sequential and polite by design.

## Roadmap

- A labelled benchmark for documentation pages (Sphinx, MkDocs, Docusaurus, GitBook).
- Site-level boilerplate removal across a crawl (blocks repeated on every page).
- Indic-language extraction evaluation (Hindi and other Devanagari news sites).
- PDF support and async crawling of many hosts at once.

## Credits

Built on trafilatura, readability-lxml, resiliparse, jusText, lxml, httpx and
Protego, and evaluated with the article-extraction-benchmark by Zyte (MIT).
The ensemble idea follows Bevendorff et al., *An Empirical Comparison of Web
Content Extraction Algorithms* (SIGIR 2023), which found that combining
extractors beats any single one.

MIT licence.
