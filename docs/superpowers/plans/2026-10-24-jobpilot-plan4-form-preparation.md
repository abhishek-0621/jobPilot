# JobPilot Plan 4: Application Form Preparation & Batch Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For each application in `FORM_IN_PROGRESS`, JobPilot opens the real Greenhouse or Lever application form in a visible, isolated Chromium window. It then works through the form in nine steps:

1. Read every field.
2. Map each field to a verified profile value, an answer-library value, a generated document, or `ASK_USER`/`USER_ONLY`.
3. Fill only the safe fields.
4. Upload the tailored resume and cover letter.
5. Read the form back to verify what was entered.
6. Save a screenshot as evidence.
7. Stop before submitting.
8. Let the user answer open questions in the dashboard.
9. Move the application to `READY_FOR_REVIEW`.

Applications are then grouped into a batch the user can review and approve, with every answer and document visible.

**Architecture:**
- **Portal adapters** implement the existing `PortalAdapter` protocol with Playwright and accessible locators. This plan covers `GreenhouseAdapter` and `LeverAdapter`.
- **Field mapping** is deterministic first, using `config/field_aliases.yaml` plus the profile and answer library. The local LLM is consulted only to suggest a canonical key for labels the rules can't match. Its suggestion is a hint that is always `FILL_AND_REVIEW`.
- **Preparation runs in two phases:**
  - `PREPARE_FORM` inspects the form, maps fields and stores the `FormFieldAnswerRow`s.
  - `VALIDATE` refills the form in a fresh context from the stored answers and reads it back, producing a validation report and a screenshot. It never clicks submit.
- **Submission** is Plan 5. The `submit()` method exists on the adapters but is gated by the existing policy and is not called in this plan.

**Tech Stack:** existing stack. Playwright is already a dependency, and Chromium is installed by `jobpilot setup`.

**Spec:** §4.4, §10, §5.3, §7 · Follow-ups: `docs/superpowers/plans/2026-10-24-plan3-followups.md`

## Global Constraints

- **Never submit in this plan.** Adapters may define `submit()`, but no task kind in this plan calls it. A test asserts the submit button is never clicked; the fixture form records a click.
- **Never guess.** Each field gets one decision from §10.2:
  - `AUTO_FILL`: exact alias match to a verified profile/identity value, or to a generated artifact.
  - `FILL_AND_REVIEW`: an answer-library value, an LLM-suggested mapping, or free text from the cover letter.
  - `ASK_USER`: a required field with no value.
  - `USER_ONLY`: demographics, declarations, work authorization, sponsorship, DOB, terms. These are never filled, even when a value exists.
  - `BLOCKED`: captcha, login wall, or an unsupported widget.
  - `NOT_APPLICABLE`: optional and unmapped.
- **Required `USER_ONLY`/`ASK_USER` fields** keep the application in `NEEDS_USER_INPUT` until the user answers in the dashboard. The user's answers are stored via `mutations.set_answer` with decision `USER_ONLY` (user-provided) and are filled in the next VALIDATE.
- **CAPTCHA, login or MFA detected** (iframe src containing `recaptcha`/`hcaptcha`/`turnstile`, password inputs, or text "verify you are human"): the decision is `BLOCKED`, the application goes to `NEEDS_USER_INPUT` with reason `manual_handoff`, and nothing is bypassed.
- **Isolated browser profile** under `storage/browser_state/` (git-ignored). Headful by default (`cfg.browser_headless: bool = False`). Navigation is allowed only to the job's application URL host plus the portal's asset hosts. All other requests are aborted via `page.route`, except fonts and CDNs listed in the adapter.
- **Uploads** only use artifact ids currently bound on the application (`resume_artifact_id`, `cover_letter_artifact_id`), resolved through the same path-containment check as `/artifacts`. The file must have `profile_version == active`.
- **Evidence**: one full-page PNG per VALIDATE, stored as artifact type `form_screenshot`. Screenshots stay local.
- **The READY_FOR_REVIEW gate** requires all of the following:
  - every required field is filled with a decision other than `ASK_USER` or `BLOCKED`;
  - validation is ok;
  - the current artifacts' `profile_version` equals the active version;
  - the application `content_hash` is recomputed.
- Untrusted page text (labels, help text, options) goes to the LLM only via `build_messages(untrusted=...)`.
- No personal data in tracked files. Fixture forms are local HTML under `tests/fixtures/forms/`, served by a local test server (pytest fixture using `http.server` on 127.0.0.1).

## Review Focus

