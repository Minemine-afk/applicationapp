"""mcf-apply command-line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from mcf_autoapply.config import Config, load_config
from mcf_autoapply.db import STATUS_DUPLICATE, STATUS_FILTERED, STATUS_NEW, Database
from mcf_autoapply.mcf.client import MCFClient
from mcf_autoapply.models import Job
from mcf_autoapply.pipeline.discover import apply_filters, discover

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


if __name__ == "__main__":
    app()
