import json
import logging
import re
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from vahtovik import usage_limits



def now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def usage(project, monkeypatch):
    """Input channels `chat` (the person's own) and `shared` (limited), and the usage that the fake claude reports.
    Returns a function that sets the percents of the 5-hour, weekly and weekly Fable limits."""
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["channels"]["chat"] = {"skill": "file", "input": {}, "env": {"FILE_CHANNEL_PATH": "data/chat-out.md"}}
    data["channels"]["shared"] = {"skill": "file", "input": {"commands": ["status"]},
                                  "env": {"FILE_CHANNEL_PATH": "data/shared-out.md"}}
    path.write_text(yaml.safe_dump(data))
    answer = project.tmp / "usage.json"
    monkeypatch.setenv("FAKE_CLAUDE_USAGE", str(answer))

    def set_usage(session=10, weekly=5, fable=0, session_reset=None, raw=None, profile=None):
        session_reset = session_reset or now() + timedelta(hours=3, minutes=11)
        rows = [
            {"kind": "session", "group": "session", "percent": session, "resets_at": session_reset.isoformat(),
             "scope": None, "severity": "normal", "is_active": True},
            {"kind": "weekly_all", "group": "weekly", "percent": weekly,
             "resets_at": (now() + timedelta(days=6, hours=7)).isoformat(), "scope": None},
            {"kind": "weekly_scoped", "group": "weekly", "percent": fable,
             "resets_at": (now() + timedelta(days=6, hours=7)).isoformat(),
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
        ]
        path = profile / "fake-usage.json" if profile else answer  # an account's profile answers on its own
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw or {"rate_limits_available": True, "rate_limits": {"limits": rows}}))
    set_usage()
    return set_usage


def _check(project):
    cfg, state, claude = project.load()
    for account in cfg.claude.accounts:
        state.set_meta(f"usage_checked_at:{account}", "")
    state.set_meta("usage_checked_at", "")  # due now
    usage_limits.check(cfg, state, claude)
    return state


def _warnings(state) -> list[str]:
    return [r["body"] for r in state.rows("SELECT body FROM deliveries WHERE channel = 'chat' ORDER BY id")]


def test_each_limit_gets_one_warning_at_each_level_it_passes(project, usage):
    usage(session=79)
    assert _warnings(_check(project)) == []
    usage(session=82)
    state = _check(project)
    warning, = _warnings(state)
    assert warning.startswith("The 5-hour limit is at 82% (past 80%). It resets ")
    assert warning.endswith(", in 3 hours 11 minutes.")
    usage(session=85)
    assert len(_warnings(_check(project))) == 1  # 80% was warned already
    usage(session=96, weekly=91, fable=80)  # a jump past 90% and 95% gives one warning, at 95%
    warning = _warnings(_check(project))[-1].splitlines()
    assert warning[0].startswith("The 5-hour limit is at 96% (past 95%).")
    assert warning[1].startswith("The weekly limit for all models is at 91% (past 90%). It resets on ")
    assert warning[1].endswith(", in 6 days 7 hours.")
    assert warning[2].startswith("The weekly Fable limit is at 80% (past 80%).")


def test_a_limit_warns_again_after_it_resets(project, usage):
    usage(session=91)
    _check(project)
    usage(session=5, session_reset=now() + timedelta(hours=5))  # the percent fell: a new window
    _check(project)
    usage(session=81, session_reset=now() + timedelta(hours=5))
    assert _warnings(_check(project))[-1].startswith("The 5-hour limit is at 81% (past 80%).")


def test_a_limit_warns_again_once_its_reset_time_has_passed(project, usage):
    usage(session=91, session_reset=now() - timedelta(minutes=5))  # the reset came, but the next check saw it late
    _check(project)
    usage(session=93, session_reset=now() + timedelta(hours=5))
    assert len(_warnings(_check(project))) == 2


def test_warnings_go_to_the_persons_own_input_channels_or_to_those_set(project, usage):
    usage(session=90)
    state = _check(project)
    assert [r["channel"] for r in state.rows("SELECT channel FROM deliveries")] == ["chat"]
    out = (project.root / "data/chat-out.md").read_text()
    assert "· Claude usage\n_vahtovik_\n\nThe 5-hour limit is at 90% (past 90%)." in out
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["usage"] = {"channels": ["log", "shared"]}
    path.write_text(yaml.safe_dump(data))
    usage(session=96)
    state = _check(project)
    assert [r["channel"] for r in state.rows("SELECT channel FROM deliveries")] == ["chat", "log", "shared"]


