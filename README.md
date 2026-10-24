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

## Where data lives

All state lives in `storage/` (SQLite database `storage/jobpilot.db`, generated artifacts).
It is git-ignored and never leaves your machine. See [docs/](docs/) for the design.
