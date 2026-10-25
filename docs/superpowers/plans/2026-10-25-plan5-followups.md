# Plan 5 follow-ups

## Before first real use
- Do a supervised first live run on one real Greenhouse/Lever posting with headful browser; check ASSET_HOSTS (CDN) and confirmation detection on the real page.
- Orphan sweep re-queues mismatched-intent requests each restart (gate_denied, no click) — add skip later.

## Deferred minors
- Task 3: minor (deferred): pre-click wait_for timeout → UNCERTAIN (conservative); test monkeypatch style; timing-sensitive (30,8) case
- Task 5: minor (deferred): repositories list_events/recent_failures still timestamp-ordered (display only); gateway __class__ swap in e2e

## Rulings
- Ruling: separate approve and submit (user asked for control; spec batch-approve then submit) — cost: existing tests updated |
- Ruling: carry into Task 2 — portal None narrowing in _adapter_for; live_portals None→[] and dedupe validator; Task 2 wires real submitter_factory — cost: none
- Ruling: confirmation needs pre-click baselines (URL changed + path-segment match; new text + form gone/URL changed; new errors for FAILED); layout/value mismatch → NEEDS_USER_INPUT; hash/stale → READY_FOR_REVIEW — prevents false CONFIRMED on real portals — cost: more UNCERTAIN outcomes the user resolves
- Ruling: SPA pushState to /thank-you counts as site-claimed success (accepted, not a defect)
- Ruling: detect_confirmation never auto-returns SUBMISSION_FAILED (errors → UNCERTAIN with evidence; user resolves); form-gone needs 3 s continuous absence; moved+text needs phrase in heading/alert/status/confirm element — FAILED invites resubmits → duplicate risk — cost: user resolves more UNCERTAIN items
- Ruling: carry into Task 3 — engine guard: after a recorded click, any adapter SUBMISSION_FAILED is coerced to UNCERTAIN; fix stale comment — cost: none
- Ruling: carry into Task 4 — click check also keyed by application_id: any prior submit_clicked for the app blocks a click unless the user later recorded "not submitted" (user_resolved_not_submitted event after that click) — Task 4's "Prepare again" creates a new hash/key, so key-only check would miss earlier clicks — cost: none
- Ruling: idempotency action "submit#N" after each user not-submitted resolution; inline typed confirm instead of modal (server check is the gate) — cost: none
- Ruling: carry into Task 5 — rowid ordering everywhere; "It was not submitted" offered on FAILED apps with unresolved click; IllegalTransition → 409 in resolve/prepare-again — cost: none
- Ruling: live submission needs recorded live intent (submit_live_intent == portal set only by typed SUBMIT while live); config flip after request → gate_denied (never silently live, never silently simulated) — cost: user re-requests after switching modes
