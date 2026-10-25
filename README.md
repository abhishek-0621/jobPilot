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
- Approved applications go to a **simulated** portal only. No real applications are sent,
  and JobPilot never sends outreach messages (it only drafts them for you).
- The dashboard shows status counts, pending approvals, the job feed (with links to the
  original listings) and the full activity log.

To see the dashboard with sample data: `uv run python scripts/seed_dry_run.py`, then
`uv run jobpilot start`.

## First real run

`uv run jobpilot doctor` lists what is still missing, each with the command that fixes it.

1. **Verify your facts.** Put your profile in `storage/profiles/` (see *Profile &
   discovery*), run `uv run jobpilot profile import`, then tick every fact on
   **Profile** (`/ui/profile`). Only verified facts are ever used.
2. **Verify your stories** (optional, needed for essay questions): see *Story bank*, then
   verify each one at `/ui/profile/stories`.
3. **Add companies:** `uv run jobpilot boards find "Company A" "Company B" --write` (or
   **Companies** in the dashboard). Workday tenants are added by hand.
4. **Run discovery:** `uv run jobpilot discover --run-now` or *Discover* on the
   **Jobs** page, then pick jobs there.
5. **Review a batch:** open **Batches**, read every answer and document, approve.
6. **Keep `dry_run: true` for this first supervised pass**: approved applications go to
   the simulated portal only, so you can check the whole flow end to end.
7. **Then go live with one portal:** set `dry_run: false` and a single portal in
   `live_portals` (e.g. `live_portals: [greenhouse]`), restart, and submit one approved
   application while watching it (see *Going live*).

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

## Story bank

