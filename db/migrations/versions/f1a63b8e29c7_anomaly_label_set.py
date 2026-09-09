"""anomaly label set

Revision ID: f1a63b8e29c7
Revises: e7c9a2d51f84
Create Date: 2026-09-10

Precision and recall need ground truth, and this project has none: the
detector reads the tonnage series, so any label derived from that same
series risks measuring the detector against itself.

Two proxies were tried and rejected on evidence. The publisher's own
year-on-year change barely separates flagged months from unflagged ones
(33% versus 27% above a 50% bar), because year-on-year movement and
departure from a seasonal pattern are different questions. A persistence
rule fared worse, marking 80% of unflagged months as genuine events, which
is not credible.

What remains is a table of labels with their basis recorded, seeded only
with cases assignable beyond argument - a service starting or stopping, and
a row whose published components do not sum - and open to human labels for
the rest. `source` distinguishes the two so a measurement can say how much
of it rests on judgement and how much on rule.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f1a63b8e29c7"
down_revision = "e7c9a2d51f84"
branch_labels = None
depends_on = None

GRAIN = postgresql.ENUM("AIRPORT", "AIRLINE", name="grain_enum", create_type=False)
DIRECTION = postgresql.ENUM(
    "INTERNATIONAL", "DOMESTIC", "TOTAL", name="direction_enum", create_type=False)


def upgrade() -> None:
    op.create_table(
        "anomaly_label",
        sa.Column("label_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("grain", GRAIN, nullable=False),
        sa.Column("entity_key", sa.String(length=64), nullable=False),
        sa.Column("period_id", sa.Integer(), nullable=False),
        sa.Column("direction", DIRECTION, nullable=False),
        # GENUINE: something happened that a person should know about.
        # SPURIOUS: nothing happened, or the movement is a defect in the data.
        sa.Column("label", sa.String(length=16), nullable=False),
        # Why, in words. A label without a stated basis cannot be audited
        # or disagreed with.
        sa.Column("basis", sa.Text(), nullable=False),
        # 'rule' for the unambiguous cases seeded automatically, 'human' for
        # anything a person decided. Reported separately.
        sa.Column("source", sa.String(length=16), nullable=False, server_default="rule"),
        sa.Column("labelled_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["period_id"], ["dim_period.period_id"],
                                name=op.f("fk_anomaly_label_period_id_dim_period")),
        sa.PrimaryKeyConstraint("label_id", name=op.f("pk_anomaly_label")),
        sa.UniqueConstraint("grain", "entity_key", "period_id", "direction",
                            name="anomaly_label_natural_key"),
        sa.CheckConstraint("label IN ('GENUINE','SPURIOUS')",
                           name=op.f("ck_anomaly_label_label_known")),
        sa.CheckConstraint("source IN ('rule','human')",
                           name=op.f("ck_anomaly_label_source_known")),
    )
    op.create_index("ix_anomaly_label_entity", "anomaly_label", ["entity_key"])


def downgrade() -> None:
    op.drop_table("anomaly_label")
