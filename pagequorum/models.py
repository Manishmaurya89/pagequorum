"""Data returned by pagequorum: a Document per page and Chunks for retrieval."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Chunk:
    id: str
    url: str
    index: int
    text: str
    heading_path: list[str]
    tokens: int
    start: int  # character offsets into Document.markdown
    end: int
    title: str | None = None
    citation_url: str | None = None  # deep link with a #:~:text= fragment when possible

    def contextualized(self) -> str:
        """Text with its title and section path prepended, for embedding."""
        head = [self.title] if self.title else []
        path = [h for h in self.heading_path if h != self.title]
        if path:
            head.append(" > ".join(path))
        return ("\n".join(head) + "\n\n" + self.text) if head else self.text

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Document:
    url: str | None
    markdown: str  # "# Title" followed by the body
    text: str  # plain-text body, no title
    title: str | None = None
    author: str | None = None
    date: str | None = None
    language: str | None = None
    description: str | None = None
    site_name: str | None = None
    canonical_url: str | None = None
    links: list[dict[str, str]] = field(default_factory=list)
    extraction: dict[str, Any] = field(default_factory=dict)
    fetch: dict[str, Any] = field(default_factory=dict)

    @property
    def word_count(self) -> int:
        return int(self.extraction.get("kept_words", 0))

    def chunks(self, max_tokens: int = 512, overlap_tokens: int = 0, token_counter=None) -> list[Chunk]:
        from .chunk import chunk_document

        return chunk_document(self, max_tokens=max_tokens, overlap_tokens=overlap_tokens,
                              token_counter=token_counter)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
