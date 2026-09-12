"""Deployment checks use a fake launchctl and a temporary HOME, never live agents."""
from datetime import datetime
import os
from pathlib import Path
import plistlib
import subprocess
from types import SimpleNamespace

from prediction_market_macro.jobs import watchdog_job

OPS = Path(__file__).resolve().parents[1] / "ops"
TICK = "com.someopark.macrotick"
WEEKLY = "com.someopark.macroweekly"


def test_tick_starts_promptly_and_streams_long_event_window_logs():
    config = plistlib.loads((OPS / "launchd" / f"{TICK}.plist").read_bytes())
    assert config["StartInterval"] == 60
    assert config["RunAtLoad"] is True
    command = config["ProgramArguments"][-1]
    assert "--no-capture-output" in command
    assert "python -u -m prediction_market_macro.jobs.tick" in command


def run_installer(tmp_path, *args):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    command_log = tmp_path / "launchctl.log"
    fake = bin_dir / "launchctl"
    fake.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$TEST_LAUNCHCTL_LOG"\n')
    fake.chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path), "TEST_LAUNCHCTL_LOG": str(command_log),
           "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", str(OPS / "install_launchd.sh"), *args],
                            env=env, text=True, capture_output=True)
    return result, command_log.read_text() if command_log.exists() else ""


def test_install_one_label_preserves_other_local_configuration(tmp_path):
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    weekly = agents / f"{WEEKLY}.plist"
    weekly.write_text("locally configured ProcessType=Interactive")
    result, calls = run_installer(tmp_path, "install", TICK)
    assert result.returncode == 0, result.stderr
    assert weekly.read_text() == "locally configured ProcessType=Interactive"
    assert (agents / f"{TICK}.plist").read_bytes() == (OPS / "launchd" / f"{TICK}.plist").read_bytes()
    assert calls.splitlines() == [f"unload {agents / (TICK + '.plist')}",
                                  f"load {agents / (TICK + '.plist')}"]


def test_default_install_still_loads_all_five_jobs(tmp_path):
    result, calls = run_installer(tmp_path)
    assert result.returncode == 0, result.stderr
    assert len([line for line in calls.splitlines() if line.startswith("load ")]) == 5


def test_unknown_label_is_rejected_before_touching_launchd(tmp_path):
    result, calls = run_installer(tmp_path, "install", TICK, "com.unrelated.job")
    assert result.returncode != 0
    assert "unknown macro launchd label" in result.stderr
    assert calls == ""
    assert not (tmp_path / "Library" / "LaunchAgents").exists()


def test_watchdog_log_includes_parseable_utc_timestamp(monkeypatch, capsys):
    monkeypatch.setattr(watchdog_job, "load_settings", lambda: SimpleNamespace(db_path="unused"))
    monkeypatch.setattr(watchdog_job, "init_db", lambda _: None)
    monkeypatch.setattr(watchdog_job, "watchdog", lambda _: [])
    watchdog_job.main()
    line = capsys.readouterr().out.strip()
    stamp = datetime.fromisoformat(line.split()[1])
    assert stamp.utcoffset().total_seconds() == 0
    assert line.endswith("missed/breaches: 0")
