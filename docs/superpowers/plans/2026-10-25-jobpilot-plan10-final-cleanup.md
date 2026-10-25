# JobPilot Plan 10: Final Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the deferred minors from Plans 1–9 that affect correctness, safety, robustness or the first real run, so that the user can go from the 3-command setup to real applications. The source of truth is the "Deferred minors" sections of `docs/superpowers/plans/*-followups.md`. Purely cosmetic test-style items are fixed only where a touched file makes it free.

## Global Constraints
- No behaviour change to the safety guarantees (approval gates, live-submit intent, no outreach sending, no fabrication, sensitive classification). Each fix keeps or tightens them.
- TDD for every behavioural fix. No personal data in tracked files. Never open `storage/` in implementation work.
- Each task: one commit; full suite, ruff, alembic check green.

### Task 1: Data & audit integrity
- `redact` handles sets, tuples and BaseModels (redacts inside them); naive datetimes → UTC tested; `allow_nan=False` in JSON dumps.
- Audit event `details` cannot overwrite `from/to/reason`; an invalid stored status gives a clear error, not a bare ValueError.
- `upsert_job` race: insert-or-ignore on the unique key (no check-then-insert).
- Duplicate `fact_id` in profile import → schema error, not DB error.
- `_approvals_containing` tolerates malformed JSON (treated as not approved).
- `config.py` read_text with `encoding="utf-8"` everywhere config/profile files are read.

### Task 2: Orchestrator & worker robustness
- Lease acquisition multi-worker safe (single UPDATE … WHERE with CAS, no select-then-update); CAS loop does not idle early after 5 losses (backs off and retries next tick).
- Discovery double-enqueue race: one DISCOVER per run (unique pending key).
- `start` takes `ops_lock` (shared) so restore cannot run under a starting server; `_plain` export serializer handles date, Decimal, Enum, Path.
- Model client: 429/408 mapped to a retryable `MODEL_RATE_LIMITED`/`MODEL_TIMEOUT`, not INVALID_SCHEMA; `aclose` does not close an injected client; malformed `available_models` body handled.
- Untrusted-block tag matching case-insensitive.

### Task 3: Discovery, matching & documents correctness
- Location match on word boundaries ("Pune" ≠ "Puneet"); exclude-keywords also checked in the description.
- `\b` tech-term matching works for `c++`, `.net`, `c#`, `node.js`.
- Category path URL-encoded; default ports stripped in URL normalization.
- Malformed `extracted_json` → event + re-extract, not silent replace; summary truncation at a word boundary.
- Documents: orphan PDF removed if the txt write fails; cover-letter handler cross-checks company/role; partial role mention masked in cover-letter validation.
- Re-import invalidates only documents citing changed facts (not all in-flight docs).

### Task 4: Sources, outreach & essays hardening
- Workday `externalUrl` host pinned to the tenant host; SmartRecruiters/Workday adapters declare `supports_max_postings` explicitly.
- Careers fetch: resolve the hostname and refuse private/loopback/link-local addresses.
- Outreach slug fallback: only common TLDs (com, io, ai, co, in, co.in, net, org, dev, tech, app) and the careers-inbox tool also receives the company name for the slug fallback.
- `FormField.maxlength` read from the DOM (`maxlength` attr) and passed to essay drafting.
- `companies.yaml` writes are atomic; a corrupt file shows an error on `/ui/companies` and in the add form (no silent empty list); POST-redirect-GET after add.
- Backup restore: total extraction size cap (2× archive manifest size, max 20 GB) and member count cap.

### Task 5: Dashboard & ops polish, first-run readiness
- N+1 queries removed on jobs and batch pages; jobs list and detail use the same score (latest).
- `health_check.py`/`doctor` print a clear message on connection errors (no traceback).
- Settings page logs the schedule-status exception; live-mode render test.
- `doctor` checks: profile facts verified count, stories verified count, companies configured, Ollama models pulled, Playwright Chromium installed — each with the exact command to fix.
- README "First real run" checklist: verify facts and stories, add companies (`jobpilot boards find`), run discovery, review a batch, keep `dry_run: true` for a first supervised pass, then enable one portal in `live_portals`.
- Remove dead code flagged in reviews (dead `Pipeline.runtime`, dead Optional in Workday `_parse`, unused `orch` param, redundant purge check).
