# Architecture

## Decision

CareerOS is a stateful Python product, not a coding-agent workflow. Coding agents modify the
repository; the installed `career` application owns daily execution.

## Ownership

| Layer | Ownership |
|---|---|
| `careeros.database` | Relational facts, constraints, migrations, transactions |
| `careeros.connectors` | External-system protocols and validated boundary records |
| `careeros.agents` | One independently versioned capability per agent |
| `careeros.services` | Domain invariants, deduplication, approval, audit, planning |
| `careeros.cli` | Operator input and rendering; no business invariants |
| `prompts/` | Versioned behavioral contracts, hashed into agent runs |

## Dependency direction

```text
CLI -> composition root -> agents -> services -> database
                              |
                              +-------> connector protocols

provider adapters -> connector protocols
```

The database and domain services never import provider SDKs. Connectors return validated
records; services decide whether and how state changes.

## Transaction boundaries

- Each domain command runs in a short SQLAlchemy unit of work.
- SQLite foreign keys are enabled on every connection.
- WAL mode allows concurrent readers while retaining a single transactional writer.
- Agent and connector run starts/finishes are committed independently, so failed business
  transactions still leave an operational record.
- External operations are not coupled to database transactions. They use an outbox-like
  `action_requests` record, explicit state transitions, payload hashes, and idempotency keys.

## State machine

```text
pending --approve--> approved --executor--> executing --> executed
   |                      |                     |
   +--reject--> rejected  +--cancel--> cancelled+--> failed
```

Gmail draft creation and Gmail sending have separate exact-payload approvals. The send executor
re-reads the provider draft and refuses execution if sender, recipient, subject, body, RFC
Message-ID, or idempotency marker differs. Inbox reconciliation is read-only at the provider:
it opens only known CareerOS threads and delivery notifications, then updates local state.

## Why deterministic planning

The question "what next?" is primarily a state-machine problem. A deterministic policy
creates idempotent tasks from facts:

- no/old discovery run -> discover;
- assessed new job -> review;
- shortlisted job without research -> research;
- researched shortlist without people -> decision-maker discovery;
- due application -> follow-up;
- upcoming meeting -> interview preparation;
- closed application without outcome -> learn.

An LLM may summarize or explain these tasks. It does not silently change their ordering or
execute them.

## Extension seam

Add a provider by:

1. provisioning credentials/integration;
2. implementing exactly one connector protocol;
3. validating provider payloads into domain records;
4. adding connector-contract tests with recorded, redacted fixtures;
5. registering it in the composition root;
6. adding an approved action executor where mutations are required.

## Existing databases

The command-line release retains the historical conversation tables and their migration so
existing local databases remain upgradeable. It does not include a chat engine, a web server,
or a frontend. No existing conversation or application records are deleted during this change.
