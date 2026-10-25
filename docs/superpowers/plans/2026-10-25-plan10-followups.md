# Plan 10 follow-ups

## Rulings
- `start` holds ops_lock exclusively only until the pid file is written (a lifetime shared lock would block run-now and backups); restore re-checks the server under the lock.
- Migration 0011 keeps one active DISCOVER (oldest RUNNING, else oldest QUEUED) and cancels the rest; a partial unique index enforces it.
- Careers fetches use a pinned httpcore backend (connect to the checked, globally routable IP; TLS keeps the hostname). It sets httpx's private transport pool, so httpx is pinned `<0.29`.
- Restore cap: uncompressed total ≤ max(20× archive size, 1 GB) and ≤ 20 GB; ≤ 200 000 members.
- Non-finite match scores are rejected before any write.

## Skipped (with reason)
- Masking partial role mentions in cover letters: unsafe (lets numbers in an untrusted title pass the anti-fabrication check).
- Finer-grained in-flight document invalidation: needs per-fact versioning; current behaviour is conservative.
- `purge.delete_all` parent check kept as a defensive guard.

## Remaining known limits
- Outreach follow-up messages and JSON API endpoints for outreach are not built.
