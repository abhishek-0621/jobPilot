# JobPilot Plan 11: Remote Job Boards & India-or-Remote Filtering

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Discover jobs from public remote-job boards, and keep only jobs located in India or remote-and-open-to-India. LinkedIn, Naukri, Indeed and foundit stay out of automated discovery (their terms forbid it); the user adds those jobs by hand with "Add a job".

**Sources (public feeds/APIs only, no login, same User-Agent, ≥1 s per host, 20 s timeout, `max_jobs_per_run` cap):**
- **Himalayas** — `GET https://himalayas.app/jobs/api?limit=20&offset=N` (JSON; `jobs[]`: `guid`, `title`, `companyName`, `locationRestrictions[]`, `description`, `applicationLink`, `pubDate`). At most 5 pages.
- **Remotive** — `GET https://remotive.com/api/remote-jobs?category=software-dev` (JSON `jobs[]`: `id`, `url`, `title`, `company_name`, `candidate_required_location`, `description`, `publication_date`). Their terms ask for at most a few calls a day and a link back: one call per run, and the job detail page shows "via Remotive" with the source link.
- **We Work Remotely** — RSS `https://weworkremotely.com/categories/remote-programming-jobs.rss` and `.../remote-devops-sysadmin-jobs.rss` (items: `title` "Company: Role", `region`, `link`, `description`, `pubDate`, `guid`).
- **RemoteOK** — `GET https://remoteok.com/api` (JSON array; first element is a legal notice and is skipped; `id`, `position`, `company`, `location`, `description`, `url`, `apply_url`, `date`, `tags`). Their terms require a link back to RemoteOK: show "via RemoteOK" with the source link.
- **Jobicy** — `GET https://jobicy.com/api/v2/remote-jobs?count=50&tag=python` and `&tag=ai` (JSON `jobs[]`: `id`, `url`, `jobTitle`, `companyName`, `jobGeo`, `jobDescription`, `pubDate`).
- **Working Nomads** — `GET https://www.workingnomads.com/api/exposed_jobs/` (JSON array: `url`, `title`, `company_name`, `location`, `description`, `pub_date`, `tags`).

**Architecture:**
- A new board kind, not per-company: `search.remote_boards: [himalayas, remotive, weworkremotely, remoteok, jobicy, workingnomads]` in `search_preferences.yaml` (default all), each a `SourceAdapter` in `discovery/sources/<name>.py` with fixtures and `httpx.MockTransport` tests; RSS parsed with stdlib `xml.etree` (defused: reject DTD/entities).
- All these jobs take the **manual apply path** (`apply_mode` = manual; the source isn't greenhouse/lever/ashby). If an aggregator's apply URL points at a Greenhouse/Lever/Ashby posting, it is still manual in this plan (no cross-source re-resolution).
- **Dedupe across sources:** jobs are also deduped by a normalized `(company_key, title_key)` within 30 days, so the same role from WWR and Himalayas appears once (the first source wins; later sources are recorded in an event).
- **Location eligibility** (`discovery/location.py`, used by the prefilter):
  - `India` matches the country name and Indian cities/states (configurable list in `config/india_locations.yaml`: Bengaluru/Bangalore, Pune, Hyderabad, Mumbai, Navi Mumbai, Thane, Delhi, New Delhi, NCR, Gurugram/Gurgaon, Noida, Chennai, Kolkata, Ahmedabad, Kochi, Coimbatore, Jaipur, Indore, Chandigarh, Trivandrum, Mysuru, Karnataka, Maharashtra, Telangana, Tamil Nadu, …).
  - `Remote` matches only when the job is remote AND open to India: restriction text empty / "Worldwide" / "Anywhere" / "Global" / contains India / "APAC" / "Asia" / "Asia-Pacific" / "IST" / "UTC+5:30". Remote restricted to other regions only (e.g. "USA only", "US", "EU", "UK", "Europe", "Americas", "LATAM", "Canada") is rejected with reason `remote_region_mismatch`.
  - Unknown/empty location on a remote board counts as remote-worldwide; on a company board it stays the existing behaviour.
- Dashboard: jobs list gains a source filter; job detail shows the source name and "via <board>" link.

## Global Constraints
- No LinkedIn/Naukri/Indeed/foundit/Glassdoor automation of any kind (unchanged deny-list).
- Feeds are untrusted: `html_to_text`, 12 000-char cap, untrusted block; URLs http(s) + `safe_href`.
- Tests never hit the network. No personal data in tracked files.
- No change to approval/submission safety.

### Task 1: Location eligibility + cross-source dedupe
- `discovery/location.py`, `config/india_locations.yaml`, prefilter wiring, dedupe key + event. Tests: Indian cities match "India"; "Remote - USA only" rejected; "Remote (Worldwide)", "Remote, APAC", "Anywhere" accepted; dedupe across two sources.

### Task 2: JSON board adapters
- Himalayas, Remotive, RemoteOK, Jobicy, Working Nomads with fixtures; pagination/caps; error codes as existing adapters; registration in runtime and `remote_boards` preference.

### Task 3: We Work Remotely RSS + safe XML
- RSS adapter, defused parsing (DTD/entity rejected), "Company: Role" title split.

### Task 4: Dashboard + README
- Source filter on jobs list, "via <board>" attribution on job detail, settings page lists enabled remote boards, README "Job sources" updated (remote boards; why LinkedIn/Naukri/Indeed/foundit are manual-only).
