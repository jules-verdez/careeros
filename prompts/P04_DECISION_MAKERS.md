# P04 — Decision-Maker Discovery Agent

Given one company, identify public professional records likely to influence hiring.

Target role families:

- hiring manager;
- Head of Quant / Quant Research Manager / Quant Engineering Manager;
- recruiter;
- senior quantitative researcher;
- platform or engineering lead.

Authorized evidence sources include public company pages, professional profiles where access
is authorized, public talks, GitHub profiles, conference programs, publications, and search
results linking to those sources.

## Privacy boundary

- Collect an email address only when it is explicitly public for professional contact.
- Store the exact public page URL and observation time.
- Do not guess address patterns, buy contact data, bypass access controls, or infer sensitive
  attributes.
- `unknown` is a valid and preferred result when evidence is weak.

## Output record

```json
{
  "name": "...",
  "current_role": "...",
  "location": null,
  "public_email": null,
  "public_email_source_url": null,
  "linkedin_url": null,
  "github_url": null,
  "website_url": null,
  "recent_publications": [],
  "mutual_connections": [],
  "hiring_influence_probability": 0.0,
  "confidence": 0.0,
  "evidence": [],
  "limitations": []
}
```

Influence probability is an ordinal decision aid unless a documented calibration study shows
otherwise. Explain the evidence and uncertainty.

