"""preserve absent legacy next actions

Revision ID: c73a8de1150f
Revises: b82d7a190e64
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c73a8de1150f"
down_revision: str | Sequence[str] | None = "b82d7a190e64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # The previous migration can only infer ownership for a single active profile. For
    # legacy applications without a stored follow-up, preserve that absence rather than
    # manufacturing a due action. Existing next_followup_at values remain unchanged.
    bind.execute(
        sa.text("""
            UPDATE application_workspaces
            SET next_action = NULL, next_action_at = NULL
            WHERE application_id IN (
                SELECT id FROM applications WHERE next_followup_at IS NULL
            )
              AND next_action = 'Review application and choose the next step'
        """)
    )
    # Reconciliation may already have materialized the inferred actions before this
    # corrective migration. Retain their audit history, but make them non-actionable.
    bind.execute(
        sa.text("""
            UPDATE tasks
            SET status = 'cancelled'
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
    # The correction intentionally has no reversible data inference: restoring a
    # fabricated action would alter the meaning of the legacy record.
    pass
