# JobPilot Plan 3: Tailored Resume & Cover Letter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For every selected job, JobPilot produces a one-page, ATS-friendly tailored resume (PDF + DOCX) and a short cover letter. Every claim in them traces to a verified fact. Each application's dashboard page shows previews, a bullet-by-bullet diff against the base resume, and its source facts. Once the documents are ready, the application moves to `FORM_IN_PROGRESS`, ready for Plan 4.

**Architecture:** `TAILOR` runs an LLM agent that returns a *tailoring plan*: which facts to use, their order, rewrites and the summary. Deterministic validators reject any rewrite that adds a number, tool, employer, title or date not present in its source fact. A deterministic renderer (Jinja2 HTML template, then Playwright Chromium print-to-PDF, with python-docx for DOCX) produces the files under `storage/artifacts/<application_id>/`. If the resume overflows one page, the renderer drops the lowest-priority bullets and renders again. Artifacts are versioned, hashed and attached only through `data/mutations.set_artifact`.

**Tech Stack:** existing stack plus `python-docx`, `pypdf` (page count and text extraction for validation). Playwright Chromium is already installed by `jobpilot setup`.

**Spec:** `docs/spec/JobPilot_Multi_Agent_Specification.txt` (§4.3, §9, §15) · Follow-ups: `docs/superpowers/plans/2026-10-24-plan2-followups.md`

## Global Constraints

