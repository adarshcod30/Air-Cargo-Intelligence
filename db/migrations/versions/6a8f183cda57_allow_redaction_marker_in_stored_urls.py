"""allow redaction marker in stored urls

Revision ID: 6a8f183cda57
Revises: ceffe1098071
Create Date: 2026-09-08 07:24:56.062526

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6a8f183cda57'
down_revision: Union[str, Sequence[str], None] = 'ceffe1098071'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Allow the redaction marker while still rejecting a live key.

    Written by hand: Alembic does not autogenerate CheckConstraint
    changes, so the earlier revision silently did nothing. The previous
    predicate banned any `api-key=`, which also rejected the redacted
    form `api-key=<redacted>` that we deliberately store.
    """
    op.drop_constraint(
        "ck_source_document_no_credential_in_url", "source_document", type_="check"
    )
    op.create_check_constraint(
        "no_credential_in_url",
        "source_document",
        r"source_url !~* 'api[-_]?key=(?!<redacted>)'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_source_document_no_credential_in_url", "source_document", type_="check"
    )
    op.create_check_constraint(
        "no_credential_in_url", "source_document", "source_url NOT ILIKE '%api-key=%'"
    )
