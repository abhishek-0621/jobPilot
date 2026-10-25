# Plan 9 follow-ups

## Rulings
- Essay drafting runs inline in PREPARE_FORM / MANUAL_PACKAGE (no new task kind); handlers stay the only writers.
- Essay answers can never be AUTO_FILL (mutations + handler guard); sources `essay:<ids>`, `essay:canned:<key>`, `library:essay_<key>[:<ids>]`.
- A story or canned change sends dependent answers (incl. VALIDATING, SUBMISSION_FAILED apps and library entries) back to ASK_USER and revokes approval.
- `verified: true` in the user's own file counts as verification, but never overrides a dashboard Unverify unless the story changed.
- Canned answers with numbers unsupported by stories/facts stay FILL_AND_REVIEW with a "check its numbers" flag.
- SWOT answers must be exactly four labelled parts; the whole text is claim-checked.
- Reason-for-leaving phrasings are excluded from essays (bare "left" also excludes; errs safe).

## Deferred minors (Plan 10)
- FormField has no maxlength; essays default to 1 200 characters.
- Task 4 wiring tests were written just after implementation.
