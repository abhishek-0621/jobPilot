# JobPilot

Local-first job-application assistant for a single user on a Mac. Everything runs on
`127.0.0.1`; models run locally through [Ollama](https://ollama.com/download).

## Quickstart

```sh
uv sync
uv run jobpilot setup
uv run jobpilot start
```

Then open http://127.0.0.1:8765.

`setup` installs Playwright's Chromium, checks Ollama is installed and running, pulls the
models listed in `config/model_routes.yaml`, creates `storage/` and migrates the database.
It is safe to re-run. `uv run jobpilot doctor` re-checks everything and exits non-zero if
something is wrong.

## What it does today (dry run)

`dry_run: true` is the default in `config/app.yaml`. In dry run:

- The durable orchestrator walks jobs through discovery → match → documents → form →
  validation → **your review**.
- Nothing is submitted until you approve a review batch in the dashboard; the approval is
  bound to the exact content hash you saw, so anything that changes afterwards must be
  re-reviewed.
- Approved applications go to a **simulated** portal only. No real applications or outreach
  messages are ever sent.
- The dashboard shows status counts, pending approvals, the job feed (with links to the
  original listings) and the full activity log.

To see the dashboard with sample data: `uv run python scripts/seed_dry_run.py`, then
`uv run jobpilot start`.

## Profile & discovery

Put your own files in `storage/profiles/` (git-ignored; never commit them):

- `profile.yaml` — your candidate profile (see `tests/fixtures/profile_example.yaml` for the
  layout; that file is a fictional example).
- `search_preferences.yaml` — target titles, locations, work modes, `min_match_score`
  (defaults come from `config/search_preferences.yaml`).
- `companies.yaml` — the boards to watch (see *Job sources*; layout as in
  `config/companies.example.yaml`).

```sh
uv run jobpilot profile import   # stores a new profile version; prints counts only
uv run jobpilot sources check    # checks each board in companies.yaml is reachable
uv run jobpilot start
```

Then, in the dashboard:

1. Open **Profile** (`/ui/profile`) and verify each fact. Matching uses verified facts only;
   until at least one is verified a banner is shown and jobs are not scored.
2. On **Jobs** (`/ui/jobs`), click **Discover**. Jobs are fetched from the public boards,
   de-duplicated, extracted and scored by the local model; those below `min_match_score`
   or failing a hard filter (location, seniority, excluded keyword, experience gap) are rejected with a reason.

## Job sources

Automated discovery uses public, login-free job-board APIs only:

| Source | Add it with |
| --- | --- |
| Greenhouse | token from `boards.greenhouse.io/<token>` |
| Lever | token from `jobs.lever.co/<token>` |
| Ashby | token from `jobs.ashbyhq.com/<token>` |
| SmartRecruiters | company id from `jobs.smartrecruiters.com/<id>` |
| Workday | tenant, `wd_host` and `site`, added by hand (below) |

**Add companies by name.** Let JobPilot find which boards a company has:

```sh
uv run jobpilot boards find "Company A" "Company B"            # just print what it finds
uv run jobpilot boards find "Company A" --write                # also append to companies.yaml
```

Adding companies via the finder rewrites `companies.yaml` and drops any comments you added by hand.

Or open **Companies** (`/ui/companies`) in the dashboard, type a name and press *Add company*.
Both probe each source's public endpoint (at most one request per second per host) and keep
only boards with at least one live posting. New boards are appended to
`storage/profiles/companies.yaml` without duplicates; existing entries are kept.
`uv run jobpilot sources check` then confirms each board is reachable.

**Workday** tenants cannot be guessed, so add them to `storage/profiles/companies.yaml` yourself
(the `wd_host` and `site` come from the careers-page URL
`https://acme.wd5.myworkdayjobs.com/Careers`):

```yaml
companies:
  - name: Acme Corp
    source: workday
    token: acme
    extra: {wd_host: wd5, site: Careers}
```

**LinkedIn, Naukri and other login sites** are never automated. Found a job there? Use
**Add a job** (`/ui/jobs/new`) and paste its URL, title, company and description. JobPilot
prepares your resume, cover letter and drafted answers, then you apply yourself on the
site and press *Mark as applied*.

Why they are never automated: those sites require your login, forbid automated access in
their terms, and a bot on your account risks getting it restricted. JobPilot never opens
them, never stores their credentials and never scrapes them; the same holds for any
application page that sits behind a login.

## Documents

Selecting a MATCHED job (Jobs page → **Select**) creates an application and queues
tailoring. The resume agent only picks, orders and rewords your *verified* facts;
every rewrite is checked deterministically (no new numbers, tech terms or proper
nouns) and falls back to the original fact if rejected. Employer names, titles and
dates are copied from the profile, never from the model. The resume is rendered to a
one-page PDF plus DOCX, then a cover letter (≤ 250 words, 2–3 cited facts) is written
and the application moves to `FORM_IN_PROGRESS`.

- Files live under `storage/artifacts/<application_id>/` (git-ignored), versioned
  `resume_vN.pdf`, `resume_vN.docx`, `resume_vN_diff.json`, `cover_letter_vN.pdf/.txt`.
- Review them at `/ui/applications/<id>`: PDF preview, DOCX download, a diff of original
  vs tailored bullets with their source facts, and the cover letter with its evidence.
- **Regenerate documents** on that page moves the application back to
  `DOCUMENTS_PENDING` (revoking any approval) and re-runs tailoring.
- Re-importing your profile marks documents citing a changed or removed fact as stale
  and regenerates them the same way.

## Applying

The prepare pipeline, end to end (Greenhouse and Lever forms):

1. **Discover** — Jobs → *Discover* fetches, extracts and scores postings.
2. **Verify your profile** — only verified facts are ever used (Profile page).
3. **Select** a MATCHED job. Resume and cover letter are generated (see Documents).
4. **Form preparation** — the application form is opened in an isolated browser profile
   (`storage/browser_state/`, headful by default) and read, never submitted. Each field is
   filled from your verified profile, your answer library or the generated documents,
   or flagged for you.
5. **Answer questions** — open `/ui/applications/<id>/answers`. Required fields with no
   value wait in `NEEDS_USER_INPUT` until you answer them. Review anything marked
   FILL_AND_REVIEW (library answers, model-suggested mappings).
6. **Validate** — the form is filled again, read back, and a full-page screenshot is
   stored; the application becomes `READY_FOR_REVIEW`.
7. **Review batch** — *Batches* → build a batch, inspect answers, documents and screenshots.
8. **Approve** — approval is bound to the content hash you saw; edits afterwards require a
   rebuild and re-approval. Approving never submits anything.
9. **Submit** — on the batch page, *Submit approved applications* is a separate, explicit
   step. Each row says whether it "will submit LIVE to <portal>" or is a "dry run
   (simulated)".

Never automated:

- Sensitive questions (demographics, work authorization, sponsorship, date of birth,
  terms and declarations) are never filled for you; you answer them yourself.
- CAPTCHA, login and MFA walls are detected and handed back to you (`manual_handoff`);
  nothing is bypassed.
- In dry run (the default) submission is simulated and the real form's submit button is
  never clicked. See *Going live* below.

Screenshots and documents live under `storage/artifacts/<application_id>/`
(`form_vN.png`, `resume_vN.pdf`, ...), are served only through `/artifacts`, and stay local.

## Going live

Real submission needs **two independent switches**, both edited by hand in
`config/app.yaml` (there is no dashboard toggle):

1. `dry_run: false`
2. the portal named in `live_portals`, e.g. `live_portals: [greenhouse]`

Either one alone keeps every submission simulated. Restart JobPilot after editing; a red
bar at the top of every page lists the portals that submit for real.

Switching live mode on (or off) does not affect submissions already requested. A request
made while a portal was simulated never runs live, and one confirmed with `SUBMIT` never
silently runs simulated: such a queued submit stops (`live_intent_missing` /
`live_mode_off`), the application stays `APPROVED`, and you press *Submit approved
applications* again (typing `SUBMIT` for a live portal).

What happens when you press *Submit approved applications* for a live portal:

- You must type `SUBMIT` to confirm; the server rejects the request otherwise.
- The form is opened again and re-verified against what you approved (layout, every
  value, documents, approval hash). Any difference, or a CAPTCHA/login/MFA wall, stops
  before the click and sends the application back to you; nothing is retried.
- The single submit control is clicked exactly once (never Enter, never a script submit).
- The page is checked for confirmation evidence. With evidence the application becomes
  `CONFIRMED`, with the confirmation reference (if the page shows one) and a screenshot
  on the application page. Without clear evidence it becomes `SUBMISSION_UNCERTAIN`.

Resolving uncertain submissions: an *Uncertain (n)* link appears in the navigation. On
`/ui/uncertain` check the portal or your email, then choose *It was submitted* (marks it
`CONFIRMED`) or *It was not submitted* (marks it `SUBMISSION_FAILED`). JobPilot never
clicks a second time on its own: a failed application offers *Prepare again*, which
revokes the approval and re-reads the form, so you review, approve and submit it again.

## Where data lives

All state lives in `storage/` (SQLite database `storage/jobpilot.db`, generated artifacts).
It is git-ignored and never leaves your machine. See [docs/](docs/) for the design.
