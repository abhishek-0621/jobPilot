# Plan 2 follow-ups (carry into Plan 3+)

## Must handle in Plan 3
- Artifacts citing facts that changed on re-import must be invalidated (importer only checks answers today).
- Profiles imported before experience_ranges existed fall back to company_start until re-imported.

## Deferred minors
- Task 1: minor (deferred): O(n²) changed check; artifacts citing changed facts not invalidated (Plan 3 must); duplicate fact_id → DB error not schema error
- Task 2: minor (deferred): category path not urlencoded; library values strings; clear-missing still logs; private importer imports
- Task 3: minor (deferred): orphan revoke branch ignores live SUBMIT (gate re-checks status); default ports not stripped; re-seen/missing-location dedupe tests
- Task 4: minor (deferred): \b fails for c++/.net excludes; double html_to_text; per-instance limiter; naive updated_at
- Task 5: minor (deferred): malformed extracted_json replaced silently; summary truncation mid-word; import order
- Task 6: minor (deferred): location substring match ("Pune" in "Puneet"); exclude check skips description; unknown mode=60
- Task 7: minor (deferred): discovery double-enqueue race; jobs page N+1; client leak if lifespan never runs; sources check KeyError on unknown source; no app_from_config wiring test
- Task 8: minor (deferred): fixture labels not verbatim; hint string replace fragile; test import style
- Task 9: minor (deferred): injected-text e2e doesn't assert untrusted fence (covered in unit tests); dead Pipeline.runtime field

## Rulings made during execution
- Ruling: no worktree — single checkout, nothing else in flight — cost: none
- Ruling: use build_runtime(cfg, session_factory, *, client=None) — cost: none |
- Ruling: facts updated in place (fact_id sole PK), no per-version fact history — events record changes; migration not worth it now — cost: can't diff old fact text later
- Ruling: invalidate_application must revoke per-item, not the whole multi-app approval — carried into Task 2 (revoked_item_ids_json + gate) — siblings otherwise stuck APPROVED-but-unsubmittable — cost: one migration
- Ruling: carry into Task 3 dispatch — set_artifact checks ownership+kind; orphan sweep moves item-revoked APPROVED apps back to READY_FOR_REVIEW — cost: none
- Ruling: carry into Task 4 — html_to_text iterative walk (no RecursionError on deep untrusted HTML) — cost: none
- Ruling: carry into Task 5 — per-posting ValidationError skip (event), CompanyBoard.token pattern ^[A-Za-z0-9_-]{1,64}$, drop assert in finder — cost: none
- Ruling: carry into Task 6 — replace assert ctx.gateway with explicit raise; add agents/prompts/__init__.py — cost: none
- Ruling: weighted score example is 81.5 not 80.5 (plan arithmetic bug; 24+25+12+10.5+10) — cost: none
- Ruling: make_match_handler(prefs) factory instead of plain handler — handlers get no prefs — cost: Task 7 wires factory
- Ruling: failed hard filters carried as "hard_filter:<name>" entries in concerns — MatchAssessment contract has no field — cost: concerns list mixes kinds
- Ruling: no linter has ever run — add ruff as dev dep + CI lint step + fix findings in Task 9 — cost: some churn
