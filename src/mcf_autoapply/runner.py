"""One full run (discover → approve → apply → notify), and the always-on `serve` loop."""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable

from mcf_autoapply.config import Config
from mcf_autoapply.db import Database
from mcf_autoapply.mcf.client import MCFClient
from mcf_autoapply.notify import Notifier
from mcf_autoapply.pipeline import queue as Q
from mcf_autoapply.pipeline.answers import AnswerBank
from mcf_autoapply.pipeline.discover import discover

Log = Callable[[str], None]   # receives rich markup

STATE_STYLE = {
    Q.APPLIED: "green", Q.DRY_RUN_OK: "cyan", Q.APPROVED: "blue", Q.NEEDS_INPUT: "yellow",
    Q.FAILED: "red", Q.EXTERNAL: "magenta", Q.SKIPPED: "dim", Q.ALREADY_APPLIED: "green",
    Q.CLOSED: "dim",
}


def styled_state(state: str) -> str:
    style = STATE_STYLE.get(state, "red")
    return f"[{style}]{state:<15}[/{style}]"


def apply_queue(cfg: Config, db: Database, dry_run: bool, limit: int | None,
                headless: bool | None, log: Log):
    """Apply to queued jobs in a browser. Returns the RunResult, or None if nothing queued."""
    from mcf_autoapply.mcf.applier import Applier
    from mcf_autoapply.mcf.browser import open_page
    from mcf_autoapply.pipeline import apply as A

    live = not dry_run and cfg.apply.mode != "dry_run"
    if not dry_run and not live:
        log("[yellow]apply.mode is dry_run, so nothing will be submitted. "
            "Set it to manual or auto in config.yml to apply for real.[/yellow]")
    jobs = A.pending(db, not live, limit)
    if not jobs:
        log("Nothing queued. Approve jobs with `mcf-apply approve <ID>`"
            + (" or let auto mode pick them." if cfg.apply.mode == "auto" else "."))
        return None

    def show(title, outcome):
        log(f"{styled_state(outcome.state)} {title}  [dim]{outcome.detail}[/dim]")

    log(f"{'Submitting' if live else 'Dry run (no submissions)'}: up to {len(jobs)} job(s)…")
    with open_page(cfg.apply, headless=headless) as page:
        result = A.run(
            cfg, db,
            lambda: Applier(page, cfg.apply.screenshots_dir, cfg.apply.resume_name, not live),
            dry_run=not live, limit=limit, on_outcome=show,
        )
    if result.stopped:
        log(f"[yellow]Stopped: {result.stopped}[/yellow]")
    return result


def finish_notifications(notifier: Notifier, result, log: Log, new_passed: int = 0,
                         auto_approved: int = 0) -> None:
    notifier.run_summary(new_passed, auto_approved, result)
    notifier.report_stop(result.stopped if result else None)
    for err in notifier.errors:
        log(f"[yellow]Telegram: {err}[/yellow]")
    notifier.errors.clear()


def run_once(cfg: Config, db: Database, notifier: Notifier, log: Log,
             headless: bool | None = True, handle_taps: bool = True) -> None:
    if handle_taps:
        for done in notifier.handle_updates():   # taps since the last run
            log(f"Telegram: {done}")
    with MCFClient(cfg.discovery.requests_per_second) as client:
        found = discover(cfg, db, client)
        log(f"Discovered {len(found.new_uuids)} new, {len(found.new_passed)} passed filters.")
        decisions = Q.auto_approve(cfg, db, client, AnswerBank(cfg.apply.answers_path))
    for d in decisions:
        log(f"{styled_state(d.state)} {d.job_uuid[:8]}  {d.title}"
            + (f"  [dim]{d.note}[/dim]" if d.note else ""))
    notifier.send_question_cards()
    notifier.send_review_cards()
    result = apply_queue(cfg, db, dry_run=False, limit=None, headless=headless, log=log)
    finish_notifications(notifier, result, log, len(found.new_passed),
                         sum(d.state == Q.APPROVED for d in decisions))


def login(cfg: Config, log: Log, notifier: Notifier | None = None,
          headless: bool | None = None) -> bool:
    """Open the Singpass QR login. Each QR goes to Telegram (if linked) and stays on screen."""
    from mcf_autoapply.mcf.browser import login_via_qr, open_page

    def on_qr(qr, n):
        if notifier and notifier.send_login_qr(qr, n):
            log("Singpass QR sent to Telegram" + (" (refreshed)." if n else "."))
        elif n == 0:
            log("Scan the Singpass QR in the browser window with the Singpass app.")
        if notifier:
            for err in notifier.errors:
                log(f"[yellow]Telegram: {err}[/yellow]")
            notifier.errors.clear()

    log("Opening the Singpass login. Waiting up to 10 minutes for you to approve it…")
    with open_page(cfg.apply, headless=False if headless is None else headless) as page:
        return login_via_qr(page, on_qr)


def serve(cfg: Config, db: Database, notifier: Notifier, log: Log,
          clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
          run: Callable[[], None] | None = None, do_login: Callable[[], bool] | None = None,
          max_loops: int | None = None) -> None:
    """Run every interval, and handle Telegram taps and /login, /run in between."""
    interval = cfg.serve.interval_minutes * 60
    run = run or (lambda: run_once(cfg, db, notifier, log, handle_taps=False))
    do_login = do_login or (lambda: login(cfg, log, notifier))
    next_run = clock()   # run straight away on start

    def run_now() -> str:
        nonlocal next_run
        next_run = clock()
        return "Starting a run now."

    def login_cmd() -> str:
        notifier._send("Opening the Singpass login on the home machine. "
                       "The QR code will arrive here in a few seconds.")
        try:
            ok = do_login()
        except Exception as e:   # e.g. no display available
            return f"Login failed: {e}"
        if ok:
            run_now()
            return "✅ Logged in. Starting a run."
        return "Didn't detect a login within 10 minutes. Send /login to try again."

    commands = {"/run": run_now, "/login": login_cmd}
    loops = 0
    while max_loops is None or loops < max_loops:
        loops += 1
        if clock() >= next_run:
            next_run = clock() + interval
            try:
                run()
            except Exception as e:   # a bad run must never stop the service
                log(f"[red]Run failed: {e}[/red]\n{traceback.format_exc()}")
                notifier.alert("run_error", f"A run failed: {type(e).__name__}: {e}")
        wait = max(1, min(50, next_run - clock()))
        if notifier.enabled:
            for done in notifier.handle_updates(timeout=int(wait), commands=commands):
                log(f"Telegram: {done}")
            if notifier.errors:   # Telegram unreachable: don't spin
                for err in notifier.errors:
                    log(f"[yellow]Telegram: {err}[/yellow]")
                notifier.errors.clear()
                sleep(min(wait, 30))
        else:
            sleep(wait)
