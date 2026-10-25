# Plan 6 follow-ups

## Deferred minors (Plan 10)
- Task 2: minor (deferred): duck-typed supports_max_postings; externalUrl host not pinned; dead Optional in _parse
- Task 5: minor (deferred): validation before status check; double select in overview; rollback assertion in 409 test
- Task 6: minor (deferred → final wave): atomic companies.yaml write + corrupt-file form error + README note on comments; ineffective sleep patch; obscure assertions; no PRG
- Final: minor (deferred): GET /ui/companies should show corrupt-file error instead of empty list

## Rulings
- Ruling: T4 drafts only the fixed common list now; story-bank hook added in Plan 9 — cost: none |
- Ruling: carry into Task 2 — finder passes max_postings=prefs.max_jobs_per_run to adapters that accept it; cli.py sources dict uses runtime registry; SR id validated/quoted; section title escaped; break on empty page; http(s) enforcement for posting URLs in a shared helper — cost: none
- Ruling: carry into Task 3 — Workday urls through normalize_url; require_http returns stripped; reject %2e/%2f in externalPath — cost: none
- Ruling: no extra phone/linkedin aliases (ordinary labels already match) — cost: none
- Ruling: carry into Task 5 — apply_mode check inside registry.form_url (covers LiveSubmitter); first_paragraph returns parts[1] when ≥2; manual add matching existing job records note in event — cost: none
- Ruling: carry into Task 6 — local date (date.today()) for applied_on validation; copy button feedback + fallback; vacuous script assertion fix — user is in IST — cost: none
