# Contributing

Use Python 3.12+ and uv. Start with fictional examples and a disposable database; tests must
never depend on an operator's private files, credentials, mailbox, or paid provider account.

```bash
uv sync --frozen --extra dev
uv run career --database-url sqlite:///development.db init
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov --cov-report=term-missing
uv run python scripts/public_release.py --check
uv build
```

Keep migrations versioned and compatible with existing records. Check installed distributions
when changing prompts, migrations, or packaging. The wheel includes both runtime resources.

Explain the problem, resulting behavior, and relevant validation in a pull request. Use mocks
for provider tests, retain provenance, and preserve exact-payload approval and idempotency.
Never add a send/submit shortcut or treat missing eligibility evidence as a negative fact.
See [agent rules](docs/agent-rules.md) and [safety](docs/safety-and-approvals.md).

Store personal configuration in ignored `_private/config/`, secrets in `.env`, and generated
results in `artifacts/`. Do not commit real applications, CVs, contacts, or provider responses.
New public paths must be added explicitly to the source manifest in `pyproject.toml`.
