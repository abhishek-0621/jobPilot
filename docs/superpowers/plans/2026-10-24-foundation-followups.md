# Foundation follow-ups (carry into Plan 2+)

## Hard prerequisite for any edit UI
- Every answer/artifact/URL edit must call ApprovalService.invalidate_application (no caller exists yet). Add one repository-level mutator + a test that every write path revokes.

## Deferred minors (from task reviews)
- Task 1: minor (deferred): config.py read_text no encoding; test_loads_routes cwd-relative
- Task 2: minor (deferred): SubmitResult.error_code is str not ErrorCode; extra-forbid test only on FieldAnswer
- Task 3: minor (deferred): redact passes sets/BaseModels through unredacted; naive datetime→UTC untested; allow_nan default
- Task 4: minor (deferred): REPLACE bypasses audit triggers; alembic.ini path only in source checkout; upsert_job check-then-insert; no tests for outreach UNIQUE / NOT NULL / FKs; migration style
- Task 5: minor (deferred): details can overwrite from/to/reason keys; invalid stored status raises ValueError; thin edge tests for Batch/Job/AgentRun; [-1] in test fragile
- Task 7: minor (deferred): may_submit test omits approved_item_hash; _approvals_containing bare json.loads; tie-break relies on ULID ordering; answer hash excludes label/field_type
- Task 8: minor (deferred): 4xx (429/408) mapped to INVALID_SCHEMA (all MODEL_* retry anyway); aclose closes injected client; loc echo in repair msg; available_models malformed body; case-variant untrusted tag; missing timeout-fallback tests
- Task 9: minor (deferred): fill_fields replaces not accumulates; repeat-key under fail_mode untested; no RED captured
- Task 10: minor (deferred): lease==agent timeout; select-then-update lease not multi-worker safe; count via len(list()); outreach dry_run=False denial untested
- Task 11: minor (deferred): /jobs max score vs detail latest score inconsistency; health_check.py traceback on conn error; TestClient not closed
- Task 12: minor (deferred): N+1 in batch page; seed sys.path hack; pytest -W error collection warning
- Task 12: minor (deferred): orphan sweep may re-queue apps whose SUBMIT FAILED but stayed APPROVED (gate-safe, repeat retries); host allow-list hard-coded

## Rulings made during execution
- Ruling: work on feature branch in the only checkout — repo was just created, nothing to isolate from — cost if wrong: none, branch is disposable)
- Ruling: UI = Jinja2+HTMX in the FastAPI process instead of spec's Streamlit — user asked for a final production design with no later switching and easy setup — cost if wrong: dashboard work is redone in Streamlit (small).
- Ruling: extend existing .gitignore rather than create — cost: none |
- Ruling: APPROVED — our state machine moves an app to APPROVED on approval, spec's intent (approved + valid) is preserved — cost: none |
- Ruling: add extra="forbid" to AppConfig/ModelRoute (typo'd dry_run must not be ignored) — carried into Task 2 dispatch — cost: none
- Ruling: SQLite returns naive datetimes — add UTCDateTime TypeDecorator (attach UTC on load) now, carried into Task 5 dispatch — Task 10 lease/backoff comparisons need aware datetimes — cost if wrong: small refactor
- Ruling: outreach SKIPPED not allowed from SEND_IN_PROGRESS/DELIVERY_UNKNOWN (narrower than plan "any non-terminal") — a send may be in flight; spec forbids claiming outcomes without evidence — cost if wrong: user cannot skip an unknown-delivery draft (minor)
- Ruling: no SUBMISSION_IN_PROGRESS→CONFIRMED edge; Task 10 must record SUBMITTED then CONFIRMED (two events) — keeps "clicked submit" and "confirmation seen" as separate evidence per spec §3 — cost: one extra event
- Ruling: may_submit gets extra kwarg approved_item_hash (from batch_item) — ApprovalRow stores only batch hash, so per-app hash must come from caller — cost: Task 7/10 signatures differ from plan text by one kwarg
- Ruling: outreach approvals are per-draft (approval.content_hash = draft hash) — resolves reviewer ⚠️ — cost: no multi-draft outreach batches (fine: one per company)
- Ruling: gate hardening (approved_item_ids_json must decode to list else not_approved; empty hashes = mismatch; GateResult/PolicyConfig frozen+forbid) carried into Task 7 dispatch — safety gate, cheap — cost: none
- Ruling: re-approving a PARTIALLY_APPROVED batch is allowed; never clear prior selected flags — supports approving a batch in several sittings — cost: none
- Ruling: SIM confirmation ref uses hex after "sha256:" (plan's key[:8] was a plan bug) + deep-copy default form — carried into Task 10 dispatch — cost: none
- Ruling: pluggable handlers dict for non-side-effect kinds (Plans 2-6 add real handlers without touching loop); OUTREACH_SEND simulated-only in this plan — cost: small API surface beyond plan text
- Ruling: "max 3 attempts" = 3 total tries with waits 2s then 8s; 32s step unused — plan says max 3 and lists 2/8/32; total-attempts reading is the conservative one — cost: one fewer retry for flaky model calls
- Ruling: keep filterwarnings for Starlette TestClient deprecation, no httpx2 dep (classifier denied adding it; not needed) — cost: none
- Ruling: carry into Task 12 — CLI start binds uvicorn to cfg.bind_host; worker_loop guards run_once with log+sleep; /applications 404 only on NoResultFound — cost: none
- Ruling: approve endpoints enqueue SUBMIT for newly approved apps after commit (plan never said who enqueues SUBMIT) — approval is the user trigger; gate still re-checks — cost: none
- Ruling: vendor htmx.org@2.0.4 from unpkg, sha256 recorded — pinned public lib, no CDN at runtime — cost: none
- Ruling: SUBMIT enqueued in same txn as approval + startup sweep for orphaned APPROVED — supersedes earlier "after commit" ruling — cost: none
- Ruling: Important #1 (invalidate_application has no caller) deferred to Plan 2 as HARD PREREQUISITE for any edit UI — foundation has no edit path and may_submit already blocks on hash mismatch — cost if forgotten: edited apps stay shown as APPROVED (still never submitted)
