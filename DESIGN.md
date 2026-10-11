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
| Applying | Requires Singpass login on `www.mycareersfuture.gov.sg`. Uses your MCF profile resume or an uploaded one. Some jobs send you to an external employer site instead. | Use browser automation (Playwright) with a persistent profile. External-site jobs are flagged "manual". |

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

### 3.2 Filter (`pipeline/filter.py`): deterministic, cheap, runs first
Rules from `config.yml`:
- `salary_min` (normalised to monthly), `employment_types`, `position_levels`
- `max_years_experience_required` (skip jobs that are clearly out of reach)
- `company_blocklist` / `company_allowlist` (by UEN or name)
- `title_exclude_keywords` (e.g. "intern", "sales commission only")
- Skip jobs that aren't `Open`, and skip companies already applied to in the last N days.

Every rejected job is stored with a reason, so you can audit and tune the rules.

### 3.3 Score (`pipeline/score.py`)
Two layers:
1. **Skill overlap**: Jaccard similarity between the MCF `skills[]` and your skills list. Free and instant.
2. **LLM fit assessment**: send your resume, your preferences and the job description to Claude
   (default `claude-sonnet-5-5`) and ask for structured JSON output:
   ```json
   {"fit_score": 0-100, "must_have_gaps": ["..."], "strengths": ["..."],
    "seniority_match": "under|match|over", "recommendation": "apply|maybe|skip",
    "reasoning": "two sentences"}
   ```
Final score = weighted blend. Cache by `(job_uuid, resume_hash, prompt_version)` so a job is
only re-scored when your resume or the prompt changes.

### 3.4 Tailor (`pipeline/tailor.py`)
- **Cover letter**: generated from a template and your resume, 150–250 words, grounded only in facts
  from your resume. The prompt forbids inventing experience.
- **Screening questions**: answered from an **answer bank** (`answers.yml`) first, matched by
  normalised question text plus embedding similarity. Yes/no and numeric questions (notice period,
  expected salary, work-pass status) must come from the bank and never from the LLM. Any question
  without a confident match sends the job to `needs_input`.

### 3.5 Review queue (`web/` or `cli review`)
Every scored job lands in one of three states:

| Mode | Behaviour |
|---|---|
| `manual` (default) | You approve or edit each application in the dashboard. |
| `auto_threshold` | Auto-approve when `score ≥ auto_apply_min_score` AND every screening question is answered from the bank AND the job is on the MCF portal. Everything else waits for you. |
| `dry_run` | Run the whole pipeline but never click submit. Use this first. |

The dashboard shows the job, the score breakdown, gaps, the draft cover letter and the answers,
with **Approve / Edit / Skip** buttons.

### 3.6 Apply (`mcf/applier.py`)
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

### 3.7 Track
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

## 4. Data model (SQLite via SQLModel)

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
  resume_pdf: ./me/resume.pdf
  resume_text: ./me/resume.md        # what the LLM reads
  skills: [Python, SQL, Tableau, Power BI, ETL]
  years_experience: 4
searches:
  - query: "data analyst"
  - query: "business intelligence"
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
  daily_cap: 15
  delay_seconds: [45, 120]            # random wait between submissions
llm:
  model: claude-sonnet-5-5
schedule: "0 9,18 * * *"              # twice a day
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
| DB | SQLite + SQLModel | Single file, no server to run |
| LLM | Anthropic SDK, structured outputs | Scoring JSON and cover letters |
| CLI | Typer | `mcf-apply discover / score / review / apply / run` |
| Dashboard | FastAPI + HTMX + Jinja | A small review UI without a JavaScript build step |
| Scheduling | cron / systemd timer (or APScheduler in `serve`) | Simple |
| Notifications | Telegram bot or email | "8 jobs waiting for review", "Session expired" |

---

## 7. Safety, politeness and reliability

- **Rate limits**: at most 1 API request per second for discovery; random 45–120 s between
  submissions; `daily_cap` (default 15) on applications.
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
│   │   ├── filter.py
│   │   ├── score.py
│   │   ├── tailor.py
│   │   └── answers.py      # answer-bank matching
│   ├── notify.py
│   └── web/                # FastAPI + HTMX review dashboard
└── tests/
    ├── fixtures/           # saved MCF JSON + HTML snapshots
    └── test_*.py
```

---

## 9. Build plan

| Phase | Deliverable | Value on its own |
|---|---|---|
| **1. Discover + filter** | `mcf-apply discover` fills SQLite; `mcf-apply list` shows matches | A better job feed than MCF's email alerts |
| **2. Score + tailor** | LLM fit scores, gap analysis, draft cover letters | Shows where to spend effort |
| **3. Review dashboard** | Approve / edit / skip UI, answer-bank editor | One place to manage everything |
| **4. Apply (dry run)** | Playwright fills forms up to the submit button and saves screenshots | Lets you check the automation safely |
| **5. Apply (live)** | Real submission, daily cap, notifications, scheduler | Fully automated with you in control |
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
4. Do you want one resume, or several resume variants picked per job category?
