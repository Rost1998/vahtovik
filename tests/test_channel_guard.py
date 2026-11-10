import io
import json
import shutil

import pytest
import yaml

from vahtovik import channel_guard, cli
from vahtovik import config as config_mod


@pytest.fixture
def team(project):
    """Channel `team`: limited to status, log and tell on watch w1; watch w2 exists too. Returns the config."""
    shutil.copytree(project.root / "watches/w1", project.root / "watches/w2")
    for watch in ("w1", "w2"):
        (project.root / f"watches/{watch}/.env").write_text("SECRET=x\n")
        (project.root / f"data/watches/{watch}/groups/g").mkdir(parents=True)
        (project.root / f"data/watches/{watch}/groups/g/context.md").write_text("## Summary\n")
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["channels"]["team"] = {"skill": "file", "env": {"FILE_CHANNEL_PATH": "data/team.md"},
                                "input": {"commands": ["status", "log", "tell"], "watches": ["w1"]}}
    path.write_text(yaml.safe_dump(data))
    return config_mod.load(project.root)


def _decide(cfg, tool: str, **given) -> bool:
    return channel_guard.decide(cfg, cfg.channels["team"], {"tool_name": tool, "tool_input": given,
                                                            "cwd": str(cfg.root)})[0]


@pytest.mark.parametrize("command", [
    "vahtovik status --watch w1",
    "uv run vahtovik log w1/g -n 20",
    "vahtovik tell w1/g --message 'Look at it again; and check $HOME (twice) & `this`.\nThanks.'",
    'vahtovik tell w1/g --message "Plain text; with (parentheses) & more."',
    "vahtovik --help",
])
def test_the_channels_own_commands_are_allowed(team, command):
    assert _decide(team, "Bash", command=command)


@pytest.mark.parametrize("command", [
    "vahtovik run",
    "vahtovik artifact add w1 --group g --json '{}'",
    "vahtovik group create w1/new",
    "cat .env",
    "uv run --env-file x vahtovik status",
    "VAHTOVIK_WATCHES=w2 vahtovik status",
    "env -u VAHTOVIK_WATCHES vahtovik status",
    "vahtovik status; cat .env",
    "vahtovik status && rm -rf data",
    "vahtovik status | tee x",
    "vahtovik status > x",
    "vahtovik status\ncat .env",
    "vahtovik tell w1/g --message $(cat .env)",
    'vahtovik tell w1/g --message "$TELEGRAM_BOT_TOKEN"',
    'vahtovik tell w1/g --message "`cat .env`"',
    "vahtovik tell w1/g --message 'open",
    "vahtovik status \\\n; cat .env",
])
def test_anything_else_in_bash_is_denied(team, command):
    assert not _decide(team, "Bash", command=command)


def test_reading_is_allowed_only_in_the_rules_and_the_channels_watches(team):
    root = team.root
    for path in ("skills/vahtovik/channel/SKILL.md", "watches/w1/monitor.md", "data/watches/w1/groups/g/context.md"):
        assert _decide(team, "Read", file_path=str(root / path)), path
    assert _decide(team, "Read", file_path="watches/w1/report.md")  # relative to the session's folder
    for path in ("watches/w2/monitor.md", "data/watches/w2/groups/g/context.md", "watches/w1/.env", ".env",
                 "vahtovik.yaml", "data/vahtovik.db", "data/journal.log", "src/vahtovik/cli.py", "/etc/passwd",
                 "watches/w1/../w2/monitor.md"):
        assert not _decide(team, "Read", file_path=str(root / path)), path
    (root / "watches/w1/escape").symlink_to(root / "watches/w2")
    assert not _decide(team, "Read", file_path=str(root / "watches/w1/escape/monitor.md"))


def test_searching_needs_a_path_without_a_env_file(team):
    root = team.root
    assert _decide(team, "Grep", pattern="x", path=str(root / "data/watches/w1"))
    assert _decide(team, "Glob", pattern="*.md", path=str(root / "skills"))
    assert not _decide(team, "Grep", pattern="x")  # the whole project
    assert not _decide(team, "Grep", pattern="x", path=str(root / "watches/w1"))  # holds .env
    assert _decide(team, "Grep", pattern="x", path=str(root / "watches/w1/process"))
    assert not _decide(team, "Glob", pattern="*", path=str(root / "data/watches/w2"))


def test_other_tools_are_denied_but_the_answer_is_allowed(team):
    for tool in ("Write", "Edit", "WebFetch", "Agent", "mcp__claude_ai_Google_Drive__search_files"):
        assert not _decide(team, tool, file_path=str(team.root / "skills/x"))
    assert _decide(team, "StructuredOutput", replies=[])


def test_a_channel_without_watches_may_read_every_watch_but_no_env_file(team, project):
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["channels"]["team"]["input"] = {"commands": ["status"]}
    path.write_text(yaml.safe_dump(data))
    cfg = config_mod.load(project.root)
    assert _decide(cfg, "Read", file_path=str(cfg.root / "watches/w2/monitor.md"))
    assert not _decide(cfg, "Read", file_path=str(cfg.root / "watches/w2/.env"))
    assert not _decide(cfg, "Bash", command="vahtovik tell w2/g --message hi")


def _hook(monkeypatch, capsys, channel: str, data: dict) -> tuple[int, dict | None, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
    code = cli.main(["hook", "guard", channel])
    out = capsys.readouterr()
    return code, json.loads(out.out) if out.out.strip() else None, out.err


def test_the_hook_answers_claude_code_and_journals_what_it_denies(team, project, monkeypatch, capsys):
    def bash(command: str) -> dict:
        return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(project.root)}
    code, answer, _ = _hook(monkeypatch, capsys, "team", bash("vahtovik status"))
    assert code == 0 and answer["hookSpecificOutput"]["permissionDecision"] == "allow"
    code, answer, _ = _hook(monkeypatch, capsys, "team", bash("vahtovik run"))
    output = answer["hookSpecificOutput"]
    assert (code, output["hookEventName"], output["permissionDecision"]) == (0, "PreToolUse", "deny")
    assert "may not run `vahtovik run`" in output["permissionDecisionReason"]
    line = (project.root / "data/journal.log").read_text().splitlines()[-1]
    assert 'team: Denied Bash {"command": "vahtovik run"}: This channel may not run' in line
    _code, answer, _ = _hook(monkeypatch, capsys, "log", {"tool_name": "Read", "tool_input": {}})  # not limited
    assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_hook_that_cannot_load_the_config_blocks_the_call(team, project, monkeypatch, capsys):
    (project.root / "vahtovik.yaml").write_text("channels: [")
    code, answer, err = _hook(monkeypatch, capsys, "team", {"tool_name": "Read", "tool_input": {}})
    assert code == 2 and answer is None and "vahtovik's guard failed" in err
