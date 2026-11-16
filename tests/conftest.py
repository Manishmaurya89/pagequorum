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


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    """Tests never touch the network: private-address checks see made-up hosts as public,
    and only `localhost` resolves (to loopback), as it does everywhere."""
    import socket

    def fake_getaddrinfo(host, *args, **kwargs):
        if host == "localhost":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 0))]

    monkeypatch.setattr("pagequorum.fetch.socket.getaddrinfo", fake_getaddrinfo)
