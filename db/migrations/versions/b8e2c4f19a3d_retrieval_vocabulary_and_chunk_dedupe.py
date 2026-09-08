"""retrieval vocabulary and chunk dedupe

Revision ID: b8e2c4f19a3d
Revises: 7a71406d5f81
Create Date: 2026-09-09

Inverse document frequency must be computed over the whole corpus and then
applied identically when indexing and when querying. The indexing pass and
the API run in different processes, so the weights have to be persisted or
the two will silently disagree and every score becomes meaningless.

`content_sha` exists because the open-data corpus contains the same record
in several documents; without it the top-k is three copies of one passage.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8e2c4f19a3d"
down_revision = "7a71406d5f81"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rag_vocab",
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("document_frequency", sa.Integer(), nullable=False),
        sa.Column("total_documents", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("token", name=op.f("pk_rag_vocab")),
    )
    op.add_column(
        "document_chunk", sa.Column("content_sha", sa.String(length=32), nullable=True)
    )
    op.create_index("ix_document_chunk_sha", "document_chunk", ["content_sha"])


def downgrade() -> None:
    op.drop_index("ix_document_chunk_sha", table_name="document_chunk")
    op.drop_column("document_chunk", "content_sha")
    op.drop_table("rag_vocab")
