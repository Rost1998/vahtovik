import os
from datetime import datetime, time, timedelta

import pytest
import yaml
from conftest import ROLE

from vahtovik import config as config_mod
from vahtovik.config import ConfigError


def _edit_yaml(path, change) -> None:
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data))


def _poller(project):
    return project.root / "pollers/p1/poller.yaml"


def test_loads_the_project_with_its_role_and_poller(project):
    cfg = config_mod.load(project.root)
    poller, role = cfg.poller("p1"), cfg.role("r1")
    assert [s.id for s in poller.sources] == ["src-a", "src-b"] and poller.roles == ["r1"]
    assert poller.interval == timedelta(hours=1) and poller.enabled
    assert (role.description, role.skills, role.body) == ("Follows one product.", ["file", "rss"],
                                                          "Assess each item.\nReport everything with the file skill.")
    worker = cfg.stage("worker", role)
    assert (worker.model, worker.effort, worker.max_budget_usd) == ("opus", "xhigh", 1.0)
    assert cfg.stage("monitor", poller).effort == "low"


def test_sessions_get_the_newest_opus_and_high_effort_unless_set(project):
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: d["claude"].pop("stages"))
    project.role("r1", ROLE.replace("effort: xhigh\n", ""))
    cfg = config_mod.load(project.root)
    for owner, stage in ((cfg.poller("p1"), "monitor"), (cfg.role("r1"), "worker"), (None, "watchdog")):
        settings = cfg.stage(stage, owner)
        assert (settings.model, settings.effort) == ("opus", "high")


def test_a_role_and_a_poller_set_the_model_and_effort_of_their_sessions(project):
    _edit_yaml(project.root / "vahtovik.yaml",
               lambda d: d["claude"]["stages"].update(watchdog={"model": "sonnet", "effort": "medium"}))
    project.role("r1", ROLE.replace("effort: xhigh", "model: claude-fable-5-1\neffort: medium"))
    _edit_yaml(_poller(project), lambda d: d.update(claude={"model": "haiku", "effort": "max"}))
    cfg = config_mod.load(project.root)
    monitor = cfg.stage("monitor", cfg.poller("p1"))
    worker = cfg.stage("worker", cfg.role("r1"))
    watchdog = cfg.stage("watchdog")
    assert (monitor.model, monitor.effort) == ("haiku", "max")  # wins over vahtovik.yaml's stage
    assert (worker.model, worker.effort, worker.max_budget_usd) == ("claude-fable-5-1", "medium", 1.0)
    assert (watchdog.model, watchdog.effort) == ("sonnet", "medium")  # belongs to no role


def test_a_workers_own_model_and_effort_win_over_its_roles(project):
    project.role("r1", ROLE.replace("effort: xhigh", "model: opus\neffort: medium"))
    cfg = config_mod.load(project.root)
    both = cfg.stage("worker", cfg.role("r1"), "fable", "max")
    one = cfg.stage("worker", cfg.role("r1"), effort="low")
    assert (both.model, both.effort, both.max_budget_usd) == ("fable", "max", 1.0)
    assert (one.model, one.effort) == ("opus", "low")  # the role's model stays


@pytest.mark.parametrize("where, change", [
    ("vahtovik", lambda d: d["claude"].update(effort="hihg")),
    ("vahtovik", lambda d: d["claude"]["stages"]["monitor"].update(effort="hihg")),
    ("poller", lambda d: d.update(claude={"effort": "hihg"})),
])
def test_an_unknown_effort_is_a_config_error(project, where, change):
    _edit_yaml(project.root / "vahtovik.yaml" if where == "vahtovik" else _poller(project), change)
    with pytest.raises(ConfigError, match="unknown effort 'hihg'"):
        config_mod.load(project.root)


@pytest.mark.parametrize("text, error", [
    (ROLE.replace("effort: xhigh", "effort: hihg"), "unknown effort 'hihg'"),
    (ROLE.replace("effort: xhigh", "recheck: 1d"), "unknown front matter recheck"),
    (ROLE.replace("description:", "name: other\ndescription:"), "names it 'other'; use the folder name"),
    (ROLE.replace("effort: xhigh", "account: nobody"), "unknown account 'nobody'"),
    (ROLE.replace("skills: [file, rss]", "skills: file"), "skills must be a list of names"),
])
def test_a_bad_role_is_a_config_error(project, text, error):
    project.role("r1", text)
    with pytest.raises(ConfigError, match=error):
        config_mod.load(project.root)


def test_names_of_roles_and_pollers_are_lowercase_words(project):
    project.role("Bad_Role")
    with pytest.raises(ConfigError, match="the role name 'Bad_Role' must be lowercase"):
        config_mod.load(project.root)


