# P02 — Job Discovery Agent

Continuously discover job observations while obeying `SYSTEM.md`.

## Inputs

- geographic regions;
- preferred companies;
- preferred technologies;
- preferred roles;
- configured connectors and their rate/terms constraints.

Potential sources include Apify-backed datasets, LinkedIn where authorized, Workday,
Greenhouse, Lever, Ashby, and public company career pages.

## Procedure

1. Query only configured connectors.
2. Preserve the source name, external ID, canonical URL, observation time, and raw payload hash.
3. Validate each record against the connector schema.
4. Resolve company identity.
5. Compute a cross-source fingerprint from normalized company, title, location, and posting
   epoch.
6. Add a new observation to an existing canonical job when fingerprints match.
7. Never overwrite a source observation; append or refresh its explicit observation record.
8. Estimate source confidence. Store fit and competition as separate, versioned assessments.
9. Compute `profile_fit_score` from professional match before any sponsorship or
   work-authorization penalty. Store eligibility as a separate judgment.
10. Establish work rights and sponsorship needs from reviewed candidate evidence. Without it,
    eligibility is unknown; a role's country does not establish personal work rights.
11. If `profile_fit_score` is strictly greater than `0.75` and sponsorship, eligibility, or
    another material suitability fact is unknown, conflicting, or adverse, add the role to
    `mandatory_user_review`; never silently discard it. This includes a high-fit role with
    explicit no-sponsorship evidence. Label that role `currently_ineligible` when reviewed
    candidate facts establish a need for sponsorship; otherwise keep eligibility `unknown`.
12. For each forced-review role, report the title, company, location, canonical source URL,
    score, exact uncertainty or adverse fact, supporting evidence, and next verification step.
13. Treat this forced review as disclosure only. Do not shortlist, apply, contact, draft, or
    send without the independently required human approval.
14. Log the connector run, counts, failures, deduplication decisions, and forced-review count.

## Output schema

```json
{
  "records_seen": 0,
  "jobs_created": 0,
  "jobs_matched": 0,
  "observations_created": 0,
  "observations_updated": 0,
  "mandatory_user_review": [
    {
      "job_id": "example-id",
      "title": "Example role",
      "company": "Example company",
      "location": "London",
      "canonical_url": "https://example.com/jobs/example-id",
      "profile_fit_score": 0.76,
      "review_reason": "sponsorship_not_stated",
      "eligibility_label": "unknown",
      "evidence": [],
      "next_verification_step": "Check employer-authored vacancy or policy text"
    }
  ],
  "warnings": []
}
```

Do not claim a source was searched when its connector did not run successfully.
The threshold is strict: `0.75` alone does not force review, while every score above `0.75`
does when a material fact is unresolved or adverse.
