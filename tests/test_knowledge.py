import os

import yaml
from conftest import ROLE, artifact, give, monitor_answer, run_turns, turn_answer, work_log

from vahtovik import cli, delivery, knowledge, workers
from vahtovik.stages import monitor
from vahtovik.tmux import Tmux


def _skill(project, name: str, kind: str = "skills", description: str | None = "Sort new items by urgency.",
           extra: str = "") -> object:
    folder = project.root / "knowledge" / kind / name
    folder.mkdir(parents=True, exist_ok=True)
    front = f"name: {name}\n" + (f"description: {description}\n" if description else "") + extra
    (folder / "SKILL.md").write_text(f"---\n{front}---\nRun scripts/check.py.\n")
    return folder


def _problems(capsys, prefix: str = "problem:") -> list[str]:
    cli.main(["check"])
    return [line for line in capsys.readouterr().out.splitlines() if line.startswith(prefix)]


def test_skills_are_found_and_listed(project):
    folder = _skill(project, "triage")
    cfg, _, _ = project.load()
    found, problems = knowledge.collect(cfg, ["triage", "nope"])
    assert [s.name for s in found] == ["workers", "triage"] and problems == [
        f"skill nope not found: {cfg.knowledge_dir}/skills/nope/SKILL.md is missing"]
    assert knowledge.listing(found[1:]) == f"- `triage` (`{folder / 'SKILL.md'}`): Sort new items by urgency."
    assert knowledge.listing([]) == "- none"
    assert "triage" in knowledge.available(cfg) and "workers" not in knowledge.available(cfg)


def test_check_accepts_the_project(project, capsys):
    assert _problems(capsys) == []


def test_check_reports_skill_problems(project, capsys):
    _skill(project, "vague", description=None)
    _skill(project, "named", extra="").joinpath("SKILL.md").write_text("---\nname: Other\ndescription: d\n---\nB.\n")
    _skill(project, "workers")  # a specialized skill with a shared skill's name
    (project.root / "knowledge/skills/empty").mkdir()
    _skill(project, "broken-yaml").joinpath("SKILL.md").write_text("---\nname: [x\n---\nBody.\n")
    problems = _problems(capsys)
    skills = project.root / "knowledge/skills"
    assert f"problem: skills skill vague: {skills}/vague/SKILL.md has no description in its front matter" in problems
    assert "problem: skills skill named: its front matter names it 'Other'; use the folder name" in problems
    assert "problem: skills skill workers: a shared skill has the same name" in problems
    assert f"problem: skills skill empty not found: {skills}/empty/SKILL.md is missing" in problems
    assert any("broken-yaml" in p and "is not valid YAML" in p for p in problems)


def test_check_reports_what_roles_sources_and_channels_miss(project, capsys):
    _skill(project, "needs-token", extra="credentials: [vault]\nrequired_env: [VAULT_TOKEN]\n")
    project.role("r1", ROLE.replace("skills: [file, rss]", "skills: [file, rss, needs-token, ghost]"))
    path = project.root / "pollers/p1/poller.yaml"
    data = yaml.safe_load(path.read_text())
    data["sources"][1].update(skill="needs-token", credentials=["org-b"])
    path.write_text(yaml.safe_dump(data))
    path = project.root / "vahtovik.yaml"
    data = yaml.safe_load(path.read_text())
    data["channels"]["tg"] = {"skill": "telegram", "credentials": ["ops-bot"], "env": {"TELEGRAM_CHAT_ID": "-1"}}
    path.write_text(yaml.safe_dump(data))
    problems = _problems(capsys)
    credentials = project.root / "knowledge/credentials"
    skills = project.root / "knowledge/skills"
    assert problems == [
        f"problem: channel tg: credentials file {credentials}/telegram.env is missing",
        f"problem: channel tg: credentials file {credentials}/ops-bot.env is missing",
        f"problem: channel tg: environment variable TELEGRAM_BOT_TOKEN not set; put it in a credentials file of "
        f"{credentials}",
        f"problem: role r1: skill ghost not found: {skills}/ghost/SKILL.md is missing",
        f"problem: role r1: credentials file {credentials}/vault.env is missing",
        f"problem: role r1: environment variable VAULT_TOKEN not set; put it in a credentials file of {credentials}",
        f"problem: poller p1, source src-b: credentials file {credentials}/vault.env is missing",
        f"problem: poller p1, source src-b: credentials file {credentials}/org-b.env is missing",
        f"problem: poller p1, source src-b: environment variable VAULT_TOKEN not set; put it in a credentials file "
        f"of {credentials}",
    ]
    project.credentials("vault", "VAULT_TOKEN=v\n")
    project.credentials("org-b", "OTHER=1\n")
    project.credentials("telegram", "TELEGRAM_BOT_TOKEN=t\n")
    project.credentials("ops-bot", "TELEGRAM_BOT_TOKEN=t2\n")
    assert _problems(capsys) == [f"problem: role r1: skill ghost not found: {skills}/ghost/SKILL.md is missing"]