1. **A demographic or authorization question is labeled ambiguously** (e.g. "Are you legally able to work here?"). It must still be `USER_ONLY` via keyword classification before any LLM involvement. (Task 2 `test_authorization_variants_user_only`)
2. **The LLM suggests filling a field with a value not in the profile.** Impossible: the LLM only suggests a canonical key and values always come from the profile. (Task 3 `test_llm_suggests_key_not_value`)
3. **The form layout changes between PREPARE and VALIDATE** (field removed or renamed). VALIDATE reports `FORM_LAYOUT_CHANGED` and the application goes to `NEEDS_USER_INPUT`. (Task 4 `test_layout_change_detected`)
4. **CAPTCHA on the form.** `BLOCKED` and manual handoff, never bypassed. (Task 4 `test_captcha_blocks`)
5. **An answer is edited in the dashboard after approval.** The item's approval is revoked (existing `set_answer` behaviour), and the batch view shows it as needing re-approval. (Task 6 `test_edit_after_approval_requires_reapproval`)

---

## File structure

```
src/jobpilot/forms/
  model.py         # FormField (extends portals.base), FieldMapping, decisions helpers
  classify.py      # sensitive-question classifier (USER_ONLY), required/optional
  mapping.py       # map_fields(snapshot, profile, library, artifacts, aliases) -> list[FieldAnswer]
  browser.py       # isolated persistent context factory, request allowlist, captcha/login detection
src/jobpilot/portals/greenhouse.py, lever.py
src/jobpilot/agents/form_prep.py           # PREPARE_FORM + VALIDATE agents and handlers
src/jobpilot/agents/prompts/field_map_v1.md
src/jobpilot/dashboard/templates/answers.html, batch.html (extend)
tests/fixtures/forms/greenhouse_basic.html, greenhouse_custom.html, lever_basic.html, captcha.html
tests/unit/test_classify.py, test_mapping.py; tests/browser/test_greenhouse_adapter.py, test_lever_adapter.py; tests/e2e/test_prepare_pipeline.py
```

---

### Task 1: Isolated browser, request allowlist, captcha/login detection, fixture form server

**Files:** Create `forms/{__init__,browser}.py`, `tests/fixtures/forms/*.html`, `tests/browser/conftest.py` (local HTTP server fixture), `tests/browser/test_browser.py`. Modify `config.py` (`browser_headless: bool = False`, `browser_state_dir`), `config/app.yaml`.

**Interfaces:**
- `@asynccontextmanager async def portal_page(cfg, allowed_hosts: set[str]) -> AsyncIterator[Page]`. Persistent context at `cfg.storage_path/"browser_state"/<portal>`, viewport 1280×900. `page.route("**/*")` aborts requests whose host is not in `allowed_hosts` and logs a count. Default timeout is 20 s.
- `async def detect_blockers(page) -> list[str]` returns any of `captcha`, `login`, `mfa`, `bot_check`.
- Fixture forms: realistic Greenhouse markup (`#application_form`, `input#first_name`, `#last_name`, `#email`, `#phone`, `input[type=file]#resume`, `#cover_letter`, custom `question_*` fields with `<label>`, a select, a radio group, and a checkbox "I agree"). Lever markup (`input[name=name]`, `email`, `phone`, `org`, `urls[LinkedIn]`, `input[type=file][name=resume]`, `textarea[name=comments]`, `.application-question` cards). One captcha fixture. The submit button sets `window.__submitted = true`.

- [ ] Tests (marked `browser`): `test_disallowed_host_aborted`, `test_detect_captcha`, `test_detect_login`, `test_persistent_context_in_storage`. Fail, implement, pass, commit `feat: isolated portal browser and blocker detection`.

### Task 2: Sensitive-question classifier

**Files:** `forms/classify.py`, `tests/unit/test_classify.py`.

**Interfaces:** `def classify_sensitive(label: str, help_text: str = "", options: list[str] = []) -> str | None`. Returns a category or `None`:
- `demographic`: gender, race, ethnic, veteran, disability, pronoun, sexual orientation, caste, religion, marital, age, date of birth, DOB.
- `authorization`: authorized/eligible/legally able/permitted to work, right to work, work permit, visa, sponsorship, citizenship, nationality.
- `declaration`: I certify/agree/acknowledge/confirm/consent, terms, privacy policy, background check, accurate and complete.
- `compensation`: salary, CTC, compensation, pay expectation. This maps to `FILL_AND_REVIEW` when the library has a value, else `ASK_USER`.

Matching is case-insensitive whole-phrase via `has_phrase`, plus a few regexes for "work in <country>". Unit tests use 40+ realistic label variants, including tricky ones: "Will you now or in the future require sponsorship…", "How do you identify?", "Are you 18 or older?" (age → demographic → USER_ONLY), "I understand that providing false information…". Commit `feat: sensitive question classifier`.

### Task 3: Field mapping (rules first, LLM key suggestion second)

