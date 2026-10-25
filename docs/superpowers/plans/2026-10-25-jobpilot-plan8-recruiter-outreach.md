# JobPilot Plan 8: Recruiter Outreach Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After an application is applied (CONFIRMED or SUBMITTED_BY_USER) or the user selects it for outreach, JobPilot finds at most one public recruiting contact for the company, drafts one short, verified message, and puts it in a separate **Outreach** review queue. The user reviews and approves it, sends it themselves (copy / `mailto:` / open LinkedIn), and clicks **Mark as sent**. JobPilot never sends a message itself and never claims a message was sent when only a draft exists.

**Architecture:**
- The tables `contact_candidate` and `outreach_draft`, `OutreachStatus`, its transitions, and the task kinds `OUTREACH_RESEARCH` / `OUTREACH_SEND` already exist (foundation). This plan fills them in.
- **Contact sources (public, no login, no guessing)** in `outreach/contacts.py`:
  1. Addresses published in the job posting text itself (e.g. `careers@acme.com`, "Contact: Priya, Talent Partner") — the posting is untrusted text, so only regex-extracted emails whose domain matches the company's careers/job host domain, or a recruiter name + title from a deterministic pattern, are kept.
  2. A recruiting inbox published on the company careers page: a single HTTP GET of the configured `careers_url` (optional `CompanyBoard.extra["careers_url"]`), same fetcher, User-Agent and rate limit, regex for `mailto:` links whose local part is in an allowlist (`careers`, `jobs`, `recruiting`, `recruitment`, `talent`, `hr`, `hiring`, `people`) and whose domain matches the company domain.
  3. A contact the user adds by hand on the outreach page (name, title, channel `email|linkedin`, address or profile URL, source URL). This is how LinkedIn recruiters get in: the user finds them, JobPilot never browses LinkedIn.
  - Each candidate stores `public_source_url`, `confidence` (published email in posting 0.9, careers inbox 0.7, user-added 1.0), `channel`, and `verified_at` only for user-added contacts. Nothing inferred from naming patterns is ever stored.
  - Ranking: user-added > named recruiter in posting > generic inbox. One is chosen; the rest are kept as alternates the user can switch to.
  - None found → draft status `SKIPPED` with reason `no_public_contact` and the dashboard says "No suitable public contact found".
- **Draft** (`agents/outreach.py`, `OutreachAgent`): LLM route `cover_letter` profile, prompt `outreach_v1.md`, job text in the untrusted block. Output ≤ 120 words, 2–4 sentences: role applied for, one or two verified evidence points, a polite close. Validated with the existing `validate_rewrite` (no new numbers, tech terms, proper nouns, leadership claims), the cover-letter banned list, a word cap, and placeholder check (`[`/`]` rejected). The application date is included only from the recorded submission event. Falls back to a deterministic template filled with verified values when the LLM output fails validation twice.
- **Flow:** the SUBMIT confirm handler and `mark_applied` enqueue `OUTREACH_RESEARCH` when `outreach.auto_draft` (default true); the dashboard "Draft outreach" button enqueues it for any applied application. Research creates the draft: NOT_ELIGIBLE → CONTACT_RESEARCH → DRAFT_READY → NEEDS_REVIEW.
- **Approval & send:** outreach approval is its own hash-bound record (`approval.py` scope `outreach`), never implied by an application approval. Editing the message or switching the contact invalidates it (through `data/mutations.py`). Approve is a button; **Mark as sent** is the user's own statement (actor `user`) and needs a valid approval for the current hash: APPROVED → SEND_IN_PROGRESS → SENT in one action, recording `user_marked_outreach_sent` (ids only). `OUTREACH_SEND` stays registered with no handler that sends; enqueuing it is refused by policy (`outreach_send_disabled`).
- **Dedupe:** `UniqueConstraint(company_key, campaign_id)` already exists. Campaign id = `cfg.outreach.campaign` (default `"default"`). A second application at the same company in the same campaign gets no new draft (event `outreach_duplicate_company`). A follow-up message is not supported in this plan.
- **Retention:** migration `0009_outreach` adds `contact_candidate.created_at`, `contact_candidate.application_id` (FK, nullable), `outreach_draft.created_at`, `outreach_draft.skip_reason`. The Plan 7 maintenance prune then becomes live. Delete-application removes its contacts and drafts (events stay). Export bundles include the draft and contact.

**Tech Stack:** existing.

**Spec:** §4.5, §11 (outreach), §5 outreach states, §6 ContactCandidate / OutreachDraft, §13 contact data policy.

## Global Constraints

