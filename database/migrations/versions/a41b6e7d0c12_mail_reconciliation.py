"""mail reconciliation

Revision ID: a41b6e7d0c12
Revises: f70f4c205301
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a41b6e7d0c12"
down_revision: str | Sequence[str] | None = "f70f4c205301"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "person_contacts",
        sa.Column(
            "delivery_status",
            sa.Enum(
                "UNKNOWN",
                "DELIVERABLE",
                "RISKY",
                "INVALID",
                name="contact_delivery_status",
                native_enum=False,
            ),
            nullable=False,
            server_default="UNKNOWN",
        ),
    )
    op.add_column(
        "person_contacts",
        sa.Column("delivery_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("person_contacts", sa.Column("delivery_detail", sa.Text(), nullable=True))
    op.create_index(
        op.f("ix_person_contacts_delivery_status"),
        "person_contacts",
        ["delivery_status"],
        unique=False,
    )

    op.add_column("emails", sa.Column("rfc822_message_id", sa.String(length=998), nullable=True))
    op.create_index(
        op.f("ix_emails_rfc822_message_id"),
        "emails",
        ["rfc822_message_id"],
        unique=False,
    )

    op.create_table(
        "email_delivery_events",
        sa.Column("outbound_email_id", sa.String(), nullable=True),
        sa.Column("notification_email_id", sa.String(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "HARD_BOUNCE",
                "SOFT_BOUNCE",
                "DELAYED",
                name="delivery_event_kind",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("recipient_address", sa.String(length=320), nullable=True),
        sa.Column("smtp_status", sa.String(length=32), nullable=True),
        sa.Column("diagnostic", sa.Text(), nullable=True),
        sa.Column("matched_by", sa.String(length=64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["notification_email_id"],
            ["emails.id"],
            name=op.f("fk_email_delivery_events_notification_email_id_emails"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["outbound_email_id"],
            ["emails.id"],
            name=op.f("fk_email_delivery_events_outbound_email_id_emails"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_delivery_events")),
        sa.UniqueConstraint(
            "notification_email_id",
            name=op.f("uq_email_delivery_events_notification_email_id"),
        ),
    )
    op.create_index(
        op.f("ix_email_delivery_events_kind"),
        "email_delivery_events",
        ["kind"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_delivery_events_occurred_at"),
        "email_delivery_events",
        ["occurred_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_delivery_events_outbound_email_id"),
        "email_delivery_events",
        ["outbound_email_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_delivery_events_recipient_address"),
        "email_delivery_events",
        ["recipient_address"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_email_delivery_events_recipient_address"),
        table_name="email_delivery_events",
    )
    op.drop_index(
        op.f("ix_email_delivery_events_outbound_email_id"),
        table_name="email_delivery_events",
    )
    op.drop_index(
        op.f("ix_email_delivery_events_occurred_at"),
        table_name="email_delivery_events",
    )
    op.drop_index(op.f("ix_email_delivery_events_kind"), table_name="email_delivery_events")
    op.drop_table("email_delivery_events")

    op.drop_index(op.f("ix_emails_rfc822_message_id"), table_name="emails")
    op.drop_column("emails", "rfc822_message_id")

    op.drop_index(op.f("ix_person_contacts_delivery_status"), table_name="person_contacts")
    op.drop_column("person_contacts", "delivery_detail")
    op.drop_column("person_contacts", "delivery_checked_at")
    op.drop_column("person_contacts", "delivery_status")
