"""operating metrics

Revision ID: d5b83c2f47a1
Revises: c4f1a7e93b02
Create Date: 2026-09-09

The ingestion layer kept one tonnage column per dataset and discarded the
rest - 257 other fields across the payloads already downloaded, including
freight tonne-kilometres, available tonne-kilometres, departures and load
factors. Cargo load factor is FTK over ATK and both sides were on disk the
whole time.

Separate from fact_cargo_movement because these are different units on
different denominators; widening the fact table would mean either a mass of
nullable columns or storing a percentage in a field named tonnage_kg.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d5b83c2f47a1"
down_revision = "c4f1a7e93b02"
branch_labels = None
depends_on = None

# postgresql.ENUM, not sa.Enum: `create_type` is a dialect-level argument.
# sa.Enum accepts it silently and still emits CREATE TYPE, which fails
# against types the first migration already created.
GRAIN = postgresql.ENUM(
    "AIRPORT", "AIRLINE", name="grain_enum", create_type=False,
)
DIRECTION = postgresql.ENUM(
    "INTERNATIONAL", "DOMESTIC", "TOTAL", name="direction_enum", create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "fact_operating_metric",
        sa.Column("metric_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("grain", GRAIN, nullable=False),
        sa.Column("entity_key", sa.String(length=64), nullable=False),
        sa.Column("period_id", sa.Integer(), nullable=False),
        sa.Column("direction", DIRECTION, nullable=False),
        sa.Column("metric", sa.String(length=40), nullable=False),
        sa.Column("value", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit", sa.String(length=24), nullable=False),
        sa.Column("source_document_id", sa.BigInteger(), nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["period_id"], ["dim_period.period_id"],
                                name=op.f("fk_fact_operating_metric_period_id_dim_period")),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_document.source_document_id"],
                                name=op.f("fk_fact_operating_metric_source_document_id_source_document")),
        sa.PrimaryKeyConstraint("metric_id", name=op.f("pk_fact_operating_metric")),
        sa.UniqueConstraint("grain", "entity_key", "period_id", "direction", "metric",
                            name="operating_metric_natural_key"),
        sa.CheckConstraint("value >= 0", name=op.f("ck_fact_operating_metric_value_non_negative")),
    )
    op.create_index("ix_fact_operating_metric_entity_key", "fact_operating_metric", ["entity_key"])
    op.create_index("ix_fact_operating_metric_metric", "fact_operating_metric", ["metric"])


def downgrade() -> None:
    op.drop_table("fact_operating_metric")
