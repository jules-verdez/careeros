# Security and private data

CareerOS stores sensitive career and communication records in a local database. The database
and its backups are not encrypted by this application. Use appropriate device protection,
backups, and retention for your own data.

Keep `.env`, credential files, SQLite databases and sidecars, CVs, contact exports, message
drafts, provider payloads, and local assistant configuration out of Git. `.gitignore` covers
common private paths, but it does not remove files already committed.

Before sharing a release, run `python scripts/public_release.py --check`, inspect the export,
and review Git history separately. The checker detects selected credential formats, literal
secret assignments, personal filesystem paths, and non-example email addresses. It is a
heuristic guard, not proof that arbitrary personal data or every secret format is absent.
Exports contain the exact source snapshot that was scanned. Database sidecars, logs, contact
exports, credential containers, and symlinked parent directories are also rejected.

If a credential was committed, revoke or rotate it first. Removing a file or rewriting history
does not invalidate a credential and cannot remove copies held by other people.

Do not post credentials, mailbox contents, or personal records in a public issue. If the
hosting repository offers private vulnerability reporting, use that channel. Otherwise obtain
a private reporting channel from the maintainer before sharing exploit details. No dedicated
security email address or response-time commitment is configured in this source release.

External drafts and sends have separate exact-payload approvals. Application packets remain
local; the operator submits through the employer and records a receipt. Local do-not-contact
rules remain binding even when an action has otherwise been approved.
