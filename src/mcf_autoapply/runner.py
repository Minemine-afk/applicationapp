"""One full run (discover → approve → apply → notify), and the always-on `serve` loop."""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from contextlib import contextmanager

from mcf_autoapply.config import Config
from mcf_autoapply.db import Database
from mcf_autoapply.mcf.client import MCFClient
from mcf_autoapply.notify import Notifier
from mcf_autoapply.overrides import apply_overrides
from mcf_autoapply.pipeline import queue as Q
from mcf_autoapply.pipeline.answers import AnswerBank
from mcf_autoapply.pipeline.discover import discover
from mcf_autoapply.sources import short_id, source_of

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
    from mcf_autoapply.jobstreet.applier import JobStreetApplier
    from mcf_autoapply.mcf.applier import Applier
    from mcf_autoapply.mcf.browser import open_page
    from mcf_autoapply.pipeline import apply as A

    live = not dry_run and cfg.apply.mode != "dry_run"
    if not dry_run and not live:
        log("[yellow]apply.mode is dry_run, so nothing will be submitted. "
            "Set it to manual or auto in config.yml to apply for real.[/yellow]")
    jobs = A.pending(db, not live, limit, jobstreet=cfg.jobstreet.enabled)
    if not jobs:
        log("Nothing queued. Approve jobs with `mcf-apply approve <ID>`"
            + (" or let auto mode pick them." if cfg.apply.mode == "auto" else "."))
        return None
    sites = {source_of(j["job_uuid"]) for j in jobs}
    if "jobstreet" in sites and not cfg.jobstreet.headless:
        headless = False   # JobStreet's bot check wants a visible browser

    def show(title, outcome):
        log(f"{styled_state(outcome.state)} {title}  [dim]{outcome.detail}[/dim]")

    log(f"{'Submitting' if live else 'Dry run (no submissions)'}: up to {len(jobs)} job(s)…")
    with open_page(cfg.apply, headless=headless) as page:
        skipped = []
        for site in sorted(sites):
            skipped += sync_applied_jobs(page, db, log, site)
        if skipped:
            log(f"Skipped {len(skipped)} queued job(s) already listed as applied: "
                + "; ".join(skipped[:5]))

        def applier_for(source: str):
            if source == "jobstreet":
                return JobStreetApplier(page, cfg.apply.screenshots_dir,
                                        cfg.jobstreet.resume_name, not live)
            return Applier(page, cfg.apply.screenshots_dir, cfg.apply.resume_name, not live)

        result = A.run(cfg, db, applier_for, dry_run=not live, limit=limit, on_outcome=show)
    result.skipped_applied = skipped
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
    apply_overrides(cfg, db)   # e.g. /mode set from Telegram
    if handle_taps:
        for done in notifier.handle_updates():   # taps since the last run
            log(f"Telegram: {done}")
    with MCFClient(cfg.discovery.requests_per_second) as client:
        found = discover(cfg, db, client)
        log(f"Discovered {len(found.new_uuids)} new, {len(found.new_passed)} passed filters.")
        for problem in found.problems:
            log(f"[yellow]{problem}[/yellow]")
            notifier.alert("discover:" + problem.split(":")[0], problem)
        decisions = Q.auto_approve(cfg, db, client, AnswerBank(cfg.apply.answers_path))
    for d in decisions:
        log(f"{styled_state(d.state)} {short_id(d.job_uuid)}  {d.title}"
            + (f"  [dim]{d.note}[/dim]" if d.note else ""))
    notifier.send_question_cards()
    notifier.send_review_cards()
    result = apply_queue(cfg, db, dry_run=False, limit=None, headless=headless, log=log)
    finish_notifications(notifier, result, log, len(found.new_passed),
                         sum(d.state == Q.APPROVED for d in decisions))


def sync_applied_jobs(page, db: Database, log: Log, site: str = "mcf") -> list[str]:
    """Refresh a site's applied-jobs list before applying; a failure never blocks a run."""
    from mcf_autoapply.mcf.applied import AppliedSyncError
    from mcf_autoapply.sources import LABELS

    if site == "jobstreet":
        from mcf_autoapply.jobstreet.session import sync_applied
    else:
        from mcf_autoapply.mcf.applied import sync_applied
    name = LABELS.get(site, site)
    try:
        result = sync_applied(page, db)
    except AppliedSyncError as e:
        log(f"[yellow]Couldn't read {name}'s applied jobs ({e}); using what's known.[/yellow]")
        return []
    except Exception as e:
        log(f"[yellow]{name} applied-jobs sync failed: {type(e).__name__}: {e}[/yellow]")
        return []
    log(f"{name} lists {result.total} application(s); {result.new} new to us.")
    return result.released


