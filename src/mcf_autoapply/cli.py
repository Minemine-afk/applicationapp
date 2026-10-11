"""mcf-apply command-line interface."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from mcf_autoapply.config import Config, load_config
from mcf_autoapply.db import STATUS_DUPLICATE, STATUS_FILTERED, STATUS_NEW, Database
from mcf_autoapply.mcf.client import MCFClient
from mcf_autoapply.models import Job
from mcf_autoapply.notify import CHAT_ID_KEY, Notifier
from mcf_autoapply import runner as R
from mcf_autoapply.pipeline import queue as Q
from mcf_autoapply.pipeline.answers import AnswerBank
from mcf_autoapply.pipeline.discover import apply_filters, discover
from mcf_autoapply.pipeline.pacer import Pacer
from mcf_autoapply.telegram import TelegramAPI, TelegramError

app = typer.Typer(help="Find, rank and apply to jobs on MyCareersFuture.", no_args_is_help=True)
console = Console()

ConfigOpt = Annotated[
    Optional[Path],
    typer.Option("--config", "-c", help="Path to config.yml (default: $MCF_CONFIG or ./config.yml)"),
]


def _load(config: Path | None) -> tuple[Config, Database]:
    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    return cfg, Database(cfg.db_path)


def _salary(row) -> str:
    if row["salary_min"] is None and row["salary_max"] is None:
        return "-"
    lo, hi = row["salary_min"] or 0, row["salary_max"] or 0
    unit = {"Monthly": "/mo", "Annual": "/yr", "Hourly": "/hr"}.get(row["salary_type"], "")
    return f"{lo:,}–{hi:,}{unit}"


def _rank(row) -> dict:
    return json.loads(row["rank_details"]) if row["rank_details"] else {}


@app.command("discover")
def discover_cmd(
    config: ConfigOpt = None,
    max_pages: Annotated[Optional[int], typer.Option(help="Override pages per search term")] = None,
) -> None:
    """Fetch new jobs for every saved search and filter them."""
    cfg, db = _load(config)
    if max_pages:
        cfg.discovery.max_pages = max_pages
    with MCFClient(cfg.discovery.requests_per_second) as client, console.status("Searching MCF…"):
        result = discover(cfg, db, client)

    table = Table(title="Discovery", show_edge=False)
    table.add_column("Search term")
    table.add_column("Fetched", justify="right")
    table.add_column("New", justify="right")
    for tr in result.terms:
        table.add_row(tr.term, str(tr.fetched), str(tr.new))
    console.print(table)
    console.print(
        f"\n[bold]{len(result.new_uuids)}[/bold] new jobs, "
        f"[bold green]{len(result.new_passed)}[/bold green] passed your filters."
    )
    if result.new_passed:
        console.print("Run [cyan]mcf-apply list[/cyan] to see them, best matches first.")
    db.close()


@app.command("list")
def list_cmd(
    config: ConfigOpt = None,
    status: Annotated[
        str, typer.Option(help="new | filtered_out | duplicate | all")
    ] = STATUS_NEW,
    search: Annotated[Optional[str], typer.Option("--search", "-s", help="Match title or company")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
    sort: Annotated[str, typer.Option(help="score | date")] = "score",
    fresh: Annotated[bool, typer.Option("--fresh", help="Only fresh-grad / entry-level jobs")] = False,
) -> None:
    """List stored jobs, best matches first (default: ones that passed the filters)."""
    _, db = _load(config)
    statuses = None if status == "all" else [status]
    rows = db.jobs(statuses, limit=None if fresh else limit, search=search, sort=sort)
    if fresh:
        rows = [r for r in rows if _rank(r).get("fresh_grad")][:limit]
    table = Table(show_edge=False, show_lines=False)
    table.add_column("ID", style="dim")
    table.add_column("Score", justify="right")
    table.add_column("Posted")
    table.add_column("Title", style="bold", max_width=48)
    table.add_column("Company", max_width=30)
    table.add_column("Salary", justify="right")
    table.add_column("Exp", justify="right")
    if status != STATUS_NEW:
        table.add_column("Why", style="yellow", max_width=40)
    for row in rows:
        rank = _rank(row)
        score = "-" if row["score"] is None else str(row["score"])
        title = row["title"] + (" [green](fresh grad)[/green]" if rank.get("fresh_grad") else "")
        cells = [
            row["uuid"][:8], score, row["posted_at"] or "-", title,
            row["company_name"] + (" [dim](agency)[/dim]" if row["is_agency"] else ""),
            _salary(row),
            "-" if row["min_years_exp"] is None else f"{row['min_years_exp']}y",
        ]
        if status != STATUS_NEW:
            cells.append("; ".join(json.loads(row["filter_reasons"])))
        table.add_row(*cells)
    console.print(table)
    console.print(f"[dim]{len(rows)} shown. `mcf-apply show <ID>` for details.[/dim]")
    db.close()


@app.command("show")
def show_cmd(job_id: str, config: ConfigOpt = None) -> None:
    """Show one job in full. ID can be the first few characters."""
    _, db = _load(config)
    row = db.get_job(job_id)
    if not row:
        console.print(f"[red]No unique job matches '{job_id}'.[/red]")
        raise typer.Exit(1)
    job = Job.from_api(json.loads(row["raw"]))
    console.print(f"[bold]{job.title}[/bold]  ·  {job.company_name}")
    console.print(f"[link={job.url}]{job.url}[/link]")
    console.print(
        f"Salary {_salary(row)}  ·  {', '.join(job.employment_types) or '-'}  ·  "
        f"{', '.join(job.position_levels) or '-'}  ·  "
        f"{'-' if job.min_years_exp is None else f'{job.min_years_exp}+ yrs'}  ·  "
        f"posted {job.posted_at}  ·  closes {job.expires_at}"
    )
    console.print(f"Status: [cyan]{row['status']}[/cyan]", end="")
    reasons = json.loads(row["filter_reasons"])
    console.print(f"  ({'; '.join(reasons)})" if reasons else "")
    if job.skills:
        console.print(f"Skills: {', '.join(job.skills)}")
    rank = _rank(row)
    if rank:
        console.print(
            f"Score [bold]{row['score']}[/bold]  (entry-level {rank['entry_score']}: "
            f"{', '.join(rank['entry_signals']) or 'no entry-level signals'}; "
            f"skills {rank['skills_score']}: {', '.join(rank['matched_skills']) or 'none matched'})"
        )
    console.print()
    console.print(job.description_text())
    db.close()


@app.command("refilter")
def refilter_cmd(config: ConfigOpt = None) -> None:
    """Re-apply config.yml filters to every stored job (no downloading)."""
    cfg, db = _load(config)
    counts = apply_filters(db, cfg)
    console.print(
        f"[green]{counts[STATUS_NEW]}[/green] pass, {counts[STATUS_FILTERED]} filtered out, "
        f"{counts[STATUS_DUPLICATE]} duplicates."
    )
    db.close()


@app.command("stats")
def stats_cmd(config: ConfigOpt = None) -> None:
    """Show counts by status and the most common reasons jobs were filtered out."""
    _, db = _load(config)
    counts = db.status_counts()
    console.print(
        f"Total {sum(counts.values())}  ·  [green]pass {counts.get(STATUS_NEW, 0)}[/green]  ·  "
        f"filtered {counts.get(STATUS_FILTERED, 0)}  ·  duplicates {counts.get(STATUS_DUPLICATE, 0)}"
    )
    table = Table(title="Why jobs were filtered out", show_edge=False)
    table.add_column("Reason")
    table.add_column("Jobs", justify="right")
    for reason, n in db.reason_counts()[:20]:
        table.add_row(reason, str(n))
    console.print(table)
    db.close()


# --- applying ------------------------------------------------------------------------------

STATE_STYLE = R.STATE_STYLE


def _print_decisions(decisions) -> None:
    for d in decisions:
        style = STATE_STYLE.get(d.state, "white")
        note = f"  [dim]{d.note}[/dim]" if d.note else ""
        console.print(f"[{style}]{d.state:<15}[/{style}] {d.job_uuid[:8]}  {d.title}{note}")


@app.command("approve")
def approve_cmd(
    job_ids: Annotated[list[str], typer.Argument(help="Job IDs (first few characters is enough)")],
    config: ConfigOpt = None,
) -> None:
    """Queue jobs to apply to. Fetches each job's screening questions."""
    cfg, db = _load(config)
    with MCFClient(cfg.discovery.requests_per_second) as client:
        decisions = Q.approve(db, client, AnswerBank(cfg.apply.answers_path), job_ids)
    _print_decisions(decisions)
    if any(d.state == Q.NEEDS_INPUT for d in decisions):
        console.print("Answer the open questions with [cyan]mcf-apply questions[/cyan].")
    db.close()


