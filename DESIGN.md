# MCF Auto-Apply: Design

A personal tool that finds relevant jobs on [MyCareersFuture](https://www.mycareersfuture.gov.sg) (MCF),
ranks them against your profile, tailors your application, and submits it, with you approving
each application or setting rules for which ones go through without asking.

---

## 1. Goals and non-goals

**Goals**
- Pull new job postings from MCF on a schedule using your saved searches.
- Drop jobs that fail hard rules (salary floor, employment type, seniority, blocked companies).
- Score each remaining job against your resume and preferences, and explain the score.
- Generate a tailored cover letter or answers for each job.
- Submit applications through the MCF website with your logged-in session.
- Track every job and application so nothing is applied to twice and you can see outcomes.

**Non-goals**
- **Automating the Singpass login.** Singpass requires 2FA (app or face verification), and its
  terms forbid handing your credentials to third-party software. You log in yourself in a
  browser window the tool opens. The tool then reuses that session until it expires.
- Mass-applying to everything. Each application is checked against a quality threshold first.
  Spray-and-pray applications get filtered out by recruiters and can get your account flagged.

---

## 2. How MCF works (verified Oct 2026)

| Area | Finding | Impact on the design |
|---|---|---|
| Job search | Public JSON API: `GET https://api.mycareersfuture.gov.sg/v2/jobs?search=<q>&limit=<n>&page=<p>`. No auth needed. | Discovery uses plain HTTP. No browser needed. |
| Job detail | `GET /v2/jobs/{uuid}` returns `title`, `description` (HTML), `skills[]`, `salary{min,max,type}`, `employmentTypes`, `positionLevels`, `minimumYearsExperience`, `postedCompany`, `metadata.jobPostId`, `status`. | Gives the filter and scorer structured fields. |
| Screening questions | Each job links to `_links.screeningQuestions`. | Fetch these before applying. Unanswered questions pause the job for your review. |
| robots.txt | `User-agent: * / Disallow:` (crawling allowed). | Still rate-limit politely (see §7). |
| Applying | Requires Singpass login. The site sends one GraphQL mutation, `createApplication(documentId, jobUuid, jobScreeningQuestionResponses, …)`: a resume from your MCF profile plus yes/no answers. **There is no cover-letter field.** Careers@Gov jobs (`psdUrl` set) apply elsewhere. | Playwright drives the real form in a persistent, logged-in profile. Success is confirmed from the page and from the mutation's response. |
| Screening questions | Public: `GET /v2/jobs/{uuid}/screening-questions` returns `[{question}]`, always yes/no. Only ~1 in 10 jobs has any. | Fetched when a job is approved and answered from `answers.yml`. |
| Application limits | None found in the site's code, but its UI warns: "Avoid mass applications and focus on roles where you can genuinely add value." | Conservative pacing defaults (§3.7). |
| **JobStreet** (optional source) | Search: `sg.jobstreet.com/api/jobsearch/v5/search` returns JSON without login, but robots.txt disallows it and the terms forbid automated applications. Job and apply pages sit behind a Cloudflare challenge; the apply form is SEEK's (resume → employer questions → review). | Off by default. `sources.py` abstracts sites; `jobstreet/` holds its client, applier, login and applied-jobs sync. Own caps (2/h, 8/day), never auto-approved by default, visible browser, 403 → hour-long pause. Indeed is not supported (403 to everything). |

> Re-check MCF's Terms of Use before enabling automatic submission. The discovery, scoring and
> drafting features are useful on their own even if you decide to click "Submit" yourself.

---

## 3. Architecture

```
            ┌──────────────┐   schedule (cron / APScheduler)
            │  Scheduler   │──────────────────────────────┐
            └──────────────┘                              ▼
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────────┐   ┌──────────┐
│ Discover │──▶│  Filter  │──▶│  Score   │──▶│ Review queue │──▶│  Apply   │
│ MCF API  │   │ rules.yml│   │ LLM+rules│   │ dashboard/CLI│   │Playwright│
└──────────┘   └──────────┘   └──────────┘   └──────────────┘   └──────────┘
      │              │              │               │                │
      └──────────────┴──────────────┴───────┬───────┴────────────────┘
                                            ▼
                                   ┌─────────────────┐
                                   │ SQLite: jobs,   │
                                   │ scores, apps,   │
                                   │ answers, events │
                                   └─────────────────┘
```

### 3.1 Discover (`mcf/client.py`)
- For each saved search in `config.yml`, page through `/v2/jobs`, sorted newest first.
- Stop paging when a whole page holds jobs already in the DB, or when the posting date is older
  than `max_age_days`.
- Upsert by `uuid`. Store the raw JSON so later re-scoring doesn't need re-fetching.
- Results come back newest first (`sortBy=new_posting_date`), so paging stops at the first
  page that has no new jobs or reaches postings older than `max_age_days`. A repeat run takes
  about one request per search term.
- **Title synonyms**: each search can list `also:` titles ("BI analyst", "reporting analyst",
  "analytics executive"). Each one is queried, and results are de-duplicated by `uuid`.
- Runs **hourly during active hours** (default 08:00–22:00 SGT), so new postings are scored
  and queued within about an hour of going live. Applying early gets you noticed more than
  applying a day later.

### 3.2 Filter (`pipeline/filter.py`): deterministic, cheap, runs first
Rules from `config.yml`:
- `salary_min` (normalised to monthly), `employment_types`, `position_levels`
- `max_years_experience_required` (skip jobs that are clearly out of reach)
- `company_blocklist` / `company_allowlist` (by UEN or name)
- `title_exclude_keywords` (e.g. "intern", "sales commission only")
- Skip jobs that aren't `Open`, and skip companies already applied to in the last N days.

- `title_include_keywords`: the title must contain one of these as a whole word. Searches return
  loosely related jobs ("cloud engineer" also returns network and data-centre roles), so this keeps
  the feed on target.
- Reposts: the same employer and normalised title as a job already in the feed is marked
  `duplicate`. Agencies often post one opening several times.

Every rejected job is stored with all its reasons, so you can audit and tune the rules.
`mcf-apply refilter` re-applies changed rules to every stored job without downloading again.

### 3.3 Score (`pipeline/score.py`)
Two layers:
1. **Rule-based rank** (built): entry-level fit (title words like *graduate* or *junior*, MCF's
   "Fresh/entry level" level, 0–1 years required), weighted 60%, plus how many of your skills
   appear in the job, weighted 40%. Free and instant, and needs no API key. Titles with seniority
   words (senior, lead, principal…) are filtered out unless they also have an entry-level word.
2. **LLM fit assessment**: send your resume, your preferences and the job description to Claude
   (default `claude-sonnet-5-5`) and ask for structured JSON output:
   ```json
   {"fit_score": 0-100, "must_have_gaps": ["..."], "strengths": ["..."],
    "seniority_match": "under|match|over", "recommendation": "apply|maybe|skip",
    "reasoning": "two sentences"}
   ```
Final score = weighted blend. When you have several resume versions (§3.4), the job is scored
against each one and the best-scoring version is used for the application. Cache by `(job_uuid, resume_hash, prompt_version)` so a job is
only re-scored when your resume or the prompt changes.

### 3.4 Tailor (`pipeline/tailor.py`)
> MCF applications take no cover letter (§2), so for MCF tailoring means picking the right
> resume and answering screening questions. Cover letters only matter for external sites.

- **Resume versions**: `profile.resumes` holds two or three versions, each tagged with the job
  types it targets (e.g. analytics, BI, data engineering). The best-scoring version is uploaded,
  so more jobs clear the threshold without stretching the truth.
- **Cover letter**: generated from a template and your resume, 150–250 words, grounded only in facts
  from your resume. The prompt forbids inventing experience.
- **Screening questions**: answered from an **answer bank** (`answers.yml`) first, matched by
  normalised question text plus embedding similarity. Yes/no and numeric questions (notice period,
  expected salary, work-pass status) must come from the bank and never from the LLM. Any question
  without a confident match sends the job to `needs_input`.
- **Growing the answer bank**: when you answer a `needs_input` question, the answer is saved to
  the bank and every job waiting on that question is released. Unanswered questions are the
  main thing holding jobs back in auto mode, and the backlog shrinks fast after the first week.

### 3.5 Review queue (`web/` or `cli review`)
Every scored job lands in one of three states:

| Mode | Behaviour |
|---|---|
| `manual` (default) | You approve or edit each application in the dashboard. |
| `auto_threshold` | Auto-approve when `score ≥ auto_apply_min_score` AND every screening question is answered from the bank AND the job is on the MCF portal. Jobs in the quick-review band (below) go to your phone. Everything else waits in the dashboard. |
| `dry_run` | Run the whole pipeline but never click submit. Use this first. |

The dashboard shows the job, the score breakdown, gaps, the draft cover letter and the answers,
with **Approve / Edit / Skip** buttons.

**Quick approval on your phone (Telegram)** (built, `notify.py`): jobs scoring in the quick-review band
(`quick_review_min_score` to `auto_apply_min_score`, default 60–75) are sent to a Telegram bot
as a short card: title, company, salary, score, top two gaps. The card has **Apply / Skip**
buttons. Tapping Apply queues the job with its draft cover letter and saved answers, so you can
clear the borderline jobs in seconds wherever you are. `needs_input` questions are also sent as
Telegram messages, and your reply is saved to the answer bank.

### 3.6 Apply (`mcf/applier.py`) (built)
The page hooks come from the MCF web app's own element IDs and `data-cy` attributes, kept in
`mcf/selectors.py`. They are tested against a stand-in page (`tests/fixtures/fake_mcf.html`).
The flow is: job page → Apply → *Add personal details* / *Upload resume* / *Add experience* (shown
only when needed) → *Questions From Employer* → *Review application* → Submit → "Your
application is on its way!". A step that MCF won't let past (a validation message stays) fails
that job for you to finish by hand.

- Playwright with a **persistent browser context** (`~/.mcf-autoapply/browser-profile`), so
  your Singpass session cookies survive between runs.
- `mcf-apply login` opens a visible browser window. You log in with Singpass and the tool
  detects that you're logged in.
- For each approved job: open `https://www.mycareersfuture.gov.sg/job/<slug>-<uuid>`, click
  Apply, pick or upload the resume, fill the cover letter and screening answers, take a
  screenshot, submit, and confirm the success state.
- If the session has expired: stop the run, mark the remaining jobs `pending`, and notify you to
  log in again. Never retry the login automatically.
- If the page structure looks different (a selector is missing): fail that job with a screenshot
  and the HTML dump, and don't guess.
- Keep all selectors in one file (`mcf/selectors.py`). MCF will change its UI, and this keeps the
  fix in one place.

### 3.7 Pacing (`pipeline/pacer.py`)
Submissions are spread across the day instead of sent in one burst:
- A queue of approved jobs drains in **active hours only**, with at most `hourly_cap` per hour
  and `daily_cap` per day, and a random 45–120 s wait between submissions.
- Highest score and newest posting go first, so if the daily cap is hit, the weakest matches wait.
- **Gradual increase**: with `ramp` set, the daily cap goes up step by step over a few weeks
  (e.g. 15 → 25 → 40), and only steps up after a clean week (see §7).

### 3.8 Track
State machine per job:

```
discovered → filtered_out
           → scored → skipped
                    → needs_input → approved
                    → approved → applying → applied → (viewed | interview | rejected | offer)
                                          → failed (retryable / manual)
                    → external (apply on employer site yourself)
```
Later outcome states are updated by hand in the dashboard, or by parsing MCF's
"My Applications" page.

---

## 4. Data model (SQLite)

```text
jobs(uuid PK, job_post_id, title, company_name, company_uen, salary_min, salary_max,
     salary_type, employment_types JSON, position_levels JSON, min_years_exp,
     skills JSON, description_html, url, posted_at, raw JSON, first_seen_at, status)

evaluations(id PK, job_uuid FK, resume_hash, prompt_version, rule_score, llm_score,
            final_score, recommendation, gaps JSON, strengths JSON, reasoning, created_at)

applications(id PK, job_uuid FK UNIQUE, state, cover_letter, answers JSON,
             resume_file, approved_by (user|auto), submitted_at, screenshot_path,
             error, attempts)

answer_bank(id PK, question_norm, question_raw, answer, answer_type, embedding BLOB)

events(id PK, job_uuid, ts, type, payload JSON)   -- audit log of every action
```

`applications.job_uuid UNIQUE` is the hard guarantee against double-applying.

---

## 5. Configuration

`config.yml`
```yaml
profile:
  resumes:                            # the best-scoring version is used per job
    - name: analytics
      pdf: ./me/resume-analytics.pdf
      text: ./me/resume-analytics.md  # what the LLM reads
      targets: [data analyst, reporting]
    - name: bi
      pdf: ./me/resume-bi.pdf
      text: ./me/resume-bi.md
      targets: [business intelligence, dashboards]
  skills: [Python, SQL, Tableau, Power BI, ETL]
  years_experience: 4
searches:
  - query: "data analyst"
    also: ["reporting analyst", "analytics executive", "insights analyst"]
  - query: "business intelligence"
    also: ["BI analyst", "BI developer"]
    employment_types: [Full Time, Permanent]
filters:
  salary_min_monthly: 5000
  max_years_experience_required: 7
  position_levels: [Executive, Senior Executive, Professional]
  title_exclude_keywords: [intern, trainee]
  company_blocklist: []
  max_age_days: 14
apply:
  mode: dry_run                       # dry_run | manual | auto_threshold
  auto_apply_min_score: 75
  quick_review_min_score: 60          # 60–75 go to Telegram for a one-tap decision
  daily_cap: 40                       # final cap once the ramp completes
  hourly_cap: 4
  active_hours: ["08:00", "22:00"]    # Asia/Singapore
  ramp: [15, 25, 40]                  # step up one level after each clean week
  delay_seconds: [45, 120]            # random wait between submissions
llm:
  model: claude-sonnet-5-5
schedule: "5 8-21 * * *"              # discover + score hourly during active hours
notify:
  telegram: true                      # quick approvals, needs_input questions, alerts
```

`answers.yml`
```yaml
- q: "What is your notice period?"
  a: "1 month"
- q: "Are you a Singapore Citizen or Permanent Resident?"
  a: "Yes"
- q: "What is your expected monthly salary?"
  a: "6500"
```

Secrets (`ANTHROPIC_API_KEY`, optional Telegram bot token) go in `.env` and are never committed.

---

## 6. Tech stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Best Playwright and LLM SDK support, quick to iterate |
| HTTP | `httpx` + `tenacity` | Async calls, retries with backoff |
| Browser | Playwright (Chromium, persistent context) | Reliable waits, screenshots, keeps the session |
| DB | SQLite (Python's built-in `sqlite3`) | Single file, no server to run |
| LLM | Anthropic SDK, structured outputs | Scoring JSON and cover letters |
| CLI | Typer | `mcf-apply discover / score / review / apply / run` |
| Dashboard | FastAPI + HTMX + Jinja | A small review UI without a JavaScript build step |
| Scheduling | cron / systemd timer (or APScheduler in `serve`) | Simple |
| Notifications | Telegram bot (`python-telegram-bot`) | One-tap Apply/Skip on borderline jobs, answering new screening questions, "Session expired" and pause alerts |

---

## 7. Safety, politeness and reliability

- **Rate limits**: at most 1 API request per second for discovery; random 45–120 s between
  submissions; `hourly_cap` and `daily_cap` on applications (§3.7).
- **Automatic pause**: submissions stop and you get a Telegram alert if the tool sees a
  CAPTCHA, a warning or rate-limit message from MCF, or more than 2 failed submissions in an
  hour. When the tool pauses, the ramp drops back one step. I don't know whether MCF limits
  applications, so the tool watches for these signs instead of assuming a safe number.
- **Quality check**: phase 6 tracks the response rate for each score range. If low-scoring
  applications get no replies, raise `auto_apply_min_score` or lower the cap rather than
  sending more.
- **Kill switch**: `apply.mode: dry_run`, or the presence of a `STOP` file, halts all submissions.
- **Idempotency**: a unique constraint on applications, plus a pre-submit check of MCF's
  "Applied" badge on the job page.
- **Truthfulness**: the LLM never invents answers to factual questions. Cover letters must be
  grounded in the resume, and a check rejects letters that name skills or employers missing from it.
- **Privacy**: everything stays local. The only external calls are to MCF and the LLM API.
  Resume text goes to the LLM, and you can turn off scoring and drafting (`llm.enabled: false`)
  to use rule-based scoring only.
- **Observability**: every step writes to `events`, failures keep screenshots, and `mcf-apply
  status` prints today's funnel (discovered → filtered → scored → applied).

---

## 8. Project layout

```
applicationapp/
├── pyproject.toml
├── config.example.yml
├── answers.example.yml
├── src/mcf_autoapply/
│   ├── cli.py              # Typer entrypoint
│   ├── config.py           # pydantic settings
│   ├── db.py / models.py   # SQLModel tables
│   ├── mcf/
│   │   ├── client.py       # public jobs API
│   │   ├── applier.py      # Playwright submission flow
│   │   ├── session.py      # login / session check
│   │   └── selectors.py
│   ├── pipeline/
│   │   ├── discover.py     # search → store → filter
│   │   ├── filter.py
│   │   ├── score.py
│   │   ├── tailor.py
│   │   ├── answers.py      # answer-bank matching
│   │   └── pacer.py        # hourly/daily caps, ramp, auto-pause
│   ├── notify.py           # Telegram bot: quick approvals, alerts
│   └── web/                # FastAPI + HTMX review dashboard
└── tests/
    ├── fixtures/           # saved MCF JSON + HTML snapshots
    └── test_*.py
```

---

## 9. Build plan

| Phase | Deliverable | Value on its own |
|---|---|---|
| **1. Discover + filter** ✅ | `mcf-apply discover` fills SQLite; `mcf-apply list` shows matches | A better job feed than MCF's email alerts |
| **2. Score + tailor** | LLM fit scores, gap analysis, draft cover letters | Shows where to spend effort |
| **3. Review dashboard** | Approve / edit / skip UI, answer-bank editor | One place to manage everything |
| **4. Apply (dry run)** ✅ | Playwright fills forms up to the submit button and saves screenshots | Lets you check the automation safely |
| **5. Apply (live)** ✅ | Real submission, hourly pacing and ramp, automatic pause, Telegram quick approvals, hourly scheduler | Fully automated with you in control |
| **6. Outcomes** | Sync "My Applications" status; response-rate stats per score bucket | Shows how to tune the threshold |

Testing: unit-test the filter, scoring and answer matching against saved fixtures. Test the
applier against saved HTML snapshots and through a dry-run smoke test before any live run.

---

## 10. Open questions for you

1. Run locally (laptop or home server) or on a small VPS? The Singpass login needs a visible
   browser once every session lifetime, which is easier locally.
2. Which LLM budget is acceptable? At about 40 jobs a day, Sonnet scoring and drafting costs
   around a few dollars a month.
3. Should the default mode for live runs be `manual` or `auto_threshold`?
4. Which job types should each resume version target? Send me the resumes and I'll suggest the split.
