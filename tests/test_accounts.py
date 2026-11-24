import json

import pytest
import yaml
from conftest import ROLE, artifact, give, monitor_answer, run_turns, turn_answer, work_log

from vahtovik import cli
from vahtovik import config as config_mod
from vahtovik.config import ConfigError
from vahtovik.stages import monitor
from vahtovik.tmux import Tmux
from vahtovik.workers import NEW_FOR_EXISTING, spec_for


def _edit_yaml(path, change) -> None:
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data))


def _accounts(project, account: str | None = "work") -> None:
    """Accounts `work` and `home` in vahtovik.yaml; role r1 and poller p1 use `account`."""
    _edit_yaml(project.root / "vahtovik.yaml", lambda d: d["claude"].update(accounts={
        "work": {"config_dir": str(project.tmp / "claude-work")}, "home": {"config_dir": "profiles/home"}}))
    project.role("r1", ROLE.replace("effort: xhigh", f"effort: xhigh\naccount: {account}") if account else ROLE)
    _edit_yaml(project.root / "pollers/p1/poller.yaml",
               lambda d: d.update(claude={"account": account}) if account else d.pop("claude", 0))


def test_a_role_and_a_poller_may_name_an_account(project):
    _accounts(project)
    cfg = config_mod.load(project.root)
    assert cfg.claude.accounts == {"work": project.tmp / "claude-work", "home": project.root / "profiles/home"}
    assert cfg.role("r1").account == "work" and cfg.poller("p1").account == "work"


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
    _edit_yaml(project.root / "pollers/p1/poller.yaml", lambda d: d.update(claude={"account": "work"}))
    with pytest.raises(ConfigError, match="unknown account 'work'; known: none"):
        config_mod.load(project.root)


def test_the_sessions_of_a_role_and_a_poller_use_their_account(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "a-token")
    _accounts(project)
    cfg, state, claude = project.load()
    project.queue(monitor_answer(), monitor_answer())
    monitor.run(cfg, state, claude, cfg.poller("p1"), "r1", project.tmp / "raw")
    give(project, artifact("a1", "g"))
    project.queue(turn_answer([work_log()]))
    run_turns(cfg, state, claude, Tmux(cfg), run_id="r1")
    calls = project.calls()
    assert len(calls) == 3
    for call in calls:
        assert call["env"]["CLAUDE_CONFIG_DIR"] == str(project.tmp / "claude-work")
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in call["env"]  # it would win over the account's login
        excludes = json.loads(call["args"][call["args"].index("--settings") + 1])["claudeMdExcludes"]
        assert str(project.tmp / "claude-work" / "CLAUDE.md") in excludes
    row = state.active_worker("r1", "g")
    assert claude.session_file(row["session_id"]).is_relative_to(project.tmp / "claude-work")

    _accounts(project, account=None)
    cfg, state, claude = project.load()
    spec, _ = spec_for(cfg, claude, state, state.worker(row["id"]))
    env = claude.env(spec)
    assert env["CLAUDE_CONFIG_DIR"] == str(cfg.claude.config_dir) and env["CLAUDE_CODE_OAUTH_TOKEN"] == "a-token"


def test_a_role_moved_to_another_account_starts_new_worker_sessions(project):
    give(project, artifact("a1", "g"))
    cfg, state, claude = project.load()
    project.queue(turn_answer([work_log()]))
    run_turns(cfg, state, claude, Tmux(cfg), run_id="r1")
    row = state.active_worker("r1", "g")
    spec, _ = spec_for(cfg, claude, state, row)
    assert (spec.mode, spec.session_id) == ("resume", row["session_id"])

    _accounts(project)
    cfg, state, claude = project.load()
    spec, note = spec_for(cfg, claude, state, state.worker(row["id"]))
    assert spec.mode == "new" and spec.session_id != row["session_id"] and note == NEW_FOR_EXISTING
    assert claude.session_file(row["session_id"]).is_relative_to(cfg.claude.config_dir)  # its costs still count


def test_trust_is_read_from_the_accounts_profile(project):
    _accounts(project)
    cfg, _, claude = project.load()
    config = project.tmp / "claude-work" / ".claude.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"projects": {str(cfg.root): {"hasTrustDialogAccepted": True}}}))
    assert claude.trusts(cfg.data_dir, "work") is True
    assert claude.trusts(cfg.data_dir) is None and claude.trusts(cfg.data_dir, "home") is None


def test_status_shows_the_accounts(project, capsys):
    _accounts(project)
    cli.main(["status", "-v"])
    out = "\n" + capsys.readouterr().out
    assert "\npoller p1: enabled, roles r1, account work, last monitored never" in out
    assert "\nrole r1: skills file, rss, account work, workers $0.00 in 24 hours" in out


def test_check_shows_each_login_and_the_problems_of_its_profile(project, capsys):
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
        f"worker sessions would stop at its \"trust this folder\" screen; run `CLAUDE_CONFIG_DIR={work} "
        f"{project.tmp / 'fake-claude'}` in {project.root} and accept",
    ]
