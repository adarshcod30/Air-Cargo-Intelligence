"""Building the passage index.

Run as a module:
    python -m services.rag.index --limit 40
    python -m services.rag.index --rebuild
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.common.config import SETTINGS
from services.common.logging import get_logger
from services.rag.chunker import chunk_document
from services.rag.embedder import DIMS, Embedder, compute_idf
from services.warehouse.loader import get_engine

log = get_logger(__name__)


def _vector_column(session: Session) -> bool:
    return bool(session.execute(text("""
        SELECT count(*) FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.relname = 'document_chunk' AND a.attname = 'embedding'
          AND format_type(a.atttypid, a.atttypmod) LIKE 'vector%'
    """)).scalar())


def _resolve(raw_path: str) -> Path:
    p = Path(raw_path)
    return p if p.is_absolute() else Path(SETTINGS.raw_dir).parents[1] / p


def build(limit: int | None = None, rebuild: bool = False) -> dict:
    """Two passes: collect the corpus, then embed it.

    The second pass cannot start until the first finishes, because IDF is a
    property of the whole corpus. Chunking twice would be wasteful, so pass
    one keeps the text in memory - roughly 6 MB here, which is cheaper than
    re-parsing 36 PDFs.
    """
    engine = get_engine()
    written = skipped = duplicates = 0

    with Session(engine) as session:
        native = _vector_column(session)
        if rebuild:
            session.execute(text("DELETE FROM document_chunk"))
            session.execute(text("DELETE FROM rag_vocab"))
            session.commit()

        docs = session.execute(text("""
            SELECT source_document_id, raw_path, media_type, title
            FROM source_document
            WHERE raw_path IS NOT NULL
            ORDER BY source_document_id
        """)).mappings().all()
        if limit:
            docs = docs[:limit]

        already = {
            r[0] for r in session.execute(
                text("SELECT DISTINCT source_document_id FROM document_chunk")
            )
        }

        # -- pass 1: chunk the corpus and learn term rarity ----------------
        staged: list[tuple[int, list]] = []
        corpus: list[str] = []
        for d in docs:
            if d["source_document_id"] in already:
                skipped += 1
                continue
            chunks = chunk_document(_resolve(d["raw_path"]), d["media_type"])
            if not chunks:
                continue
            staged.append((d["source_document_id"], chunks))
            corpus.extend(c.content for c in chunks)

        idf = compute_idf(corpus)
        session.execute(text("DELETE FROM rag_vocab"))
        total = len(corpus)
        for tok, weight in idf.items():
            # Stored as a frequency pair rather than the derived weight so
            # the smoothing formula can change without a re-index.
            session.execute(text("""
                INSERT INTO rag_vocab (token, document_frequency, total_documents)
                VALUES (:t, :df, :n) ON CONFLICT (token) DO UPDATE
                SET document_frequency = EXCLUDED.document_frequency,
                    total_documents = EXCLUDED.total_documents
            """), {"t": tok, "df": max(1, round((total + 1) / pow(2.718281828, weight - 1)) - 1),
                   "n": total})
        session.commit()
        log.info(f"vocabulary: {len(idf)} term(s) over {total} passage(s)")

        embedder = Embedder(idf=idf)

        # -- pass 2: embed and store ---------------------------------------
        seen_sha: set[str] = set()
        for sid, chunks in staged:
            # Deduplicate before embedding, not after: a repeated passage
            # costs a network round trip that produces a vector we then
            # discard, and the open-data corpus repeats a lot.
            fresh = []
            for c in chunks:
                sha = hashlib.md5(c.content.encode()).hexdigest()[:32]
                if sha in seen_sha:
                    duplicates += 1
                    continue
                seen_sha.add(sha)
                fresh.append((c, sha))
            if not fresh:
                continue

            vectors = embedder.embed_batch([c.content for c, _ in fresh])
            # embed_batch returns one vector per input, in order. If it
            # ever does not, storing a chunk against another chunk's
            # vector is far worse than failing here.
            for (c, sha), vec in zip(fresh, vectors, strict=True):
                # pgvector accepts its own literal form; the portable
                # fallback stores the same JSON array as text, so one
                # code path writes both and the retriever reads either.
                payload = (
                    "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
                    if native else json.dumps([round(v, 6) for v in vec])
                )
                session.execute(text("""
                    INSERT INTO document_chunk
                        (source_document_id, seq, page, content, token_estimate,
                         embedding, embed_model, content_sha)
                    VALUES (:sid, :seq, :page, :content, :tok, :emb, :model, :sha)
                    ON CONFLICT (source_document_id, seq) DO NOTHING
                """), {
                    "sid": sid, "seq": c.seq, "page": c.page,
                    "content": c.content, "tok": c.token_estimate,
                    "emb": payload, "model": embedder.model, "sha": sha,
                })
                written += 1
            session.commit()

    return {
        "chunks_written": written,
        "duplicates_skipped": duplicates,
        "documents_skipped": skipped,
        "vocabulary_terms": len(idf),
        "embed_model": embedder.model,
        "dims": DIMS,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the passage index")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    report = build(limit=args.limit, rebuild=args.rebuild)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