- **JobPilot never sends outreach.** No SMTP, no email API, no LinkedIn automation, no browser opened for outreach. `OUTREACH_SEND` tasks are refused. A test asserts no outreach code path imports `smtplib` or opens Playwright.
- **No guessed addresses.** Emails come only from published text, with the domain matching the company domain; never `first.last@` construction. A test feeds a posting with a name and no email and asserts no email contact is created.
- **Separate approval.** Application approval never approves outreach; outreach approval is bound to `sha256(contact_id + channel + message)`.
- **One per company per campaign.** Enforced by the DB constraint and checked before research.
- **Untrusted input.** Posting text and careers pages go through `html_to_text`, 12 000-char cap, untrusted block. The careers fetch is one GET, public host, http(s) only, `safe_href` on every rendered URL.
- **No personal data in tracked files.** Fixtures use fictional companies and `example.com`-style domains.
- Tests never hit the network (existing guard); careers pages via `httpx.MockTransport`.

## Review Focus

1. **Outreach could be sent or marked sent without approval** for the current content. (Task 4 `test_mark_sent_requires_current_approval`)
2. **A guessed or off-domain email is stored.** (Task 1 `test_no_guessed_or_offdomain_emails`)
3. **Two drafts for one company in one campaign.** (Task 3 `test_one_draft_per_company_campaign`)
4. **Draft states facts not in the verified profile** or an application date that isn't recorded. (Task 2 `test_draft_rejects_unverified_claims`)
5. **Application approval leaks into outreach.** (Task 4 `test_application_approval_does_not_approve_outreach`)

---

### Task 1: Migration + contact finding
- **Files:** migration `0009_outreach.py`, `tables.py` columns, `outreach/__init__.py`, `outreach/contacts.py` (`extract_from_posting(text, company_domain)`, `async fetch_careers_inbox(url, fetcher, company_domain)`, `rank(candidates)`), config `outreach: {auto_draft: true, campaign: "default"}`, `CompanyBoard.extra["careers_url"]` validated as http(s).
- **Company domain:** from the job's canonical/apply URL host, mapped for ATS hosts (greenhouse/lever/ashby/smartrecruiters/workday) to the careers_url host when configured; if none can be determined, posting emails are kept only when the domain's second-level label equals the normalized company slug.
- **Tests:** `test_no_guessed_or_offdomain_emails`, `test_careers_inbox_allowlist`, `test_rank_prefers_user_then_named`, migration upgrade/downgrade, `alembic check` clean.

### Task 2: Outreach drafting agent
- **Files:** `agents/outreach.py`, `agents/prompts/outreach_v1.md`, deterministic fallback template.
- **Tests:** `test_draft_rejects_unverified_claims`, `test_draft_word_cap_and_placeholders`, `test_fallback_template_uses_verified_values`, `test_application_date_only_when_recorded`, injection text in posting ignored.

### Task 3: Research task + flow wiring
- **Files:** OUTREACH_RESEARCH handler (finds contacts, persists them with `application_id`, creates the draft through `mutations.py`, transitions, events), enqueue from SUBMIT confirm and `mark_applied` when `auto_draft`, `actions.request_outreach(app_id)` (409 unless applied), policy refusing OUTREACH_SEND, delete-application / export integration.
- **Tests:** `test_one_draft_per_company_campaign`, `test_no_contact_skips_with_reason`, `test_research_only_after_applied`, `test_outreach_send_refused`, `test_delete_app_removes_contacts_and_drafts`, `test_contact_retention_prunes`.

### Task 4: Outreach approval + Mark as sent
- **Files:** `approval.py` outreach scope, `mutations.set_outreach_message` / `choose_contact` / `add_contact` (each invalidates approval), `actions.approve_outreach`, `actions.mark_outreach_sent(id, note)`, `actions.skip_outreach`.
- **Tests:** `test_mark_sent_requires_current_approval`, `test_edit_invalidates_outreach_approval`, `test_application_approval_does_not_approve_outreach`, `test_mark_sent_twice_409`, `test_no_send_code_paths` (no smtplib/playwright imports under outreach).

### Task 5: Outreach dashboard + README
- **Files:** `/ui/outreach` queue (status, company, role, contact + source link, confidence, channel), `/ui/outreach/{id}` (editable message, copy button, `mailto:` link with subject/body URL-encoded for email, "Open profile" link for LinkedIn via `safe_href`, alternates, add-contact form, approve, mark-as-sent with optional note, skip), "Draft outreach" button on the application page, application page shows "Recruiter contacted: yes (you marked sent on …) / draft only / none". Nav link "Outreach". README "Recruiter outreach" section.
- **Tests:** `test_outreach_queue_renders`, `test_detail_shows_draft_only_label`, `test_mailto_encoding`, `test_unsafe_profile_url_not_linked`, CSRF/host checks as existing forms.
