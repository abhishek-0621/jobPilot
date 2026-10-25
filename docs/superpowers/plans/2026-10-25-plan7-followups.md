# Plan 7 follow-ups

## Rulings
- Task 1: `discover --run-now` and the server can share one DB safely (CAS lease); plist allowlist matches exact command shape.
- Task 2/3: `RESTORE_IN_PROGRESS` marker detected by doctor/start; `storage/run/ops.lock` honoured by restore, discover --run-now, maintenance and backup; malformed manifest → RestoreError; same-second backup suffix.
- Task 3: export routes under `/ui/`; dashboard export streamed from a temp file then deleted; maintenance prunes `storage/exports` older than `retention_days.exports`.
- Task 4: SUBMISSION_FAILED stays prunable; pruning clears artifact refs and prepare-again regenerates missing documents; `data_deleted` event is written atomically with the deletion.
- Final review: daily scheduled backup (`schedule.backup`, default on, errors logged, exit 0); delete-all removes `.pre-restore-*`; restore refuses a cross-volume DB up front; maintenance prunes old `.pre-restore-*` (skipped while a restore marker exists); doctor lists them.

## Deferred minors (Plan 10)
- Task 1: CAS loop gives up after 5 losses → early idle.
- Task 2: extraction size cap; one-part TREES file; test patching style; concurrency test overlap Event.
- Task 3: `start` doesn't take ops_lock; `_plain` handles only datetime.
- Task 5: unlogged except in settings status; weak "on" assertion; no live-mode render test; duplicated note.
- Final: redundant `resolve().parent == storage` check in `purge.delete_all` (harmless).