@app.command("skip")
def skip_cmd(job_ids: list[str], config: ConfigOpt = None) -> None:
    """Mark jobs as not wanted, so auto mode never applies to them."""
    _, db = _load(config)
    _print_decisions(Q.skip(db, job_ids))
    db.close()


@app.command("queue")
def queue_cmd(
    config: ConfigOpt = None,
    state: Annotated[Optional[str], typer.Option(help="Only this state")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
) -> None:
    """Show applications: queued, waiting on answers, applied, failed."""
    cfg, db = _load(config)
    rows = db.applications([state] if state else None, limit=limit)
    table = Table(show_edge=False)
    for col in ("ID", "State", "Score", "Title", "Company", "Note"):
        table.add_column(col, max_width=44 if col in ("Title", "Note") else None)
    for r in rows:
        style = STATE_STYLE.get(r["state"], "white")
        note = r["error"] or (r["submitted_at"] or "")[:16].replace("T", " ")
        table.add_row(r["job_uuid"][:8], f"[{style}]{r['state']}[/{style}]",
                      "-" if r["score"] is None else str(r["score"]), r["title"],
                      r["company_name"], note)
    console.print(table)
    counts = db.application_counts()
    console.print("  ·  ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "Queue is empty.")
    pacer = Pacer(cfg.apply, db)
    console.print(
        f"Mode [bold]{cfg.apply.mode}[/bold]  ·  today {pacer.applied_today()}/{pacer.daily_cap()}"
        f"  ·  last hour {pacer.applied_last_hour()}/{cfg.apply.hourly_cap}"
        + (f"  ·  [red]{pacer.blocked_reason()}[/red]" if pacer.blocked_reason() else "")
    )
    db.close()


@app.command("questions")
def questions_cmd(config: ConfigOpt = None) -> None:
    """List screening questions that still need your answer."""
    cfg, db = _load(config)
    waiting = Q.unanswered_questions(db)
    if not waiting:
        console.print("No unanswered questions.")
    for i, (question, jobs) in enumerate(waiting.items(), 1):
        console.print(f"[bold]{i}.[/bold] {question}")
        console.print(f"   [dim]waiting: {'; '.join(jobs[:3])}"
                      f"{f' +{len(jobs) - 3} more' if len(jobs) > 3 else ''}[/dim]")
    if waiting:
        console.print("\nAnswer with [cyan]mcf-apply answer <number> yes|no[/cyan]. "
                      "These go to employers as your answers, so answer truthfully.")
    db.close()


@app.command("answer")
def answer_cmd(
    question: Annotated[str, typer.Argument(help="Number from `mcf-apply questions`, or the text")],
    answer: Annotated[str, typer.Argument(help="yes or no")],
    config: ConfigOpt = None,
) -> None:
    """Save your answer to a screening question and release jobs waiting on it."""
    cfg, db = _load(config)
    waiting = list(Q.unanswered_questions(db))
    if question.isdigit():
        if not 1 <= int(question) <= len(waiting):
            console.print(f"[red]No question {question}. See `mcf-apply questions`.[/red]")
            raise typer.Exit(1)
        question = waiting[int(question) - 1]
    if answer.lower() not in ("yes", "no"):
        console.print("[red]Answer must be yes or no.[/red]")
        raise typer.Exit(1)
    bank = AnswerBank(cfg.apply.answers_path)
    bank.add(question, answer)
    console.print(f"Saved: {question} → [bold]{answer.lower()}[/bold]")
    _print_decisions(Q.refresh_answers(db, bank))
    db.close()


@app.command("login")
def login_cmd(config: ConfigOpt = None) -> None:
    """Open a browser window so you can log in to MCF with Singpass."""
    cfg, _ = _load(config)
    from mcf_autoapply.mcf.browser import open_page, wait_for_login

    console.print("Log in with Singpass in the browser window. Waiting up to 10 minutes…")
    with open_page(cfg.apply, headless=False) as page:
        ok = wait_for_login(page)
    console.print("[green]Logged in. The session is saved for future runs.[/green]" if ok
                  else "[red]Didn't detect a login. Run `mcf-apply login` again.[/red]")
    if not ok:
        raise typer.Exit(1)


def _log(msg: str) -> None:
    console.print(msg)


@app.command("apply")
def apply_cmd(
    config: ConfigOpt = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Fill the forms but never submit")] = False,
    limit: Annotated[Optional[int], typer.Option("--limit", "-n")] = None,
    headless: Annotated[Optional[bool], typer.Option("--headless/--show-browser")] = None,
) -> None:
    """Apply to queued jobs, within your hourly and daily limits."""
    cfg, db = _load(config)
    notifier = Notifier.from_config(cfg, db)
    result = R.apply_queue(cfg, db, dry_run, limit, headless, _log)
    R.finish_notifications(notifier, result, _log)
    notifier.close()
    db.close()


@app.command("run")
def run_cmd(
    config: ConfigOpt = None,
    headless: Annotated[Optional[bool], typer.Option("--headless/--show-browser")] = True,
) -> None:
    """Discover new jobs, auto-approve (in auto mode) and apply once. For cron."""
    cfg, db = _load(config)
    notifier = Notifier.from_config(cfg, db)
    R.run_once(cfg, db, notifier, _log, headless=headless)
    notifier.close()
    db.close()


@app.command("serve")
def serve_cmd(config: ConfigOpt = None) -> None:
    """Keep running: a run every serve.interval_minutes, plus Telegram taps, /login and /run."""
    cfg, db = _load(config)
    notifier = Notifier.from_config(cfg, db)
    if cfg.telegram.enabled and not notifier.enabled:
        console.print("[yellow]Telegram is enabled but not linked: set the bot token and run "
                      "`mcf-apply telegram-setup`. Continuing without it.[/yellow]")
    console.print(f"Serving: a run every {cfg.serve.interval_minutes} min. Ctrl+C to stop.")
    try:
        R.serve(cfg, db, notifier, _log)
    except KeyboardInterrupt:
        pass
    finally:
        notifier.close()
        db.close()


@app.command("telegram-setup")
def telegram_setup_cmd(config: ConfigOpt = None) -> None:
    """Link your Telegram chat: send your bot any message first, then run this."""
    cfg, db = _load(config)
    token = os.environ.get(cfg.telegram.token_env)
    if not token:
        console.print(f"[red]Set {cfg.telegram.token_env} to your bot token first "
                      "(create a bot with @BotFather).[/red]")
        raise typer.Exit(1)
    api = TelegramAPI(token)
    try:
        chats = [u["message"]["chat"] for u in api.updates() if "message" in u]
        if not chats:
            console.print("[red]No messages found. Send your bot any message in Telegram, "
                          "then run this again.[/red]")
            raise typer.Exit(1)
        chat = chats[-1]
        db.set_state(CHAT_ID_KEY, str(chat["id"]))
        db.commit()
        api.send(chat["id"], "✅ Linked to mcf-apply. You'll get job cards, screening "
                             "questions and alerts here. Send /status any time.")
    except TelegramError as e:
        console.print(f"[red]Telegram: {e}[/red]")
        raise typer.Exit(1) from e
    finally:
        api.close()
    console.print(f"[green]Linked chat {chat['id']} "
                  f"({chat.get('username') or chat.get('first_name', '')}).[/green]")
    if not cfg.telegram.enabled:
        console.print("Now set [cyan]telegram.enabled: true[/cyan] in config.yml.")
    db.close()


@app.command("notify")
def notify_cmd(config: ConfigOpt = None) -> None:
    """Send pending job cards and screening questions to Telegram now."""
    cfg, db = _load(config)
    notifier = Notifier.from_config(cfg, db)
    if not notifier.enabled:
        console.print("[red]Telegram is off: set telegram.enabled, the bot token env var, "
                      "and run `mcf-apply telegram-setup`.[/red]")
        raise typer.Exit(1)
    for done in notifier.handle_updates():
        console.print(f"Telegram: {done}")
    q, j = notifier.send_question_cards(), notifier.send_review_cards()
    console.print(f"Sent {q} question(s) and {j} job card(s).")
    for err in notifier.errors:
        console.print(f"[yellow]Telegram: {err}[/yellow]")
    notifier.close()
    db.close()


@app.command("bot")
def bot_cmd(config: ConfigOpt = None) -> None:
    """Keep running and handle Telegram button taps instantly (Ctrl+C to stop)."""
    cfg, db = _load(config)
    notifier = Notifier.from_config(cfg, db)
    if not notifier.enabled:
        console.print("[red]Telegram is off. See `mcf-apply telegram-setup`.[/red]")
        raise typer.Exit(1)
    console.print("Listening for Telegram taps… (Ctrl+C to stop)")
    try:
        while True:
            for done in notifier.handle_updates(timeout=50):
                console.print(f"Telegram: {done}")
            for err in notifier.errors:
                console.print(f"[yellow]Telegram: {err}[/yellow]")
            if notifier.errors:
                notifier.errors.clear()
                time.sleep(10)
    except KeyboardInterrupt:
        pass
    finally:
        notifier.close()
        db.close()


@app.command("unpause")
def unpause_cmd(config: ConfigOpt = None) -> None:
    """Resume submissions after an automatic pause. The daily cap restarts its ramp."""
    cfg, db = _load(config)
    pacer = Pacer(cfg.apply, db)
    if not (p := pacer.paused()):
        console.print("Not paused.")
    else:
        pacer.unpause()
        console.print(f"Unpaused (was: {p['reason']}). Daily cap is now {pacer.daily_cap()}.")
    db.close()


if __name__ == "__main__":
    app()
