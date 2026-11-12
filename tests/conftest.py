from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def article_html() -> str:
    return (FIXTURES / "article.html").read_text(encoding="utf-8")


@pytest.fixture
def docs_html() -> str:
    return (FIXTURES / "docs.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Fetcher politeness and retry delays should not slow the test suite."""
    import pagequorum.fetch as fetch

    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
