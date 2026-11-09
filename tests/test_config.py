from datetime import timedelta

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


def test_an_unknown_scan_mode_is_rejected(project):
    path = project.root / "watches/w1/watch.yaml"
    data = yaml.safe_load(path.read_text())
    assert config_mod.load(project.root).watch("w1").sources[0].scan == "ai"
    data["sources"][0]["scan"] = "magic"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="scan of source 'src-a' must be one of ai, script"):
        config_mod.load(project.root)
