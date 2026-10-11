# applicationapp

MCF Auto-Apply finds, ranks and applies to jobs on [MyCareersFuture](https://www.mycareersfuture.gov.sg).
You approve each application or set rules for automatic approval.
See [DESIGN.md](DESIGN.md) for the architecture and build plan.

**Status: phase 1 (discover, filter, rule-based ranking).** AI scoring, cover letters and applying come in later phases.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp config.example.yml config.yml     # then edit searches and filters
```

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

## Tests

```bash
.venv/bin/pytest
```
