# JobPilot — System Design

**Status:** Draft v1 · 2026-10-24
**Spec:** [docs/spec/JobPilot_Multi_Agent_Specification.txt](../spec/JobPilot_Multi_Agent_Specification.txt)
**Host:** Apple Silicon MacBook, 24 GB unified memory, macOS, Python 3.13, `uv`, Ollama installed.

This document resolves the open decisions in the spec (§20) and the differences between the spec and the earlier chat proposal, then describes components, data, flows, and trade-offs. Where the two disagree, **the spec wins** unless stated otherwise.

---

## 1. Requirements

### 1.1 Functional (from spec §1–§11)
1. Import a base resume (DOCX/PDF) into a versioned, user-verified **fact bank**.
2. Discover jobs from permitted sources, normalize, deduplicate (keeping provenance).
3. Score jobs on 5 weighted dimensions + separate hard filters, with evidence.
4. Generate a tailored resume + cover letter where every claim maps to a `source_fact_id`.
5. Inspect and fill supported application forms; abstain on unknown/legal fields; never submit in preparation mode.
6. Build a **versioned batch**; user approves a subset; approval is bound to a content hash.
7. Submit approved applications; record evidence; never blind-retry; `SUBMISSION_UNCERTAIN` when unknown.
8. Research one public professional recruiting contact per company; draft one message; separate approval to send.
9. Dashboard as source of truth: every job, artifact, answer, event, approval, evidence.

### 1.2 Non-functional
| Concern | Target |
|---|---|
| Users | 1 (local, single-tenant) |
| Volume | ≤100 jobs/run, ≤ tens of applications/day — trivial for SQLite |
| Latency | Dominated by local LLM (seconds–minutes per task); UI must stay responsive → work runs out-of-process from the UI |
| Memory budget | ~24 GB shared: OS+browser+app ≈ 8 GB → **≤ ~12 GB resident model**, one model loaded at a time |
| Reliability | Crash-resumable; every side effect idempotent or preflight-checked |
| Privacy | All data on-device; dashboard bound to `127.0.0.1`; secrets in Keychain; 0 secrets in logs |
| Safety default | `DRY_RUN=true` shipped; real submit/send needs config change **and** approval |

### 1.3 Constraints
Solo developer, local hardware, no cloud fallback, portal terms vary and must be reviewed per adapter.

---

## 2. Resolved decisions

| Question | Chat proposal | Spec | **Decision** | Why |
|---|---|---|---|---|
| Orchestration | LangGraph | Plain Python state machine | **Plain state machine + DB-persisted state** | Business state must live in SQLite anyway; LangGraph adds a second state store and buys little for a linear pipeline with gates. Revisit if branching/parallel sub-graphs appear. |
| UI | FastAPI + React | Streamlit MVP | **Server-rendered Jinja2 + HTMX, served by the same FastAPI app** | Final choice, no later swap: no Node toolchain, no second process, approval forms post to the same services the API uses. |
| DB layer | SQLite | SQLite + migrations | **SQLite (WAL) + SQLAlchemy 2.0 + Alembic** | Typed models, real migrations; small cost. |
| Models | `gemma4:12b` everywhere | Small model first, benchmark stronger one | **Router with per-task profiles; default `gemma4:e4b` for extraction/mapping, `gemma4:12b` candidate for tailoring/cover letter; Qwen 3.6 27B only if benchmark justifies** | 12B Q4 ≈ 8–9 GB fits; 27B+ will pressure 24 GB alongside a browser. Pin exact tags after the Phase-0 benchmark. |
| Model calls | — | Structured outputs | **Ollama `/api/chat` with `format=<JSON Schema>`, `temperature=0` for extraction, Pydantic-validated, one repair retry** | Spec §12.4. |
| Process model | — | — | **One process: `jobpilot start` = FastAPI + dashboard + orchestrator worker loop (asyncio task)** | One command to run; worker never blocks request handling; orchestrator is single-writer. |
| Setup | — | setup_mac.sh | **`uv sync` → `uv run jobpilot setup` (Playwright Chromium, Ollama model pulls, migrations) → `uv run jobpilot start`; `jobpilot doctor` for health** | Three commands from clone to running. |
| Concurrency | — | Bounded | **One worker, sequential task execution; per-source rate limiter** | Single model in memory; SQLite single writer; simplest correct choice. |
| Scheduling | — | launchd | **launchd triggers `POST /runs/discovery`; never submission** | Spec §3.2. |
| First portal | — | Easiest permitted | **Greenhouse + Lever public posting APIs for discovery; local mock form for application** | Documented public APIs; no scraping/ToS risk for MVP. LinkedIn/Naukri deferred to Phase 8 after policy review. |

