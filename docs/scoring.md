# Scoring and measurement

CareerOS stores three distinct judgment inputs for a job \(j\):

- \(f_j \in [0,1]\): fit;
- \(c_j \in [0,1]\): confidence in the assessment;
- \(q_j \in [0,1]\): estimated competition.

The initial ordering proxy is

\[
s_j = f_j c_j \left(1 - \lambda q_j\right), \qquad \lambda = 0.35.
\]

This is an explicit heuristic, not a calibrated probability and not a causal estimate of offer
likelihood. Its purpose is stable queue ordering while outcome data are sparse.

## Mandatory high-fit review boundary

Sponsorship and work authorization must not be hidden inside professional fit. Define

\[
r_j \in [0,1]
\]

as the candidate-to-role professional match, computed before any immigration or sponsorship
penalty, and let \(e_j\) be the separately evidenced eligibility state. Establish the candidate's
work rights and sponsorship requirements from reviewed profile facts. Without that evidence,
eligibility remains unknown; the software has no country-specific personal default.

The forced-review indicator is

\[
R_j = \mathbf{1}\!\left[r_j > 0.75\right]
      \mathbf{1}\!\left[e_j \in
      \{\text{unknown},\text{conflicting},\text{unavailable}\}
      \;\lor\; u_j = 1\right],
\]

where \(u_j\) denotes another material unresolved suitability fact. Whenever \(R_j=1\), the
role must appear in `mandatory_user_review`, even if it remains ineligible or excluded from
the actionable shortlist. The report must include the source URL, \(r_j\), the exact issue,
the supporting evidence, and the next verification step. Missing vacancy-level sponsorship
text is `unknown`, not proof that sponsorship is unavailable.

This is a strict boundary: \(r_j=0.75\) does not trigger it, while \(r_j>0.75\) does. Forced
review is a visibility rule only and never authorizes an application or outreach action.

The runtime implements this boundary in `career opportunities`, the job listing, and daily
review/dashboard views. `--limit` and `jobs --sponsor-only` cannot suppress mandatory entries.
Ranking first selects the latest assessment per candidate/job, then applies the priority
formula using the explicit professional-fit score. Historical assessments without that field
use a clearly labelled legacy `fit_score` fallback. See [Opportunity review](opportunity-review.md)
for structured eligibility evidence and update commands.

## Calibration plan

Once enough applications exist:

1. freeze feature definitions before evaluation;
2. use time-ordered validation to prevent future leakage;
3. measure calibration with reliability curves and Brier score;
4. compare ranking utility using precision-at-\(k\) and expected operator-time value;
5. stratify by role family, source, geography, and outreach channel;
6. retain propensities or randomized exploration where feasible to reduce selection bias;
7. never train on model-generated labels as if they were observed outcomes.

Conversion rates should be reported with denominators and interval estimates. Small samples
must not drive aggressive workflow changes.
