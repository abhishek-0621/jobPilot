# JobPilot Plan 9: Story Bank & Supervised Essay Answers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open-ended application questions ("What are you most proud of?", "Biggest failure?", "Strengths and weaknesses / SWOT", "Describe a challenging project", "Explain something you built and how good it is", "Tell us about a time you helped a customer / disagreed / led / learned fast") get drafted answers grounded **only** in the user's own verified stories and facts. Every drafted answer is FILL_AND_REVIEW (never AUTO_FILL), shown with the stories it used, and the user approves it like any other answer. A question with no suitable verified story is ASK_USER with a note naming what is missing — never an invented story.

**Architecture:**
- **Story bank file (private):** `storage/profiles/stories.yaml` (git-ignored), loaded by `profile/stories.py` with Pydantic (`extra="forbid"`):
  - `standing_rules: list[str]` (e.g. "use 4+ years", "name gaps plainly") — appended to the drafting prompt as user rules.
  - `stories: list[Story]`, each `Story`: `story_id` (slug), `title`, `themes: list[Theme]` (enum: `ownership, achievement, proud_of, challenge, failure, performance, customer, disagreement, teamwork, leadership, learning, ambiguity, quality_vs_speed, built_explain, strengths, weaknesses, why_ai, other`), `situation`, `task`, `action`, `result`, `tradeoffs` (optional), `evidence: list[str]` (metrics/claims as stated), `missing: list[str]` (the user's own "[ADD]" notes), `verified: bool = false`.
  - `canned: dict[QuestionKey, str]` — answers the user wrote themselves (e.g. weakness, overall expertise). Used verbatim (FILL_AND_REVIEW) when present; still validated against facts for numbers.
  - The committed example `config/stories.example.yaml` uses a fictional person and company only.
- **Import & verification:** stories are imported into a new table `story` (migration `0010_story_bank`) with `profile_version` and a content hash; a changed story becomes unverified again. Dashboard `/ui/profile/stories` lists stories with Verify/Unverify buttons, shows `missing` notes, and links back to which applications used each story. Verification goes through the same path as facts (`verify_fact` analogue `verify_story`, actor `user`, audit event).
- **Question classifier:** `forms/essay.py` `classify_essay(label, help_text) -> QuestionKey | None` — deterministic patterns + synonyms (`config/essay_questions.yaml`, committed: question keys → patterns, e.g. `failure`: "biggest failure", "a time you failed", "mistake you made", "setback"; `swot`: "swot", "strengths and weaknesses", "opportunities", "threats"; `proud_of`: "proud", "greatest achievement", "most significant accomplishment"; `built_explain`: "describe a project", "something you built", "walk us through", "how good"; `challenge`, `customer`, `disagreement`, `teamwork`, `leadership`, `learning`, `ambiguity`, `why_ai`, `strengths`, `weaknesses`). The essay classifier runs after the sensitive-question classifier: sensitive questions are never essays. Only textarea / long text fields qualify.
- **Selection:** `select_stories(key, job, stories) -> list[Story]` (≤ 2, verified only): theme match first, then overlap between the job's extracted skills/requirements and story evidence/tech terms, then story order in the file (the user's ranking).
- **Drafting agent:** `agents/essay.py` `EssayAgent`, model route `essay` (add to `model_routes.yaml`, same model as cover letter), prompt `essay_v1.md`. Input: the question text (untrusted, from the form or the manual package list), the selected stories, verified facts, standing rules, job summary (untrusted block). Output: `answer` (≤ the field's maxlength, default 1 200 chars; ~120–250 words), `story_ids`, `fact_ids`. Validation (reject → one retry → ASK_USER):
  - every number, tech term and proper noun must appear in the selected stories' text or verified facts (`validate_rewrite` with the stories' text added to the allowed source);
  - no leadership/people-management claim unless present in a story;
  - no placeholder brackets, no "[ADD]" text, nothing from a story's `missing` notes stated as fact;
  - SWOT: must have four labelled parts; Threats/Weaknesses may use `weaknesses`/gap facts only.
