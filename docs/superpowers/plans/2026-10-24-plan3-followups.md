# Plan 3 follow-ups (carry into Plan 4+)

## Must handle in Plan 4
- READY_FOR_REVIEW gate must also require current artifacts' profile_version == active (same check as handlers).
- Form agent must use cover_letter_text artifact for text fields and resume PDF/DOCX per portal; attach only via set_artifact-bound ids.

## Deferred minors
- Task 1: minor (deferred): skill facts not in profile sorted last; contact line filters `source`; thin docx test
- Task 3: minor (deferred): file n vs DB version drift; diff json written pre-commit; start_tailor race
- Task 4: minor (deferred): orphan pdf if txt write fails; handler doesn't cross-check company/role
- Task 5: minor (deferred): unused orch param in regenerate_documents; any re-import discards in-flight docs even if unrelated facts changed; e2e __class__ swap
- partial role mention not masked in cover letter validation (fails safe)

## Rulings made during execution
- Ruling (in plan): render in async agent via artifact_dir tool; handler records rows — cost: none |
- Ruling: carry into Task 3 — html_to_pdf wraps Playwright errors in DocRenderError; fit_one_page reuses one browser and deletes overlong out on failure; chromium_available documented import-time only — cost: none
- Ruling: single-word capitalized word not in source/tech/skills = new_proper_noun; leadership claim-phrase list; unit class on numbers (no noun adjacency) — conservative anti-fabrication; may reject some legit rewrites (they fall back to original text, safe) — cost: fewer rewrites
- Ruling: carry into Task 4 — summary source ids added to used_fact_ids; one shared skill-split helper; skill group from fact row `field` not id — cost: none
- Ruling: carry into Task 5 — other_companies excludes employers named in profile roles/cited facts; placeholder TODO/XXX case-sensitive; test that paragraph fabrication (invented employer/number) is rejected — cost: none
- Ruling: stale-docs fix by stamping artifacts with profile_version and gating TAILOR/COVER_LETTER handlers on active version (re-queue TAILOR on mismatch) — facts update in place so ids alone cannot detect value change — cost: one migration
- Ruling: extra regenerable states (NEEDS_USER_INPUT, VALIDATION_FAILED, APPROVED via invalidate) accepted
