# JobPilot Plan 5 — Approval-Gated Real Submission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the user has (1) switched a portal to live in settings and (2) approved an application in a batch, JobPilot submits that application on the real Greenhouse/Lever form. Before it clicks anything, it re-fills and re-verifies the form from the exact approved answers and documents. Afterwards it detects the portal's confirmation, saves evidence (a screenshot, the confirmation text and URL), and marks the application `CONFIRMED`, or `SUBMISSION_UNCERTAIN` if it can't prove the outcome. It never retries blindly.

**Architecture:**
- A `LiveSubmitter` implements the `PortalAdapter` submit side for the real portals. Per submission it opens `portal_page` and runs the Greenhouse/Lever adapter.
- Before submitting it runs a **pre-submit verification**:
  - re-inspect the form, and require the snapshot hash to equal the stored one;
  - fill from the stored answers, using the same gate rules;
  - read back every expected value;
  - require bound artifacts to have `profile_version == active`;
  - recompute `application_hash`, and require it to equal the approved item hash.
- Only if every check passes does it click the form's submit control **once**. Then `detect_confirmation()` checks the URL pattern, the confirmation text and the disappearance of the form.
- The engine's write-ahead, idempotency, timeout → `UNCERTAIN` and no-retry protocol (Foundation) is unchanged.
- **Live mode is opt-in per portal.** `cfg.dry_run` must be `false` **and** the portal must be listed in `cfg.live_portals`. The defaults stay `dry_run: true` and `live_portals: []`. The engine picks the `LiveSubmitter` only when both hold; otherwise it uses `SimulatedAdapter` (dry run).
- The dashboard shows:
  - a persistent banner when live mode is on;
  - a "Submit approved applications" button that enqueues SUBMIT tasks only for approved, live-eligible apps;
  - the evidence on the application page;
  - an "Uncertain" queue with "I checked: it was submitted" / "It was not submitted" actions. The latter moves the app to `SUBMISSION_FAILED`, never back to submit.

**Tech Stack:** existing.

**Spec:** §4.4, §5.3–5.4, §10.4, §14 · Follow-ups: `docs/superpowers/plans/2026-10-24-plan4-followups.md`

## Global Constraints

- **Two independent switches** gate real submission: `dry_run: false` and the portal named in `live_portals`. Both are in `config/app.yaml` / `storage/` overrides. There is no dashboard toggle for either, so turning live mode on is always a deliberate file edit. Tests assert the defaults.
- **Submission requires all of the following.** The existing `may_submit` gate passes, which covers the APPROVED status, a valid approval, the hash match, the `SUBMIT_APPLICATION` capability, no unresolved answers and no duplicate. The pre-submit verification also passes. One failed check means no click: the application goes to `READY_FOR_REVIEW` with its approval revoked (reason codes recorded), and nothing is retried.
- **Exactly one click.** The submit control is located inside the form root by `button[type=submit], input[type=submit]`, or a button whose accessible name matches `^(submit|submit application|apply|send application)$` (case-insensitive). Zero or more than one match is `SUBMIT_CONTROL_AMBIGUOUS`, which means a manual handoff with no click. There is no Enter key and no JavaScript `form.submit()`.
- **The confirmation must have evidence.** Each portal has a confirmation detector: URL contains `/confirmation`/`thank`/`submitted`, or the page text matches `thank you for applying|application (has been )?(received|submitted)|we('ve| have) received your application`, or the form is gone along with an explicit success element. With evidence the application is `SUBMITTED` then `CONFIRMED`, with `confirmation_reference` (application id parsed from the page if present) and a screenshot artifact `submit_confirmation`. Without evidence within 30 s it is `SUBMISSION_UNCERTAIN`, and validation errors on the page are `SUBMISSION_FAILED`.
- **`find_existing_submission`** checks the local DB/events for a prior click with the same idempotency key. If one exists the outcome is `UNCERTAIN` and there is no click. Portals give no public "my applications" lookup, so a click is never repeated.
- **CAPTCHA, login or MFA appearing at submit time** is a manual handoff, never a bypass. The click is not attempted.
- **No real network in tests.** All of this is exercised on local fixture portals with confirmation pages served by `form_server`. The hard network guard stays.
- **No personal data** in tracked files, events or logs. Events carry codes and ids only.

