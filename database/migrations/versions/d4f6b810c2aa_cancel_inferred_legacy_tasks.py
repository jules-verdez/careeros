"""cancel inferred legacy application tasks

Revision ID: d4f6b810c2aa
Revises: c73a8de1150f
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f6b810c2aa"
down_revision: str | Sequence[str] | None = "c73a8de1150f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # c73 was applied after a local operator read had already materialized tasks. Use
    # the persisted application relationship rather than parsing a hash key.
    op.get_bind().execute(
        sa.text("""
            UPDATE tasks
            SET status = 'CANCELLED'
            WHERE source_agent = 'application-workflow'
              AND status IN ('OPEN', 'IN_PROGRESS')
              AND deduplication_key LIKE 'application-next:%'
              AND id IN (
                SELECT application_tasks.task_id
                FROM application_tasks
                JOIN application_workspaces
                  ON application_workspaces.application_id = application_tasks.application_id
                WHERE application_workspaces.next_action IS NULL
              )
        """)
    )


def downgrade() -> None:
    # Do not resurrect actions that were intentionally absent in the legacy record.
    pass
