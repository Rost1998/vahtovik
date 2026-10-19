import subprocess
from datetime import timedelta

import pytest

from vahtovik import cli, timer


@pytest.fixture
def systemd(tmp_path, monkeypatch):
    """A fake systemctl and loginctl; returns the list of commands run."""
    commands = []

    def command(*args):
        commands.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="no\n" if args[0] == "loginctl" else "", stderr="")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(timer, "_command", command)
    monkeypatch.setattr(timer.shutil, "which", lambda name: f"/usr/bin/{name}")
    return commands


@pytest.mark.parametrize("every, calendar", [
    (timedelta(minutes=1), "*:0/1"), (timedelta(minutes=15), "*:0/15"),
    (timedelta(hours=1), "0/1:00"), (timedelta(hours=6), "0/6:00"), (timedelta(days=1), "daily"),
])
def test_on_calendar(every, calendar):
    assert timer.on_calendar(every) == calendar


@pytest.mark.parametrize("every", [timedelta(minutes=45), timedelta(hours=5), timedelta(seconds=90), timedelta(days=2)])
def test_on_calendar_rejects_uneven_periods(every):
    with pytest.raises(timer.TimerError):
        timer.on_calendar(every)


def test_install_and_uninstall(project, systemd, capsys):
    assert cli.main(["install-timer", "--every", "30m"]) == 0
    folder = timer.unit_dir()
    service, timer_unit = (folder / "vahtovik.service").read_text(), (folder / "vahtovik.timer").read_text()
    assert f"WorkingDirectory={project.root}" in service
    assert 'ExecStart="/usr/bin/uv" run --quiet vahtovik run' in service
    assert "OnCalendar=*:0/30" in timer_unit and "Persistent=true" in timer_unit
    assert ["systemctl", "--user", "enable", "--now", "vahtovik.timer"] in systemd
    out = capsys.readouterr().out
    assert out.startswith("Installed vahtovik.timer: a cycle every 30m") and "enable-linger" in out
    assert "Installed the systemd user timer: a cycle every 30m" in (project.root / "data/journal.log").read_text()

    assert cli.main(["uninstall-timer"]) == 0
    assert not (folder / "vahtovik.timer").exists() and not (folder / "vahtovik.service").exists()
    assert ["systemctl", "--user", "disable", "--now", "vahtovik.timer"] in systemd
    assert cli.main(["uninstall-timer"]) == 0
    assert capsys.readouterr().out.endswith("No vahtovik timer is installed.\n")


def test_uneven_period_is_refused(project, systemd, capsys):
    assert cli.main(["install-timer", "--every", "45m"]) == 2
    assert "must divide an hour or a day" in capsys.readouterr().err
    assert systemd == [] and not timer.unit_dir().exists()