Open-ended questions ("What are you most proud of?", "Biggest failure?", "Strengths and
weaknesses / SWOT", "Describe a challenging project", "Tell us about a time you helped a
customer / disagreed / led / learned fast", "Why AI?") are answered from **your own
stories**, never invented ones.

1. Copy `config/stories.example.yaml` (a fictional example) to
   `storage/profiles/stories.yaml` (git-ignored) and write your stories: `story_id`,
   `title`, `themes`, `situation`, `task`, `action`, `result`, optional `tradeoffs`,
   `evidence` (metrics exactly as you would state them) and `missing` (your own "[ADD]"
   notes for details you still need to fill in). Add `standing_rules` (e.g. how to state
   your years of experience) and `canned` answers you wrote yourself (e.g. `weaknesses`).
   Unknown keys or themes are rejected.
2. `uv run jobpilot profile import` imports the story bank along with the profile (counts
   only are printed).
3. Open **Profile → Story bank** (`/ui/profile/stories`) and verify each story. Only
   verified stories are used. Editing a story makes it unverified again, and any drafted
   answer that used it goes back to you for review.

How answers are drafted (no fabrication):

- An essay question (long-text field, matched by the patterns in
  `config/essay_questions.yaml`) is drafted by the local model (route `essay`) from at most
  two verified stories, your verified facts and your standing rules. Sensitive questions
  (compensation, reasons for leaving, work authorisation, demographics, legal) are never
  drafted.
- Every number, technology and name in a draft must appear in the stories or facts used;
  leadership claims need a story that says so; nothing from `missing` notes is ever
  stated; SWOT answers need four labelled parts, with Weaknesses/Threats drawn only from
  your weakness material. A draft that fails is retried once, then left to you.
- Drafts are always **FILL_AND_REVIEW**: shown on the answers page with "Drafted from:
  <story titles>" and approved like any other answer (editing one revokes approval). A
  question with no suitable verified story is **ASK_USER** with a note such as
  `needs_story:customer`. A canned answer is used verbatim.
- After reviewing a draft you can **Save to answer library** for that question type; it is
  then offered (still for review) on later applications.

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

## Recruiter outreach

After you have really applied (you clicked **Mark as applied**, or a live submission was
confirmed), JobPilot drafts one short note to a public recruiting contact at that company
and puts it in the **Outreach** page. It **never sends anything**: no email, no LinkedIn,
no browser. You review the draft, approve it, send it yourself (copy, **Open in your mail
app**, or **Open profile** for LinkedIn) and then click **Mark as sent**.

- Contacts come only from public sources: an email published in the job posting or a
  `careers@`/`jobs@`/`recruiting@`-style inbox on the company's careers page, always on
  the company's own domain. Addresses are never guessed. Add the optional careers page to a
  board in `companies.yaml` as `extra: {careers_url: "https://www.example.com/careers"}`.
- LinkedIn recruiters get in only when you add them yourself on the draft page.
- If nothing public is found the draft says "No suitable public contact found"; adding a
  contact brings it back to review.
- The note uses only your verified facts (no new numbers, tools, names or claims) and is at
  most 120 words. Editing it or switching the contact revokes your approval.
- One draft per company per campaign (`outreach.campaign` in `config/app.yaml`, default
  `default`). Set `outreach.auto_draft: false` to draft only when you click **Draft
  outreach** on an application.
- Dry-run (simulated) submissions never get outreach.

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

## Running daily

macOS only. JobPilot can run discovery for you every day with a LaunchAgent:

```
uv run jobpilot schedule install              # daily at schedule.discover_at (default 08:30)
uv run jobpilot schedule install --at 07:15   # another time
uv run jobpilot schedule install --at 08:00,14:00,20:00   # several times a day
uv run jobpilot schedule install --with-server  # also keep `jobpilot start` running from login
uv run jobpilot schedule status
uv run jobpilot schedule uninstall
```

Installing again is safe (it replaces the previous agent). Logs go to `storage/logs/`.

What runs: `jobpilot discover` (find jobs, extract and score them) followed by a daily
backup (set `schedule.backup: false` in `config/app.yaml` to turn it off; rotation keeps
the newest `backup_keep` archives), then retention maintenance, and, with `--with-server`,
the dashboard. Running `discover --run-now` by hand does not back up; use
`uv run jobpilot backup`. What never runs on a schedule: tailoring, preparing
forms, approval, submission and outreach sending. Those only happen when you press the
button in the dashboard. The *Settings* page shows the schedule, the last backup,
retention and model routes (read-only).

## Backups & restore

```
uv run jobpilot backup                        # storage/backups/jobpilot-<time>.tar.gz
uv run jobpilot backup --keep 30
uv run jobpilot restore <archive> --confirm RESTORE
```

- A backup holds the database, `storage/profiles` and `storage/artifacts`. It does not
  hold browser sessions (`browser_state`), logs, exports or other backups.
- Scheduled daily runs make a backup automatically (disable with `schedule.backup: false`).
  Only the newest `backup_keep` archives are kept (default 14); older ones are rotated out.
- Restore is CLI only and needs the typed word `RESTORE`. The archive is verified first,
  JobPilot must not be running, and your current data is saved aside (`storage/.pre-restore-<time>`) before it is replaced.
- Backups contain personal data. Files are created private (mode `0600`) under the
  git-ignored `storage/`; keep copies somewhere equally private.

## Your data

```
uv run jobpilot export app <application_id>   # audit bundle (zip) in storage/exports
uv run jobpilot export all                    # every table as JSON, plus profiles/artifacts
uv run jobpilot delete app <application_id> --confirm <application_id>
uv run jobpilot delete all --confirm "DELETE ALL" [--include-backups]
```

- Exports contain personal data; they are written to `storage/exports/`. Files there older
  than `retention_days.exports` (7 days) are deleted by maintenance.
- `delete all` also removes the `storage/.pre-restore-*` copies a restore leaves behind;
  maintenance prunes those after `retention_days.artifacts`, and `jobpilot doctor` lists
  any that are present with their age.
- Deleting an application is available in the dashboard (type the application id) and the
  CLI, and only for finished applications. Deleting everything is CLI only.
- Retention (`retention_days` in `config/app.yaml`): finished applications lose their
  documents and screenshots after 365 days, old contact candidates go after 180 days and
  exports after 7 days. Applications still in progress are never pruned.
- The audit history is append-only: deletion adds a `data_deleted` event with ids and
  counts, never personal details, and does not remove earlier events.

## Where data lives

All state lives in `storage/` (SQLite database `storage/jobpilot.db`, generated artifacts).
It is git-ignored and never leaves your machine. See [docs/](docs/) for the design.
