"""forecast interval coverage

Revision ID: e7c9a2d51f84
Revises: d5b83c2f47a1
Create Date: 2026-09-10

Interval coverage was reported as unmeasurable because it was checked only
against forecast periods the warehouse already held - six samples, which
cannot distinguish an 80% interval from a 50% one.

It is measurable the same way the point error is: by walking forward over
held-out points. Each forecast now carries how many backtest folds its band
contained the truth, so coverage can be pooled across series rather than
averaged over three-sample percentages.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e7c9a2d51f84"
down_revision = "d5b83c2f47a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("forecast", sa.Column("interval_hits", sa.Integer(), nullable=True))
    op.add_column("forecast", sa.Column("interval_folds", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "interval_hits_within_folds", "forecast",
        "interval_hits IS NULL OR interval_folds IS NULL "
        "OR (interval_hits >= 0 AND interval_hits <= interval_folds)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_forecast_interval_hits_within_folds"), "forecast", type_="check")
    op.drop_column("forecast", "interval_folds")
    op.drop_column("forecast", "interval_hits")
