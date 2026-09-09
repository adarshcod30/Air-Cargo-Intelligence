"""Turning passages into vectors.

Two backends behind one interface, and which one produced a vector is
recorded on every row. That matters more than it sounds: vectors from
different models are not comparable, so a corpus indexed half with one and
half with another silently returns nonsense. `embed_model` on
`document_chunk` is what lets the retriever refuse to mix them.

  titan    managed embeddings, 1024 dimensions, normalised.
  hashed   the hashing trick - deterministic, dependency-free, offline.

The fallback is not a toy. Feature hashing with a signed auxiliary hash is
a standard technique; on a corpus this size it retrieves the right passage
for keyword-shaped queries, which is most of what gets asked here. It is
weaker on paraphrase, and the console labels retrieval accordingly rather
than implying semantic search that is not happening.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

from services.common.bedrock import BedrockUnavailable, get_client
from services.common.config import SETTINGS
from services.common.logging import get_logger

log = get_logger(__name__)

DIMS = 1024
HASHED_MODEL = "hashed-tfidf-v1"

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "and", "for", "with", "from", "that", "this", "was", "are", "were",
    "has", "have", "had", "not", "but", "all", "any", "its", "per", "than",
    "into", "over", "under", "each", "such", "which", "been", "will", "would",
}


def _tokens(text: str) -> list[str]:
    """Lexical tokens only - measurements are deliberately excluded.

    Half the tokens in a published cargo table are bare figures, and every
    one of them is unique across the corpus, so IDF scores each at the cap.
    Left in, they turn a statistics page into a maximum-weight vector of
    noise and bury the airport name that makes the passage findable.

    Rarity is a good proxy for informativeness in prose and a bad one for
    measurements: nobody retrieves a passage by searching for `427602`.
    The figures are not lost - they live in fact_cargo_movement, which is
    the only place a number is allowed to come from anyway.
    """
    return [
        t for t in _TOKEN.findall(text.lower())
        if len(t) > 2 and t not in _STOP and not t.isdigit()
    ]


def _h(token: str, salt: str = "") -> int:
    return int.from_bytes(hashlib.blake2b((salt + token).encode(), digest_size=8).digest(), "big")


def embed_hashed(
    text: str, dims: int = DIMS, idf: dict[str, float] | None = None
) -> list[float]:
    """Signed feature hashing with sublinear TF and, when supplied, IDF.

    The sign from a second hash keeps unrelated tokens colliding into the
    same bucket from reinforcing each other: collisions cancel in
    expectation instead of accumulating into a spurious match.

    IDF is not optional in practice, only in signature. Without it every
    cargo query in this corpus returned the same open-data records,
    because tokens like `scheduled_domestic_cargo_in_tonne_` appear in
    every one of them and drowned out the airport name that made the query
    specific. Rarity is the whole signal here.
    """
    counts = Counter(_tokens(text))
    if not counts:
        return [0.0] * dims
    vec = [0.0] * dims
    for tok, n in counts.items():
        weight = 1.0 + math.log(n)
        if idf is not None:
            # Unseen tokens are maximally specific, not neutral: a term
            # absent from the corpus is the strongest possible signal that
            # a passage containing it is the one being asked for.
            weight *= idf.get(tok, _MAX_IDF)
        idx = _h(tok) % dims
        sign = 1.0 if (_h(tok, "sign") & 1) else -1.0
        vec[idx] += sign * weight
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


_MAX_IDF = 8.0


def compute_idf(documents: list[str]) -> dict[str, float]:
    """Smoothed IDF over the corpus, capped so one rare typo cannot dominate."""
    n = max(1, len(documents))
    df: Counter[str] = Counter()
    for doc in documents:
        df.update(set(_tokens(doc)))
    return {
        tok: min(_MAX_IDF, math.log((n + 1) / (d + 1)) + 1.0)
        for tok, d in df.items()
        if len(tok) <= 64
    }


class Embedder:
    """Picks a backend once, then stays on it for the whole corpus."""

    def __init__(
        self, prefer_managed: bool = True, idf: dict[str, float] | None = None
    ) -> None:
        self._client = get_client()
        self.model = HASHED_MODEL
        self.idf = idf
        if prefer_managed and SETTINGS.rag_enabled and self._client.available:
            self.model = self._client.embed_model_id
        log.info(f"embedder: {self.model} (idf terms: {len(idf or {})})")

    @property
    def managed(self) -> bool:
        return self.model != HASHED_MODEL

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed several passages at once, preserving order."""
        if not self.managed:
            return [embed_hashed(t, idf=self.idf) for t in texts]
        try:
            return self._client.embed_many(texts, dimensions=DIMS)
        except BedrockUnavailable as exc:
            log.warning(f"managed embeddings failed ({exc}); switching to {HASHED_MODEL}")
            self.model = HASHED_MODEL
            return [embed_hashed(t, idf=self.idf) for t in texts]

    def embed(self, text: str) -> list[float]:
        if not self.managed:
            return embed_hashed(text, idf=self.idf)
        try:
            return self._client.embed(text, dimensions=DIMS)
        except BedrockUnavailable as exc:
            # Downgrade the whole corpus rather than producing a mixture:
            # a half-Titan, half-hashed index is not searchable at all.
            log.warning(f"managed embeddings failed ({exc}); switching to {HASHED_MODEL}")
            self.model = HASHED_MODEL
            return embed_hashed(text, idf=self.idf)


def cosine(a: list[float], b: list[float]) -> float:
    """Both backends emit normalised vectors, so this is a dot product."""
    return sum(x * y for x, y in zip(a, b))
