import os
import shutil
from datetime import datetime, time, timedelta

import pytest
import yaml

from vahtovik import config as config_mod
from vahtovik.config import ConfigError


def test_loads_project_and_merges_stage_settings(project):
    cfg = config_mod.load(project.root)
    watch = cfg.watch("w1")
    assert [s.id for s in watch.sources] == ["src-a", "src-b"]
    assert watch.interval == timedelta(hours=1)
    group = cfg.stage(watch, "group")
    assert (group.model, group.effort, group.max_budget_usd) == ("opus", "xhigh", 1.0)
    assert cfg.stage(watch, "monitor").effort == "low"


def _edit_yaml(path, change) -> None:
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data))


def test_sessions_get_the_newest_opus_and_high_effort_unless_set(project):
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: d["claude"].pop("stages"))
    _edit_yaml(project.root / "watches/w1/watch.yaml", lambda d: d.pop("claude"))
    cfg = config_mod.load(project.root)
    for watch, stage in ((cfg.watch("w1"), "monitor"), (cfg.watch("w1"), "group"), (None, "watchdog")):
        settings = cfg.stage(watch, stage)
        assert (settings.model, settings.effort) == ("opus", "high")


def test_a_watchs_model_and_effort_reach_all_its_sessions(project):
    _edit_yaml(project.root / "vahtovik.yaml",
               lambda d: d["claude"]["stages"].update(watchdog={"model": "sonnet", "effort": "medium"}))
    _edit_yaml(project.root / "watches/w1/watch.yaml",
               lambda d: d["claude"].update(model="claude-fable-5-1", effort="medium"))
    cfg = config_mod.load(project.root)
    watch = cfg.watch("w1")
    monitor, group, watchdog = cfg.stage(watch, "monitor"), cfg.stage(watch, "group"), cfg.stage(None, "watchdog")
    assert (monitor.model, monitor.effort) == ("claude-fable-5-1", "medium")  # wins over vahtovik.yaml's stage
    assert (group.model, group.effort, group.max_budget_usd) == ("claude-fable-5-1", "xhigh", 1.0)  # the watch's stage wins
    assert (watchdog.model, watchdog.effort) == ("sonnet", "medium")  # belongs to no watch


@pytest.mark.parametrize("file, change", [
    ("vahtovik.yaml", lambda d: d["claude"].update(effort="hihg")),
    ("vahtovik.yaml", lambda d: d["claude"]["stages"]["monitor"].update(effort="hihg")),
    ("watches/w1/watch.yaml", lambda d: d["claude"].update(effort="hihg")),
    ("watches/w1/watch.yaml", lambda d: d["claude"]["group"].update(effort="hihg")),
])
def test_an_unknown_effort_is_a_config_error(project, file, change):
    _edit_yaml(project.root / file, change)
    with pytest.raises(ConfigError, match="unknown effort 'hihg'"):
        config_mod.load(project.root)



def test_tmux_server_is_vahtoviks_own_unless_set_to_null(project):
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    del data["tmux"]
    path.write_text(yaml.safe_dump(data))
    assert config_mod.load(project.root).tmux.socket == "vahtovik"
    data["tmux"] = {"socket": None}
    path.write_text(yaml.safe_dump(data))
    assert config_mod.load(project.root).tmux.socket is None


def test_watch_with_unknown_channel_is_rejected(project):
    path = project.root / "watches/w1/watch.yaml"
    data = yaml.safe_load(path.read_text())
    data["channels"] = ["nope"]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="unknown channel nope"):
        config_mod.load(project.root)


def test_duplicate_source_ids_are_rejected(project):
    path = project.root / "watches/w1/watch.yaml"
    data = yaml.safe_load(path.read_text())
    data["sources"][1]["id"] = "src-a"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="unique"):
        config_mod.load(project.root)


def test_bad_auth_mode_is_rejected(project):
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["claude"]["auth"] = "password"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="claude.auth"):
        config_mod.load(project.root)


