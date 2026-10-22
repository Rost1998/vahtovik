import json
from pathlib import Path

import yaml

from vahtovik.claude_session import SessionSpec


def _spec(cfg, mode="throwaway", **kw):
    workdir = cfg.group_dir("w1", "g")
    workdir.mkdir(parents=True, exist_ok=True)
    return SessionSpec(stage="group", workdir=workdir, prompt="do it", settings=cfg.stage(cfg.watch("w1"), "group"),
                       schema={"type": "object"}, system_prompt="MAIN SKILL", mode=mode, **kw)


def _value(args, flag):
    return args[args.index(flag) + 1]


def test_throwaway_args(project):
    cfg, _, claude = project.load()
    spec = _spec(cfg)
    args = claude.print_args(spec)
    assert args[:4] == [cfg.claude.binary, "-p", "--output-format", "json"]
    assert "--no-session-persistence" in args and "--resume" not in args
    assert _value(args, "--append-system-prompt") == "MAIN SKILL"
    assert _value(args, "--permission-mode") == "bypassPermissions"
    assert _value(args, "--effort") == "xhigh" and _value(args, "--max-budget-usd") == "1.0"
    excludes = json.loads(_value(args, "--settings"))["claudeMdExcludes"]
    assert str(Path.home() / ".claude" / "CLAUDE.md") in excludes
    assert str(cfg.root / "CLAUDE.md") in excludes
    assert str(spec.workdir / "CLAUDE.md") not in excludes  # the group's own CLAUDE.md must load


def test_new_and_resume_args(project):
    cfg, _, claude = project.load()
    new = claude.print_args(_spec(cfg, "new", session_id="abc", name="vahtovik: w1/g"))
    assert _value(new, "--session-id") == "abc" and _value(new, "--name") == "vahtovik: w1/g"
    assert "--no-session-persistence" not in new
    resumed = claude.print_args(_spec(cfg, "resume", session_id="abc"))
    assert _value(resumed, "--resume") == "abc"
    assert "--append-system-prompt" not in resumed and "--session-id" not in resumed


def test_login_modes(project, monkeypatch):
    cfg, _, claude = project.load()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    spec = _spec(cfg)
    assert "--bare" not in claude.print_args(spec)
    assert "ANTHROPIC_API_KEY" not in claude.env(spec)
    assert claude.env(spec)["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert claude.env(spec)["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"] == "false"

    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["claude"]["auth"] = "api-key"
    path.write_text(yaml.safe_dump(data))
    cfg, _, claude = project.load()
    spec = _spec(cfg)
    args = claude.print_args(spec)
    assert "--bare" in args and _value(args, "--add-dir") == str(spec.workdir)
    env = claude.env(spec)
    assert env["ANTHROPIC_API_KEY"] == "key" and env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] == "1"


def test_invalid_answer_is_retried(project):
    cfg, _, claude = project.load()
    project.queue({"structured_output": {"wrong": True}}, {"structured_output": {"ok": True}})
    spec = _spec(cfg)
    spec.schema = {"type": "object", "required": ["ok"]}
    result = claude.run(spec, project.tmp / "raw")
    assert result.ok and result.output == {"ok": True}
    first, second = project.calls()
    assert first["args"] == second["args"] and first["prompt"] == second["prompt"]
    assert abs(result.cost_usd - 0.02) < 1e-9


def test_interactive_args_end_with_the_prompt_and_group_sessions_get_the_hooks(project):
    cfg, _, claude = project.load()
    args = claude.interactive_args(_spec(cfg, "resume", session_id="abc"))
    assert args[0] == cfg.claude.binary and "-p" not in args and args[-1] == "do it"
    settings = json.loads(_value(args, "--settings"))
    assert settings["skipDangerousModePermissionPrompt"] is True and settings["promptSuggestionEnabled"] is False
    assert settings["hooks"]["Stop"][0]["hooks"][0]["command"].endswith("-m vahtovik hook stop")


def test_trust_is_read_from_the_claude_config(project):
    cfg, _, claude = project.load()
    config = cfg.claude.config_dir / ".claude.json"
    assert claude.trusts(cfg.data_dir) is None
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"projects": {str(cfg.root): {"hasTrustDialogAccepted": True}}}))
    assert claude.trusts(cfg.data_dir / "watches") is True
    config.write_text(json.dumps({"projects": {str(cfg.root): {"hasTrustDialogAccepted": False}}}))
    assert claude.trusts(cfg.data_dir) is False
