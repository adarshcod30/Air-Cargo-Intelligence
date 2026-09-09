"""pipeline state in the warehouse

Revision ID: c4f1a7e93b02
Revises: b8e2c4f19a3d
Create Date: 2026-09-09

The scheduler recorded its outcome to a file next to the processed data. The
process that runs the pipeline and the process that serves the dashboard are
not the same machine, and on a serverless host that file does not exist at
all - so the dashboard reported "last ingest unknown" however many runs had
succeeded.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4f1a7e93b02"
down_revision = "b8e2c4f19a3d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pipeline_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_state")),
        # One row, always. The table holds the latest outcome, not a history;
        # per-run history already lives in agent_run and ingest_run.
        sa.CheckConstraint("id = 1", name=op.f("ck_pipeline_state_single_row")),
    )


def downgrade() -> None:
    op.drop_table("pipeline_state")
