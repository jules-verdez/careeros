# Roadmap

## Milestone 1 — durable kernel

- [x] normalized SQLite schema and migration path;
- [x] connector/agent boundaries;
- [x] job ingestion and deduplication;
- [x] research and public-person records;
- [x] local evidence-bound outreach drafts;
- [x] approvals and hash-chained audit;
- [x] deterministic review/dashboard.
- [x] deduplicated opportunity queue, evidenced eligibility, and uncapped high-fit disclosure.
- [x] local application tracker with receipts, stage evidence, deadlines, follow-ups, and funnel counts.
- [x] immutable candidate evidence versions and hash-frozen application packs.
- [x] timestamped official vacancy verification with robots and unknown/unreachable states.
- [x] interview preparation, conflict-safe scheduling, rescheduling, and debrief capture.

## Milestone 2 — provisioned read connectors

1. [x] Google Workspace read/compose authorization and inbox reconciliation.
2. [x] Direct Greenhouse, Lever, Ashby, and SmartRecruiters job sources.
3. [x] Provider contract tests and replay fixtures.
4. Incremental cursors, backoff, retention, and source health dashboard.

## Milestone 3 — model gateway

1. Provision a model gateway and record provider/model IDs.
2. Strict structured outputs for research, people, and message drafts.
3. Citation/evidence validation before persistence.
4. Prompt regression tests and adversarial hallucination cases.

## Milestone 4 — approved external drafts/actions

1. [x] Gmail draft creation with idempotency.
2. Calendar proposal parsing and conflict-safe availability.
3. [x] Separate approval for email sending; application/calendar commitments remain pending.
4. [x] Gmail send, reply, and bounce reconciliation; calendar remains pending.

## Milestone 5 — learning system

1. [x] Outcome evidence capture and source/role-family funnel counts (probabilities remain out of scope).
2. Time-aware score calibration.
3. Experiment registry and selection-bias controls.
4. Retention/deletion controls and encrypted backups.

## Deployment prerequisite

If using Vercel, install the missing CLI:

```bash
npm i -g vercel
```

Then provision external integrations and pull their environment through the deployment
platform before building concrete provider adapters.
