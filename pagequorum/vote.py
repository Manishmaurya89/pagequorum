"""Block-level voting: which blocks of the page does each extractor consider content?

Each voter's output is *aligned* with the page: the page's tokens (all blocks
concatenated in document order) and the voter's tokens are matched with
``difflib.SequenceMatcher``, which finds the longest common runs in order. A
block's coverage by a voter is the fraction of the block's tokens that fall in
matched runs.

Alignment matters. An order-free check (is this phrase anywhere in the output?)
wrongly credits a menu item, breadcrumb or image caption that repeats words of
the article. With alignment, each output token can vouch for one position on the
page only, and only in the right order.

A block is kept when the weighted sum of covering voters reaches the threshold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .dom import Block
from .text import tokenize

COVERAGE = 0.5  # a voter covers a block when it matches at least half of its tokens


@dataclass
class VoterIndex:
    name: str
    tokens: list[str] = field(default_factory=list)

    @property
    def n_tokens(self) -> int:
        return len(self.tokens)


def build_index(name: str, lines: list[str]) -> VoterIndex:
    tokens: list[str] = []
    for line in lines:
        tokens.extend(tokenize(line))
    return VoterIndex(name, tokens)


def align(blocks: list[Block], stream: list[str]) -> list[float]:
    """Coverage of each block (0..1) by an in-order alignment with ``stream``."""
    page: list[str] = []
    owner: list[int] = []
    for i, b in enumerate(blocks):
        page.extend(b.tokens)
        owner.extend([i] * len(b.tokens))
    matched = [0] * len(blocks)
    if page and stream:
        sm = SequenceMatcher(None, page, stream, autojunk=True)
        for a, _, size in sm.get_matching_blocks():
            if size == 1 and len(blocks[owner[a]].tokens) > 2:
                continue  # a lone shared word ("the", "and") is not evidence
            for k in range(a, a + size):
                matched[owner[k]] += 1
    return [m / len(b.tokens) if b.tokens else 0.0 for m, b in zip(matched, blocks)]


def cast_votes(blocks: list[Block], indices: list[VoterIndex]) -> None:
    """Fill ``block.votes``: the coverage of each block by each voter (0..1)."""
    content = [b for b in blocks if b.tokens]
    for b in content:
        b.votes = {}
    for idx in indices:
        for b, cov in zip(content, align(content, idx.tokens)):
            b.votes[idx.name] = cov


def score(block: Block, weights: dict[str, float], active: list[str]) -> float:
    """Weighted number of active voters that cover the block."""
    return sum(weights.get(n, 1.0) for n in active if block.votes.get(n, 0.0) >= COVERAGE)