def apply_only(cfg: Config, db: Database, notifier: Notifier, log: Log,
               headless: bool | None = True) -> None:
    """Apply to what's already queued, without searching first (/apply)."""
    apply_overrides(cfg, db)
    result = apply_queue(cfg, db, dry_run=False, limit=None, headless=headless, log=log)
    finish_notifications(notifier, result, log)


def login(cfg: Config, log: Log, notifier: Notifier | None = None,
          headless: bool | None = None, site: str = "mcf") -> bool:
    """Log in to a site. MCF: Singpass QR to Telegram. JobStreet: you sign in in a window."""
    from mcf_autoapply.mcf.browser import is_logged_in, login_via_qr, open_page, save_session

    if site == "jobstreet":
        return login_jobstreet(cfg, log, notifier)

    def on_qr(qr, n):
        if notifier and notifier.send_login_qr(qr, n):
            log("Singpass QR sent to Telegram" + (" (refreshed)." if n else "."))
        elif n == 0:
            log("Scan the Singpass QR in the browser window with the Singpass app.")
        if notifier:
            for err in notifier.errors:
                log(f"[yellow]Telegram: {err}[/yellow]")
            notifier.errors.clear()

    def on_unclear(png: bytes, note: str) -> None:
        log(f"[yellow]{note}[/yellow]")
        if notifier and notifier.enabled:
            try:
                notifier.api.send_photo(notifier.chat_id, png, f"🔍 {note}")
            except Exception as e:
                log(f"[yellow]Telegram: {e}[/yellow]")

    def tell(text: str) -> None:
        log(text)
        if notifier:
            notifier._send(text)

    log("Opening the Singpass login. Waiting up to 10 minutes for you to approve it…")
    with open_page(cfg.apply, headless=False if headless is None else headless) as page:
        try:
            ok = login_via_qr(page, on_qr, on_unclear=on_unclear)
        finally:
            if notifier:
                notifier.clear_login_qr()   # whatever happened, that QR is no longer usable
        if ok:
            save_session(page.context, cfg.apply)   # don't wait for close: keep it now
    if not ok:
        return False
    # Prove the login survives a browser restart (the way every run opens it), so a
    # problem shows up now rather than as "logged_out" on the next run.
    with open_page(cfg.apply, headless=True) as page:
        persisted = is_logged_in(page)
    if persisted:
        tell("💾 Login saved: it survives reopening the browser.")
    else:
        tell("⚠️ Logged in, but the login did not survive reopening the browser. "
             "Runs will report logged_out. Please tell the developer.")
    return True


def login_jobstreet(cfg: Config, log: Log, notifier: Notifier | None = None) -> bool:
    """Open JobStreet's sign-in in a visible window; you log in; the session is saved."""
    from mcf_autoapply.jobstreet import session as JS
    from mcf_autoapply.mcf.browser import open_page, save_session

    def tell(text: str) -> None:
        log(text)
        if notifier:
            notifier._send(text)

    with open_page(cfg.apply, headless=False) as page:
        ok = JS.login(page, tell)
        if ok:
            page.wait_for_timeout(3000)       # let the site finish writing its login cookies
            save_session(page.context, cfg.apply)
    if not ok:
        return False
    # Reopen the way JobStreet runs do (a visible window unless jobstreet.headless), and
    # show what the browser sees if the login is gone, so it can be fixed from the evidence.
    with open_page(cfg.apply, headless=cfg.jobstreet.headless) as page:
        persisted = JS.check_logged_in(page)
        if not persisted:
            evidence = _login_evidence(cfg, page, "jobstreet-reopen")
    if persisted:
        tell("💾 JobStreet login saved: it survives reopening the browser.")
    else:
        tell("⚠️ Logged in to JobStreet, but the login did not survive reopening the browser. "
             f"Please send the developer this message and the picture.\n{evidence}")
        if notifier and notifier.enabled:
            try:
                notifier.api.send_photo(notifier.chat_id,
                                        (cfg.apply.screenshots_dir / "jobstreet-reopen.png").read_bytes(),
                                        "What the reopened browser shows on JobStreet.")
            except Exception as e:
                log(f"[yellow]Telegram: {e}[/yellow]")
    return True


