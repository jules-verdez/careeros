"""contact enrichment observations

Revision ID: e27f43a16b5c
Revises: c15e8b72d940
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e27f43a16b5c"
down_revision: str | Sequence[str] | None = "c15e8b72d940"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contact_enrichment_observations",
        sa.Column("person_id", sa.String(), nullable=False),
        sa.Column("company_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column(
            "fallback_reason",
            sa.Enum(
                "HUNTER_MISSING",
                "HUNTER_ACCEPT_ALL",
                "HUNTER_LOW_CONFIDENCE",
                name="contact_enrichment_reason",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("email_address", sa.String(length=320), nullable=True),
        sa.Column("provider_status", sa.String(length=64), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("credits_spent", sa.Integer(), nullable=False),
        sa.Column("free_lookup", sa.Boolean(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("response_sha256", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.CheckConstraint(
            "credits_spent >= 0",
            name=op.f("ck_contact_enrichment_observations_nonnegative_enrichment_credits"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_contact_enrichment_observations_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_contact_enrichment_observations_person_id_people"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_enrichment_observations")),
        sa.UniqueConstraint(
            "person_id",
            "provider",
            "response_sha256",
            name="uq_contact_enrichment_observation",
        ),
    )
    for column in (
        "company_id",
        "email_address",
        "observed_at",
        "person_id",
        "provider",
        "response_sha256",
    ):
        op.create_index(
            op.f(f"ix_contact_enrichment_observations_{column}"),
            "contact_enrichment_observations",
            [column],
            unique=False,
        )


def downgrade() -> None:
    for column in (
        "response_sha256",
        "provider",
        "person_id",
        "observed_at",
        "email_address",
        "company_id",
    ):
        op.drop_index(
            op.f(f"ix_contact_enrichment_observations_{column}"),
            table_name="contact_enrichment_observations",
        )
    op.drop_table("contact_enrichment_observations")