@pytest.mark.parametrize("value, expected", [("30m", timedelta(minutes=30)), ("2d", timedelta(days=2)), ("45 s", timedelta(seconds=45))])
def test_parse_interval(value, expected):
    assert config_mod.parse_interval(value) == expected


def test_parse_interval_rejects_garbage():
    with pytest.raises(ConfigError):
        config_mod.parse_interval("hourly")


def test_parallel_turns_is_5_unless_set(project):
    assert config_mod.load(project.root).claude.parallel_turns == 5
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["claude"]["parallel_turns"] = 0
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="claude.parallel_turns must be a whole number, 1 or more"):
        config_mod.load(project.root)


def test_old_watchdog_timing_settings_are_ignored(project):
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    assert config_mod.load(project.root).watchdog.every == timedelta(minutes=5)
    data["watchdog"] = {"every": "1m", "check_every": "5m", "notify_every": "2h"}
    path.write_text(yaml.safe_dump(data))
    settings = config_mod.load(project.root).watchdog
    assert settings.notify_every == timedelta(hours=2) and settings.every == timedelta(minutes=1)


def test_a_source_has_the_watchs_interval_unless_it_sets_its_own(project):
    path = project.root / "watches/w1/watch.yaml"
    data = yaml.safe_load(path.read_text())
    data["sources"][1]["interval"] = "1m"
    path.write_text(yaml.safe_dump(data))
    a, b = config_mod.load(project.root).watch("w1").sources
    assert (a.interval, b.interval) == (timedelta(hours=1), timedelta(minutes=1))


def test_a_watch_or_a_source_may_set_times_of_the_day(project):
    path = project.root / "watches/w1/watch.yaml"
    data = yaml.safe_load(path.read_text())
    del data["interval"]
    data["at"] = ["20:00", "08:30", "20:00"]
    data["sources"][1]["interval"] = "10m"
    path.write_text(yaml.safe_dump(data))
    watch = config_mod.load(project.root).watch("w1")
    a, b = watch.sources
    assert watch.at == a.at == [time(8, 30), time(20, 0)]
    assert (b.at, b.interval) == ([], timedelta(minutes=10))  # the source's own interval wins over its watch's at


def test_unquoted_times_are_read_as_times(project):
    path = project.root / "watches/w1/watch.yaml"
    path.write_text(path.read_text().replace("interval: 1h", "at: [8:00, 20:00, 07:15]"))
    assert config_mod.load(project.root).watch("w1").at == [time(7, 15), time(8, 0), time(20, 0)]
    path.write_text(path.read_text().replace("at: [8:00, 20:00, 07:15]", "at: 9:30"))
    assert config_mod.load(project.root).watch("w1").at == [time(9, 30)]


@pytest.mark.parametrize("change, error", [
    (lambda d: d.update(at="08:00"), "set interval or at, not both"),
    (lambda d: d["sources"][0].update(at="8 am"), "source 'src-a': bad time '8 am': use HH:MM"),
    (lambda d: d["sources"][0].update(at="24:00"), "bad time '24:00'"),
    (lambda d: d.update(interval=None, at=[]), "at: give one time of the day or more"),
])
def test_bad_times_are_rejected(project, change, error):
    _edit_yaml(project.root / "watches/w1/watch.yaml", change)
    with pytest.raises(ConfigError, match=error):
        config_mod.load(project.root)


@pytest.mark.parametrize("last, expected", [
    (datetime(2026, 11, 11, 7, 0), datetime(2026, 11, 11, 8, 0)),
    (datetime(2026, 11, 11, 8, 0), datetime(2026, 11, 11, 20, 0)),  # a run at 08:00 has done the 08:00 one
    (datetime(2026, 11, 11, 12, 0), datetime(2026, 11, 11, 20, 0)),
    (datetime(2026, 11, 11, 21, 0), datetime(2026, 11, 12, 8, 0)),
])
def test_a_source_with_times_is_due_at_the_first_one_after_its_last_monitoring(last, expected):
    source = config_mod.Source("s", "rss", at=[time(8, 0), time(20, 0)])
    assert source.next_run(last.astimezone()).astimezone().replace(tzinfo=None) == expected