@pytest.mark.parametrize("change, error", [
    (lambda d: d.update(roles=["nope"]), "unknown role nope"),
    (lambda d: d.pop("roles"), "give roles"),
    (lambda d: d["sources"][1].update(id="src-a"), "unique"),
    (lambda d: d.update(claude={"group": {}}), "give only model, effort and account"),
])
def test_a_bad_poller_is_a_config_error(project, change, error):
    _edit_yaml(_poller(project), change)
    with pytest.raises(ConfigError, match=error):
        config_mod.load(project.root)


def test_tmux_server_is_vahtoviks_own_unless_set_to_null(project):
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: d.pop("tmux"))
    assert config_mod.load(project.root).tmux.socket == "vahtovik"
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: d.update(tmux={"socket": None}))
    assert config_mod.load(project.root).tmux.socket is None


def test_notices_go_to_full_input_channels_unless_set(project):
    assert config_mod.load(project.root).notice_channels() == ["log"]
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: (d.pop("notices"), d["channels"]["other"].update(
        input={}), d["channels"]["slow"].update(input={"commands": ["status"]})))
    assert config_mod.load(project.root).notice_channels() == ["other"]  # slow is limited
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: d.update(notices=["nope"]))
    with pytest.raises(ConfigError, match="notices: unknown channel nope"):
        config_mod.load(project.root)


def test_a_channel_names_its_credentials(project):
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: d["channels"]["log"].update(credentials=["ops-bot"]))
    assert config_mod.load(project.root).channels["log"].credentials == ["ops-bot"]


def test_bad_auth_mode_is_rejected(project):
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: d["claude"].update(auth="password"))
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
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: d["claude"].update(parallel_turns=0))
    with pytest.raises(ConfigError, match="claude.parallel_turns must be a whole number, 1 or more"):
        config_mod.load(project.root)


def test_watchdog_timing_settings(project):
    assert config_mod.load(project.root).watchdog.every == timedelta(minutes=5)
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: d.update(watchdog={"every": "1m", "notify_every": "2h"}))
    settings = config_mod.load(project.root).watchdog
    assert settings.notify_every == timedelta(hours=2) and settings.every == timedelta(minutes=1)


def test_usage_settings_have_defaults_and_can_be_changed(project):
    settings = config_mod.load(project.root).usage
    assert (settings.every, settings.warn_at) == (timedelta(minutes=10), [80, 90, 95, 99])
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: d.update(usage={"every": 0, "warn_at": [95, 50, 95]}))
    settings = config_mod.load(project.root).usage
    assert (settings.every, settings.warn_at) == (timedelta(0), [50, 95])


@pytest.mark.parametrize("usage, error", [
    ({"warn_at": [80, 101]}, "usage.warn_at must be a list of whole percents"),
    ({"warn_at": ["high"]}, "usage.warn_at must be a list of whole percents"),
    ({"every": "soon"}, "usage.every: bad interval"),
])
def test_bad_usage_settings_are_config_errors(project, usage, error):
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: d.update(usage=usage))
    with pytest.raises(ConfigError, match=error):
        config_mod.load(project.root)


def test_a_source_has_the_pollers_interval_unless_it_sets_its_own(project):
    _edit_yaml(_poller(project), lambda d: d["sources"][1].update(interval="1m"))
    a, b = config_mod.load(project.root).poller("p1").sources
    assert (a.interval, b.interval) == (timedelta(hours=1), timedelta(minutes=1))


def test_a_poller_or_a_source_may_set_times_of_the_day(project):
    def change(data):
        del data["interval"]
        data["at"] = ["20:00", "08:30", "20:00"]
        data["sources"][1]["interval"] = "10m"
    _edit_yaml(_poller(project), change)
    poller = config_mod.load(project.root).poller("p1")
    a, b = poller.sources
    assert poller.at == a.at == [time(8, 30), time(20, 0)]
    assert (b.at, b.interval) == ([], timedelta(minutes=10))  # the source's own interval wins over its poller's at


def test_unquoted_times_are_read_as_times(project):
    path = _poller(project)
    path.write_text(path.read_text().replace("interval: 1h", "at: [8:00, 20:00, 07:15]"))
    assert config_mod.load(project.root).poller("p1").at == [time(7, 15), time(8, 0), time(20, 0)]
    path.write_text(path.read_text().replace("at: [8:00, 20:00, 07:15]", "at: 9:30"))
    assert config_mod.load(project.root).poller("p1").at == [time(9, 30)]


@pytest.mark.parametrize("change, error", [
    (lambda d: d.update(at="08:00"), "set interval or at, not both"),
    (lambda d: d["sources"][0].update(at="8 am"), "source 'src-a': bad time '8 am': use HH:MM"),
    (lambda d: d["sources"][0].update(at="24:00"), "bad time '24:00'"),
    (lambda d: d.update(interval=None, at=[]), "at: give one time of the day or more"),
])
def test_bad_times_are_rejected(project, change, error):
    _edit_yaml(_poller(project), change)
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
    assert config_mod.load(project.root).poller("p1").sources[0].scan == "ai"
    _edit_yaml(_poller(project), lambda d: d["sources"][0].update(scan="magic"))
    with pytest.raises(ConfigError, match="source 'src-a': scan must be one of ai, script"):
        config_mod.load(project.root)


