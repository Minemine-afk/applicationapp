# applicationapp

MCF Auto-Apply finds, ranks and applies to jobs on [MyCareersFuture](https://www.mycareersfuture.gov.sg),
and optionally [JobStreet](https://sg.jobstreet.com). You approve each application or set rules
for automatic approval.
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
mcf-apply login                 # Singpass QR on screen and in Telegram; approve in the Singpass app
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

Once set up, you can run everything from Telegram: logging in, choosing the mode, picking jobs,
answering questions and checking results.

**Setup:** run `mcf-apply setup`. It asks for a bot token (create a bot by messaging **@BotFather**
with `/newbot`), saves it in your user environment (never in `config.yml`), turns Telegram on,
installs the browser, and links your chat when you press **Start** on the bot. On Windows it
also makes mcf-apply start when you log in. Then keep `mcf-apply serve` running (see Home server).

Commands come first: `/status`, `/queue` and the rest are answered at once, and `/login`,
`/run`, `/apply`, `/update` and `/restart` stop a search or application round in progress
between jobs (never mid-form) and take over; the interrupted round continues afterwards. Only
a login in progress is waited out, since it's waiting on you.

| Command | What it does |
|---|---|
| `/login` | Singpass QR with an **Open in Singpass app** button; fresh QRs every minute until approved |
| `/mode` | 🧪 dry run / ✋ manual / 🤖 auto (stored in the database; overrides `config.yml`) |
| `/jobs [n]` | Best jobs not applied to yet, with **Apply / Skip** |
| `/questions` | Unanswered screening questions: **Yes / No** or one button per option; reply to the card for free-text ones. Answers are saved to `answers.yml` |
| `/run` | Search and apply now |
| `/run [mcf\|jobstreet]` | Search and apply now, on every site or just one |
| `/apply [mcf\|jobstreet]` | Apply to the queue now, without searching first |
| `/limits [h d]` | Show or set the per-hour and per-day caps (max 20/150); `/limits jobstreet h d` for JobStreet's own caps (max 5/20) |
| `/login jobstreet` | Opens JobStreet's sign-in in a window on the PC for you to log in |
| `/jobstreet on\|off` | Turn the JobStreet source on or off (off by default; see below) |
| `/jobstreet check` | What's stopping JobStreet applications: switch, login, search block, queue, auto-mode permission, caps, recent outcomes |
| `/jobstreet auto on\|off` | Let 🤖 Auto mode queue JobStreet jobs by itself (off by default) |
| `/config section.key value` | Edit a setting in config.yml from your phone (backup kept as config.yml.bak); `/restart` to load it |
| `/update` | Pull the latest version and restart (the Windows start script reinstalls and restarts it) |
| `/queue` | What's still to be submitted, and what was applied to today |
| `/restart` | Restart mcf-apply |
| `/status` | Mode, today's count, pause state |
| `/pause`, `/resume` | Stop or restart submissions |

Sent without asking: job cards after each search, new screening questions, run summaries
(with screenshots of dry-run review pages and failures), and alerts when the login expires or
submissions pause. Only the linked chat is obeyed. `/login` and `/run` need `mcf-apply serve`.

### Safety rules

- **Login:** the tool never types your Singpass details. Your MCF login cookies are saved in
  `data/browser-profile/session.json` (git ignores `data/`) so the login survives the browser
  being reopened for each run. When MCF ends the session, the run stops and asks for `/login`.
- **Answers:** screening answers only come from `answers.yml`. A job with an unanswered question
  waits in `needs_input` and is never guessed.
- **Pacing:** submissions happen only within `active_hours`, at most `hourly_cap` per hour, with
  a random 45–100 s gap. The daily cap starts at 50 and rises weekly to 100 (`ramp`). Jobs the
  hourly cap holds back are applied to at the top of the next hour, not at the next scheduled run.
- **Automatic pause:** a CAPTCHA or repeated failures pause all submissions until you run
  `mcf-apply unpause`. The daily cap then restarts at the bottom of the ramp.
- **No double applying:** before each applying run, your **Applied Jobs** list is read from MCF
  (including applications you made by hand). Those jobs, and reposts of them (same employer,
  same title), are never queued, shown as cards or auto-approved; anything already queued for
  one is marked `already_applied`. The form's "Applied" badge is checked too, as a last resort.
- **Stuck forms:** if MCF asks for something the tool can't fill (for example missing profile
  details), that job is marked `failed` with a screenshot and the page HTML, and nothing is
  submitted.

MCF's own guidance is to "avoid mass applications and focus on roles where you can genuinely
add value", so keep the threshold meaningful.

## JobStreet (optional)

JobStreet can be added as a second source. **Read this first:** JobStreet's robots.txt disallows
the search endpoint this uses and its terms forbid automated applications, so an account doing
this can be suspended. It is off by default; turn it on only if you accept that. (Indeed is not
supported: it blocks automated requests outright.)

To turn it on, send **/jobstreet on** to the bot (or set `jobstreet.enabled: true` in
`config.yml`). Searches then run on both sites unless a search names its own `sources`. Then:

- `/login jobstreet` opens JobStreet's sign-in in a browser window on the PC. Sign in there
  (email + code, or Google); nothing is typed for you. The session is saved like MCF's. The
  sign-in page runs reCAPTCHA, which flags automated browsers: the app therefore uses the PC's
  real Google Chrome when it's installed (`apply.browser_channel`) and hides the usual
  automation markers. If it still says "Verification failed", try **Continue with Google**;
  if that's blocked too, JobStreet can't be logged in from the app.
- JobStreet jobs appear in `/jobs` cards with a **JobStreet** badge. Only **Quick apply** jobs
  are applied to; ones that send you to the employer's own site are marked `external`.
- Employer questions are only visible inside the form. A job whose questions aren't all in
  `answers.yml` is parked as `needs_input` and the questions arrive in Telegram with option
  buttons (or a reply prompt for free-text ones). Answer, and it's queued again.
- JobStreet runs independently of MCF: its own schedule (`jobstreet.interval_minutes`, offset
  from MCF's), its own caps that neither count against nor wait on MCF's, and its own round, so
  a block or logout on one site never costs the other its hour.
- JobStreet's caps are 2/hour and 8/day by default
  (`/limits jobstreet h d`, max 5/20). JobStreet jobs are never auto-approved unless
  `jobstreet.allow_auto: true` (or `/jobstreet auto on`); then Auto mode uses the lower
  `jobstreet.auto_min_score` bar for them, since JobStreet listings carry no skills field and
  score lower. Each round attempts JobStreet's few allowed jobs first so MCF jobs can't crowd
  them out of the hour's budget.
- Runs that include JobStreet jobs open a visible browser window (its bot check passes far more
  often that way). Set `jobstreet.headless: true` to hide it.
- Your JobStreet applied-jobs page is read before each run, so anything you applied to by hand
  there, and reposts of it on either site, are skipped.
- If JobStreet starts blocking searches (403), they pause for an hour and you get one alert.

The apply-form hooks (`src/mcf_autoapply/jobstreet/selectors.py`) come from SEEK's standard
flow, which JobStreet runs on, not from inspecting JobStreet logged in. Expect the first dry run
to need adjustments: send the screenshots and the `.html` saved next to them.

## Home server

Run it on a machine at home that stays on. `mcf-apply serve` does everything in one process:
a full run every hour (`serve.interval_minutes`), plus Telegram taps and commands as they
arrive. It replaces cron and `mcf-apply bot`.

Logging in is the one manual step. When MCF's login expires you get a Telegram alert. Send
**/login** and the Singpass QR arrives in Telegram with an **Open in Singpass app** button.
Tap it on your phone and approve. QRs expire after about a minute, and fresh ones are sent
automatically.

### Option A: a computer with a screen (old laptop or desktop)

**On Windows, follow [docs/WINDOWS.md](docs/WINDOWS.md)**, a step-by-step guide.

Install as in Setup, then leave this running:

```bash
export TELEGRAM_BOT_TOKEN=...      # Windows PowerShell: $env:TELEGRAM_BOT_TOKEN="..."
.venv/bin/mcf-apply serve
```

Turn off sleep in the power settings. To start it automatically, use Task Scheduler "At log
on" on Windows, a Login Item on Mac, or a systemd user service on Linux.

### Option B: no screen (Raspberry Pi 4/5, mini PC) with Docker

The Docker image includes a virtual screen that you view in a web browser, for the QR code.

1. Install Docker (Raspberry Pi OS: `curl -fsSL https://get.docker.com | sh`).
2. Get the code and create your files next to `docker-compose.yml`:
   ```bash
   git clone https://github.com/Minemine-afk/applicationapp && cd applicationapp
   cp config.example.yml config.yml
   cp answers.example.yml answers.yml
   cp .env.example .env               # set TELEGRAM_BOT_TOKEN and VNC_PASSWORD
   ```
3. In `config.yml`, set `serve.login_view_url: http://<machine-ip>:6080/vnc.html`
   (find the IP with `hostname -I`).
4. Start it: `docker compose up -d --build`. It restarts on reboot.
5. Link Telegram: send **/start** to your bot. The first chat to do that is linked.
6. Send **/login** to your bot and tap **Open in Singpass app** on the QR it sends. The browser
   view on port 6080 is a fallback, for watching the browser or scanning the QR there.

Other commands run the same way, for example `docker compose exec mcf-apply mcf-apply queue`.
Logs: `docker compose logs -f`. Data (job database, browser login, screenshots) is in `./data`.

**Keep port 6080 inside your home network.** Don't port-forward it on your router: anyone who
reaches it and guesses the password sees your logged-in browser. To log in while you're away,
install [Tailscale](https://tailscale.com) on the machine and your laptop, and use the Tailscale
IP in `login_view_url`.

## Tests

```bash
.venv/bin/pytest
```
