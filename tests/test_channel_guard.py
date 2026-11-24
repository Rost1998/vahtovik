import io
import json

import pytest
import yaml
from conftest import ROLE

from vahtovik import channel_guard, cli
from vahtovik import config as config_mod
from vahtovik.util import schema_path


@pytest.fixture
def team(project):
    """Channel `team`: limited to status, log and tell on role r1; role r2 exists too. Returns the config."""
    project.role("r2", ROLE)
    project.credentials("feed", "SECRET=x\n")
    for role in ("r1", "r2"):
        folder = project.root / f"data/workers/{role}/a3f9c2-g"
        folder.mkdir(parents=True)
        (folder / "context.md").write_text("## Summary\n")
        (folder / ".env").write_text("SECRET=x\n")
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["channels"]["team"] = {"skill": "file", "env": {"FILE_CHANNEL_PATH": "data/team.md"},
                                "input": {"commands": ["status", "log", "tell"], "roles": ["r1"]}}
    path.write_text(yaml.safe_dump(data))
    return config_mod.load(project.root)


def _decide(cfg, tool: str, **given) -> bool:
    return channel_guard.decide(cfg, cfg.channels["team"], {"tool_name": tool, "tool_input": given,
                                                            "cwd": str(cfg.root)})[0]


@pytest.mark.parametrize("command", [
    "vahtovik status --role r1",
    "uv run vahtovik log a3f9c2 -n 20",
    "vahtovik tell r1/g --message 'Look at it again; and check $HOME (twice) & `this`.\nThanks.'",
    'vahtovik tell a3f9c2 --message "Plain text; with (parentheses) & more."',
    "vahtovik --help",
    "vahtovik messages",
    "vahtovik messages team --last 50",
    "uv run vahtovik messages -n 5",
    "vahtovik acknowledge --text 'Let me check.'",
])
def test_the_channels_own_commands_are_allowed(team, command):
    assert _decide(team, "Bash", command=command)


@pytest.mark.parametrize("command", [
    "vahtovik run",
    "vahtovik artifact add --worker r1/g --json '{}'",
    "vahtovik worker spawn r1/new",
    "cat knowledge/credentials/feed.env",
    "uv run --env-file x vahtovik status",
    "VAHTOVIK_ROLES=r2 vahtovik status",
    "env -u VAHTOVIK_ROLES vahtovik status",
    "vahtovik status; cat .env",
    "vahtovik status && rm -rf data",
    "vahtovik status | tee x",
    "vahtovik status > x",
    "vahtovik status\ncat .env",
    "vahtovik tell r1/g --message $(cat .env)",
    'vahtovik tell r1/g --message "$TELEGRAM_BOT_TOKEN"',
    'vahtovik tell r1/g --message "`cat .env`"',
    "vahtovik tell r1/g --message 'open",
    "vahtovik status \\\n; cat .env",
    "vahtovik messages other",
    "vahtovik messages team other",
    "VAHTOVIK_CHANNEL=other vahtovik acknowledge --text 'Hi.'",
])
def test_anything_else_in_bash_is_denied(team, command):
    assert not _decide(team, "Bash", command=command)


def test_reading_is_allowed_only_in_the_rules_the_knowledge_and_the_channels_roles(team):
    root = team.root
    for path in ("skills/vahtovik/channel/SKILL.md", "knowledge/skills/rss/SKILL.md", "roles/r1/ROLE.md",
                 "pollers/p1/poller.md", "data/workers/r1/a3f9c2-g/context.md"):
        assert _decide(team, "Read", file_path=str(root / path)), path
    assert _decide(team, "Read", file_path=str(schema_path("artifact")))  # the artifacts skill points to it
    assert _decide(team, "Read", file_path="roles/r1/ROLE.md")  # relative to the session's folder
    for path in ("roles/r2/ROLE.md", "data/workers/r2/a3f9c2-g/context.md", "data/workers/r1/a3f9c2-g/.env",
                 "knowledge/credentials/feed.env", "knowledge/credentials/example.env", "knowledge/credentials",
                 "vahtovik.yaml", "data/vahtovik.db", "data/journal.log", "src/vahtovik/cli.py", "/etc/passwd",
                 "roles/r1/../r2/ROLE.md"):
        assert not _decide(team, "Read", file_path=str(root / path)), path
    (root / "roles/r1/escape").symlink_to(root / "roles/r2")
    assert not _decide(team, "Read", file_path=str(root / "roles/r1/escape/ROLE.md"))


def test_searching_needs_a_path_without_secrets(team):
    root = team.root
    assert _decide(team, "Grep", pattern="x", path=str(root / "roles/r1"))
    assert _decide(team, "Glob", pattern="*.md", path=str(root / "skills"))
    assert _decide(team, "Glob", pattern="*.md", path=str(root / "knowledge/skills"))
    assert not _decide(team, "Grep", pattern="x")  # the whole project
    assert not _decide(team, "Grep", pattern="x", path=str(root / "knowledge"))  # holds the credentials
    assert not _decide(team, "Grep", pattern="x", path=str(root / "data/workers/r1"))  # holds a .env file
    assert not _decide(team, "Glob", pattern="*", path=str(root / "data/workers/r2"))


def test_other_tools_are_denied_but_the_answer_is_allowed(team):
    for tool in ("Write", "Edit", "WebFetch", "Agent", "mcp__claude_ai_Google_Drive__search_files"):
        assert not _decide(team, tool, file_path=str(team.root / "skills/x"))
    assert _decide(team, "StructuredOutput", replies=[])


def test_a_channel_without_roles_may_read_every_role_but_no_credentials(team, project):
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["channels"]["team"]["input"] = {"commands": ["status"]}
    path.write_text(yaml.safe_dump(data))
    cfg = config_mod.load(project.root)
    assert _decide(cfg, "Read", file_path=str(cfg.root / "roles/r2/ROLE.md"))
    assert _decide(cfg, "Read", file_path=str(cfg.root / "data/workers/r2/a3f9c2-g/context.md"))
    assert not _decide(cfg, "Read", file_path=str(cfg.root / "knowledge/credentials/feed.env"))
    assert not _decide(cfg, "Bash", command="vahtovik tell r2/g --message hi")


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
