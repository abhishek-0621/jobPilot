# JobPilot Plan 6 — More Job Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cover far more of the Indian and remote GenAI job market:
- **Ashby, SmartRecruiters and Workday** become new automated discovery sources, using their public job-board endpoints.
- **Ashby** gets a form adapter, so its applications can be prepared like Greenhouse and Lever.
- **Any other job** can be added by hand. That covers LinkedIn, Naukri, company sites and Workday forms behind login. The user pastes a URL and the description, and JobPilot runs extraction, matching, resume and cover letter as usual. It then produces a **manual apply package**: the documents, the drafted answers to common questions, and a one-click "Mark as applied" that records the user's own submission.
- A **company board finder** checks a company name against every supported board type and stores the ones that respond.

**Architecture:**
- New `SourceAdapter`s follow the existing Greenhouse/Lever pattern: `RateLimitedFetcher`, a typed `RawPosting`, `SourceError` codes.
- `CompanyBoard.source` grows to `greenhouse | lever | ashby | smartrecruiters | workday`. A Workday board needs `{tenant, wd_host, site}`, so the board model gains an optional `extra: dict[str, str]`.
- Manual jobs use `source="manual"` and `source_job_id = content_hash(url)`. Application portal resolution (`portals/registry.py`):
  - **greenhouse, lever, ashby** → automated preparation and submission, as in Plans 4 and 5;
  - **everything else** (`manual`, `workday`, `smartrecruiters`) → the **manual** path. Documents are generated, then the app moves from `FORM_IN_PROGRESS` to `MANUAL_APPLY_READY`, a new status. There the user applies themselves and clicks **Mark as applied**, which moves it to `SUBMITTED_BY_USER`, a new terminal-ish status counted as applied, with an optional confirmation reference typed by the user.
  - No browser automation is ever run against LinkedIn or Naukri.

**Tech Stack:** existing.

**Spec:** §8.1 (adapter capabilities), §2.2 (no bypassing restrictions) · Follow-ups: `docs/superpowers/plans/2026-10-25-plan5-followups.md`

## Global Constraints

- **Public endpoints only, no login**, using the same User-Agent and rate limit as today (≥1.0 s per host, 20 s timeout):
  - Ashby: `GET https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true`.
  - SmartRecruiters: `GET https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset=N`, plus a detail call `GET .../postings/{id}` for the description, capped at `max_jobs_per_run`.
  - Workday: `POST https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` with JSON `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`, plus a detail call `GET .../job/{externalPath}`. At most 5 pages per run.
- **No automation on LinkedIn, Naukri or any site needing login.** The manual path never opens a browser for those URLs. The registry refuses automated preparation for hosts in a deny-list (`linkedin.com`, `naukri.com`, `indeed.com`, `glassdoor.*`, `myworkdayjobs.com`, `smartrecruiters.com` apply pages); those apps always take the manual path.
- **Manual job input is untrusted.** The pasted description goes through `html_to_text` with the same 12 000-char cap and reaches models only inside the untrusted block. The URL must be http(s); `safe_href` applies.
- **"Mark as applied" is the user's own statement.** It is recorded with actor `user`, no evidence is required, and the app is never treated as automation-confirmed.
- The network guard in tests stays: all new adapters are tested with `httpx.MockTransport` fixtures, and the Ashby form adapter with the local `form_server`.
- No personal data in tracked files. The real company list lives in `storage/profiles/companies.yaml`, and the committed example lists only fictional tokens.

## Review Focus

1. **A manually added LinkedIn URL is never opened by Playwright.** It goes through manual apply only. (Task 4 `test_manual_linkedin_never_automated`)
2. **Workday pagination stops at 5 pages and at `max_jobs_per_run`.** (Task 2 `test_workday_pagination_caps`)
3. **A pasted description containing injection text** is handled the same as any untrusted job text. (Task 4 `test_manual_description_untrusted`)
4. **"Mark as applied" twice, or on the wrong status**, returns 409. (Task 5 `test_mark_applied_rules`)
5. **The board finder hits only the supported public endpoints** and records only boards that responded with postings. (Task 6 `test_board_finder_records_only_live_boards`)

---

### Task 1: Ashby + SmartRecruiters discovery
- **Files:** `discovery/sources/ashby.py`, `discovery/sources/smartrecruiters.py`, `profile/preferences.py` (extend the Literal, add `extra: dict[str,str] = {}` with key and value pattern validation), `runtime.py` (register both), and fixtures `tests/fixtures/ashby_board.json`, `smartrecruiters_postings.json`, `smartrecruiters_detail.json`.
- **Ashby field mapping:**
  - `jobs[].id` → id;
  - `title`;
  - `location` / `secondaryLocations` / `isRemote` → location, with "Remote" appended when remote;
  - `descriptionHtml` → description;
  - `jobUrl` → canonical URL;
  - `applyUrl` → application URL;
  - `publishedAt` → published date;
  - postings with `isListed == false` are skipped.
- **SmartRecruiters field mapping:**
  - list `content[]`: `id`, `name` (title), `location.city/country/remote`, `ref`;
  - detail `jobAd.sections.{companyDescription,jobDescription,qualifications,additionalInformation}.text`, concatenated as HTML;
  - canonical URL `https://jobs.smartrecruiters.com/{company}/{id}`;
  - application URL = canonical URL (manual path).
- **Error codes:** the same mapping as Greenhouse.
- **Tests:** parse fixtures, error codes, the SmartRecruiters detail cap, and the rate-limit spacing between list and detail calls.

