# CareerOS

CareerOS is a human-governed operating system for career discovery, research, ranking,
outreach, follow-up, interviews, and learning.

The Python CLI keeps versioned local records, cited evidence, and exact approval gates.
Use it to review opportunities, prepare application materials, track outcomes, and manage
approved outreach. The database owns daily state; coding assistants support the operator.

CareerOS v0.1.0rc2 is a command-line application for one person’s job search.
Installation and use require a paid seat under the [commercial license](LICENSE).
It never submits applications automatically. Back up your local database and review provider
configuration before using external connectors. You can try the complete local discovery/review
loop without accounts.

```text
Discover -> Research -> Rank -> Outreach -> Follow-up -> Interview -> Learn
```

The deterministic next-action policy derives work from database state, so the operator sees
the next safe, high-value actions instead of being asked to invent the workflow.

## Current vertical slice

- Normalized SQLAlchemy 2 schema and Alembic migration path.
- SQLite source of truth with foreign keys and WAL mode.
- Cross-source job deduplication and immutable source observations.
- Local JSON job connector for reproducible ingestion of real exports.
- Versioned fit/confidence/competition assessments kept separate from job facts.
- Immutable Markdown + JSON company-research snapshots.
- Public decision-maker records; email storage requires a public source URL.
- Official UKVI sponsor-register matching with reviewed company aliases and source hashes.
- Free direct Greenhouse, Lever, Ashby, and SmartRecruiters discovery.
- Hunter-first contact resolution with an explicit one-credit Prospeo fallback observation.
- One-request Brave source discovery whose snippets are not persisted.
- Evidence-bound local email drafts with paragraph rationales.
- Exact-payload Gmail draft and send execution with separate approvals.
- Least-privilege Gmail reply/bounce reconciliation and contact invalidation.
- Human approval state machine for irreversible external actions.
- Hash-chained append-only audit events.
- Deterministic daily work queue and persisted morning brief.
- Latest-assessment opportunity queue with separate fit and eligibility evidence.
- Complete mandatory high-fit review disclosure, including currently ineligible roles.
- Application tracker with deduplication, stage evidence, submission receipts, deadlines,
  follow-ups, and source/role-family funnel counts.
- Immutable reviewed candidate-evidence versions and hash-frozen application packs.
- Timestamped official-vacancy checks with robots compliance and explicit unknown/unreachable states.
- Interview preparation, conflict-safe local scheduling, rescheduling, and post-interview debriefs.
- Independent agent and connector run ledgers.
- Fail-closed Gmail/Calendar boundaries: no mock provider fallback.

Calendar and hosted-model adapters remain unprovisioned. Gmail, Apify, Hunter, Prospeo, Brave,
the UKVI register, and public ATS connectors fail closed when their required credentials or
inputs are absent.

## Quick start

Prerequisites: Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

From the repository root, run the following commands to try the fictional job search.

```bash
uv sync --frozen --extra dev
cp .env.example .env
# Set CAREEROS_CANDIDATE_NAME in .env before initializing.
uv run career init --email you@example.com
uv run career profile preference --kind role "Quant Researcher" --weight 1.0 --required
uv run career discover --from-json examples/jobs.json
uv run career jobs
uv run career opportunities
uv run career application list
uv run career review
uv run career dashboard
uv run career audit verify
```

`examples/jobs.json` contains fictional roles for an offline demo. Do not apply to or contact
the example employers. Replace these fixtures with your own export for normal use. The local
workflow does not need Gmail, an Apify account, or a model API key.

The job import accepts an array or `{"jobs": [...]}`:

```json
[
  {
    "source_kind": "greenhouse",
    "source_name": "Acme Greenhouse",
    "external_id": "12345",
    "source_url": "https://example.test/jobs/demo-001",
    "company_name": "Acme",
    "company_domain": "acme.example",
    "title": "Quantitative Researcher",
    "location": "Remote",
    "posted_at": "2000-01-03T09:00:00+00:00",
    "confidence": 0.95,
    "requirements": ["Python", "Statistical research"]
  }
]
```

Assess and shortlist a job explicitly:

```bash
uv run career assess JOB_ID \
  --fit 0.86 \
  --confidence 0.75 \
  --competition 0.70 \
  --why "Human assessment against the verified CV and job description."
uv run career shortlist JOB_ID
```

Review the saved opportunity queue, newest discoveries first:

```bash
uv run career opportunities --limit 20
uv run career opportunities --sort priority --json
```

Each job uses its latest assessment. `mandatory_user_review` is never cut off by `--limit`
or `jobs --sponsor-only`; it includes high-fit roles with unknown or adverse eligibility,
even when previously dismissed. Explicitly ineligible roles remain non-actionable. The queue
shows stored evidence and timestamps; it does not perform a live vacancy check.

Record vacancy-specific eligibility separately from professional fit:

```bash
uv run career assess JOB_ID --fit 0.86 --confidence 0.75 --competition 0.50 \
  --why "Professional match against the CV and employer advert." \
  --review-file ./review.json
```

See [Opportunity review](docs/opportunity-review.md) for the JSON format and daily workflow.

The application workflow is local and human-governed. Track a role, verify the official vacancy,
prepare a frozen pack, request review of that exact pack, and record the submission receipt yourself:

