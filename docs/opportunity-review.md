# Opportunity review

`career opportunities` turns saved job assessments into a review queue. It reads local state;
it does not fetch vacancies, shortlist roles, create approvals, or contact providers.

```bash
uv run career opportunities
uv run career opportunities --sort priority --limit 10
uv run career opportunities --json > ./opportunities.json
```

The output has two disjoint sections:

- `mandatory_user_review`: every assessed role with professional fit strictly above `0.75`
  and unknown, conflicting, adverse, or otherwise unresolved material evidence. This section
  is uncapped, including dismissed, applied, closed, or archived records with unresolved evidence.
  Workflow status remains visible; inclusion never revives a role or authorizes an action.
- `opportunities`: other discovered or shortlisted roles, capped by `--limit`. Explicitly
  ineligible roles are excluded from this ordinary queue. Inclusion is a research suggestion,
  not evidence of eligibility or permission to apply.

Both sections show the role, company, location, best saved vacancy URL, professional fit,
eligibility label, unresolved fact, evidence, and next verification step. The JSON also
includes confidence, priority, assessment identity, and discovery/observation/posting dates.
Missing URLs and evidence are disclosed as missing, not invented.

`--sort recent` (the default) orders by first discovery in CareerOS. It does **not** mean the
employer published the vacancy recently. `--sort priority` uses the documented fit/confidence/
competition heuristic. Neither order is a hiring probability. Saved evidence is not a live
availability check.

## Updating an assessment

`--fit` means professional match **before** sponsorship or work-authorization penalties.
Save eligibility evidence in a separate JSON file, for example `review.json`:

```json
{
  "eligibility_label": "currently_ineligible",
  "unresolved_fact": "The employer excludes sponsorship for this UK vacancy.",
  "evidence": ["Example employer wording: sponsorship is not available for this role."],
  "canonical_url": "https://example.test/jobs/researcher",
  "next_verification_step": "Verify whether the employer restriction has changed."
}
```

The example is illustrative; replace it with the actual source and evidence you reviewed.

```bash
uv run career assess JOB_ID --fit 0.86 --confidence 0.80 --competition 0.50 \
  --why "Verified professional match against the CV." --review-file ./review.json
```

Supported `eligibility_label` values:

| Label | Meaning |
| --- | --- |
| `unknown` | A vacancy-specific eligibility conclusion has not been established. |
| `confirmed` | The reviewer checked this candidate's vacancy-specific requirements and work rights. |
| `conflicting` | Recorded material evidence conflicts and needs resolution. |
| `currently_ineligible` | An explicit requirement currently prevents proceeding. |

Every structured review needs a vacancy URL and a next verification step. Conclusions other
than `unknown` require nonblank evidence. `conflicting` and `currently_ineligible` also require
an `unresolved_fact`. Use that field for other material concerns even when eligibility is
confirmed: unknown salary, experience equivalence, or another unresolved requirement will
still force high-fit human review. Unknown sponsorship is not evidence of unavailability;
a company sponsor-register match does not establish vacancy-specific eligibility.

Work rights and sponsorship needs must come from reviewed candidate evidence. Record that
evidence in a structured review. Without it, eligibility remains unknown, including when a
vacancy excludes sponsorship. The software does not infer personal work rights from a role's
country, nationality, a company licence, or provider-enriched metadata.

Without `--review-file`, the new assessment has unconfirmed eligibility. The same manual
scoring version is reassessed; the audit captures the new professional fit and review data.
Services may supply other scoring versions. The current view uses the latest `assessed_at`
for each job and candidate, breaking exact timestamp ties deterministically by assessment ID.
Older assessment versions cannot displace the current version because of a higher score.

Historical research fields (`profile_fit_score`, `eligibility`, `concern`/`gap`, `evidence`,
`next_step`/`next`, and saved URLs) are interpreted without rewriting existing records. If an
explicit professional-fit score is absent, the queue labels `fit_score` as a legacy fallback
whose pre-eligibility basis was not recorded. Arbitrary legacy labels do not confirm eligibility.
Malformed structured reviews remain visible as unconfirmed and requiring correction.

## Daily operation

`career jobs`, `career review`, and `career dashboard` also include the complete mandatory
section. Job limits and the company sponsor filter only affect their ordinary output.
Programmatic daily-agent results expose the same queue, and agent-run output retains the
mandatory evidence. A completed review task does not resolve an eligibility question: record
a new, evidenced assessment to update the disclosure.

`career review` reconciles local tasks. It cancels obsolete automatically generated role-review
tasks when assessments or job status change, preserves completed tasks, and does not create
duplicate tasks on replay. It does not create applications, messages, or approval requests.

The dashboard labels its persisted brief by date and generation time. Its mandatory-review
section is recomputed from current saved assessments and has its own timestamp, so an older
brief cannot hide a newly recorded concern. Saved brief lookup is scoped to the active candidate.

Historical SQLite `ARCHIVED` job statuses are recognized and remain terminal. No schema
migration or live database rewrite is needed for this SQLite workflow.