### Task 2: Workday discovery
- **Files:** `discovery/sources/workday.py` and fixtures for the jobs list page and job detail.
- `CompanyBoard(source="workday", token=<tenant>, extra={"wd_host": "wd3", "site": "External"})`. `wd_host` must match `^wd\d{1,2}$`, and `site` must match `^[A-Za-z0-9_-]{1,64}$`.
- **Listing:** list `jobPostings[]` gives `title`, `externalPath`, `locationsText` and `postedOn`. The detail call gives `jobPostingInfo.jobDescription` (HTML), `jobPostingInfo.location`, `jobPostingInfo.externalUrl` (canonical), `jobReqId` (id) and `remoteType`. The application URL is `externalUrl` (manual path).
- **Caps:** at most 5 pages and at most `max_jobs_per_run` detail calls.
- **Tests:** parsing, `test_workday_pagination_caps`, error codes, and rejection of a bad `wd_host`.

### Task 3: Ashby form adapter
- **Files:** `portals/ashby.py`, registered in `portals/registry.py` with `ASSET_HOSTS` = `jobs.ashbyhq.com`, `api.ashbyhq.com`, `cdn.ashbyprd.com`, `app.ashbyhq.com`. Fixture `tests/fixtures/forms/ashby.html` (Ashby-like markup: `form` with `[data-testid=application-form]`, `input[name=_systemfield_name]`, `_systemfield_email`, `_systemfield_resume` file input, custom questions with labels, a single "Submit Application" button). Add aliases for Ashby system field names to `config/field_aliases.yaml` as extra aliases of `full_name`, `email`, `phone`, `resume`, `linkedin`.
- **Capabilities:** PREPARE_APPLICATION and SUBMIT_APPLICATION, live only via `live_portals` (extend the Literal with "ashby").
- **Tests:** inspect, fill and read back, never-click-submit during preparation, and a live submit on the fixture (one POST, CONFIRMED).

### Task 4: Manual job add + manual apply path
- **Files:**
  - `api/actions.py`: `add_manual_job(s, orch, url, title, company, location, description) -> job_id`, which sets `source="manual"`, does an `upsert_job` with dedupe, and enqueues EXTRACT.
  - Dashboard **"Add a job"** form at `/ui/jobs/new` with fields URL, title, company, location and a description textarea.
  - `portals/registry.py`: `apply_mode(job) -> "automated" | "manual"`. It is automated only for source ∈ {greenhouse, lever, ashby} AND the application host not in the deny-list.
  - The cover letter follow-up becomes PREPARE_FORM when automated, or a `MANUAL_PACKAGE` task when manual.
  - `MANUAL_PACKAGE` handler: drafts answers to a fixed list of common questions (why this company, why this role, notice period, expected CTC, total experience, relocation, plus the questions from the story bank in Plan 9) using the existing mapping context and library. It stores them as `FormFieldAnswerRow`s with a `manual:` field_id prefix and decision FILL_AND_REVIEW or ASK_USER, then transitions FORM_IN_PROGRESS → MANUAL_APPLY_READY.
- **State machine:** new statuses `MANUAL_APPLY_READY` and `SUBMITTED_BY_USER`. Edges:
  - FORM_IN_PROGRESS → MANUAL_APPLY_READY (orchestrator);
  - MANUAL_APPLY_READY → SUBMITTED_BY_USER (user);
  - MANUAL_APPLY_READY → DOCUMENTS_PENDING (regenerate);
  - any non-terminal → REJECTED_BY_USER.
- **Tests:** `test_manual_linkedin_never_automated` (a LinkedIn URL and a Greenhouse-looking URL under a deny-listed host; no portal page is opened, which the guard's violations confirm is empty, and the opener spy is never called), `test_manual_description_untrusted`, `test_manual_job_full_flow_to_package`.

### Task 5: Manual apply page + Mark as applied
- **Files:** `/ui/applications/{id}/apply` (shown for MANUAL_APPLY_READY): job link (safe_href, opens a new tab), resume PDF/DOCX download, cover letter PDF plus text with a copy button, drafted answers with copy buttons (editable via `set_answer`), the **"Mark as applied"** form (optional confirmation reference, optional date), and "Not applying" (→ REJECTED_BY_USER).
- `actions.mark_applied(s, app_id, reference)` transitions MANUAL_APPLY_READY → SUBMITTED_BY_USER, stores the reference, and records the event `user_marked_applied` (ids only). Calling it twice or on the wrong status returns 409.
- The overview and jobs pages count SUBMITTED_BY_USER as applied (label "Applied (manual)").
- **Tests:** `test_mark_applied_rules`, `test_apply_page_shows_documents_and_answers`, `test_overview_counts_manual_applied`.

### Task 6: Company board finder + README
- **Files:**
  - `discovery/board_finder.py`: `async def find_boards(name: str, client) -> list[CompanyBoard]`. It derives slug candidates from the name: lowercase, with spaces removed, replaced by `-` or `_`, and with suffixes "inc", "labs", "ai", "technologies", "india" stripped; at most 6 candidates. It probes Greenhouse `boards-api…/{slug}/jobs` (200 + `jobs` key), Lever `api.lever.co/v0/postings/{slug}?mode=json` (200 + list), Ashby (200 + `jobs`) and SmartRecruiters `…/companies/{slug}/postings?limit=1` (200 + `totalFound > 0`). It returns the boards with ≥ 1 posting. Workday is not probed; tenants are added manually.
  - CLI `jobpilot boards find "Company A" "Company B" … [--write]` prints the results. `--write` appends to `storage/profiles/companies.yaml`, de-duplicated.
  - Dashboard `/ui/companies` lists the configured boards, with an "Add company" form that runs the finder.
- **README "Job sources"** section: the supported sources, how to add companies, the manual add for LinkedIn/Naukri, and why those are never automated.
- **Tests:** `test_board_finder_records_only_live_boards` (MockTransport), slug-candidate unit tests, CLI `--write` de-duplication.