def test_older_answers_without_rows_are_read_from_their_fields(project, usage):
    reset = (now() + timedelta(hours=2)).isoformat()
    usage(raw={"rate_limits_available": True, "rate_limits": {
        "five_hour": {"utilization": 99, "resets_at": reset}, "seven_day": {"utilization": 10, "resets_at": reset},
        "seven_day_opus": {"utilization": 85.4, "resets_at": None}, "seven_day_sonnet": None}})
    lines = _warnings(_check(project))[0].splitlines()
    assert re.match(r"The 5-hour limit is at 99% \(past 99%\)\. It resets (today|tomorrow) at \d\d:\d\d, in 2 hours\.$",
                    lines[0])
    assert lines[1] == "The weekly Opus limit is at 85% (past 80%). It has no reset time."


def test_the_check_waits_for_usage_every_and_skips_an_api_key(project, usage):
    cfg, state, claude = project.load()
    usage_limits.check(cfg, state, claude)
    usage_limits.check(cfg, state, claude)
    assert len(project._lines(project.log_file.with_name("calls.jsonl.usage"))) == 1
    assert project.calls() == []  # no AI session
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["claude"]["auth"] = "api-key"
    path.write_text(yaml.safe_dump(data))
    _check(project)
    assert len(project._lines(project.log_file.with_name("calls.jsonl.usage"))) == 1


def test_a_failed_check_writes_one_line_until_a_check_works(project, usage, caplog):
    usage(raw={"exit": 1, "stderr": "not logged in"})
    caplog.set_level(logging.INFO, logger="vahtovik")
    _check(project)
    _check(project)
    usage(session=10)
    _check(project)
    lines = [r.getMessage() for r in caplog.records if "Claude usage" in r.getMessage()]
    assert lines == ["Failed checking Claude usage: not logged in", "Checked Claude usage again after a failure"]


def test_no_plan_limits_no_warnings(project, usage):
    usage(raw={"rate_limits_available": False, "rate_limits": None})
    state = _check(project)
    assert _warnings(state) == [] and usage_limits.last(state)[1] == []


def test_a_run_checks_the_usage_and_status_shows_it(project, usage, capsys):
    from vahtovik import cli
    usage(session=82, weekly=7)
    cli.main(["run", "--stage", "deliver"])
    cfg, state, _ = project.load()
    assert _warnings(state)[0].startswith("The 5-hour limit is at 82% (past 80%).")
    capsys.readouterr()
    cli.main(["status"])
    out = capsys.readouterr().out
    assert re.search(r"\(warnings at: 80, 90, 95, 99\):\n  5-hour limit: 82%, resets (today|tomorrow) at ", out)
    assert "\n  weekly limit for all models: 7%, resets on " in out


def _account(project) -> object:
    """Account `work` in vahtovik.yaml. Returns its profile folder."""
    profile = project.tmp / "claude-work"
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["claude"]["accounts"] = {"work": {"config_dir": str(profile)}}
    path.write_text(yaml.safe_dump(data))
    return profile


def test_each_account_has_its_own_limits_and_warnings(project, usage, capsys):
    from vahtovik import cli
    work = _account(project)
    usage(session=82)
    usage(session=91, weekly=30, profile=work)
    state = _check(project)
    checks = project._lines(project.log_file.with_name("calls.jsonl.usage"))
    assert [c["config_dir"] for c in checks] == [str(project.tmp / "claude-home"), str(work)]
    deliveries = state.rows("SELECT subject, body FROM deliveries WHERE channel = 'chat' ORDER BY id")
    assert [r["subject"] for r in deliveries] == ["Claude usage", "Claude usage, account work"]
    assert deliveries[0]["body"].startswith("The 5-hour limit is at 82% (past 80%).")
    assert deliveries[1]["body"].startswith("The 5-hour limit is at 91% (past 90%).")
    usage(session=85)  # the default login's 80% was warned; the account's levels are its own
    usage(session=96, weekly=30, profile=work)
    assert [body[:37] for body in _warnings(_check(project))[2:]] == ["The 5-hour limit is at 96% (past 95%)"]
    cli.main(["status"])
    out = capsys.readouterr().out
    assert re.search(r"\nClaude usage at .*:\n  5-hour limit: 85%, .*\n  weekly limit for all models: 5%", out)
    assert re.search(r"\nClaude usage of account work at .*:\n  5-hour limit: 96%, ", out)


def test_a_failing_account_does_not_stop_the_other_checks(project, usage, caplog):
    work = _account(project)
    usage(raw={"exit": 1, "stderr": "not logged in"}, profile=work)
    usage(session=90)
    caplog.set_level(logging.INFO, logger="vahtovik")
    state = _check(project)
    assert [r.getMessage() for r in caplog.records if "Claude usage" in r.getMessage()][-1] == \
        "Failed checking Claude usage of account work: not logged in"
    assert _warnings(state)[0].startswith("The 5-hour limit is at 90% (past 90%).")
    assert usage_limits.failure(state, "work") == "not logged in" and usage_limits.failure(state) == ""