**Files:** `forms/model.py`, `forms/mapping.py`, `agents/prompts/field_map_v1.md`, `tests/unit/test_mapping.py`.

**Interfaces:**
- `def normalize_label(s: str) -> str`, as described in the field_aliases.yaml header comment.
- `def map_fields(snapshot: FormSnapshot, ctx: MappingContext) -> tuple[list[FieldAnswer], list[FormField]]`. Returns the mapped answers plus the unmatched fields. `MappingContext` holds the ProfileFile identity, the active answer library (key → value, decision), the bound artifacts (`resume_path`, `cover_letter_path`, `cover_letter_text`), the aliases, and `years_experience`. Per-field order:
  1. The sensitive classifier, giving `USER_ONLY` (compensation is the exception).
  2. Exact alias match.
  3. Token-subset alias match.
  4. Unmatched.
  - Values come from the profile via the alias `profile:` path, with `formats` chosen by input type/pattern/maxlength. Phone uses `national` when the form has a country-code select, else `e164`.
  - Select/radio options are matched with `options_match` from the aliases, then by casefold equality. No match gives `ASK_USER`.
  - Every `FieldAnswer` has `source_fact_id` (where the value came from), `confidence` (1.0 exact, 0.8 token) and `rationale` (≤ 200 chars, generated by code).
- `async def suggest_keys(unmatched: list[FormField], gateway) -> dict[str, str]`. The LLM (route `form_mapping`, prompt `field_map_v1`) receives the labels, help text and options as untrusted data, plus the list of canonical keys as trusted data. It returns `{field_id: canonical_key | "none"}`. Unknown keys are discarded. A suggested key's value still comes from the profile, and its decision is forced to `FILL_AND_REVIEW`. A suggestion for a sensitive field is ignored.
- Tests:
  - `test_exact_alias_autofill`
  - `test_phone_format_by_country_select`
  - `test_select_option_matching`
  - `test_unknown_required_ask_user`
  - `test_compensation_from_library_fill_and_review`
  - `test_llm_suggests_key_not_value`: the fake LLM returns "email" for "Contact handle"; the value is the profile email and the decision is `FILL_AND_REVIEW`.
  - `test_llm_cannot_unlock_sensitive`
  - `test_cover_letter_text_for_why_question`
- Commit `feat: deterministic field mapping with llm key hints`.

### Task 4: Greenhouse and Lever adapters

**Files:** `portals/greenhouse.py`, `portals/lever.py`, `tests/browser/test_*_adapter.py`.

**Interfaces:** Each adapter implements `PortalAdapter`: `capabilities` (`PREPARE_APPLICATION`, plus `SUBMIT_APPLICATION` declared but unused here), `open_job`, `inspect_form`, `fill_fields`, `validate_form`, `submit`, `verify_submission`, `find_existing_submission`.
- **`inspect_form`:**
  - Reads every visible input/select/textarea/file input within the form.
  - `field_id` is a stable key: `name`, else `id`, else a hash of the label.
  - The label is resolved in this order: `<label for>`, `aria-label`, `aria-labelledby`, the enclosing label, then the preceding question text.
  - `required` comes from the `required`/`aria-required` attribute or an asterisk in the label.
  - Options come from select options or radio labels.
- **`fill_fields`:**
  - Uses locators by field id, falling back to label (`page.get_by_label`).
  - Text fields use `fill`, selects use `select_option(label=)`, radios use `check()` on the matching label, and files use `set_input_files(path)`.
  - Never touches `USER_ONLY`, `ASK_USER` or `BLOCKED` fields unless the user provided the value (`decision == USER_ONLY` and `source_fact_id == "user"`).
  - Never checks declaration boxes unless the user did.
- **`validate_form`:** reads each value back and compares it. A file input passes when its files list is non-empty with the expected name. Reports portal validation messages (`.error`, `[aria-invalid=true]`).
- **`submit`:** `raise NotImplementedError("Plan 5")` is fine. Keep it.
- **Lever specifics:** the full name goes in one field. URL fields use `urls[...]`. Custom questions are `cards[...]` fields.
- **Tests (browser, local fixtures):**
  - `test_inspect_greenhouse_fields`
  - `test_fill_and_readback`
  - `test_never_clicks_submit` (`window.__submitted` stays false)
  - `test_layout_change_detected` (fill with a field id missing from the page gives `FORM_LAYOUT_CHANGED`)
  - `test_captcha_blocks`
  - `test_lever_full_name_and_urls`
- Commit `feat: greenhouse and lever form adapters`.

### Task 5: PREPARE_FORM and VALIDATE agents, user answers, gate to READY_FOR_REVIEW

