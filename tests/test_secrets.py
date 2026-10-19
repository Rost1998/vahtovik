import pytest
import yaml
from conftest import artifact, monitor_answer, turn_answer, work_log

from vahtovik import artifacts, cli, config as config_mod
from vahtovik.stages import group_turn, monitor


def _secrets(project, source_env=None):
    """w1 gets its own .env; src-b optionally fills A_TOKEN from another secret."""
    (project.root / "watches/w1/.env").write_text("A_TOKEN=from-watch\nORG_B_TOKEN=org-b\n")
    path = project.root / "watches/w1/watch.yaml"
    data = yaml.safe_load(path.read_text())
    if source_env:
        data["sources"][1]["env"] = source_env
    path.write_text(yaml.safe_dump(data))


def test_watch_secrets_reach_its_monitor_and_group_sessions(project, monkeypatch):
    monkeypatch.setenv("SHARED_TOKEN", "shared")
    _secrets(project, {"A_TOKEN": "ORG_B_TOKEN", "B_TOKEN": "SHARED_TOKEN"})
    cfg, state, claude = project.load()
    project.queue(monitor_answer(), monitor_answer())
    monitor.run(cfg, state, claude, cfg.watch("w1"), "r1", project.tmp / "raw")
    src_a, src_b = (call["env"] for call in project.calls())
    assert src_a["A_TOKEN"] == "from-watch" and "B_TOKEN" not in src_a
    assert src_b["A_TOKEN"] == "org-b" and src_b["B_TOKEN"] == "shared"

    artifacts.register(cfg, state, cfg.watch("w1"), artifact("a1", "g"), "src-a")
    project.queue(turn_answer([work_log()]))
    group_turn.run(cfg, state, claude, cfg.watch("w1"), "r1", project.tmp / "raw")
    assert project.calls()[-1]["env"]["ORG_B_TOKEN"] == "org-b"
    claude_md = (cfg.group_dir("w1", "g") / "CLAUDE.md").read_text()
    assert "set `A_TOKEN` to `$ORG_B_TOKEN`" in claude_md and "org-b" not in claude_md


def test_check_reports_missing_secrets(project, capsys):
    _secrets(project, {"A_TOKEN": "NOT_SET_TOKEN"})
    skill = project.root / "skills/sources/rss/SKILL.md"
    skill.write_text(skill.read_text().replace("required_params: [url]", "required_params: [url]\nrequired_env: [C_TOKEN]"))
    assert cli.main(["check"]) == 1
    problems = [line for line in capsys.readouterr().out.splitlines() if line.startswith("problem:")]
    assert problems == [
        f"problem: watch w1, source src-a: secret C_TOKEN not set; add it to {project.root}/watches/w1/.env",
        f"problem: watch w1, source src-b: secret C_TOKEN, NOT_SET_TOKEN not set; "
        f"add it to {project.root}/watches/w1/.env",
    ]


def test_source_env_must_be_a_mapping(project):
    _secrets(project, "A_TOKEN")
    with pytest.raises(config_mod.ConfigError, match="env of source 'src-b' must be a mapping"):
        config_mod.load(project.root)
