"""Self-update from Telegram: pull the latest code, then restart.

The reinstall (`pip install -e .`) is NOT done here on purpose: doing it while mcf-apply is
running removes the package underneath the running program, and if anything goes wrong the
restart loop can't start the app at all ("No module named 'mcf_autoapply'"). Instead
scripts/start-serve-windows.cmd reinstalls right before every launch, when nothing is running.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

UP_TO_DATE = "Already up to date"
GIT_TIMEOUT_S = 120


def _git(project: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}   # never hang on a password prompt
    try:
        return subprocess.run(["git", "-C", str(project), *args], capture_output=True,
                              text=True, env=env, timeout=GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"git {args[0]} timed out after "
                                           f"{GIT_TIMEOUT_S} s (no internet?)")
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", "git is not installed or not on PATH")


def run_update(project: Path) -> tuple[str, bool]:
    """git pull. Returns (what happened, whether the pull succeeded).

    Edits to the checkout's own files (code, docs) are discarded if they block the pull:
    your settings live in config.yml, answers.yml and data/, which git never touches.
    """
    pull = _git(project, "pull", "--ff-only")
    out = (pull.stdout + pull.stderr).strip()
    if pull.returncode != 0 and ("local changes" in out or "overwritten" in out
                                 or "unmerged" in out.lower()):
        _git(project, "checkout", "--", ".")
        pull = _git(project, "pull", "--ff-only")
        out = "(local edits to the app's files were discarded)\n" + (pull.stdout + pull.stderr).strip()
    if pull.returncode != 0:
        return f"❌ git pull failed:\n{_tail(out)}", False
    if UP_TO_DATE.lower() in out.lower():
        return "✅ Already on the latest version.", True
    return f"⬇️ Updated:\n{_tail(out)}", True


def restart() -> None:
    """Exit; scripts/start-serve-windows.cmd reinstalls and starts mcf-apply again."""
    os._exit(0)


def _tail(text: str, lines: int = 8) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])[:1500]
