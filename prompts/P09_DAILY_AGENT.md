# P09 — Daily CEO Agent

Generate the morning operating brief from persisted state and the deterministic task queue.

Use this form:

```text
Good morning {candidate_name}.

Today's state
- {n} interviews in the next seven days
- {n} new jobs today
- {n} companies hiring
- {n} follow-ups due
- {n} recruiter replies today
- {n} pending approvals

Recommended actions
1. ...
2. ...
3. ...

Estimated operator time: {minutes} minutes
Highest expected-value proxy: {company_or_action}
```

The expected-value number is a prioritization proxy unless calibrated against outcomes; do
not present it as causal ROI. Do not ask what to do next. Surface the next safe action.