---

## 3. High-level architecture

```
 ┌──────────────┐  HTTP (localhost)  ┌────────────────────────────────────────────┐
 │ Browser      │ ─────────────────▶ │ jobpilot start (FastAPI+HTMX, 127.0.0.1:8765)│
 └──────────────┘                    │  routes ─▶ services (read models, commands)│
                                     │                │                           │
 launchd ── POST /runs/discovery ──▶ │      ┌─────────▼──────────┐                │
                                     │      │ Orchestrator       │ single writer  │
                                     │      │ • task queue (DB)  │                │
                                     │      │ • state machines   │                │
                                     │      │ • policy gates     │                │
                                     │      │ • approval service │                │
                                     │      └──┬───┬───┬───┬───┬─┘                │
                                     │   Finder Matcher Resume Form Outreach      │
                                     │   (agents: typed in → typed out, no I/O    │
                                     │    except via injected tools)              │
                                     │      │        │            │               │
                                     │  ┌───▼───┐ ┌──▼────────┐ ┌─▼────────────┐  │
                                     │  │Source/│ │Model      │ │Portal adapter│  │
                                     │  │HTTP   │ │gateway    │ │(Playwright)  │  │
                                     │  └───────┘ └──┬────────┘ └──────────────┘  │
                                     └───────────────┼────────────────────────────┘
                                          ┌──────────▼───┐   ┌──────────────────────┐
                                          │ Ollama :11434│   │ SQLite (WAL) +       │
                                          └──────────────┘   │ storage/artifacts/   │
                                                             └──────────────────────┘
```

### Key rules
- **Agents are pure-ish functions**: `async run(input: XInput, tools: XTools) -> XOutput`. They never write the DB, never call each other, never see policy.
- **Orchestrator is the only state writer.** Every state change = one DB transaction containing (entity update + `AuditEvent` insert).
- **Side-effect gate** sits in code between orchestrator and adapter `submit()` / `send()`; it checks dry-run, capability, approval hash, duplicate key. Model output cannot reach these calls.
- **Untrusted content** (job text, page text) is passed to the model only inside a delimited `<untrusted_content>` block in the user message; system prompts state it is data.

---

## 4. Data model

SQLite tables (SQLAlchemy). IDs are prefixed ULIDs (`job_01J…`, `app_…`) — sortable and stable.

