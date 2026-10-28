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
    assert (group.model, group.effort, group.max_budget_usd) == ("claude-opus-5-5", "xhigh", 1.0)
    assert cfg.stage(watch, "monitor").effort == "low"



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
    data["watchdog"] = {"every": "1m", "check_every": "5m", "notify_every": "2h"}
    path.write_text(yaml.safe_dump(data))
    assert config_mod.load(project.root).watchdog.notify_every == timedelta(hours=2)
