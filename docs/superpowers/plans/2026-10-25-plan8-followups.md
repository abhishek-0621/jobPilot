# Plan 8 follow-ups

## Rulings
- Outreach eligibility: SUBMITTED_BY_USER, or CONFIRMED from a non-simulated submission only.
- OUTREACH_SEND is refused at enqueue; JobPilot never sends. APPROVED→SEND_IN_PROGRESS→SENT is user-only (Mark as sent).
- SKIPPED→DRAFT_READY only for `no_public_contact` (user adds a contact); a user skip stays final.
- Opening/date/sign-off are deterministic; the model writes 1–3 sentences with no dates or numbers; deterministic fallback without a model.
- Uses the existing `outreach_draft` model route; approval hash covers outreach id, contact, channel, message and resume.
- Emails accepted only on a non-third-party company domain (`email_domain_ok`), else slug check; company_key strips legal suffixes.
- Retention keeps contacts of drafts in DRAFT_READY/NEEDS_REVIEW/APPROVED.

## Deferred minors (Plan 10)
- Careers-inbox tool passes only the company domain, so no slug fallback when the domain is unknown.
- Slug fallback accepts any TLD (lookalike risk when no company domain is known).
- Careers fetch: public hostname resolving to a private IP is not checked.
- Follow-up messages; JSON API endpoints for outreach.
