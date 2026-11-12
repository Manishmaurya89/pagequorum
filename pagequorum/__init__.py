"""pagequorum: accurate web-page extraction for RAG.

    import pagequorum as pq

    doc = pq.scrape("https://example.com/article")
    print(doc.markdown)
    for chunk in doc.chunks(max_tokens=512):
        print(chunk.heading_path, chunk.citation_url)
"""

__version__ = "0.1.0"

from .crawl import CrawlEvent, crawl  # noqa: E402
from .extract import ExtractOptions, explain, extract, extract_explained  # noqa: E402
from .fetch import Fetcher, FetchResult  # noqa: E402
from .models import Chunk, Document  # noqa: E402
from .scrape import ScrapeError, scrape, scrape_explained  # noqa: E402

__all__ = [
    "Chunk", "CrawlEvent", "Document", "ExtractOptions", "FetchResult", "Fetcher",
    "ScrapeError", "crawl", "explain", "extract", "extract_explained", "scrape", "scrape_explained",
    "__version__",
]