def test_links_hold_exactly_the_skills(project, tmp_path):
    cfg, _, _ = project.load()
    folder = tmp_path / "session"
    found, _ = knowledge.collect(cfg, ["rss"])
    knowledge.link(folder, found)
    links = folder / ".claude/skills"
    (links / "stale").symlink_to(tmp_path)
    (links / "copied").mkdir()
    knowledge.link(folder, knowledge.collect(cfg, ["file"])[0])
    assert sorted(p.name for p in links.iterdir()) == ["file", "workers"]
    assert os.readlink(links / "file") == str((cfg.knowledge_dir / "skills/file").resolve())


def _add_dirs(call) -> list[str]:
    args = call["args"]
    return [args[i + 1] for i, arg in enumerate(args) if arg == "--add-dir"]


def test_sessions_get_their_skills_and_the_credentials_of_their_skills(project, monkeypatch):
    _skill(project, "triage", extra="credentials: [triage]\n")
    project.credentials("triage", "TRIAGE_TOKEN=from-skill\nSHARED_TOKEN=skill\n")
    project.credentials("org-b", "SHARED_TOKEN=org-b\n")
    project.role("r1", ROLE.replace("skills: [file, rss]", "skills: [file, rss, triage]"))
    path = project.root / "pollers/p1/poller.yaml"
    data = yaml.safe_load(path.read_text())
    data["sources"][1].update(skill="triage", credentials=["org-b"])
    path.write_text(yaml.safe_dump(data))
    cfg, state, claude = project.load()
    project.queue(monitor_answer(), monitor_answer())
    monitor.run(cfg, state, claude, cfg.poller("p1"), "r1", project.tmp / "raw")
    src_a, src_b = project.calls()
    assert "TRIAGE_TOKEN" not in src_a["env"]
    assert src_b["env"]["TRIAGE_TOKEN"] == "from-skill" and src_b["env"]["SHARED_TOKEN"] == "org-b"
    assert _add_dirs(src_b) == [str(cfg.poller_dir("p1") / "sessions/src-b")]
    assert "- `triage` (`" in src_b["prompt"] and "- `workers` (`" in src_b["prompt"]

    give(project, artifact("a1", "g"))
    project.queue(turn_answer([work_log()]))
    run_turns(cfg, state, claude, Tmux(cfg), run_id="r1")
    call = project.calls()[-1]
    assert call["env"]["TRIAGE_TOKEN"] == "from-skill" and _add_dirs(call) == []
    assert "- `triage` (`" in call["prompt"]
    claude_md = (workers.folder(cfg, state.active_worker("r1", "g")) / "CLAUDE.md").read_text()
    assert "- `triage` (`" in claude_md and "from-skill" not in claude_md


def test_a_channel_gets_its_own_credentials(project):
    project.credentials("ops-bot", "TELEGRAM_BOT_TOKEN=ops\n")
    project.credentials("telegram", "TELEGRAM_BOT_TOKEN=default\nTELEGRAM_CHAT_ID=-1\n")
    project.config(lambda d: d["channels"].update(tg={"skill": "telegram", "credentials": ["ops-bot"],
                                                      "env": {"TELEGRAM_CHAT_ID": "-2"}}))
    cfg, _, _ = project.load()
    env = delivery.channel_env(cfg, cfg.channels["tg"], knowledge.load(cfg, "telegram"))
    assert env == {"TELEGRAM_BOT_TOKEN": "ops", "TELEGRAM_CHAT_ID": "-2"}


def test_check_reports_workers_whose_role_is_gone(project, capsys):
    cfg, state, _ = project.load()
    state.add_worker("aaaaaa", "old-role", "g", "G", [], "person", None, False)
    assert _problems(capsys) == [
        f"problem: role old-role: it has workers, but {cfg.roles_dir}/old-role/ROLE.md is gone; they get no turns "
        "until the role is back, or you archive them"]


def test_check_wants_a_channel_for_notices(project, capsys):
    project.config(lambda data: data.pop("notices"))
    assert any(line.startswith("problem: notices: no channel gets vahtovik's notices") for line in _problems(capsys))
