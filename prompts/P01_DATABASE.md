# P01 — Database Designer

Design or evolve the CareerOS relational schema under the constraints in `SYSTEM.md`.

Required domains:

- companies, aliases, people, employments, public contacts, recruiters;
- jobs, source observations, requirements, candidate profiles, preferences, assessments;
- applications, emails, recipients, meetings, attendees;
- documents, research snapshots, evidence links, notes;
- tasks, interactions, agent runs, connector runs;
- action requests, approvals/state transitions, audit events, daily briefs.

## Requirements

- Third normal form unless a documented measured-query exception justifies denormalization.
- Foreign keys, named constraints, relevant composite indexes, and explicit delete behavior.
- Cross-source identity and idempotency keys.
- Immutable observations and research snapshots.
- Scores in `[0,1]` with version, confidence, timestamp, and explanation.
- Public email requires a source URL.
- SQLite-first types and constraints; avoid dialect-specific SQL where practical.
- Alembic forward migrations and tested upgrades from an empty database.
- Discuss DuckDB analytical mirroring separately; do not use DuckDB as the transactional writer.

Return an entity/relationship rationale, SQLAlchemy 2 models, migration, index rationale,
invariants, and migration tests.

