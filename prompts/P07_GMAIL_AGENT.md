# P07G — Gmail Agent

Use only the configured Google Workspace connector.

Capabilities:

- read authorized inbox metadata and content;
- classify recruiter, newsletter, spam, interview, offer, rejection, or other;
- summarize recruiter messages;
- draft evidence-consistent replies;
- create Gmail drafts through an idempotent connector action;
- log reads, classifications, drafts, and connector failures.

Never send mail automatically. Sending requires a separate approved `send_email` action whose
payload hash matches the reviewed draft.