def test_an_unknown_scan_mode_is_rejected(project):
    path = project.root / "watches/w1/watch.yaml"
    data = yaml.safe_load(path.read_text())
    assert config_mod.load(project.root).watch("w1").sources[0].scan == "ai"
    data["sources"][0]["scan"] = "magic"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="scan of source 'src-a' must be one of ai, script"):
        config_mod.load(project.root)


def test_a_channel_with_input_is_read_every_minute_unless_set(project):
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["channels"]["log"]["input"] = {}
    data["channels"]["other"]["input"] = {"interval": "5m"}
    path.write_text(yaml.safe_dump(data))
    channels = config_mod.load(project.root).channels
    assert channels["log"].input.interval == timedelta(minutes=1)
    assert channels["other"].input.interval == timedelta(minutes=5) and channels["broken"].input is None
    data["channels"]["log"]["input"] = True
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="input must be a mapping"):
        config_mod.load(project.root)


def test_input_sessions_get_opus_and_xhigh_unless_set(project):
    cfg = config_mod.load(project.root)
    assert cfg.stage(None, "input") == config_mod.StageSettings("opus", "xhigh")
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["claude"].update(model="sonnet", effort="low")
    path.write_text(yaml.safe_dump(data))
    assert config_mod.load(project.root).stage(None, "input") == config_mod.StageSettings("opus", "xhigh")
    data["claude"]["stages"]["input"] = {"effort": "max"}
    path.write_text(yaml.safe_dump(data))
    assert config_mod.load(project.root).stage(None, "input") == config_mod.StageSettings("opus", "max")


def _input(project, **settings) -> None:
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: d["channels"]["log"].update(input=settings))


def test_an_input_channel_has_full_power_unless_it_sets_commands_or_watches(project):
    _input(project, senders=["@alice", 12345], rules="  Answer in Ukrainian.\n")
    settings = config_mod.load(project.root).channels["log"].input
    assert not settings.limited and settings.commands is None and settings.watches is None
    assert settings.senders == ["@alice", "12345"] and settings.rules == "Answer in Ukrainian."
    _input(project, commands=["status", "log"])
    settings = config_mod.load(project.root).channels["log"].input
    assert settings.limited and settings.commands == ["status", "log"] and settings.watches is None
    _input(project, watches=["w1"])
    settings = config_mod.load(project.root).channels["log"].input
    assert settings.limited and settings.commands == list(config_mod.INPUT_COMMANDS) and settings.watches == ["w1"]


@pytest.mark.parametrize("settings, error", [
    ({"commands": ["status", "run"]}, "unknown command run; commands: status, log, tell, artifact, group"),
    ({"commands": "status"}, "input.commands must be a list of names"),
    ({"watches": ["w1", "nope"]}, "input.watches: unknown watch nope"),
    ({"senders": [True]}, "input.senders must be a list of names"),
    ({"rules": ["a"]}, "input.rules must be text"),
])
def test_bad_input_limits_are_config_errors(project, settings, error):
    _input(project, **settings)
    with pytest.raises(ConfigError, match=error):
        config_mod.load(project.root)


def test_vahtovik_watches_in_the_environment_hides_the_other_watches(project, monkeypatch):
    shutil.copytree(project.root / "watches/w1", project.root / "watches/w2")
    _input(project, watches=["w2"])  # checked against all watches, whatever the environment says
    monkeypatch.setenv("VAHTOVIK_WATCHES", "w1")
    cfg = config_mod.load(project.root)
    assert list(cfg.watches) == ["w1"] and cfg.scope == ["w1"]
    with pytest.raises(ConfigError, match="unknown watch 'w2'; known: w1"):
        cfg.watch("w2")
    assert "VAHTOVIK_WATCHES" not in os.environ  # what the command starts sees all watches
    assert list(config_mod.load(project.root).watches) == ["w1", "w2"]
    monkeypatch.setenv("VAHTOVIK_WATCHES", "")
    cfg = config_mod.load(project.root)
    assert cfg.watches == {} and cfg.scope == []
