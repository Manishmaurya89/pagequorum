"""Draws docs/images/f1-{light,dark}.svg, the README's accuracy chart, from
bench/results/compare.md (python3 docs/make_chart.py; standard library only).
Firecrawl was not run here: its number is the independent run cited in the README."""

import re
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIRECRAWL = ("Firecrawl (onlyMainContent) †", 0.785)
NAMES = {  # README names for the rows of compare.md that the chart shows
    "rs-trafilatura (saved output)": "rs-trafilatura",
    "Zyte AutoExtract (commercial; saved Nov 2019 output)": "Zyte AutoExtract (commercial)",
    "**pagequorum**": "pagequorum",
    "trafilatura 2.0 (saved output)": "trafilatura 2.0",
    "Diffbot (commercial; saved Nov 2019 output)": "Diffbot (commercial)",
    "Mozilla Readability.js (saved output)": "Mozilla Readability.js",
    "Crawl4AI 0.9.5 `fit_markdown` (our run)": "Crawl4AI 0.9.5 fit_markdown",
}
THEMES = {  # page: GitHub's background, for the halo that keeps values readable over gridlines
    "light": dict(page="#ffffff", ink="#1f2328", muted="#59636e", grid="#d1d9e0", bar="#958c7c", accent="#0e6a4f"),
    "dark": dict(page="#0d1117", ink="#f0f6fc", muted="#9198a1", grid="#3d444d", bar="#6f6858", accent="#8ddcb7"),
}


def rows() -> list[tuple[str, float]]:
    found = []
    for line in (ROOT / "bench/results/compare.md").read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and cells[0] in NAMES:
            found.append((NAMES[cells[0]], float(cells[1])))
    assert len(found) == len(NAMES), "compare.md changed: update NAMES"
    return sorted(found + [FIRECRAWL], key=lambda r: -r[1])


def svg(theme: dict) -> str:
    data = rows()
    left, right, top, row, bar_h = 232, 70, 74, 30, 18
    width = 760
    plot = width - left - right
    height = top + row * len(data) + 46
    sans = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
    mono = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
    t = theme
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
           f'role="img" aria-labelledby="title desc">',
           '<title id="title">Held-out F1 on the article-extraction-benchmark</title>',
           '<desc id="desc">' + escape("; ".join(f"{n}: {v:.3f}" for n, v in data)) + "</desc>",
           f'<text x="0" y="22" font-family="{sans}" font-size="17" font-weight="600" fill="{t["ink"]}">'
           "Held-out F1, 86 news and blog pages</text>",
           f'<text x="0" y="44" font-family="{sans}" font-size="13" fill="{t["muted"]}">'
           "article-extraction-benchmark test split · same pages and scorer for every tool · higher is better</text>"]
    for tick in (0, 0.25, 0.5, 0.75, 1.0):
        x = left + plot * tick
        out.append(f'<line x1="{x:.1f}" y1="{top - 8}" x2="{x:.1f}" y2="{top + row * len(data) - 6}" '
                   f'stroke="{t["grid"]}" stroke-width="1"/>')
        out.append(f'<text x="{x:.1f}" y="{top + row * len(data) + 12}" text-anchor="middle" '
                   f'font-family="{mono}" font-size="11" fill="{t["muted"]}">{tick:g}</text>')
    for i, (name, value) in enumerate(data):
        y = top + i * row
        ours = name == "pagequorum"
        w = plot * value
        weight = ' font-weight="700"' if ours else ""
        out.append(f'<text x="{left - 12}" y="{y + 13.5}" text-anchor="end" font-family="{sans}" font-size="13"'
                   f'{weight} fill="{t["ink"]}">{escape(name)}</text>')
        # bar: square at the baseline, 4px rounded at the data end
        out.append(f'<path d="M{left},{y} h{w - 4:.1f} a4,4 0 0 1 4,4 v{bar_h - 8} a4,4 0 0 1 -4,4 h{-(w - 4):.1f} z" '
                   f'fill="{t["accent"] if ours else t["bar"]}"/>')
        out.append(f'<text x="{left + w + 8:.1f}" y="{y + 13.5}" font-family="{mono}" font-size="12"{weight} '
                   f'fill="{t["ink"]}" stroke="{t["page"]}" stroke-width="5" paint-order="stroke">{value:.3f}</text>')
    out.append(f'<text x="0" y="{height - 6}" font-family="{sans}" font-size="11.5" fill="{t["muted"]}">'
               "† not run here: an independent run with the same scorer (robertz.co, October 2026). "
               "Details and caveats under Accuracy.</text>")
    out.append("</svg>")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    for name, theme in THEMES.items():
        (ROOT / f"docs/images/f1-{name}.svg").write_text(svg(theme), encoding="utf-8")
    print("\n".join(f"{n}: {v}" for n, v in rows()))
