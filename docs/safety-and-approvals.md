# Safety and approvals

## Policy

CareerOS distinguishes reversible local work from consequential external actions.

| Operation | Default |
|---|---|
| Read free configured source | Allowed and logged |
| Use a paid-capable read API | Explicit per-request cost flag and connector log |
| Store/transform local facts | Allowed and logged |
| Generate local draft | Allowed and logged |
| Create external draft | Connector-specific, logged, idempotent |
| Send email/message | Exact human approval required |
| Submit application | Exact human approval required |
| Create calendar commitment | Exact human approval required |

## Exact-payload approval

For a canonical payload \(x\), CareerOS stores

\[
h = \operatorname{SHA256}(\operatorname{CanonicalJSON}(x)).
\]

The human approves \(h\), not a mutable draft. An executor must recompute the hash and
reject execution when it differs. Every operation also has a unique idempotency key, so a
retry cannot intentionally create a second external action.

## Do-not-contact list

`CAREEROS_DO_NOT_CONTACT` names people CareerOS must never reach: full names, exact addresses,
or `@domain` entries. The list is enforced in the services, not the interface, at every step:
staging a draft, queuing a send, approving either action, and immediately before execution, so
an exclusion added after an approval still stops the provider call. An approval never overrides
an exclusion. Blocked requests fail with an explicit explanation.

## Public contact boundary

A sendable email in `person_contacts` may be stored only when:

1. it is explicitly public for professional contact;
2. the exact public source URL is retained;
3. the workflow does not infer a pattern or sensitive attribute.

This is a product guardrail, not a complete legal determination. Deployment must still account
for applicable privacy, direct-marketing, platform-terms, retention, and deletion obligations.

Prospeo results are stored only as `contact_enrichment_observations`. They do not become
sendable contacts automatically. `--spend-one-credit` authorizes exactly one named-person
lookup, and the connector never requests mobile numbers. Brave similarly requires
`--spend-one-request`; its result snippets are displayed transiently and are not persisted.

The UKVI register is a company-level prefilter. A licence match is not evidence that a
particular vacancy offers sponsorship; only employer-authored vacancy text can establish that.

Application packets are local, immutable review objects. Their candidate version, vacancy
verification, selected evidence, answers, and SHA-256 hash are checked again at approval time.
Changing the candidate evidence or vacancy record invalidates the request. `career action approve`
only approves the exact packet; CareerOS has no browser/form executor. The operator records a
submission receipt separately after submitting through the employer's process.

Official vacancy checks are bounded read operations. CareerOS honours `robots.txt`, validates
public redirects, limits the response size, and stores `unknown` or `unreachable` when the page
cannot establish an exact open posting. A failed fetch is never interpreted as a rejection.

## Audit chain

For event \(i\),

\[
H_i = \operatorname{SHA256}\left(
  \operatorname{CanonicalJSON}(E_i, H_{i-1})
\right).
\]

`career audit verify` recomputes the chain. This detects in-database edits/deletions but does
not by itself protect against an attacker replacing the entire database and audit history.
Production backups should checkpoint the latest hash to an independent append-only store.
