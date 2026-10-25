# JobPilot Plan 7: Scheduling, Backups & Data Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** JobPilot can run unattended on the user's Mac for the safe parts and keeps their data recoverable and under their control:
- **Scheduled discovery.** A macOS LaunchAgent can run discovery at a set time each day, and optionally keep the app running at login. Submission is never scheduled.
- **Backups.** One command makes a consistent backup of the database, profile and artifacts, with rotation. Restore needs a typed confirmation.
- **Export.** The user can export one application's full audit bundle (JSON plus its documents) or all of their data.
- **Deletion and retention.** The user can delete an application's data, contacts or everything, each behind a typed confirmation. A retention setting prunes old artifacts and contact data.
- **Settings page** shows schedule status, last backup, retention and the live/dry-run state, which stays read-only.

**Architecture:**
- **Scheduling.** A `jobpilot/ops/schedule.py` module renders LaunchAgent plists (`com.jobpilot.discover`, `com.jobpilot.server`) from templates. Every program path is absolute and resolved from the current `uv` environment. `jobpilot schedule install|uninstall|status` writes or removes them under `~/Library/LaunchAgents/` and calls `launchctl bootstrap/bootout gui/$UID`. The discovery job only enqueues a DISCOVER task; the running server's worker does the work. If no server is running, `jobpilot discover --run-now` runs one discovery cycle in-process: discover → extract → match only. There is never a run of tailor, prepare or submit unattended unless the server is running and processing tasks the user already queued.
- **Backups.** `jobpilot/ops/backup.py` uses SQLite's online backup API (`sqlite3.Connection.backup`) to take a consistent DB copy while the app runs. It then builds `tar.gz` = `db.sqlite` + `storage/profiles/` + `storage/artifacts/` + `manifest.json`. The manifest holds the version, a UTC timestamp, sha256 per file and the alembic revision. The archive goes to `storage/backups/jobpilot-YYYYmmdd-HHMMSS.tar.gz`, keeping the newest `cfg.backup_keep` (default 14). Restore verifies the manifest hashes first. It refuses while the server is running (pid file / port check). It moves the current storage to `storage/.pre-restore-<ts>/` before extracting.
- **Export.** `jobpilot/ops/export.py` produces two zips:
  - `export_application(app_id) → zip`: `application.json` (job, match, answers, events with redacted details, approvals, submission result), the documents and screenshots.
  - `export_all() → zip`: the DB dump as JSON per table, plus profiles and artifacts.
- **Deletion.** `jobpilot/ops/purge.py`:
  - `delete_application(app_id)` removes the artifacts dir and the answer/artifact rows, sets the app to a `DELETED` tombstone (no PII), and keeps audit events. Events are append-only and already redacted; a `data_deleted` event is added.
  - `purge_contacts(older_than_days)` targets contact data, for Plan 8.
  - `delete_all()` wipes `storage/` except `storage/backups` unless that is chosen too. It needs the typed confirmation `DELETE ALL`.
- **Retention.** `cfg.retention_days.artifacts` (default 365) and `.contacts` (default 180) are applied by `jobpilot maintenance`, which the scheduled discovery job also runs. It never touches applications in non-terminal states.

**Tech Stack:** stdlib (`plistlib`, `tarfile`, `sqlite3`, `zipfile`, `hashlib`), existing stack.

**Spec:** §3.2 (launchd; no unattended submissions), §7.3 (export audit bundle), §13.1 (deletion/export, protect backups), §14 (recovery).

## Global Constraints

- **Never schedule submission or outreach sending.** LaunchAgents may only run `jobpilot discover` (enqueue or `--run-now`, limited to DISCOVER/EXTRACT/MATCH), `jobpilot maintenance` and `jobpilot backup`, plus optionally `jobpilot start`. A test asserts the generated plists contain no other subcommands.
- **The tests never touch the real system.** No writes to `~/Library/LaunchAgents` and no real `launchctl`. Both the home directory and `launchctl` are injectable (a `runner` callable), and tests use `tmp_path` and a fake runner.
- **Backups and exports contain personal data.** They live under `storage/` (git-ignored) with file mode `0600` and dir mode `0700`. The CLI and README warn about this.
- **Destructive actions need typed confirmations, enforced on the server:**
  - restore: `RESTORE`
  - delete application: the application id
  - delete all: `DELETE ALL`
  - Restore and delete-all are CLI only; there is no dashboard button for them. Delete-application is available in the dashboard with the confirmation field.
- **Audit history stays append-only.** Deletion never edits or removes `audit_event` rows. It adds `data_deleted` events with ids and counts only.
- **Retention never deletes data of applications in progress.** Only terminal states are pruned: CONFIRMED, SUBMITTED_BY_USER, REJECTED_BY_USER, SUBMISSION_FAILED, DELETED.
- No personal data in tracked files. Plist templates use placeholders only.

## Review Focus

1. **A plist that could run submit.** The generator's allowlist rejects it. (Task 1 `test_plist_commands_allowlisted`)
2. **Backup taken mid-write.** The online backup API copy is consistent and the manifest hashes verify. (Task 2 `test_backup_consistent_during_writes`)
3. **Restore with a tampered archive, or while the server runs.** It is refused and current data is untouched. (Task 2 `test_restore_refuses_bad_hash_and_running_server`)
4. **Delete application while it is SUBMISSION_IN_PROGRESS.** It is refused with 409. (Task 4 `test_delete_refuses_in_flight`)
5. **The export bundle leaks secrets.** Event details stay redacted and no browser state or cookies are included. (Task 3 `test_export_excludes_secrets_and_browser_state`)

