"""Optional JavaScript rendering with Playwright (``pip install 'pagequorum[render]'``
then ``playwright install chromium``).

Only used when a page's static HTML has almost no text, or when asked to. robots.txt
is checked by the Fetcher before a page is ever rendered.
"""

from __future__ import annotations

import re

APP_SHELL = re.compile(
    r"""id=["'](root|app|__next|__nuxt|svelte|___gatsby)["']|<noscript[^>]*>[^<]*(enable|turn on)\s+javascript""",
    re.I,
)


class RenderUnavailable(RuntimeError):
    pass


def looks_js_rendered(html: str, page_words: int, kept_words: int) -> bool:
    """Heuristic: an app shell with (almost) no text in the static HTML."""
    if page_words < 50:
        return True
    return kept_words < 40 and bool(APP_SHELL.search(html[:200_000]))


def render_html(url: str, user_agent: str | None = None, timeout: float = 30.0) -> tuple[str, str]:
    """Return ``(html, final_url)`` after the page's scripts have run."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RenderUnavailable(
            "JavaScript rendering needs Playwright: pip install 'pagequorum[render]' && playwright install chromium"
        ) from exc
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(user_agent=user_agent) if user_agent else browser.new_page()
                try:
                    page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
                except PlaywrightTimeout:
                    pass  # long-polling pages never go idle; take what has rendered
                return page.content(), page.url
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise RenderUnavailable(f"rendering failed: {exc}") from exc
