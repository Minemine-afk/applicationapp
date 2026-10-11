from pathlib import Path

from mcf_autoapply import setup as S
from mcf_autoapply.config import load_config

ROOT = Path(__file__).parent.parent


def test_ensure_files_copies_examples_once(tmp_path):
    for name in ("config.example.yml", "answers.example.yml"):
        (tmp_path / name).write_text((ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")
    assert S.ensure_files(tmp_path) == ["config.yml", "answers.yml"]
    (tmp_path / "config.yml").write_text("searches: [{query: mine}]\n", encoding="utf-8")
    assert S.ensure_files(tmp_path) == []                       # never overwrites yours
    assert "mine" in (tmp_path / "config.yml").read_text(encoding="utf-8")


def test_enable_telegram_turns_on_an_old_config(tmp_path):
    cfg = tmp_path / "config.yml"
    text = (ROOT / "config.example.yml").read_text(encoding="utf-8")
    cfg.write_text(S.set_section_value(text, "telegram", "enabled", "false")[0], encoding="utf-8")
    assert load_config(cfg).telegram.enabled is False

    assert S.enable_telegram(cfg) is True
    assert load_config(cfg).telegram.enabled is True
    assert S.enable_telegram(cfg) is False                     # already on


def test_set_section_value_keeps_comments():
    text = "telegram:\n  enabled: false   # turn on\n  max_cards_per_run: 8\nserve:\n  enabled: false\n"
    new, changed = S.set_section_value(text, "telegram", "enabled", "true")
    assert changed and new.startswith("telegram:\n  enabled: true   # turn on\n")
    assert new.endswith("serve:\n  enabled: false\n")


def test_windows_start_command_quotes_the_window_title():
    # Unquoted, `start` treats "mcf-apply" as the program to run ("Windows cannot find").
    cmd = S.start_command(Path(r"C:\Users\me\applicationapp\scripts\start-serve-windows.cmd"))
    assert cmd == ('start "mcf-apply" /min '
                   r'"C:\Users\me\applicationapp\scripts\start-serve-windows.cmd"')
