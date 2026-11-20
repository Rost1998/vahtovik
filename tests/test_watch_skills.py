import os

from conftest import artifact, monitor_answer, run_turns, turn_answer, work_log

from vahtovik import artifacts, cli, skills
from vahtovik.stages import monitor
from vahtovik.tmux import Tmux


def _skill(project, name="triage", description="Sort new items by urgency.", link=True):
    """Give watch w1 its own skill, and the link that Claude Code finds it by."""
    watch = project.root / "watches/w1"
    folder = watch / "skills" / name
    (folder / "scripts").mkdir(parents=True)
    front = f"name: {name}\n" + (f"description: {description}\n" if description else "")
    (folder / "SKILL.md").write_text(f"---\n{front}---\nRun scripts/check.py.\n")
    (watch / ".claude/skills").mkdir(parents=True, exist_ok=True)
    if link:
        os.symlink(f"../../skills/{name}", watch / ".claude/skills" / name)
    return folder


def _problems(capsys) -> list[str]:
    cli.main(["check"])
    return [line for line in capsys.readouterr().out.splitlines() if line.startswith("problem: watch w1")]


def test_watch_skills_are_found_and_listed(project):
    folder = _skill(project)
    cfg, _, _ = project.load()
    watch = cfg.watch("w1")
    found = skills.watch_skills(watch)
    assert [(s.name, s.description) for s in found] == [("triage", "Sort new items by urgency.")]
    assert skills.listing(found) == f"- `triage` (`{folder / 'SKILL.md'}`): Sort new items by urgency."
    assert skills.listing([]) == "- none"


def test_check_accepts_a_skill_with_its_link(project, capsys):
    _skill(project)
    assert _problems(capsys) == []


def test_check_reports_skill_and_link_problems(project, capsys):
    _skill(project, "no-link", link=False)
    _skill(project, "vague", description=None)
    watch = project.root / "watches/w1"
    links = watch / ".claude/skills"
    os.symlink("../../skills/gone", links / "gone")
    os.symlink("../../process", links / "elsewhere")
    (links / "copied").mkdir()
    (watch / "skills/empty").mkdir()
    real = (watch / "skills").resolve()
    assert _problems(capsys) == [
        f"problem: watch w1: skill empty: {watch}/skills/empty/SKILL.md is missing",
        f"problem: watch w1: skill no-link: no link {links}/no-link; make it with "
        f"`ln -s ../../skills/no-link {links}/no-link` in the vahtovik folder",
        f"problem: watch w1: skill vague: {watch}/skills/vague/SKILL.md has no description in its front matter",
        f"problem: watch w1: {links}/copied is not a link; keep the skill in {watch}/skills/copied and link to it",
        f"problem: watch w1: link {links}/elsewhere points to {watch.resolve()}/process, not to {real}/elsewhere",
        f"problem: watch w1: link {links}/gone is broken: it points to ../../skills/gone",
    ]


def test_check_wants_the_folder_name_as_the_skill_name_and_valid_front_matter(project, capsys):
    folder = _skill(project)
    (folder / "SKILL.md").write_text("---\nname: Triage\ndescription: Sort.\n---\nBody.\n")
    assert _problems(capsys) == [
        "problem: watch w1: skill triage: its front matter names it 'Triage'; use the folder name"]
    (folder / "SKILL.md").write_text("---\nname: [triage\n---\nBody.\n")
    problems = _problems(capsys)
    assert len(problems) == 1 and "front matter of" in problems[0] and "is not valid YAML" in problems[0]
    cfg, _, _ = project.load()
    assert skills.watch_skills(cfg.watch("w1")) == []


def _add_dirs(call) -> list[str]:
    args = call["args"]
    return [args[i + 1] for i, arg in enumerate(args) if arg == "--add-dir"]


def _monitor_and_turn(project):
    """One monitoring session for each source of w1, then one group turn. Returns the calls to claude."""
    cfg, state, claude = project.load()
    project.queue(monitor_answer(), monitor_answer())
    monitor.run(cfg, state, claude, cfg.watch("w1"), "r1", project.tmp / "raw")
    artifacts.register(cfg, state, cfg.watch("w1"), artifact("a1", "g"), "src-a")
    project.queue(turn_answer([work_log()]))
    run_turns(cfg, state, claude, Tmux(cfg), run_id="r1")
    return cfg, project.calls()


def test_sessions_get_the_watch_folder_when_it_has_skills(project):
    _skill(project)
    cfg, calls = _monitor_and_turn(project)
    assert len(calls) == 3
    assert all(_add_dirs(call) == [str(cfg.watch("w1").dir)] for call in calls)


def test_sessions_of_a_watch_without_skills_get_no_add_dir(project):
    _, calls = _monitor_and_turn(project)
    assert len(calls) == 3 and all(_add_dirs(call) == [] for call in calls)
