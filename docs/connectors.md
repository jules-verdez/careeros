# Connector contract

Connectors are narrow adapters, not alternate business-logic implementations.

## Read connector requirements

- validate credentials during `healthcheck`;
- use documented APIs or authorized exports;
- respect rate limits, access controls, source terms, and robots policy where applicable;
- return Pydantic-validated domain records;
- retain external IDs, URLs, timestamps, and raw payload hashes;
- use bounded retries with jitter only for retryable failures;
- report partial failure explicitly.

## Mutation connector requirements

- accept an approved action only;
- re-check the payload hash immediately before execution;
- require an idempotency key;
- store the provider result ID;
- make retry semantics explicit;
- never broaden scopes at runtime;
- redact secrets and unnecessary personal content from logs.

## No silent fallback

A missing connector raises `ConnectorNotConfiguredError`. Tests may inject fakes explicitly,
but production commands must never replace a failed source with sample data.

## Readiness checks

`career doctor --live` verifies:

- the official Apify remote MCP endpoint is enabled with OAuth in Codex;
- the optional Apify CLI binary and its separate credential-file state;
- encrypted Google Workspace OAuth, Gmail/Calendar scopes, and a read-only Gmail profile call.
- local key presence for Hunter, Prospeo, and Brave; doctor never spends a Prospeo credit or
  Brave request;
- availability of the unauthenticated UKVI register connector without downloading its CSV.

It never prints tokens, reads message content, sends mail, or runs an Apify Actor.

## Initial connector

`LocalJsonJobConnector` ingests controlled, real exports. It is useful for air-gapped tests,
manual exports, and replayable discovery runs. It does not claim to have queried the source
itself.

## Free boundaries come first

Three connectors cost nothing per call and therefore run in front of every metered one.
A vendor credit spent on a question public infrastructure answers is a credit wasted.

- **DNS and SMTP deliverability**: mail-exchanger lookup over DNS-over-HTTPS, and an
  opt-in SMTP recipient probe. The DNS half is free, anonymous and decisive in one
  direction: a domain with no mail exchanger cannot receive mail. The SMTP half opens a connection to
  a third party, is blocked on most networks, and proves nothing at a catch-all domain,
  so it requires an explicit per-call authorization and reports catch-all status rather
  than hiding it inside a score.
- **GitHub commit metadata**: addresses their owners published themselves by pushing a
  commit. Authenticated callers get five thousand requests an hour at no cost, and the
  `.patch` view of a commit is a permanent public URL on which the address is visible,
  which is exactly the citation a stored email requires. This replaces paying a scraper
  per result for the same data.
- **Company pages**: the addresses a firm prints on its own `/team`, `/about` or
  `/contact` pages, across the apex host and the usual `careers` and `jobs` subdomains.
  `robots.txt` is read and honoured. This produces the strongest provenance available
  anywhere in the pipeline, because the page both publishes the address and cites it.

Provenance is ranked, not uniform. A company page is the strongest source: the address is
published for contact and the page proves it. A paper's corresponding-author line is
equally strong. Commit metadata is weaker, because pushing a commit is not an invitation
to be contacted and many authors do not realise the address is public at all. A
pattern-built address is not a source, and a search-results URL is not a citation.

## Implemented provider adapters

- Public ATS: Greenhouse, Lever, Ashby, and SmartRecruiters. SmartRecruiters pages its public
  Posting API and downloads full details only after local title/location/freshness filtering.
- Gmail through `gws`: exact draft/send execution plus least-privilege reply and bounce
  reconciliation.
- UKVI sponsor register: follows the current CSV link from GOV.UK, validates the official asset
  host, hashes the file, and uses conservative name/alias matching.
- Hunter.io: primary public-work-address discovery and verification.
- Prospeo: opt-in fallback only after a stated Hunter failure. One invocation can spend at most
  one credit and never requests mobile data.
- Brave Search: opt-in, one-request source discovery. Results are rendered but snippets are not
  written to the CareerOS database.

Paid-capable reads require an explicit CLI cost flag even when the account currently has free
credits. This keeps cost authorization stable when a vendor changes plan or allowance.

## Additional library adapters

These adapters are available to Python integrations; they have no command-line entry point.
Callers must enforce their own explicit cost and data-sharing approval before invoking them.

- `ApifyActorRunner` runs any Apify Store actor. It reads `users/me/limits` first and refuses a
  cap larger than what remains of the monthly limit, because Apify silently lowers caps to the
  remainder and concurrent runs then overshoot. Every run passes `max_total_charge_usd`.
- `ExploriumConnector` (Vibe Prospecting) uses Explorium's documented API with the `api_key`
  header: business match, prospect search, contact enrichment, and the credit balance.
- `ClayWebhookConnector` posts one JSON row per submission to a Clay table's webhook source,
  over https only, with an optional token header.

