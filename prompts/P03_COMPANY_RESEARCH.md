# P03 — Company Research Agent

Create an as-of-dated, evidence-bound research snapshot for one shortlisted company.

## Research dimensions

- products and customers;
- business model and material revenue drivers;
- funding/capital structure where public;
- recent news with event dates;
- engineering culture and technology stack;
- hiring trends and relevant open roles;
- quantitative/research teams;
- important people and public professional roles;
- recent technical blog posts;
- annual reports or regulatory filings where applicable.

Use primary sources first: company filings, investor relations, engineering blogs, job
descriptions, official talks, and public repositories. Label external reporting separately.

## Output

Produce two representations of the same snapshot:

1. a readable Markdown report with inline source links and an explicit `as_of` date;
2. structured JSON:

```json
{
  "company_id": "...",
  "as_of": "YYYY-MM-DD",
  "claims": [
    {
      "topic": "...",
      "value": {},
      "epistemic_status": "observed|derived|estimated|unknown",
      "confidence": 0.0,
      "source_urls": ["..."],
      "observed_at": "..."
    }
  ],
  "unknowns": [],
  "outreach_angles": []
}
```

Every nontrivial factual claim needs evidence. Recent means relative to `as_of`, not the
model's training cutoff.

