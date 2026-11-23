import json

import pytest
import yaml
from conftest import artifact, monitor_answer, run_turns, turn_answer, work_log

from vahtovik import artifacts
from vahtovik import config as config_mod
from vahtovik.config import ConfigError
from vahtovik.group_session import NEW_FOR_EXISTING, spec_for
from vahtovik.stages import monitor
from vahtovik.tmux import Tmux


def _edit_yaml(path, change) -> None:
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data))


def _accounts(project, watch_account: str | None = "work") -> None:
    """Accounts `work` and `home` in vahtovik.yaml; w1 uses watch_account."""
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: d["claude"].update(accounts={
        "work": {"config_dir": str(project.tmp / "claude-work")}, "home": {"config_dir": "profiles/home"}}))
    _edit_yaml(project.root / "watches/w1/watch.yaml",
               lambda d: d["claude"].update(account=watch_account) if watch_account else d["claude"].pop("account", 0))


def test_a_watch_may_name_an_account(project):
    _accounts(project)
    cfg = config_mod.load(project.root)
    assert cfg.claude.accounts == {"work": project.tmp / "claude-work", "home": project.root / "profiles/home"}
    assert cfg.watch("w1").account == "work"
    assert set(cfg.watch("w1").claude) == {"group"}  # account is no stage


@pytest.mark.parametrize("change, error", [
    (lambda d: d["claude"].update(accounts={"work": {}}), "account 'work' needs a config_dir"),
    (lambda d: d["claude"].update(accounts=["work"]), "claude.accounts must be a mapping"),
    (lambda d: d["claude"].update(auth="api-key", accounts={"work": {"config_dir": "/x"}}),
     "claude.accounts work only with auth: subscription"),
])
def test_bad_accounts_are_config_errors(project, change, error):
    _edit_yaml(project.root / "vahtovik.yaml", change)
    with pytest.raises(ConfigError, match=error):
        config_mod.load(project.root)


def test_an_unknown_account_is_a_config_error(project):
    _edit_yaml(project.root / "watches/w1/watch.yaml", lambda d: d["claude"].update(account="work"))
    with pytest.raises(ConfigError, match="unknown account 'work'; known: none"):
        config_mod.load(project.root)


def test_a_watchs_sessions_use_its_account_and_other_sessions_the_default_login(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "a-token")
    _accounts(project)
    cfg, state, claude = project.load()
    project.queue(monitor_answer(), monitor_answer())
    monitor.run(cfg, state, claude, cfg.watch("w1"), "r1", project.tmp / "raw")
    artifacts.register(cfg, state, cfg.watch("w1"), artifact("a1", "g"), "src-a")
    project.queue(turn_answer([work_log()]))
    run_turns(cfg, state, claude, Tmux(cfg), run_id="r1")
    calls = project.calls()
    assert len(calls) == 3
    for call in calls:
        assert call["env"]["CLAUDE_CONFIG_DIR"] == str(project.tmp / "claude-work")
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in call["env"]  # it would win over the account's login
        excludes = json.loads(call["args"][call["args"].index("--settings") + 1])["claudeMdExcludes"]
        assert str(project.tmp / "claude-work" / "CLAUDE.md") in excludes
    session_id = state.group("w1", "g")["session_id"]
    assert claude.session_file(session_id).is_relative_to(project.tmp / "claude-work")

    _accounts(project, watch_account=None)
    cfg, state, claude = project.load()
    spec, _ = spec_for(cfg, claude, state, cfg.watch("w1"), "g")
    env = claude.env(spec)
    assert env["CLAUDE_CONFIG_DIR"] == str(cfg.claude.config_dir) and env["CLAUDE_CODE_OAUTH_TOKEN"] == "a-token"


def test_a_watch_moved_to_another_account_starts_new_group_sessions(project):
    cfg, state, claude = project.load()
    artifacts.register(cfg, state, cfg.watch("w1"), artifact("a1", "g"), "src-a")
    project.queue(turn_answer([work_log()]))
    run_turns(cfg, state, claude, Tmux(cfg), run_id="r1")
    session_id = state.group("w1", "g")["session_id"]
    spec, _ = spec_for(cfg, claude, state, cfg.watch("w1"), "g")
    assert (spec.mode, spec.session_id) == ("resume", session_id)

    _accounts(project)
    cfg, state, claude = project.load()
    spec, note = spec_for(cfg, claude, state, cfg.watch("w1"), "g")
    assert spec.mode == "new" and spec.session_id != session_id and note == NEW_FOR_EXISTING
    assert claude.session_file(session_id).is_relative_to(cfg.claude.config_dir)  # its costs still count


def test_trust_is_read_from_the_accounts_profile(project):
    _accounts(project)
    cfg, _, claude = project.load()
    config = project.tmp / "claude-work" / ".claude.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"projects": {str(cfg.root): {"hasTrustDialogAccepted": True}}}))
    assert claude.trusts(cfg.data_dir, "work") is True
    assert claude.trusts(cfg.data_dir) is None and claude.trusts(cfg.data_dir, "home") is None


def test_status_shows_a_watchs_account(project, capsys):
    from vahtovik import cli
    _accounts(project)
    cli.main(["status", "-v"])
    assert "\nw1: enabled, account work, last monitored never, " in "\n" + capsys.readouterr().out


def test_check_shows_each_login_and_the_problems_of_its_profile(project, capsys):
    from vahtovik import cli
    _accounts(project)
    work, home = project.tmp / "claude-work", project.root / "profiles/home"
    home.mkdir(parents=True)
    (home / "fake-auth.json").write_text(json.dumps({"loggedIn": False, "authMethod": "none"}))
    work.mkdir()
    (work / ".claude.json").write_text(json.dumps({"projects": {str(project.root): {"hasTrustDialogAccepted": False}}}))
    assert cli.main(["check"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert "claude default login: logged in as person@example.com (max), login ends 2027-01-20" in lines
    assert "claude account work: logged in as person@example.com (max), login ends 2027-01-20" in lines
    assert [line for line in lines if line.startswith("problem:")] == [  # safe_dump sorted the accounts
        f"problem: claude account home: not logged in, in {home}; run `CLAUDE_CONFIG_DIR={home} "
        f"{project.tmp / 'fake-claude'}` in {project.root}, log in, and accept the trust screen",
        f"problem: claude account work: Claude Code does not trust {project.root / 'data'} or a folder above it, so "
        f"group sessions would stop at its \"trust this folder\" screen; run `CLAUDE_CONFIG_DIR={work} "
        f"{project.tmp / 'fake-claude'}` in {project.root} and accept",
    ]
