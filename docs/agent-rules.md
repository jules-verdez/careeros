# Agent rules

These rules apply when discovering, scoring, ranking, or recommending roles.
Operator-specific policy belongs in an ignored local `AGENTS.md` and reviewed profile facts.
Keep personal work-authorization evidence and contact exclusions out of public examples.

- Establish work rights and sponsorship needs from reviewed candidate evidence. Without
  that evidence, eligibility is unknown; geography alone never establishes a personal status.
- Compute `profile_fit_score` from the professional match before sponsorship or
  work-authorization penalties. Eligibility and professional fit are separate judgments.
- If `profile_fit_score` is strictly greater than `0.75`, never silently remove a role
  because sponsorship, eligibility, or another material fact is unknown, conflicting, or
  adverse. Disclose it in `mandatory_user_review`.
- Explicit no-sponsorship evidence still requires high-fit disclosure. Label it
  `currently_ineligible` when reviewed candidate facts establish that sponsorship is needed;
  otherwise keep eligibility `unknown`. Neither state is actionable.
- Show the title, company, location, canonical URL, score, exact uncertainty or adverse
  fact, evidence, and next verification step. Missing evidence means unknown.
- The threshold is strict: `0.75` does not trigger mandatory review.
- Mandatory review is disclosure, not approval. It never authorizes shortlisting,
  applying, contacting, drafting, or sending.
- Honour local do-not-contact exclusions across drafting, queuing, and execution.
  A human approval is not permission to override an exclusion.