- No personal data in tracked files; tests use the synthetic "Asha Example" profile fixture. Generated artifacts live under git-ignored `storage/artifacts/`.
- Employer names, job titles, dates, degrees and institutions are copied from facts verbatim, never from model output. Only bullet wording, bullet order, skill order and the summary come from the model.
- **Rewrite validator** (deterministic, `documents/validate.py`): a rewrite is rejected if it contains (a) any number or percentage not present in the source fact, (b) any token in the known-tech vocabulary (`config/tech_terms.txt`, about 400 terms, committed) not present in the source fact *or* in the verified skill facts, (c) any capitalized multi-word proper noun not in the source fact, or (d) it is shorter than 30% or longer than 160% of the source by characters. A rejected rewrite falls back to the original fact text. The output records `rewrites_rejected`.
- One page for the resume, exactly: PDF page count == 1 (pypdf). A4, margins 12 mm, base font 10.5 pt. If it overflows, drop the lowest-priority bullets (the plan's priority order) until it fits. If 6 bullets are dropped and it still doesn't fit, the task fails with `DOC_RENDER`.
- The cover letter is ≤ 250 words, has no placeholders (`[`, `{`, `<` brackets, "TODO", "XXX"), and mentions the exact company and role title from the JobRow. It uses 2–3 verified evidence points. Generic flattery is filtered by a banned-phrase list (`config/cover_letter_banned.txt`).
- Untrusted job text goes only through `build_messages(..., untrusted=...)`.
- Each artifact: `content_hash = sha256` of the file bytes, `source_fact_ids_json` lists every fact used, `version` increments per application+type.

## Review Focus

1. **Model invents a metric or tool in a rewrite.** Rejected by the validator, falls back to the original, warning recorded. (Task 2 `test_rewrite_with_new_number_rejected`, `test_rewrite_with_new_tool_rejected`)
2. **Resume overflows one page.** Bullets dropped by priority and page count 1 asserted; still overflowing gives `DOC_RENDER`. (Task 3 `test_overflow_drops_low_priority_bullets`)
3. **Fact changes after documents were generated.** Re-import invalidates the artifacts that cite it and the approval, and the application goes back to `DOCUMENTS_PENDING`. (Task 5 `test_reimport_invalidates_artifact_citing_changed_fact`)
4. **Cover letter names the wrong company or keeps a placeholder.** Rejected. (Task 4 `test_cover_letter_wrong_company_rejected`, `test_placeholder_rejected`)
5. **Regenerate while approved.** The new artifact replaces the old through `set_artifact`, which revokes the item approval. (Task 5 `test_regenerate_revokes_approval`)

---

## File structure

```
config/tech_terms.txt, config/cover_letter_banned.txt
src/jobpilot/documents/
  model.py          # ResumeDoc, Section, Bullet, CoverLetterDoc (pure data)
  validate.py       # validate_rewrite(), validate_cover_letter()
  render.py         # render_resume_html/pdf/docx, render_cover_letter_*, fit_one_page()
  templates/resume.html.j2, cover_letter.html.j2
  diff.py           # bullet_diff(base, tailored) -> list[DiffRow]
  store.py          # write_artifact() -> DocumentArtifactRow (versioned, hashed)
src/jobpilot/agents/tailor.py, prompts/tailor_v1.md, prompts/cover_letter_v1.md
src/jobpilot/dashboard/templates/application.html
tests/unit/test_doc_validate.py, test_render.py, test_diff.py; tests/agent_contracts/test_tailor.py; tests/e2e/test_tailor_pipeline.py
```

---

### Task 1: Document model, base resume from facts, and DOCX/HTML/PDF renderer

**Files:** Create `documents/{__init__,model,render}.py`, `documents/templates/resume.html.j2`, `tests/unit/test_render.py`. Modify `pyproject.toml` (`uv add python-docx pypdf`; package data for templates).

**Interfaces:**
- `class Bullet(BaseModel): fact_id: str; text: str; priority: int` (lower = more important)
- `class RoleBlock(BaseModel): employer: str; title: str; start: str; end: str; location: str | None; bullets: list[Bullet]`
- `class ResumeDoc(BaseModel): name: str; headline: str; contact_line: str; summary: str; skills: list[tuple[str, list[str]]]; roles: list[RoleBlock]; projects: list[RoleBlock]; education: list[str]; achievements: list[Bullet]`
- `def base_resume(profile: ProfileFile, facts: list[CandidateFactRow]) -> ResumeDoc`. Built only from verified facts, with identity from the ProfileFile. The contact line joins phone, email and links with " | ". Priority is the order within the profile.
- `def render_resume_html(doc: ResumeDoc) -> str` (autoescape on)
- `async def html_to_pdf(html: str, out: Path) -> int` (Playwright Chromium, `page.pdf(format="A4", margin=12mm, print_background=False)`; returns page count via pypdf)
- `async def fit_one_page(doc: ResumeDoc, out: Path, max_drops: int = 6) -> tuple[ResumeDoc, list[str]]`. Drops the highest-`priority` bullet among roles/projects/achievements, never the first bullet of a role, and re-renders until the page count is 1. Returns the final doc and the dropped fact_ids. Raises `DocRenderError(DOC_RENDER)`.
- `def render_resume_docx(doc: ResumeDoc, out: Path) -> None`: python-docx with headings and bullets, single column, no tables (ATS).
- PDF tests are marked `@pytest.mark.browser` and skipped if Chromium is not installed. CI installs it (`uv run playwright install chromium` step added to `ci.yml`).

- [ ] **Step 1: Failing tests:** `test_base_resume_uses_only_verified_facts`, `test_html_escapes_fact_text` (`<script>` in a fact is escaped), `test_pdf_single_page_for_fixture` (browser), `test_overflow_drops_low_priority_bullets` (synthetic doc with 40 bullets ends at 1 page and the dropped ids are the highest priority numbers; browser), `test_docx_contains_all_sections`, `test_unfittable_raises_doc_render`.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS.
- [ ] **Step 5:** `git commit -m "feat: resume document model and one-page renderer"`

### Task 2: Rewrite validator and bullet diff

**Files:** Create `documents/validate.py`, `documents/diff.py`, `config/tech_terms.txt`, `tests/unit/test_doc_validate.py`, `tests/unit/test_diff.py`.

**Interfaces:**
- `def validate_rewrite(source: str, rewrite: str, allowed_terms: set[str]) -> list[str]` returns violation codes: `new_number`, `new_tech:<term>`, `new_proper_noun:<x>`, `length`. Numbers are normalized (`60%`, `60 percent` and `60` are the same; `1,000+` and `1000` are the same). Tech matching is case-insensitive whole-phrase with lookarounds (reuse `util/textmatch.has_phrase`).
- `def load_tech_terms(path) -> set[str]`
- `class DiffRow(BaseModel): fact_id: str; section: str; original: str; tailored: str; status: Literal["kept","rewritten","rewrite_rejected","dropped","added_from_bank"]`
- `def bullet_diff(base: ResumeDoc, tailored: ResumeDoc, rejected: set[str], dropped: set[str]) -> list[DiffRow]`

- [ ] **Step 1: Failing tests:** `test_rewrite_with_new_number_rejected` ("cut retrieval time by 60%" → "cut retrieval time by 75%"), `test_rewrite_with_new_tool_rejected` (adds "Kubernetes"), `test_rewrite_allowed_tech_from_verified_skills`, `test_number_normalization_equivalence`, `test_length_bounds`, `test_proper_noun_added_rejected`, `test_diff_statuses`.
- [ ] **Step 2–4:** FAIL, implement, PASS. **Step 5:** `git commit -m "feat: deterministic rewrite validator and bullet diff"`

### Task 3: TAILOR agent (resume)

**Files:** Create `agents/tailor.py`, `agents/prompts/tailor_v1.md`, `documents/store.py`, `tests/agent_contracts/test_tailor.py`. Modify `runtime.py` (register TAILOR), `config/model_routes.yaml` (`resume_tailoring` already exists).

**Interfaces:**
- `class _TailorLLM(BaseModel): summary: str (≤ 450 chars); selected_fact_ids: list[str] (≤ 22); bullet_rewrites: list[{fact_id, text}] (≤ 22); skills_order: list[str] (≤ 30); priority: list[str]  # fact_ids most→least important`
- `class TailoringPlan(AgentOutput)`: application_id, profile_version, summary, selected_fact_ids, bullet_rewrites, skills_order, priority, rewrites_rejected: list[str], unsupported_claims: list[str]
- The agent loads the job's requirements and summary (untrusted) plus the verified facts and the match evidence (trusted, ids and values). It validates that every fact_id is verified (else `OutputRejected("unverified_fact")`) and runs `validate_rewrite` per rewrite, falling back to the original on violation. `skills_order` is filtered to verified skills, and unknown skills are dropped and reported in `unsupported_claims`. The summary is validated as one rewrite against the concatenation of the selected facts. On violation it falls back to a deterministic summary: `"{headline}. {years:.0f}+ years; {top 3 required skills present in verified skills}."`
- Handler `make_tailor_handler(paths)`:
  - Requires the application to be in `NOT_STARTED` or `DOCUMENTS_PENDING`.
  - Transitions to `DOCUMENTS_PENDING`.
  - Builds the tailored `ResumeDoc`, runs `fit_one_page`, renders PDF and DOCX, and writes both artifacts via `store.write_artifact(s, application_id, type, path, source_fact_ids)`. Versions are incremented and `content_hash` is sha256 of the bytes.
  - Calls `mutations.set_artifact(..., "resume", pdf_artifact_id)`. The DOCX is stored as type `resume_docx`.
  - Stores the diff JSON as an artifact of type `resume_diff`.
  - Follow-up `(COVER_LETTER, "application", id)`. Add `TaskKind.COVER_LETTER`.
  - Rendering runs in the handler, and an async render inside the sync handler uses the engine's worker loop. If the handler signature is sync, perform the render in the agent and pass the paths in the output. **Ruling for the implementer:** render in the agent (it is async), using a tool `artifact_dir(application_id) -> Path`; the handler only records rows and transitions.
- `POST /applications/{id}/tailor` and `/ui/applications/{id}/tailor` enqueue TAILOR (409 if one is queued or running). Selecting a job (Plan 2) now also enqueues TAILOR automatically.

- [ ] **Step 1: Failing tests (fake gateway, browser-marked where rendering):** `test_tailor_happy_path_writes_versioned_artifacts`, `test_tailor_rejects_unverified_fact`, `test_tailor_invalid_rewrite_falls_back`, `test_tailor_unknown_skill_dropped`, `test_tailor_wraps_untrusted`, `test_select_enqueues_tailor`.
- [ ] **Step 2–4:** FAIL, implement, PASS. **Step 5:** `git commit -m "feat: evidence-grounded resume tailoring agent"`

### Task 4: COVER_LETTER agent

**Files:** Create `documents/templates/cover_letter.html.j2`, `agents/prompts/cover_letter_v1.md`, `config/cover_letter_banned.txt`. Modify `agents/tailor.py` (or `agents/cover_letter.py`), `documents/validate.py`, `runtime.py`.

**Interfaces:**
- `class _CoverLLM(BaseModel): paragraphs: list[str] (2–4); evidence_fact_ids: list[str] (2–3)`
- `def validate_cover_letter(text: str, company: str, role: str, banned: list[str]) -> list[str]` returns codes `too_long`, `placeholder`, `missing_company`, `missing_role`, `banned:<phrase>`, `wrong_company:<x>` (another company name from the active jobs table appears).
- Each paragraph is also checked with `validate_rewrite` against the concatenation of the cited facts plus the fixed company and role strings. On any violation the agent retries once with the violation codes appended to the instructions. If it still fails: `OutputRejected("cover_letter_invalid")`.
- The handler renders the PDF and a `.txt` (plain text for form fields), stores artifact types `cover_letter` and `cover_letter_text`, calls `set_artifact(..., "cover_letter", pdf_id)`, and transitions `DOCUMENTS_PENDING → FORM_IN_PROGRESS`.

- [ ] **Step 1: Failing tests:** `test_cover_letter_happy_path`, `test_cover_letter_wrong_company_rejected`, `test_placeholder_rejected`, `test_banned_phrase_rejected`, `test_retry_once_then_reject`, `test_application_moves_to_form_in_progress`.
- [ ] **Step 2–4:** FAIL, implement, PASS. **Step 5:** `git commit -m "feat: grounded cover letter agent"`

### Task 5: Application page, artifact invalidation on re-import, regenerate

**Files:** Create `dashboard/templates/application.html`, `tests/e2e/test_tailor_pipeline.py`. Modify `profile/importer.py`, `dashboard/routes.py`, `api/main.py` (artifact download `GET /artifacts/{id}`, served only from `storage/artifacts` with path containment checked), `README.md`.

**Interfaces:**
- `/ui/applications/{id}`:
  - Job links (safe_href).
  - Status timeline.
  - Inline PDF previews via `<iframe src="/artifacts/{id}">`.
  - The diff table: original vs tailored, status badge and source fact.
  - Cover letter text and its evidence facts.
  - "Regenerate documents" button, which enqueues TAILOR and is allowed from `DOCUMENTS_PENDING`/`FORM_IN_PROGRESS`/`READY_FOR_REVIEW`. It moves the application back to `DOCUMENTS_PENDING`; a transition edge is added if missing, along with tests.
- `GET /artifacts/{artifact_id}`: `FileResponse` with the correct media type and `Content-Disposition: inline`. It resolves the path, asserts it is within `cfg.storage_path/"artifacts"`, and returns 404 otherwise.
- Importer: on re-import, every `DocumentArtifactRow` whose `source_fact_ids_json` contains a changed or removed fact gets its application invalidated. That application is moved to `DOCUMENTS_PENDING` if it is past it, and TAILOR is enqueued (event `documents_stale`).

- [ ] **Step 1: Failing tests:** `test_reimport_invalidates_artifact_citing_changed_fact`, `test_regenerate_revokes_approval`, `test_artifact_route_path_containment` (an artifact row with path `../../etc/passwd` returns 404), `test_application_page_renders_diff_and_previews`, plus e2e `test_select_to_form_in_progress` (fake gateway, browser-marked): select a MATCHED job, run until idle, and check the application is `FORM_IN_PROGRESS` with 1-page resume and cover letter artifacts.
- [ ] **Step 2–4:** FAIL, implement, PASS. **Step 5:** `git commit -m "feat: application documents page, staleness and regeneration"`

## Out of scope (Plan 4+)

Filling application forms (Plan 4), approval-gated real submission (Plan 5), outreach (Plan 6).