---

### Task 1: LaunchAgent scheduling
- **Files:** `ops/__init__.py`, `ops/schedule.py`, and CLI `jobpilot schedule install [--at HH:MM] [--with-server] | uninstall | status`. Config `schedule.discover_at: "08:30"`.
- **Plists:**
  - `ProgramArguments` = [absolute `uv` path, `run`, `--project`, repo dir, `jobpilot`, `discover`, `--run-now`].
  - `StartCalendarInterval` = {Hour, Minute}.
  - `StandardOutPath`/`StandardErrorPath` = `storage/logs/discover.log`.
  - `EnvironmentVariables` PATH includes the Homebrew/Ollama dirs.
  - The server plist uses `RunAtLoad` + `KeepAlive` for `jobpilot start`.
- **Allowlist:** `{discover, maintenance, backup, start}`.
- **Install behaviour:** `launchctl bootstrap gui/<uid> <plist>`. Install is idempotent: bootout first if the agent is already loaded.
- **Status:** shows whether each agent is loaded (`launchctl print gui/<uid>/<label>` through the runner) and the next run time.
- **`discover --run-now`:** runs the orchestrator in-process with only the DISCOVER, EXTRACT and MATCH agents registered. Other kinds stay queued for the server. A `jobpilot maintenance` pass runs after it.
- **Tests:**
  - `test_plist_commands_allowlisted`
  - `test_install_writes_plist_and_bootstraps` (fake home and runner)
  - `test_uninstall_bootout_and_removes`
  - `test_run_now_only_discovery_kinds` (a queued SUBMIT/TAILOR task is left untouched)

### Task 2: Backup & restore
- **Files:** `ops/backup.py` and CLI `jobpilot backup [--keep N]` and `jobpilot restore <archive> --confirm RESTORE`. Config `backup_keep: 14`.
- **Backup:**
  - Takes the DB with the online backup API into a temp file in `storage/backups/.tmp/`.
  - Builds the tar with relative paths and no symlinks (skip them, with a warning).
  - Writes `manifest.json` with sha256 per member and the alembic head.
  - Uses mode 0600, writes atomically (temp + rename), then rotates.
- **Restore:**
  - Refuses when `jobpilot start` is running: a pid file `storage/run/server.pid` written by `start`, plus a port check.
  - Verifies every hash.
  - Rejects members with absolute paths or `..`, which guards against path traversal.
  - Moves the existing `storage/{jobpilot.db,profiles,artifacts}` to `storage/.pre-restore-<ts>/`, extracts, then runs migrations.
- **Tests:** `test_backup_consistent_during_writes` (a thread inserting rows while backing up; the restored DB passes the integrity check), `test_restore_refuses_bad_hash_and_running_server`, `test_restore_rejects_path_traversal`, `test_rotation_keeps_n`, `test_backup_file_modes`.

### Task 3: Export
- **Files:** `ops/export.py`, CLI `jobpilot export app <id> | all`, dashboard "Export bundle" button on the application page (GET `/applications/{id}/export` serves the zip; Content-Disposition attachment).
- **Bundle:** `application.json` uses Pydantic models. Events use `redacted_details` only. Artifacts are included by path under `storage/artifacts/<id>` only. `browser_state` is never included.
- **`export_all`:** JSON per table. It skips raw credentials; there are none, but it asserts no `keyring` data is read.
- **Tests:** `test_export_bundle_contents`, `test_export_excludes_secrets_and_browser_state`, `test_export_all_tables`.

### Task 4: Deletion, retention, maintenance
- **Files:** `ops/purge.py`, `ops/maintenance.py`, CLI `jobpilot delete app <id> --confirm <id>`, `jobpilot delete all --confirm "DELETE ALL" [--include-backups]`, `jobpilot maintenance`. Dashboard delete-application form on the application page with a typed-id field, terminal states only.
- **Status:** new ApplicationStatus `DELETED` (terminal). Edges are from terminal states only.
- **Maintenance:** prunes artifacts of terminal apps older than `retention_days.artifacts`, and contacts older than `retention_days.contacts` (a no-op table guard until Plan 8). It runs the SQLite `PRAGMA optimize` and an integrity check, and records a `maintenance_run` event with counts.
- **Tests:** `test_delete_refuses_in_flight`, `test_delete_app_removes_files_keeps_events`, `test_delete_all_requires_confirm`, `test_retention_only_terminal`.

### Task 5: Settings page + README
- **Files:** `/ui/settings` (read-only):
  - dry_run/live portals with a "change in config/app.yaml" note
  - schedule status (via `schedule.status` with the real runner, guarded so it is skipped on non-macOS)
  - last backup (newest archive name and size)
  - retention values
  - model routes
  - links to export all (CLI hint)
  - Nav link "Settings".
- **README:** "Running daily", "Backups & restore" and "Your data" sections.
- **Tests:** `test_settings_page_renders`, `test_settings_page_no_mutating_controls_for_live_mode`.
