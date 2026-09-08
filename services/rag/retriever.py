"""Finding the passage behind an answer.

This is the piece that upgrades a citation from "this came from this PDF"
to "this came from this paragraph, on this page". It does not compute any
figure and is not allowed to: retrieval supplies context and provenance,
while every number in an answer still comes from SQL over the semantic
layer. See services/semantic/nl.py for the check that enforces it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.common.config import SETTINGS
from services.common.logging import get_logger
from services.rag.embedder import Embedder, cosine

log = get_logger(__name__)

# Alphanumeric runs only: tsquery syntax characters in a user question
# would otherwise be parsed as operators rather than searched for.
_WORD = re.compile(r"[a-z0-9]+")

_embedder: Embedder | None = None


def load_idf(session: Session) -> dict[str, float]:
    """Reconstruct query-time weights from the stored frequencies.

    Recomputed from df/n with the same smoothing the indexer used, so the
    two cannot drift: if the formula changes, both sides change together.
    """
    import math

    rows = session.execute(
        text("SELECT token, document_frequency, total_documents FROM v_rag_vocab")
    ).all()
    return {
        t: min(8.0, math.log((n + 1) / (df + 1)) + 1.0)
        for t, df, n in rows
    }


def embedder(session: Session | None = None) -> Embedder:
    """Query embedder, sharing the corpus vocabulary with the indexer.

    Built lazily and cached: loading the vocabulary is one query, but doing
    it per request would put it on the latency path of every question.
    """
    global _embedder
    if _embedder is None:
        idf = load_idf(session) if session is not None else None
        _embedder = Embedder(idf=idf or None)
    return _embedder


@dataclass
class Passage:
    chunk_id: int
    source_document_id: int
    content: str
    page: int | None
    score: float
    document_title: str | None = None
    publisher: str | None = None
    source_url: str | None = None

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "source_document_id": self.source_document_id,
            "page": self.page,
            "score": round(self.score, 4),
            "excerpt": self.content[:600],
            "document_title": self.document_title,
            "publisher": self.publisher,
            "source_url": self.source_url,
        }


@dataclass
class Retrieval:
    query: str
    passages: list[Passage] = field(default_factory=list)
    backend: str = ""
    model: str = ""

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "backend": self.backend,
            "embed_model": self.model,
            "passages": [p.to_dict() for p in self.passages],
        }


def _native(session: Session) -> bool:
    return bool(session.execute(text("""
        SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.relname = 'v_document_chunk' AND a.attname = 'embedding'
          AND format_type(a.atttypid, a.atttypmod) LIKE 'vector%'
    """)).scalar())


def _lexical(
    session: Session, query: str, k: int, idf: dict[str, float] | None = None
) -> list[tuple[int, float]]:
    """Full-text ranking over the same passages.

    Dense vectors and keyword search fail differently, which is the whole
    reason to run both. The dense side matched `international`, `freight`
    and `tonne` three separate ways and buried the one passage that
    actually said `Delhi`; full text ranks a rare proper noun exactly as
    highly as the query implies it should.
    """
    # OR, not AND. websearch_to_tsquery joins bare terms with AND, so
    # "Delhi international freight tonnage" required a passage containing
    # all four and matched nothing in the AAI tables - the one corpus that
    # actually names the airport. Disjunction lets ts_rank weigh how many
    # terms hit, which is the judgement we wanted from it in the first place.
    terms = [t for t in _WORD.findall(query.lower()) if len(t) > 2 and not t.isdigit()]
    if not terms:
        return []

    # Search on the discriminative half of the question, not all of it.
    # "Delhi international freight tonnage" is one specific term and three
    # that occur in nearly every passage in the corpus; including the
    # common three lets them out-vote the one that identifies the answer.
    # The vocabulary table already knows which is which, so reuse it
    # rather than inventing a second notion of importance.
    if idf:
        # Terms absent from the corpus are dropped here, though the dense
        # embedder still treats them as maximally specific. The defaults
        # differ because the failure modes do: a hashed unseen token is
        # harmless noise in a vector, but in full-text search it matches
        # nothing while crowding out the term that would have. The query
        # "Delhi international freight tonnage" selected on `tonnage` -
        # a word this corpus never uses, since it says `tonne` - and so
        # searched on nothing at all.
        present = [t for t in terms if t in idf] or terms
        ranked = sorted(present, key=lambda t: idf[t] if t in idf else 0.0, reverse=True)
        top = idf.get(ranked[0], 0.0)
        terms = [t for t in ranked if idf.get(t, 0.0) >= top * 0.6][:4] or ranked[:2]
    tsquery = " | ".join(terms)
    rows = session.execute(text("""
        SELECT c.chunk_id,
               ts_rank(to_tsvector('simple', c.content),
                       to_tsquery('simple', :q)) AS rank
        FROM v_document_chunk c
        WHERE to_tsvector('simple', c.content) @@ to_tsquery('simple', :q)
        ORDER BY rank DESC
        LIMIT :k
    """), {"q": tsquery, "k": k}).all()
    return [(int(cid), float(r)) for cid, r in rows]


def _fuse(dense: list[int], lexical: list[int], k_rrf: int = 60) -> dict[int, float]:
    """Reciprocal rank fusion.

    Fuses on *rank* rather than score, which is the point: a cosine
    similarity and a ts_rank are not on comparable scales, and normalising
    them against each other would invent a relationship that does not
    exist. Rank is the only thing the two retrievers genuinely share.
    """
    scores: dict[int, float] = {}
    for ranking in (dense, lexical):
        for pos, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k_rrf + pos + 1)
    return scores


def search(session: Session, query: str, top_k: int | None = None) -> Retrieval:
    k = top_k or SETTINGS.rag_top_k
    emb = embedder(session)
    qvec = emb.embed(query)

    # Refuse to search across mixed models. Comparing a Titan vector to a
    # hashed one produces a confident-looking score that means nothing.
    models = [
        r[0] for r in session.execute(
            text("SELECT DISTINCT embed_model FROM v_document_chunk WHERE embed_model IS NOT NULL")
        )
    ]
    if not models:
        return Retrieval(query=query, backend="empty", model=emb.model)
    if emb.model not in models:
        log.warning(
            f"index was built with {models}; query embedder is {emb.model}. "
            "Re-index before searching."
        )
        return Retrieval(query=query, backend="model-mismatch", model=emb.model)

    if _native(session):
        literal = "[" + ",".join(f"{v:.6f}" for v in qvec) + "]"
        # Over-fetch from each retriever: fusion can only promote a passage
        # that at least one side returned, so a tight top-k on both starves it.
        pool = max(k * 4, 20)
        dense_rows = session.execute(text("""
            SELECT c.chunk_id, c.source_document_id, c.content, c.page,
                   1 - (c.embedding <=> CAST(:q AS vector)) AS score,
                   c.document_title, c.publisher, c.source_url
            FROM v_document_chunk c
            WHERE c.embed_model = :model
            ORDER BY c.embedding <=> CAST(:q AS vector)
            LIMIT :k
        """), {"q": literal, "k": pool, "model": emb.model}).mappings().all()

        lex = _lexical(session, query, pool, idf=emb.idf)
        fused = _fuse([r["chunk_id"] for r in dense_rows], [c for c, _ in lex])

        known = {r["chunk_id"]: dict(r) for r in dense_rows}
        missing = [c for c in fused if c not in known]
        if missing:
            extra = session.execute(text("""
                SELECT c.chunk_id, c.source_document_id, c.content, c.page,
                       0.0 AS score, c.document_title,
                       c.publisher, c.source_url
                FROM v_document_chunk c
                WHERE c.chunk_id = ANY(:ids)
            """), {"ids": missing}).mappings().all()
            known.update({r["chunk_id"]: dict(r) for r in extra})

        ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:k]
        passages = []
        for cid, fscore in ordered:
            row = known.get(cid)
            if row is None:
                continue
            row = dict(row)
            row["score"] = fscore
            passages.append(Passage(**row))
        return Retrieval(
            query=query, passages=passages,
            backend="hybrid(pgvector-hnsw + fts, rrf)", model=emb.model,
        )

    # Portable path: score in Python. Fine for a corpus of this size and it
    # keeps a checkout without the extension fully functional.
    rows = session.execute(text("""
        SELECT c.chunk_id, c.source_document_id, c.content, c.page, c.embedding,
               c.document_title, c.publisher, c.source_url
        FROM v_document_chunk c
        WHERE c.embed_model = :model
    """), {"model": emb.model}).mappings().all()

    scored: list[Passage] = []
    for r in rows:
        try:
            vec = json.loads(r["embedding"])
        except (TypeError, json.JSONDecodeError):
            continue
        d = {kk: vv for kk, vv in dict(r).items() if kk != "embedding"}
        scored.append(Passage(**d, score=cosine(qvec, vec)))
    scored.sort(key=lambda p: p.score, reverse=True)
    return Retrieval(query=query, passages=scored[:k], backend="sequential", model=emb.model)
