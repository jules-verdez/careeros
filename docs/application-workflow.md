# Application workflow

CareerOS owns the local record of an application while you retain control of every external
submission. The workflow is:

```text
tracked -> vacancy checked -> evidence pack prepared -> approval requested
       -> exact pack reviewed -> human submits -> receipt recorded
       -> next action -> screening/interview/offer -> outcome and learning
```

## Candidate evidence

Create a reviewed JSON snapshot. Every fact needs a source; the source can be a CV section,
a project URL, or another document that you have checked.

```json
{
  "name": "Demo Candidate",
  "headline": "Quantitative research and data systems",
  "facts": [
    {
      "key": "python",
      "kind": "experience",
      "text": "Built Python reporting tools.",
      "source": "reviewed-cv.md#experience",
      "tags": ["Python"]
    },
    {
      "key": "research-project",
      "kind": "project",
      "text": "Built an auditable research simulator.",
      "source": "https://example.test/project",
      "tags": []
    }
  ]
}
```

Importing the file creates an immutable candidate version. A changed snapshot creates a new
version; existing packets continue to point at the old version. `profile diff` shows the factual
change before a new pack is prepared.

## Application specification and packet

The packet specification selects evidence and maps application questions to evidence keys:

```json
{
  "evidence_keys": ["python", "research-project"],
  "answers": [
    {
      "question": "Describe a relevant project.",
      "evidence_keys": ["research-project"]
    }
  ]
}
```

The saved packet contains the exact candidate version, vacancy verification, selected facts,
generated CV markdown, answers, and a SHA-256 content hash. Requirements are included as a
checklist; the assembler does not claim that you satisfy an unverified requirement.

Approval requires a current official posting, an open status, a named human reviewer, and a
vacancy-specific eligibility review with no unresolved material issue. A high professional fit
with unknown or adverse sponsorship remains disclosed for review and cannot become actionable
through this workflow.

## Applications and interviews

`application track` is idempotent for a job. `record-submission` requires a timestamp and receipt
and records the exact packet used. It does not submit anything. Stage changes require employer
evidence; your reflection is stored separately. `application funnel` reports counts by role family
and source, with pending applications visible and no invented hiring probabilities.

Interview preparation selects evidence from a candidate version and stores the plan locally.
Scheduling checks overlaps and preserves the supplied timezone. Rescheduling moves the preparation
task; debriefing records employer feedback separately from reflection and creates the next local
action. Calendar invitations are not created by these commands.

## Vacancy checks

`vacancy check` reads one operator-selected official URL, honours its `robots.txt` policy, follows
public redirects, bounds the response size, and parses matching `JobPosting` metadata when present.
HTTP failures, ambiguous dates, missing structured metadata, and a page that does not match the
role are retained as `unreachable` or `unknown`; they are never silently treated as closed.
Verification records are timestamped, hash-protected, and stale after 48 hours for submission
approval. The saved opportunity queue still remains a research view until this check is made.
