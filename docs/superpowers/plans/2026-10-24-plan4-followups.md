# Plan 4 follow-ups (carry into Plan 5+)

## Must handle in Plan 5
- Verify ASSET_HOSTS against real Greenhouse/Lever forms before enabling real submit (CDN hosts may be missing; fails safe).
- Add SUBMIT_APPLICATION capability + real submit/verify_submission/find_existing_submission; keep dry_run gate.

## Incident
- During Task 5 development one test run made a real GET to boards.greenhouse.io/exampleai/jobs/2001 (fixture example URL). Nothing filled or submitted. Now prevented by a hard network guard in tests and zero-field manual handoff.

## Deferred minors
- Task 3: minor (deferred): stop-list over-blocks "Partner company"/"Parent company name" (safe); routes.py and importer.py still hardcode aliases path (carry into Task 4); library per-entry decision unread (Task 5 handles user answers)
- Task 5: minor (deferred): reread VALIDATION_FAILED path untested; theoretical unguarded playwright routes (connect_over_cdp, sync API) unused
- Task 7: minor (deferred): opener wrapper lacks finally; attribute-order-dependent assertion; no e2e for captcha/LLM-suggested

## Rulings
- Ruling: portal_page yields PortalSession(page, blocked) — needed to expose blocked list — cost: interface differs from plan text
- Ruling: new `background` category (criminal, government employment, relatives, conflict of interest, non-compete) → USER_ONLY, precedence after declaration — cost: none
- Ruling: carry remaining misses into Task 3 (arrested, pending charge, family member worked at, former employee, terminated/asked to resign, restrictive covenants, politically exposed, initial here, date signed) — unmatched fields default to ASK_USER anyway, so risk is limited to alias/LLM mis-mapping — cost: none
- Ruling: AUTO_FILL exact-only + third-party stop-token list (referrer/manager/emergency/recruiter/...) → ASK_USER; FormField gains help_text — prevents own data in fields about other people — cost: more questions to user
- Ruling: user-provided = source_fact_id=="user" only (user_values param dropped); uploads confined to upload_root; checkbox groups grouped — cost: none
- Ruling: capabilities {PREPARE_APPLICATION} only until Plan 5 — cost: none
- Ruling: carry into Task 5 — upload-root guard keyed on snapshot field kind (not answer field_type); checkbox_group selector visible-only — cost: none
- Ruling: user-provided answer rows (source_fact_id=="user") are never overwritten by automation; VALIDATE handler does FORM_IN_PROGRESS→VALIDATING→result in one txn; layout change → NEEDS_USER_INPUT (no auto re-prepare) — cost: user re-triggers after layout change
- Ruling: carry into Task 7 — disable checkbox for status!=READY_FOR_REVIEW rows; shared MODEL_SUGGESTED constant (drop confidence heuristic); Rebuild button only when pending/changed; escaping test with <script> label — cost: none
- Ruling: form_posts assertion is vacuous (fixture submit never POSTs) — fix in final wave by making fixture submit really POST to local server — cost: none