- **Wiring:**
  - `form_prep` / `forms/mapping.py`: an essay field with no answer-library hit enqueues/runs the essay draft and stores a `FormFieldAnswerRow` with decision FILL_AND_REVIEW, `source="essay:<story_ids>"`; no suitable verified story → ASK_USER with note `needs_story:<key>`.
  - `manual_package`: add common essay questions (proud_of, failure, challenge, strengths, weaknesses, why_ai) to the drafted list.
  - Approval: essay answers are normal answers, hash-bound; editing through `mutations.set_answer` invalidates approval. An unverified or changed story invalidates drafts that used it (they go back to needs-review with a "story changed" note).
  - Answer library: when the user approves an essay answer, offer "Save to answer library for this question type".

**Spec:** §4.3 (form answers from verified facts only), §9 (question handling), §10 (no fabrication), user request 2026-10-25 (personal questions from their real experience).

## Global Constraints

- **No fabrication.** An answer may use only verified stories, verified facts and the user's canned answers. Nothing from `missing` notes is stated as fact. A story marked unverified is never used.
- **Never auto-filled.** Essay answers are always FILL_AND_REVIEW or ASK_USER; never AUTO_FILL, never submitted without the normal approval.
- **Sensitive questions stay sensitive.** Compensation, reasons for leaving, demographic, legal etc. are never routed to the essay agent.
- **Untrusted input.** Question labels and job text go in the untrusted block; prompt-injection in a label cannot change rules.
- **No personal data in tracked files.** The real story bank lives only in `storage/profiles/stories.yaml`. Fixtures and the example use a fictional person ("Sam Example", "Example Corp"). The privacy-marker test must pass.
- Tests never hit the network; the model is faked in tests.

## Review Focus

1. **A number, tool or employer not in the stories/facts appears in an answer.** (Task 3 `test_essay_rejects_unsupported_claims`)
2. **An unverified story, or a story's `missing` note, is used.** (Task 3 `test_unverified_and_missing_never_used`)
3. **A sensitive question (salary, reason for leaving) is drafted as an essay.** (Task 2 `test_sensitive_never_essay`)
4. **Essay answer ends up AUTO_FILL or approved without review.** (Task 4 `test_essay_answers_require_review`)
5. **Changing a story leaves stale approved answers.** (Task 4 `test_story_change_invalidates_answers`)

---

### Task 1: Story bank schema, import, migration
- **Files:** `profile/stories.py`, migration `0010_story_bank.py`, `tables.py` `StoryRow`, `config/stories.example.yaml`, setup/doctor mention, CLI `jobpilot profile import` also imports stories when the file exists.
- **Tests:** schema rejects unknown keys/themes, import idempotent, changed story → unverified, `verify_story` audit event, migration up/down, `alembic check`.

### Task 2: Essay question classifier
- **Files:** `forms/essay.py`, `config/essay_questions.yaml`.
- **Tests:** synonyms per key (≥ 3 phrasings each), `test_sensitive_never_essay`, short text fields not essays, injection text in a label still classified by pattern only.

### Task 3: Story selection + essay agent
- **Files:** `agents/essay.py`, `agents/prompts/essay_v1.md`, `model_routes.yaml` route `essay`.
- **Tests:** `test_essay_rejects_unsupported_claims`, `test_unverified_and_missing_never_used`, `test_swot_four_parts`, `test_canned_answer_used_verbatim`, `test_no_story_asks_user`, selection ranking, maxlength respected.

### Task 4: Wiring into form prep, manual package, approval
- **Files:** `forms/mapping.py` / `agents/form_prep.py`, `agents/manual_package.py`, `data/mutations.py`, answer library save.
- **Tests:** `test_essay_answers_require_review`, `test_story_change_invalidates_answers`, `test_manual_package_includes_essays`, `test_essay_source_recorded`.

### Task 5: Dashboard + README
- **Files:** `/ui/profile/stories` (list, verify/unverify, missing notes, used-by), application review page shows "Drafted from: <story titles>" next to each essay answer and the ASK_USER note for missing stories; README "Story bank" section explaining `stories.yaml`, verification and the no-fabrication rule.
- **Tests:** page renders, verify via POST with CSRF/origin checks, review page shows story titles, autoescape of story text.
