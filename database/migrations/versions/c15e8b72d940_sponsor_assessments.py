"""sponsor assessments

Revision ID: c15e8b72d940
Revises: a41b6e7d0c12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c15e8b72d940"
down_revision: str | Sequence[str] | None = "a41b6e7d0c12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_sponsorship_assessments",
        sa.Column("company_id", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "MATCHED",
                "PROBABLE_ALIAS",
                "NOT_FOUND",
                "AMBIGUOUS",
                name="sponsor_match_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("matched_organisation", sa.String(length=500), nullable=True),
        sa.Column("candidate_organisations_json", sa.JSON(), nullable=False),
        sa.Column("match_basis", sa.String(length=64), nullable=False),
        sa.Column("match_score", sa.Float(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "match_score >= 0 AND match_score <= 1",
            name=op.f("ck_company_sponsorship_assessments_valid_sponsor_match_score"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_company_sponsorship_assessments_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "company_id",
            name=op.f("pk_company_sponsorship_assessments"),
        ),
    )
    op.create_index(
        op.f("ix_company_sponsorship_assessments_assessed_at"),
        "company_sponsorship_assessments",
        ["assessed_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_company_sponsorship_assessments_source_sha256"),
        "company_sponsorship_assessments",
        ["source_sha256"],
        unique=False,
    )
    op.create_index(
        op.f("ix_company_sponsorship_assessments_status"),
        "company_sponsorship_assessments",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_company_sponsorship_assessments_status"),
        table_name="company_sponsorship_assessments",
    )
    op.drop_index(
        op.f("ix_company_sponsorship_assessments_source_sha256"),
        table_name="company_sponsorship_assessments",
    )
    op.drop_index(
        op.f("ix_company_sponsorship_assessments_assessed_at"),
        table_name="company_sponsorship_assessments",
    )
    op.drop_table("company_sponsorship_assessments")
