"""Terminal views of the saved opportunity queue."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.text import Text

from careeros.services.opportunities import Opportunity, OpportunityQueue


def render_opportunities(
    console: Console, queue: OpportunityQueue, *, mandatory_only: bool = False
) -> None:
    console.print(
        Text(
            f"Saved role evidence — {queue.generated_at.isoformat(timespec='minutes')}",
            style="bold",
        )
    )
    console.print(Text(queue.note))
    console.print(
        Text(f"mandatory_user_review ({len(queue.mandatory_user_review)})", style="bold yellow")
    )
    if not queue.mandatory_user_review:
        console.print("No assessed roles currently trigger mandatory review.")
    for card in queue.mandatory_user_review:
        render_opportunity(console, card)
    if mandatory_only:
        return
    console.print(
        Text(
            f"Other roles to review ({len(queue.opportunities)}/{queue.total_other_opportunities})",
            style="bold",
        )
    )
    for card in queue.opportunities:
        render_opportunity(console, card)
    if not queue.total_assessed_jobs:
        console.print("No assessed roles yet. Use `career jobs`, then `career assess JOB_ID ...`.")


def render_opportunity(console: Console, card: Opportunity) -> None:
    console.print(Text(f"\n{card.title} — {card.company}", style="bold"))
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column(overflow="fold")
    fields = [
        ("Job ID", card.job_id),
        ("Location", card.location or "Not recorded"),
        ("Source", card.canonical_url or "Missing; verify the employer vacancy URL"),
        ("Professional fit", f"{card.profile_fit_score} ({card.fit_basis})"),
        ("Eligibility", card.eligibility_label.value),
        ("Workflow status", card.job_status.value),
        ("Issue", card.unresolved_fact or "No material concern recorded in this review"),
        ("Evidence", "\n".join(card.evidence)),
        ("Next verification", card.next_verification_step),
        ("First discovered", card.first_seen_at.isoformat(timespec="minutes")),
        ("Assessed", card.assessed_at.isoformat(timespec="minutes")),
    ]
    for label, value in fields:
        table.add_row(Text(label), Text(value))
    console.print(table)