| Table | Key columns | Notes |
|---|---|---|
| `candidate_profile` | `profile_id`, `version`, `preferences_json`, `created_at` | New row per version; never updated in place |
| `candidate_fact` | `fact_id`, `profile_version`, `category`, `field`, `value`, `source_artifact`, `source_location`, `verified_by_user`, `allowed_for_resume`, `sensitivity` | Fact bank (spec §6.3) |
| `answer_library` | `answer_id`, `profile_version`, `question_key`, `value_json`, `decision_default` | Approved form answers (e.g. notice period) |
| `job` | `job_id`, `source`, `source_job_id`, `canonical_url`, `application_url`, `company`, `title`, `location`, `work_mode`, `description_text`, `extracted_json`, `dedupe_key`, `canonical_job_id` (nullable → duplicates), `status`, `first_seen_at`, `last_seen_at` | `UNIQUE(source, source_job_id)` |
| `match_assessment` | `job_id`, `profile_version`, dimension scores, `total_score`, `hard_filter_passed`, `evidence_json`, `confidence` | |
| `application` | `application_id`, `job_id` UNIQUE, `portal`, `status`, `batch_id`, `resume_artifact_id`, `cover_letter_artifact_id`, `content_hash`, `idempotency_key`, `confirmation_reference` | One application per canonical job |
| `document_artifact` | `artifact_id`, `application_id`, `type`, `path`, `content_hash`, `version`, `source_fact_ids_json` | Files under `storage/artifacts/<application_id>/` |
| `form_field_answer` | `application_id`, `field_id`, `label`, `field_type`, `proposed_value_json`, `decision`, `source_fact_id`, `confidence`, `rationale`, `validation_errors_json` | |
| `approval_batch` | `batch_id`, `version`, `status`, `content_hash`, `created_at` | |
| `batch_item` | `batch_id`, `version`, `application_id`, `item_hash`, `selected` | Frozen snapshot per version |
| `approval` | `approval_id`, `batch_id`, `batch_version`, `kind` (`APPLICATION`/`OUTREACH`), `content_hash`, `approved_item_ids_json`, `approved_at`, `revoked_at` | |
| `contact_candidate` | `contact_id`, `company`, `name`, `professional_title`, `public_source_url` NOT NULL, `channel`, `confidence`, `verified_at` | |
| `outreach_draft` | `outreach_id`, `application_id`, `company_key`, `campaign_id`, `contact_id`, `channel`, `message_text`, `resume_artifact_id`, `content_hash`, `status`, `sent_at`, `evidence_ref` | `UNIQUE(company_key, campaign_id)` enforces one per company |
| `agent_run` | `run_id`, `agent_name`, `task_id`, `status`, `model_tag`, `prompt_version`, `input_ref`, `output_ref`, `latency_ms`, `token_estimate`, `error_code` | |
| `task` | `task_id`, `kind`, `entity_type`, `entity_id`, `status`, `attempts`, `not_before`, `lease_until` | Durable work queue; resume after crash |
| `audit_event` | `event_id`, `timestamp_utc`, `entity_type`, `entity_id`, `run_id`, `actor`, `action`, `outcome`, `redacted_details_json`, `evidence_ref`, `duration_ms`, `error_code` | **Append-only** via SQLite `BEFORE UPDATE/DELETE` triggers that `RAISE(ABORT)` |

### Hashing
`content_hash = sha256(canonical_json(obj))` where canonical JSON = sorted keys, no whitespace, UTF-8. An application's hash covers: `application_id`, `application_url`, resume/cover-letter artifact hashes, every `FormFieldAnswer` (`field_id`, `proposed_value`, `decision`), and planned action list. Batch hash = hash of sorted `(application_id, item_hash)` for selected items.

---

## 5. Workflow & state

State enums and allowed transitions are exactly spec §5.2. Transitions are declared as `dict[State, frozenset[State]]` per entity; `transition()` rejects anything else.

```
Job:  DISCOVERED → NORMALIZED → MATCHED ─┬─▶ (selected) → Application NOT_STARTED
                    │            └─ REJECTED
                    └─ DUPLICATE / STALE / CLOSED

Application:
NOT_STARTED → DOCUMENTS_PENDING → FORM_IN_PROGRESS ─┬─ NEEDS_USER_INPUT ─┐
                                                    └─▶ VALIDATING ◀─────┘
VALIDATING → VALIDATION_FAILED | READY_FOR_REVIEW
READY_FOR_REVIEW → APPROVED | REJECTED_BY_USER | FORM_IN_PROGRESS (edit → approval invalidated)
APPROVED → SUBMISSION_IN_PROGRESS → SUBMITTED → CONFIRMED
                                  ├→ SUBMISSION_UNCERTAIN  (no automatic exit except user/inspection)
                                  └→ SUBMISSION_FAILED
```

### Task loop (orchestrator worker)
1. Lease next `task` where `status=QUEUED and not_before<=now` (sets `lease_until=now+timeout`).
2. Load inputs, call agent via gateway with deadline.
3. Validate output (Pydantic + business validators).
4. In one transaction: persist output, transition entity, write `AuditEvent`, enqueue follow-up tasks, mark task done.
5. On startup: tasks with expired leases → requeued **unless** kind is side-effecting (`SUBMIT`, `SEND`) → entity moved to `SUBMISSION_UNCERTAIN` / `DELIVERY_UNKNOWN`.

### Side-effect protocol (submit / send)
```
preflight: gate.may_submit(app, approval, policy)        # pure function, spec §5.4
persist:   app → SUBMISSION_IN_PROGRESS + event "submit_started" (commit BEFORE clicking)
act:       adapter.submit(...)  (dry-run → SimulatedAdapter)
persist:   SUBMITTED/CONFIRMED + evidence  | timeout → SUBMISSION_UNCERTAIN
```
Write-ahead state means a crash between "persist" and "act" is detected on restart and never auto-retried.

---

## 6. API surface (FastAPI, localhost only)