```bash
uv run career application track JOB_ID --role-family "Quant research" --source "Employer board"
uv run career profile import --file ./candidate-evidence.json --reviewer "Your Name"
uv run career vacancy check JOB_ID --official-url https://employer.example/jobs/123
uv run career packet prepare APPLICATION_ID --spec ./application-spec.json
uv run career packet show PACKET_ID --export ./packs/application-123
uv run career packet request-approval PACKET_ID
uv run career action approve ACTION_ID --reviewer "Your Name"
uv run career application record-submission APPLICATION_ID \
  --at 2026-10-01T10:30:00+01:00 \
  --receipt "Employer confirmation ABC-123" \
  --packet PACKET_ID
uv run career application next APPLICATION_ID \
  --action "Check response" --at 2026-10-08T09:00:00+01:00 --followup
```

`career packet request-approval` freezes the reviewed candidate version, vacancy verification,
selected evidence, answers, and content hash. It does not open a browser or submit a form.
`career application record-submission` is the explicit human report of an external submission.
See [Application workflow](docs/application-workflow.md) for the evidence format, interview commands,
and failure states.

Import the two required research representations:

```bash
uv run career research "Acme" \
  --markdown research/acme.md \
  --json research/acme.json \
  --as-of 2000-01-03 \
  --confidence 0.90
```

Record a public professional contact:

```bash
uv run career person add \
  --company "Acme" \
  --name "Sample Contact" \
  --title "Head of Quant Research" \
  --influence 0.85 \
  --confidence 0.80 \
  --email "recipient@acme.example" \
  --email-source-url "https://acme.example/team/contact"
```

Stage outreach locally:

```bash
uv run career outreach PERSON_ID \
  --subject "Quant research at Acme" \
  --body ./draft.txt \
  --why "Establishes cited relevance." \
  --why "Makes a specific low-friction request." \
  --evidence DOCUMENT_ID
uv run career action list --status pending
uv run career action approve ACTION_ID --reviewer "Your Name"
uv run career action execute ACTION_ID
uv run career action request-send EMAIL_ID
uv run career action approve SEND_ACTION_ID --reviewer "Your Name"
uv run career action execute SEND_ACTION_ID
```

Approval does **not** execute or send. Draft creation and sending are separate approvals, and
each executor re-checks the immutable payload plus its idempotency identity. There is no
`--yes-send` shortcut.

Reconcile only CareerOS threads and delivery notices from the last seven days:

```bash
uv run career inbox --since-hours 168
```

The sponsor register is a prefilter, not vacancy-specific sponsorship proof:

```bash
uv run career sponsor sync
uv run career sponsor list --status matched
uv run career sponsor alias COMPANY_ID "Exact Licensed Organisation Limited"
uv run career jobs --sponsor-only
```

Paid-capable fallbacks require an explicit one-call authorization:

```bash
uv run career person enrich PERSON_ID \
  --reason hunter_accept_all \
  --spend-one-credit

uv run career evidence search \
  'site:example.com quant research team' \
  --country GB \
  --spend-one-request
```

Prospeo observations are not promoted into the public-email send path. Brave snippets are not
persisted.

## CLI

```text
career init
career discover
career jobs
career opportunities
career application track|list|show|next|record-submission|stage|funnel
career profile import|versions|diff
career packet prepare|show|request-approval
career vacancy check|record|show
career interview schedule|show|reschedule|debrief|cancel
career assess
career shortlist
career research
career person add|list|enrich|enrichments
career sponsor sync|alias|list
career evidence search
career outreach
career review
career followup
career inbox
career dashboard
career doctor
career action list|show|request-send|approve|execute|reject
career task list|done
career audit verify
career db current
```

## Architecture

```text
Python command-line application
       |
Composition root (dependency injection)
       |
Independent agents -------- deterministic next-action policy
       |                                  |
Application services              approval state machine
       |                                  |
SQLAlchemy repositories / unit-of-work / hash-chained audit
       |
SQLite source of truth

Connectors <------ validated records ------> external systems
```

See:

- [Architecture](docs/architecture.md)
- [Data model](docs/data-model.md)
- [Safety and approvals](docs/safety-and-approvals.md)
- [Connector contract](docs/connectors.md)
- [Scoring and measurement](docs/scoring.md)
- [Opportunity review](docs/opportunity-review.md)
- [Roadmap](docs/roadmap.md)
- [Agent rules](docs/agent-rules.md)
- [Contributing](CONTRIBUTING.md)
- [Security and private data](SECURITY.md)
- [Preparing a public release](docs/public-release.md)

## External integrations

Configure only the providers you intend to use. Optional connector credentials are listed in
`.env.example`; store real values in your ignored `.env`. Gmail requires a separately installed
and authenticated Google Workspace CLI. Calendar and hosted-model adapters are not implemented.
See [Connectors](docs/connectors.md) for the boundaries and configuration requirements.

Keep CVs, drafts, contact lists, database backups, provider credentials, and personal
`AGENTS.md` rules in your private workspace. The source examples are entirely fictional.

Check local authentication without reading mailbox content:

```bash
uv run career doctor
```

Add `--live` to make one read-only Gmail profile request. The doctor distinguishes the
OAuth-authenticated Apify MCP connection from the optional `apify` CLI credential store; the
MCP can be ready even when `apify info` is not logged in.

## Development

```bash
uv run alembic upgrade head
uv run python scripts/public_release.py --check
uv run pytest --cov --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
```

The repository uses versioned migrations. Do not replace migrations with
`Base.metadata.create_all()` in application code.

## License

One paid seat covers one individual’s own job search. The license restricts redistribution,
resale and hosting, and requires a separate written license for agency or client use. See the
[CareerOS Single-Seat Commercial License](LICENSE) for the full terms.
