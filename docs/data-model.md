# Data model

CareerOS separates identity, observations, estimates, content, workflow state, and actions.

## Core identities

- `companies` plus `company_aliases`;
- `people` plus time-aware `person_employments`;
- `person_contacts`, with a database constraint requiring a public source URL for email;
- `contact_enrichment_observations`, which keeps paid vendor observations separate from
  sendable public contacts and records provider status, cost, reason, and response hash;
- `recruiters` as a subtype of people;
- `candidate_profiles`, preferences, and evidenced skills.

## Opportunity facts

- `jobs` is the canonical cross-source identity.
- `job_observations` stores each provider's external ID, URL, observation time, confidence,
  content hash, and raw document.
- `job_requirements` stores normalized requirements.
- `job_assessments` stores versioned fit, confidence, competition, explanation, and time.

A score is not a job fact. Multiple scoring versions can coexist without rewriting the job.

## Evidence and research

Content is stored once in `documents`, addressed by SHA-256. Association tables link
documents to companies, people, jobs, and applications without polymorphic foreign keys.
`research_reports` pairs one Markdown document with one structured JSON document at an
explicit `as_of_date`.

## Execution state

- `applications` tracks the application state machine and next follow-up time.
- `application_workspaces` adds candidate ownership, deadline, next-action, role-family,
  and source fields without changing historical application facts.
- `application_events` is the ordered evidence timeline for tracking, submission, stage,
  interview, and outcome events.
- `application_submissions` stores one human-reported submission receipt and the exact
  packet used; it never implies that CareerOS submitted the form.
- `application_packets` freezes selected candidate evidence and the vacancy verification
  under a content hash.
- `emails` and recipients track local/provider messages.
- `email_delivery_events` records hard/soft bounces and delays; contacts carry current
  delivery status, and outbound messages retain Gmail plus RFC 822 identities.
- `meetings` and attendees track interviews and proposed events.
- `interactions` provides a channel-neutral timeline.
- `tasks` plus entity link tables form the deterministic work queue.
- `daily_briefs` are revisioned snapshots; generating a new view never rewrites an old one.

## Candidate evidence and verification

- `candidate_versions` stores immutable, human-reviewed evidence snapshots. Packets and
  interview workspaces reference a specific version, so later profile edits cannot silently
  change already-prepared materials.
- `vacancy_verifications` stores timestamped official-page checks, status, evidence, and
  reviewer identity. A verification is considered fresh for 48 hours for submission approval;
  unknown or unreachable checks remain explicit states.
- `interview_workspaces` stores preparation plans, cited candidate examples, and a separate
  post-interview debrief for each locally recorded meeting.

## Governance

- `agent_runs` records agent/prompt/model versions and inputs/outputs.
- `connector_runs` records provider operations and failure summaries.
- `action_requests` stores exact payload hashes, idempotency keys, reviews, and execution state.
- `audit_events` is append-only and hash-chained.
- `company_sponsorship_assessments` records exact, probable, ambiguous, and absent UKVI
  register matches together with the register URL and content hash.

## SQLite and DuckDB

SQLite is the transactional writer. DuckDB compatibility means analytical exports should use
portable scalar types and explicit enums, not that two engines should write the same database.
A future analytical job should copy immutable tables/snapshots into DuckDB or Parquet and
record the extraction watermark.