| Method & path | Purpose |
|---|---|
| `GET /health` | Ollama reachable + configured tags present, DB migration head, disk, browser installed |
| `POST /runs/discovery` | Enqueue discovery run |
| `GET /jobs?status=&min_score=` · `GET /jobs/{id}` | Feed + detail (with match, sources, events) |
| `POST /jobs/{id}/select` | Create application |
| `GET /applications/{id}` | Full detail page payload (docs, answers, timeline, evidence) |
| `PATCH /applications/{id}/answers/{field_id}` | Edit answer → invalidates approval |
| `POST /batches` · `GET /batches/{id}` | Build / view versioned batch |
| `POST /batches/{id}/approve` body `{version, content_hash, application_ids}` | Approve; 409 if hash stale |
| `GET /outreach` · `POST /outreach/{id}/approve` | Separate outreach queue |
| `GET /events?entity_id=` | Activity log |
| `POST /control/pause` · `/control/resume` · `/control/stop` | Stop new work; show in-flight |
| `GET /applications/{id}/export` | Audit bundle (JSON + docs zip) |

The UI posts the **hash it displayed**; the server recomputes and rejects mismatch — the user can only approve what they saw.

---

## 7. Model gateway

`ModelGateway.generate(task: TaskProfileName, messages, schema: type[BaseModel]) -> (BaseModel, AgentRunMeta)`

- Routes from `config/model_routes.yaml`: `model_tag`, `temperature`, `num_ctx`, `timeout_s`, `keep_alive`.
- Calls `POST http://127.0.0.1:11434/api/chat` with `stream=false`, `format=schema.model_json_schema()`.
- Validates; on `ValidationError` sends one repair turn (error summary only); second failure → `MODEL_INVALID_SCHEMA`.
- No fallback to another model unless the route lists `fallback`; never cloud.
- `keep_alive` on the shared model avoids reload thrash between agents.

---

## 8. Error handling

Error codes follow spec §14.3 (`SOURCE_*`, `MODEL_*`, `DOC_*`, `FORM_*`, `AUTH_*`, `SUBMIT_*`, `OUTREACH_*`). Each is an enum member with a `recovery` attribute (`RETRY_BACKOFF`, `USER_INPUT`, `MARK_UNCERTAIN`, `DISABLE_SOURCE`, `FAIL`). Retries: max 3, exponential backoff (2s, 8s, 32s) — **never** for `SUBMIT_*`/`OUTREACH_*` send paths.

---

## 9. Security

- Dashboard/API bind `127.0.0.1` only; config validation rejects other hosts.
- Credentials: `keyring` (macOS Keychain) via `security/secrets.py`; never in YAML, prompts, or logs.
- `observability/redaction.py` scrubs event details: keys matching `password|token|secret|cookie|authorization|api_key` and value patterns (JWT-like, long hex, bearer); email/phone redacted except allowlisted profile email in non-log contexts.
- Playwright uses a dedicated persistent context under `storage/browser_state/` (never the user's Chrome profile).
- No agent receives shell, filesystem, SQL, or arbitrary browser tools.

---

## 10. Scale & what to revisit

| Trigger | Revisit |
|---|---|
| Need parallel portal work while LLM runs | Split browser worker from LLM worker; keep DB as queue |
| Branching/sub-workflows become hard to express | Consider LangGraph with DB checkpointer, keeping DB as business truth |
| Dashboard needs rich client interactivity (complex diff editing) | Add targeted JS islands; API stays the same |
| Model quality insufficient | Benchmark `gemma4:26b` / `qwen3.6:27b` with memory telemetry |
| >1 user / devices | Postgres + auth; out of scope now |

---

## 11. Delivery

Spec §18 phases map to separate implementation plans, each producing working software:

| Plan | Spec phases | File |
|---|---|---|
| **1. Foundation** | 0 (env/benchmark harness stub) + 1 + gates from 5 | `docs/superpowers/plans/2026-10-24-jobpilot-foundation.md` |
| 2. Profile ingestion + Finder + Matcher | 0 (profile) + 2 | to write after Plan 1 |
| 3. Resume + cover letter | 3 | later |
| 4. Form preparation + batch review UI | 4 + 5 | later |
| 5. Submission proof (one permitted portal) | 6 | later |
| 6. Outreach | 7 | later |
| 7. Additional portals + hardening | 8 + 9 | later |
