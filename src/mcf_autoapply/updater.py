"""Self-update from Telegram: pull the latest code, reinstall, restart."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

UP_TO_DATE = "Already up to date"


def run_update(project: Path) -> tuple[str, bool]:
    """git pull + pip install. Returns (what happened, whether anything changed)."""
    pull = subprocess.run(["git", "-C", str(project), "pull", "--ff-only"],
                          capture_output=True, text=True)
    out = (pull.stdout + pull.stderr).strip()
    if pull.returncode != 0:
        return f"❌ git pull failed:\n{_tail(out)}", False
    if UP_TO_DATE.lower() in out.lower():
        return "✅ Already on the latest version.", False
    pip = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", str(project)],
                         capture_output=True, text=True)
    if pip.returncode != 0:
        return f"⚠️ Code updated but reinstall failed:\n{_tail(pip.stdout + pip.stderr)}", True
    return f"⬇️ Updated:\n{_tail(out)}", True


def restart() -> None:
    """Exit; scripts/start-serve-windows.cmd starts mcf-apply again within a minute."""
    os._exit(0)


def _tail(text: str, lines: int = 8) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])[:1500]