**Files:** `agents/form_prep.py`, `dashboard/templates/answers.html`, routes, `runtime.py`. Modify the cover letter handler so it follows up with `PREPARE_FORM`. Modify `orchestrator/approval.py`'s `application_hash` if needed so it includes the answer decisions.

**Interfaces:**
- **PREPARE_FORM agent:**
  - Opens the job's `application_url` (from JobRow, falling back to `canonical_url` plus the portal-specific apply path) with the adapter chosen by `job.source`.
  - Detects blockers.
  - Inspects the form, runs `map_fields` and `suggest_keys`, and returns `FormPlan(application_id, fields: list[FieldAnswer], blockers, snapshot_hash)`.
- **PREPARE_FORM handler:**
  - Upserts `FormFieldAnswerRow`s. Updates to an existing row go through `mutations.set_answer`.
  - Stores `snapshot_hash` in a new `application.form_snapshot_hash` column (migration).
  - If blockers are present, or any required field is `ASK_USER`/`USER_ONLY`/`BLOCKED` without a user value: transition `FORM_IN_PROGRESS → NEEDS_USER_INPUT` (reason lists the field ids). Otherwise follow up with `VALIDATE`.
- **VALIDATE agent:**
  - Re-opens the form and re-inspects it. If the snapshot hash changed, the result is `FORM_LAYOUT_CHANGED`.
  - Fills from the stored answers and uploads the bound artifacts. This requires each artifact's `profile_version` to equal the active version; otherwise it uses the stale flow from `documents_flow`.
  - Runs `validate_form` and takes a screenshot.
- **VALIDATE handler:**
  - Stores the screenshot artifact.
  - Transitions `VALIDATING → READY_FOR_REVIEW` if ok, recomputing `application.content_hash` via `ApprovalService.application_hash`. Otherwise `VALIDATION_FAILED` with errors.
  - Path: `FORM_IN_PROGRESS → VALIDATING`, then the result.
- **Answers page `/ui/applications/{id}/answers`:**
  - A table showing label, decision badge, value, source and rationale.
  - Editable inputs for `ASK_USER`/`USER_ONLY`/`FILL_AND_REVIEW` rows. `USER_ONLY` rows carry the hint "You are answering this yourself".
  - Saving uses `set_answer` with `source_fact_id="user"`. Then, if every required field is resolved, it transitions `NEEDS_USER_INPUT → FORM_IN_PROGRESS` and enqueues `VALIDATE`.
  - Declaration checkboxes are shown verbatim and require explicit ticking by the user.
- **Tests:**
  - `test_prepare_maps_and_needs_user_input`
  - `test_user_answers_then_validate_ready`
  - `test_validate_stale_artifact_requeues_tailor`
  - `test_screenshot_artifact_stored`
  - `test_content_hash_recomputed_on_ready`
- Commit `feat: form preparation, validation and user answers`.

### Task 6: Batch review UI with full detail

**Files:** Extend `dashboard/templates/batch.html` and routes; add `tests/unit/test_batch_review.py`.

**Interfaces:**
- Batch page rows show:
  - company/role, with links to the job and portal;
  - the resume preview link and the cover letter link;
  - an answers summary with counts by decision, expanding to the full answer table;
  - warnings (FILL_AND_REVIEW count, any LLM-suggested mapping);
  - the screenshot thumbnail link;
  - the item hash.
- Per-row checkbox and "Approve selected" (existing hash-bound flow).
- Rows whose item hash no longer matches the live `application_hash` are shown as "Changed since batch was built: rebuild batch", with a "Rebuild" button that calls the existing `rebuild_batch`.
- Tests:
  - `test_batch_page_shows_answers_and_docs`
  - `test_edit_after_approval_requires_reapproval`
  - `test_rebuild_after_change`
- Commit `feat: batch review with answers, documents and evidence`.

### Task 7: End-to-end prepare pipeline (fixture portal)

**Files:** `tests/e2e/test_prepare_pipeline.py` (browser), README "Applying" section.

- The fixture Greenhouse form is served locally. A MATCHED job with `application_url` pointing to it is selected, with a fake gateway for tailor, cover letter and field_map.
- `run_until_idle`, then answer one USER_ONLY question via the answers page, then `run_until_idle` again. The application ends `READY_FOR_REVIEW` with a screenshot, the resume uploaded (read back) and `window.__submitted == false`.
- Build a batch, approve it, then `run_until_idle`. The SimulatedAdapter path confirms (dry_run). Assert no request ever reached the fixture's submit endpoint.
- Commit `test: prepare pipeline end-to-end; docs`.

## Out of scope (Plan 5)

Real submission via adapters' `submit()` with confirmation detection, disabling dry_run per portal, post-submit verification, uncertain-outcome handling UI.