## Review Focus

1. **The approved content differs from what is on the page at submit time** (the user edited an answer, the profile was re-imported, or the form changed). No click, back to review. (Task 2 `test_presubmit_mismatch_never_clicks`)
2. **The portal is not in `live_portals` while `dry_run` is false.** Simulated only; the real portal is never touched. (Task 1 `test_live_requires_both_switches`)
3. **The confirmation page doesn't load (timeout).** `UNCERTAIN`, and a second SUBMIT task for the same application never clicks again. (Task 3 `test_timeout_uncertain_no_second_click`)
4. **Two submit buttons, or none.** Ambiguous, manual handoff. (Task 2 `test_ambiguous_submit_control`)
5. **The user marks an uncertain application "not submitted".** It goes to `SUBMISSION_FAILED` and is not re-queued automatically. (Task 4 `test_uncertain_resolution_not_submitted`)

---

### Task 1: Live-mode configuration and adapter selection

**Files:**
- Modify `config.py` (`live_portals: list[Literal["greenhouse","lever"]] = []`) and `config/app.yaml`.
- Modify `orchestrator/engine.py` `_adapter_for`. It returns `(LiveSubmitter(cfg, portal), simulated=False)` only when `not cfg.dry_run and app.portal in cfg.live_portals`, and otherwise `(SimulatedAdapter, True)`.
- `api/main.py` / `runtime.py` register a submitter factory rather than a static adapter.
- Dashboard `base.html` shows a red persistent banner "LIVE SUBMISSION ON for: greenhouse, lever" when any portal is live.
- `/health` reports `live_portals`.
- Tests: `test_defaults_dry_run_and_no_live`, `test_live_requires_both_switches` (four combinations), `test_banner_shown_when_live`.

### Task 2: Pre-submit verification and single-click submit

**Files:**
- `portals/live_submit.py` (`LiveSubmitter`).
- `portals/playwright_form.py`: add `locate_submit_control()` and `click_submit_once()`.
- Fixture forms `greenhouse_submit.html` and `lever_submit.html`. On POST these redirect (303) to `/confirmation.html`, which shows "Thank you for applying" and an id `APP-12345`. Variants: no-confirmation, an error page, and two submit buttons.

**Interfaces:**
- `LiveSubmitter.submit(approved: ApprovedApplication) -> SubmitResult`. It loads the application (stored answers, bound artifacts, snapshot hash, job URL) from the DB via an injected loader, then inside `portal_page`:
  - detect blockers, and if any, raise `ManualHandoff("BLOCKED:<kind>")`;
  - open the form and inspect it; a snapshot hash mismatch raises `PreSubmitMismatch("FORM_LAYOUT_CHANGED")`;
  - fill the stored answers plus the bound files, then `validate_form`; any failure raises `PreSubmitMismatch("VALUE_MISMATCH")`;
  - compare artifact versions; a mismatch raises `PreSubmitMismatch("STALE_DOCUMENTS")`;
  - recompute `application_hash` against `approved.approved_hash`; a mismatch raises `PreSubmitMismatch("HASH_MISMATCH")`;
  - `locate_submit_control`; ambiguity raises `ManualHandoff("SUBMIT_CONTROL_AMBIGUOUS")`;
  - **write a `submit_clicked` event and commit before clicking** (injected `before_click` callback);
  - click once, then `detect_confirmation` with a 30 s timeout;
  - screenshot to `storage/artifacts/<app>/submit_v{n}.png`;
  - return `SubmitResult` with status `CONFIRMED`/`SUBMISSION_UNCERTAIN`/`SUBMISSION_FAILED` and `confirmation_reference`.
- Engine handling:
  - `PreSubmitMismatch` revokes the approval via `invalidate_application`, moves the application to `READY_FOR_REVIEW` (through the needed transitions) and fails the task with the code.
  - `ManualHandoff` moves the application to `NEEDS_USER_INPUT` with the reason.
  - Neither is `UNCERTAIN`, because no click happened. Add the transition edges `SUBMISSION_IN_PROGRESS→READY_FOR_REVIEW` and `→NEEDS_USER_INPUT`, allowed only for actor `"presubmit"`. This is valid only when no `submit_clicked` event exists for the idempotency key, and it is enforced in code.