def _login_evidence(cfg: Config, page, name: str) -> str:
    """Screenshot + HTML of the page, and a line about it (URL, title, cookies seen)."""
    cfg.apply.screenshots_dir.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(cfg.apply.screenshots_dir / f"{name}.png"), full_page=False)
        (cfg.apply.screenshots_dir / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        cookies = sorted(c["name"] for c in page.context.cookies(page.url))
    except Exception:
        cookies = []
    return (f"URL: {page.url}\nTitle: {page.title()}\nCookies for this site: {len(cookies)} "
            f"({', '.join(cookies[:12])})\nSaved in data/screenshots/{name}.png and .html")


SERVE_LOCK_PORT = 47391


class AlreadyRunning(RuntimeError):
    pass


def single_instance(port: int = SERVE_LOCK_PORT):
    """Hold a local port as a lock so only one `serve` runs (two would double-apply)."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        raise AlreadyRunning("mcf-apply serve is already running on this computer") from None
    return sock


class TaskRunner:
    """Runs "run" and "login" one at a time on a background thread.

    Telegram is handled on the main thread, so the bot keeps answering while a long search,
    application round or login is in progress. Each task is queued at most once.
    """

    def __init__(self, handlers: dict[str, Callable[[], None]], log: Log,
                 background: bool = True) -> None:
        self.handlers = handlers
        self.log = log
        self.current: str | None = None
        self._queue: list[str] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._background = background
        if background:
            threading.Thread(target=self._loop, daemon=True, name="mcf-tasks").start()

    @property
    def busy(self) -> bool:
        return self.current is not None or bool(self._queue)

    @property
    def active(self) -> str | None:
        """The running task, or the one about to start."""
        with self._lock:
            return self.current or (self._queue[0] if self._queue else None)

    def submit(self, name: str, first: bool = False) -> str:
        """Queue a task. Returns "started", "queued" (after another task) or "already".

        `first` puts it ahead of other queued tasks (it still waits for the running one).
        """
        with self._lock:
            if name == self.current or name in self._queue:
                return "already"
            state = "queued" if self.busy else "started"
            if first:
                self._queue.insert(0, name)
            else:
                self._queue.append(name)
        if self._background:
            self._wake.set()
        else:
            self._drain()   # tests: run right away, on this thread
        return state

    def _loop(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            self._drain()

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._queue or (not self._background and self.current):
                    return
                self.current = self._queue.pop(0)
            try:
                self.handlers[self.current]()
            except Exception as e:   # a failed task must never stop the service
                self.log(f"[red]{self.current} failed: {e}[/red]\n{traceback.format_exc()}")
            finally:
                with self._lock:
                    self.current = None


ACTIVITY = {"run": "searching and applying", "apply": "applying",
            "login": "waiting for your Singpass login", "update": "updating"}


def serve(cfg: Config, db: Database, notifier: Notifier, log: Log,
          clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
          run: Callable[[], None] | None = None, do_login: Callable[[], bool] | None = None,
          max_loops: int | None = None, background: bool = True,
          do_update: Callable[[], tuple[str, bool]] | None = None,
          do_restart: Callable[[], None] | None = None) -> None:
    """A run every interval plus /run and /login, all in the background, so Telegram taps and
    commands are answered immediately, even mid-run.
    """
    interval = cfg.serve.interval_minutes * 60

    @contextmanager
    def worker_notifier():
        # The background thread needs its own database connection (SQLite connections
        # can't be shared across threads) and its own Telegram client.
        if not background:
            yield db, notifier
            return
        wdb = Database(cfg.db_path)
        wn = Notifier.from_config(cfg, wdb)
        try:
            yield wdb, wn
        finally:
            for err in wn.errors:
                log(f"[yellow]Telegram: {err}[/yellow]")
            wn.close()
            wdb.close()

    def run_task() -> None:
        with worker_notifier() as (wdb, wn):
            try:
                (run or (lambda: run_once(cfg, wdb, wn, log, handle_taps=False)))()
            except Exception as e:   # a bad run must never stop the service
                log(f"[red]Run failed: {e}[/red]\n{traceback.format_exc()}")
                wn.alert("run_error", f"A run failed: {type(e).__name__}: {e}")

    def apply_task() -> None:
        with worker_notifier() as (wdb, wn):
            try:
                apply_only(cfg, wdb, wn, log)
            except Exception as e:
                log(f"[red]Apply failed: {e}[/red]\n{traceback.format_exc()}")
                wn.alert("run_error", f"Applying failed: {type(e).__name__}: {e}")

    login_site = {"site": "mcf"}

    def login_task() -> None:
        site = login_site["site"]
        with worker_notifier() as (wdb, wn):
            if site == "jobstreet":
                wn._send("Opening JobStreet's sign-in in a browser window on the PC. "
                         "Sign in there; nothing is typed for you.")
            else:
                wn._send("Opening the Singpass login. The QR code will arrive here in a few "
                         "seconds. Tap it within a minute.")
            try:
                ok = (do_login or (lambda: login(cfg, log, wn, site=site)))()
            except Exception as e:   # e.g. no display available
                wn._send(f"Login failed: {e}")
                return
            wn._send("✅ Logged in. Starting a run." if ok else
                     f"Didn't detect a login within 10 minutes. Send /login{' jobstreet' if site == 'jobstreet' else ''} to try again.")
        if ok:
            tasks.submit("run")

    def update_task() -> None:
        from mcf_autoapply import updater

        with worker_notifier() as (wdb, wn):
            summary, pulled = (do_update or (lambda: updater.run_update(cfg.project_dir)))()
            log(summary)
            wn._send(summary)
            if pulled:
                wn._send("♻️ Restarting: the start script reinstalls the app, then starts it. "
                         "Back in a minute or two (\"🟢 mcf-apply is running\" means it worked).")
            else:
                wn._send("Not restarting, since nothing was pulled. Fix the problem above (or "
                         "send /restart to restart anyway).")
        if pulled:
            (do_restart or updater.restart)()

    def restart_task() -> None:
        with worker_notifier() as (wdb, wn):
            wn._send("♻️ Restarting. Back in about a minute.")
        (do_restart or __import__("mcf_autoapply.updater").updater.restart)()

    tasks = TaskRunner({"run": run_task, "apply": apply_task, "login": login_task,
                        "update": update_task, "restart": restart_task}, log, background)

    def update_cmd(args: str) -> str:
        return {"started": "Checking for an update…",
                "queued": "The update will start as soon as the current task finishes.",
                "already": "An update is already in progress."}[tasks.submit("update", first=True)]

    def restart_cmd(args: str) -> str | None:
        return {"started": None, "queued": "Restarting as soon as the current task finishes.",
                "already": "Already restarting."}[tasks.submit("restart", first=True)]

    def run_cmd(args: str) -> str:
        return {"started": "Starting a run now (search, then apply). Results arrive here "
                           "when it's done. /apply skips the search.",
                "queued": "A run will start as soon as the current task finishes.",
                "already": "A run is already in progress or queued."}[tasks.submit("run")]

    def apply_cmd(args: str) -> str:
        return {"started": "Applying to the queue now, no search first.",
                "queued": "Applying will start as soon as the current task finishes.",
                "already": "Applying is already in progress or queued."}[tasks.submit("apply")]

    def login_cmd(args: str) -> str | None:
        site = args.strip().lower() or "mcf"
        if site in ("js", "jobstreet"):
            site = "jobstreet"
        elif site not in ("mcf", "mycareersfuture"):
            return "Use /login (MyCareersFuture) or /login jobstreet."
        else:
            site = "mcf"
        if site == "jobstreet" and not cfg.jobstreet.enabled:
            return "JobStreet is off: set jobstreet.enabled: true in config.yml first."
        login_site["site"] = site
        busy = ACTIVITY.get(tasks.current, "the current task")
        return {"started": None,   # the login task sends its own messages
                "queued": f"The login will start as soon as {busy} finishes; it's next in line.",
                "already": "A login is already in progress or queued."}[
                    tasks.submit("login", first=True)]

    commands = {"/run": run_cmd, "/apply": apply_cmd, "/login": login_cmd,
                "/update": update_cmd, "/restart": restart_cmd}
    notifier.register_commands(serve=True)
    notifier.clear_login_qr()   # a QR from before a restart can't be approved any more
    notifier._send("🟢 <b>mcf-apply is running.</b> Searching for jobs now; "
                   f"then every {cfg.serve.interval_minutes} min. /help for commands.")
    next_run = clock()   # run straight away on start
    loops = 0
    while max_loops is None or loops < max_loops:
        loops += 1
        if clock() >= next_run:
            next_run = clock() + interval
            tasks.submit("run")
        notifier.activity = ACTIVITY.get(tasks.active) if tasks.active else None
        wait = max(1, min(50, next_run - clock()))
        if notifier.api is not None:   # linked, or waiting for /start to link
            for done in notifier.handle_updates(timeout=int(wait), commands=commands):
                log(f"Telegram: {done}")
            if notifier.errors:   # Telegram unreachable: don't spin
                for err in notifier.errors:
                    log(f"[yellow]Telegram: {err}[/yellow]")
                notifier.errors.clear()
                sleep(min(wait, 30))
        else:
            sleep(wait)
