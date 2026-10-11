"""`mcf-apply setup`: one command for everything that would otherwise be done by hand."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

STARTUP_NAME = "mcf-apply.cmd"


def ensure_files(project: Path) -> list[str]:
    """Create config.yml and answers.yml from their examples if they don't exist."""
    created = []
    for name, example in (("config.yml", "config.example.yml"),
                          ("answers.yml", "answers.example.yml")):
        if not (project / name).exists() and (project / example).exists():
            shutil.copyfile(project / example, project / name)
            created.append(name)
    return created


def set_section_value(text: str, section: str, key: str, value: str) -> tuple[str, bool]:
    """Set `key: value` inside a top-level YAML section, keeping comments and layout."""
    lines = text.splitlines(keepends=True)
    in_section = False
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(section)}:\s*(#.*)?$", line):
            in_section = True
            continue
        if in_section and re.match(r"^\S", line):   # next top-level key: section ended
            break
        if in_section and (m := re.match(rf"^(\s+{re.escape(key)}:\s*)([^#\n]*?)(\s*#.*)?$", line.rstrip("\n"))):
            if m.group(2).strip() == value:
                return text, False
            lines[i] = f"{m.group(1)}{value}{m.group(3) or ''}\n"
            return "".join(lines), True
    return text, False


def enable_telegram(config_path: Path) -> bool:
    text = config_path.read_text(encoding="utf-8")
    new, changed = set_section_value(text, "telegram", "enabled", "true")
    if changed:
        config_path.write_text(new, encoding="utf-8")
    return changed


def save_user_env(name: str, value: str) -> bool:
    """Save an environment variable for this user permanently (Windows: setx)."""
    os.environ[name] = value
    if sys.platform != "win32":
        return False
    result = subprocess.run(["setx", name, value], capture_output=True, text=True)
    return result.returncode == 0


def install_browser(log: Callable[[str], None]) -> bool:
    log("Installing the browser used for applying (first time takes a minute)…")
    result = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
    return result.returncode == 0


def windows_startup_dir() -> Path:
    return (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            / "Startup")


def install_windows_autostart(project: Path) -> Path:
    """Start `mcf-apply serve` (minimised) whenever you log in to Windows."""
    script = project / "scripts" / "start-serve-windows.cmd"
    target = windows_startup_dir() / STARTUP_NAME
    target.write_text(
        "@echo off\r\nrem Starts mcf-apply when you log in. Delete this file to stop that.\r\n"
        f"{start_command(script)}\r\n", encoding="utf-8")
    return target


def start_command(script: Path) -> str:
    # `start` takes its first *quoted* argument as the window title. Passed unquoted,
    # Windows tried to run a program called "mcf-apply" and couldn't find it.
    return f'start "mcf-apply" /min "{script}"'


def start_windows_serve(project: Path) -> None:
    script = project / "scripts" / "start-serve-windows.cmd"
    subprocess.Popen(start_command(script), shell=True, cwd=project)


def keep_awake_when_plugged_in() -> bool:
    result = subprocess.run(["powercfg", "/change", "standby-timeout-ac", "0"],
                            capture_output=True, text=True)
    return result.returncode == 0


def wait_for_start(notifier, log: Callable[[str], None], timeout_s: int = 300) -> bool:
    """Wait for /start in Telegram, which links the chat."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and notifier.chat_id is None:
        notifier.handle_updates(timeout=20)
        for err in notifier.errors:
            log(f"Telegram: {err}")
        notifier.errors.clear()
    return notifier.chat_id is not None
