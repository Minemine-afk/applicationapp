# applicationapp

MCF Auto-Apply finds, ranks and applies to jobs on [MyCareersFuture](https://www.mycareersfuture.gov.sg).
You approve each application or set rules for automatic approval.
See [DESIGN.md](DESIGN.md) for the architecture and build plan.

**Status:** discovery, filtering, ranking and applying (with a dry-run mode) are built. AI scoring
and the review dashboard come next.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium   # browser used for applying
cp config.example.yml config.yml     # then edit searches and filters
cp answers.example.yml answers.yml   # your screening-question answers
```

Run it on your own computer: logging in needs Singpass on your phone and a visible browser
window.

The example config searches for software, AI, DevOps/cloud and data roles (including related
titles and fresh-graduate searches), for someone with 0–2 years of experience.

Put your skills under `profile.skills`. Keep your resume itself in `me/`, which git ignores.

## Usage

```bash
mcf-apply discover            # fetch new jobs for every saved search and filter them
mcf-apply list                # jobs that passed your filters, best match first
mcf-apply list --fresh        # only fresh-grad / entry-level jobs
mcf-apply list --sort date    # newest first instead
mcf-apply list -s python      # ... matching a title or company
mcf-apply list --status filtered_out   # what was dropped, and why
mcf-apply show 8ecba28a       # one job in full (first few characters of the ID)
mcf-apply stats               # counts, plus the most common reasons jobs were dropped
mcf-apply refilter            # re-apply config.yml filters to stored jobs, no download
```

Run `.venv/bin/mcf-apply` or activate the venv first.

### How jobs are ranked

Each job that passes the filters gets a score from 0 to 100:

- **Entry-level fit (60%)**: the title has a word like *graduate*, *junior* or *associate*,
  MCF lists it as "Fresh/entry level", or it asks for 0–1 years. Jobs with any of these strong
  signals are tagged **(fresh grad)**.
- **Skills (40%)**: how many of your `profile.skills` appear in the job's title, skills list
  or description.

`mcf-apply show <ID>` explains a job's score. Change the weights under `ranking` in `config.yml`.

### Tuning the filters

1. Run `mcf-apply stats` to see why jobs are being dropped.
2. Check `mcf-apply list --status filtered_out` for jobs you'd have wanted.
3. Edit `filters` in `config.yml` and run `mcf-apply refilter`. This re-checks every stored job
   in a second.

Jobs are stored in `data/jobs.db` (SQLite) with the full API response, so later phases can
score them without downloading them again.

### Running it on a schedule

Discovery only fetches until it reaches jobs it already has, so a repeat run takes about
15 seconds. To run it hourly from 8am to 9pm:

```cron
5 8-21 * * * cd /path/to/applicationapp && .venv/bin/mcf-apply discover >> data/discover.log 2>&1
```

## Applying

MCF applications have no cover letter. You pick a resume from your MCF profile and answer
the employer's yes/no screening questions, if there are any. The tool does exactly that in a
real browser, logged in as you.

```bash
mcf-apply login                 # opens a browser; log in with Singpass yourself (once per session)
mcf-apply approve 8c7d83be 9dfbf2a6   # queue jobs (IDs from `mcf-apply list`)
mcf-apply questions             # screening questions that need your answer
mcf-apply answer 1 yes          # saved to answers.yml and reused for every job that asks it
mcf-apply apply --dry-run       # fill each form up to the review page, screenshot, don't submit
mcf-apply queue                 # what's queued, applied, failed and why
```

Check the screenshots in `data/screenshots/`. When they look right, set `apply.mode` in
`config.yml`:

- `manual`: `mcf-apply apply` submits only the jobs you approved.
- `auto`: jobs scoring `auto_apply_min_score` or more are approved by themselves as well.

`mcf-apply run` does everything in one go (discover, auto-approve, apply), headless, for cron:

```cron
5 8-21 * * * cd /path/to/applicationapp && .venv/bin/mcf-apply run >> data/run.log 2>&1
```

### Telegram

Get job cards, screening questions and alerts on your phone, and answer them with one tap.

1. In Telegram, message **@BotFather**, send `/newbot` and follow the steps. It gives you a
   bot token.
2. Put the token in your environment, not in `config.yml`:
   `export TELEGRAM_BOT_TOKEN=123456:ABC...`. For cron, add it to the crontab line.
3. Send your new bot any message, then run `mcf-apply telegram-setup`. This links your chat,
   and only that chat's buttons are accepted.
4. Set `telegram.enabled: true` in `config.yml`.

What you get:

- **Job cards** with **Apply / Skip** for borderline jobs: scores from `quick_review_min_score`
  up to `auto_apply_min_score` in auto mode, or every job above the floor in manual mode.
- **Screening questions** with **Yes / No**. Your answer is saved to `answers.yml` and every job
  waiting on that question is queued.
- **Run summaries:** what was applied, what failed and why, and today's count. Runs where
  nothing happened send nothing.
- **Alerts** when your MCF login expires or submissions pause, at most every 6 hours.
- **`/status`**: send it to the bot at any time.

`mcf-apply run` picks up your taps at the start of each run. For instant handling, keep
`mcf-apply bot` running. `mcf-apply notify` sends any pending cards right away.

### Safety rules

- **Login:** the tool never types your Singpass details. When the session expires, the run
  stops and asks you to run `mcf-apply login` again.
- **Answers:** screening answers only come from `answers.yml`. A job with an unanswered question
  waits in `needs_input` and is never guessed.
- **Pacing:** submissions happen only within `active_hours`, at most `hourly_cap` per hour, with
  a random 45–120 s gap. The daily cap starts at 15 and rises weekly to 40 (`ramp`).
- **Automatic pause:** a CAPTCHA or repeated failures pause all submissions until you run
  `mcf-apply unpause`. The daily cap then restarts at the bottom of the ramp.
- **No double applying:** one application per job, and jobs MCF already shows as "Applied" are
  skipped.
- **Stuck forms:** if MCF asks for something the tool can't fill (for example missing profile
  details), that job is marked `failed` with a screenshot and the page HTML, and nothing is
  submitted.

MCF's own guidance is to "avoid mass applications and focus on roles where you can genuinely
add value", so keep the threshold meaningful.

## Tests

```bash
.venv/bin/pytest
```
