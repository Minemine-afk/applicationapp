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


def run_update(project: Path) -> tuple[str, bool]:
    """git pull. Returns (what happened, whether anything changed)."""
    pull = subprocess.run(["git", "-C", str(project), "pull", "--ff-only"],
                          capture_output=True, text=True)
    out = (pull.stdout + pull.stderr).strip()
    if pull.returncode != 0:
        return f"❌ git pull failed:\n{_tail(out)}", False
    if UP_TO_DATE.lower() in out.lower():
        return "✅ Already on the latest version.", False
    return f"⬇️ Updated:\n{_tail(out)}", True


def restart() -> None:
    """Exit; scripts/start-serve-windows.cmd reinstalls and starts mcf-apply again."""
    os._exit(0)


def _tail(text: str, lines: int = 8) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])[:1500]