def test_a_channel_with_input_is_read_every_minute_unless_set(project):
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: (d["channels"]["log"].update(input={}),
                                                          d["channels"]["other"].update(input={"interval": "5m"})))
    channels = config_mod.load(project.root).channels
    assert channels["log"].input.interval == timedelta(minutes=1)
    assert channels["other"].input.interval == timedelta(minutes=5) and channels["broken"].input is None
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: d["channels"]["log"].update(input=True))
    with pytest.raises(ConfigError, match="input must be a mapping"):
        config_mod.load(project.root)


def test_a_channel_starts_a_new_session_after_a_quiet_hour_unless_set(project):
    path = project.root / "vahtovik.yaml"
    _edit_yaml(path, lambda d: (d["channels"]["log"].update(input={}),
                                d["channels"]["other"].update(input={"new_session_after": 0, "history": 0})))
    channels = config_mod.load(project.root).channels
    assert (channels["log"].input.new_session_after, channels["log"].input.history) == (timedelta(hours=1), 5)
    assert (channels["other"].input.new_session_after, channels["other"].input.history) == (timedelta(0), 0)
    for settings, problem in (({"new_session_after": "soon"}, "input.new_session_after: bad interval 'soon'"),
                              ({"history": -1}, "input.history must be a whole number, 0 or more")):
        _edit_yaml(path, lambda d, s=settings: d["channels"]["log"].update(input=s))
        with pytest.raises(ConfigError, match=problem):
            config_mod.load(project.root)


def test_input_sessions_get_opus_and_xhigh_unless_set(project):
    cfg = config_mod.load(project.root)
    assert cfg.stage("input") == config_mod.StageSettings("opus", "xhigh")
    path = project.root / "vahtovik.yaml"
    _edit_yaml(path, lambda d: d["claude"].update(model="sonnet", effort="low"))
    assert config_mod.load(project.root).stage("input") == config_mod.StageSettings("opus", "xhigh")
    _edit_yaml(path, lambda d: d["claude"]["stages"].update(input={"effort": "max"}))
    assert config_mod.load(project.root).stage("input") == config_mod.StageSettings("opus", "max")


def _input(project, **settings) -> None:
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: d["channels"]["log"].update(input=settings))


def test_an_input_channel_has_full_power_unless_it_sets_commands_or_roles(project):
    _input(project, senders=["@alice", 12345], rules="  Answer in Ukrainian.\n")
    settings = config_mod.load(project.root).channels["log"].input
    assert not settings.limited and settings.commands is None and settings.roles is None
    assert settings.senders == ["@alice", "12345"] and settings.rules == "Answer in Ukrainian."
    _input(project, commands=["status", "log"])
    settings = config_mod.load(project.root).channels["log"].input
    assert settings.limited and settings.commands == ["status", "log"] and settings.roles is None
    _input(project, roles=["r1"])
    settings = config_mod.load(project.root).channels["log"].input
    assert settings.limited and settings.commands == list(config_mod.INPUT_COMMANDS) and settings.roles == ["r1"]


@pytest.mark.parametrize("settings, error", [
    ({"commands": ["status", "run"]}, "unknown command run; commands: status, log, tell, artifact, worker"),
    ({"commands": "status"}, "input.commands must be a list of names"),
    ({"roles": ["r1", "nope"]}, "input.roles: unknown role nope"),
    ({"senders": [True]}, "input.senders must be a list of names"),
    ({"rules": ["a"]}, "input.rules must be text"),
])
def test_bad_input_limits_are_config_errors(project, settings, error):
    _input(project, **settings)
    with pytest.raises(ConfigError, match=error):
        config_mod.load(project.root)


def test_vahtovik_roles_in_the_environment_hides_the_other_roles(project, monkeypatch):
    project.role("r2")
    _input(project, roles=["r2"])  # checked against all roles, whatever the environment says
    monkeypatch.setenv("VAHTOVIK_ROLES", "r1")
    cfg = config_mod.load(project.root)
    assert list(cfg.roles) == ["r1"] and cfg.scope == ["r1"] and list(cfg.pollers) == ["p1"]
    with pytest.raises(ConfigError, match="unknown role 'r2'; known: r1"):
        cfg.role("r2")
    assert "VAHTOVIK_ROLES" not in os.environ  # what the command starts sees all roles
    assert list(config_mod.load(project.root).roles) == ["r1", "r2"]
    monkeypatch.setenv("VAHTOVIK_ROLES", "r2")
    cfg = config_mod.load(project.root)
    assert list(cfg.roles) == ["r2"] and cfg.pollers == {}  # p1's role is r1