- Tests (browser):
  - `test_live_submit_confirmed` (the fixture records exactly one POST; CONFIRMED with `APP-12345`)
  - `test_presubmit_mismatch_never_clicks` (edit a stored answer behind the approval's back; zero POSTs)
  - `test_layout_change_never_clicks`
  - `test_ambiguous_submit_control`
  - `test_blocker_at_submit_handoff`
  - `test_validation_error_page_failed`

### Task 3: Idempotency, timeout and uncertain handling

**Files:** `portals/live_submit.py`, `orchestrator/engine.py`.

**Interfaces:**
- `LiveSubmitter.find_existing_submission(key)` returns an `UNCERTAIN` `SubmitResult` if a `submit_clicked` event exists for the key, else `None`.
- A timeout in `detect_confirmation` gives `SUBMISSION_UNCERTAIN` (`SUBMIT_NO_CONFIRMATION`).
- An engine-level `wait_for` timeout after the click is already `UNCERTAIN`.
- Raise `adapter_timeout_s` for live submits to 120 s.
- Tests:
  - `test_timeout_uncertain_no_second_click`: a fixture confirmation that never arrives gives `UNCERTAIN`; enqueue SUBMIT again and the result is still one POST.
  - `test_crash_after_click_is_uncertain`: an exception raised after `before_click` gives `UNCERTAIN`.
  - `test_restart_with_inflight_live_submit_uncertain`.

### Task 4: Submit button, evidence and uncertain-resolution UI

**Files:** dashboard routes and templates (batch, application, a new `uncertain.html`), `api/actions.py`, `README.md`.

**Interfaces:**
- **Batch page:**
  - "Submit approved applications" (POST) enqueues SUBMIT for APPROVED items without a queued or running SUBMIT, in the same transaction.
  - The page shows per-row "will submit LIVE to <portal>" or "dry run (simulated)".
  - In live mode a confirmation modal requires typing `SUBMIT` before the POST. This is a server-side check on a form field `confirm == "SUBMIT"`.
- Approve no longer auto-enqueues SUBMIT. Approving and submitting are separate steps, and the Foundation's same-transaction enqueue moves to this button. The tests are updated, and the orphan sweep queues only apps whose submit was explicitly requested; this is tracked by a new `application.submit_requested_at` column.
- **Application page:**
  - submission status;
  - the confirmation reference;
  - the evidence screenshot;
  - a link to the job;
  - the timeline.
- **`/ui/uncertain`:**
  - Lists `SUBMISSION_UNCERTAIN` applications with their evidence.
  - Action "It was submitted" moves the application to `CONFIRMED` (actor `user`, reason `user_confirmed`).
  - Action "It was not submitted" moves it to `SUBMISSION_FAILED` (actor `user`). The UI then offers "Prepare again", which resets the application to `FORM_IN_PROGRESS`, revokes its approval and enqueues `PREPARE_FORM`. That is an explicit user action, with a state edge added.
- **README "Going live"** section covers how to switch a portal to live, what happens at submit, and how to resolve uncertain applications.
- Tests:
  - `test_submit_button_requires_typed_confirm_in_live`
  - `test_approve_does_not_submit`
  - `test_uncertain_resolution_submitted`
  - `test_uncertain_resolution_not_submitted`
  - `test_prepare_again_after_failed`

### Task 5: End-to-end live submission on fixture portals

**File:** `tests/e2e/test_live_submit_pipeline.py` (browser).

Steps:
1. Set `cfg.dry_run=False` and `live_portals=["greenhouse"]`. Point `application_url` at the `form_server` Greenhouse submit fixture.
2. Select a job. Generate documents and prepare the form, answering the user-only questions.
3. Approve the application, then click "Submit approved applications" with `SUBMIT` typed.
4. Expect `CONFIRMED` with `APP-12345`, exactly 1 POST, and a submit evidence artifact.
5. Repeat with `live_portals=[]` and assert 0 POSTs and a simulated confirmation.

The network guard stays green.

## Out of scope

Recruiter outreach (Plan 6). launchd scheduling and backups (Plan 7).
