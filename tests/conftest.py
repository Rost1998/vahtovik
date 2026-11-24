import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from vahtovik import artifacts, inbox, knowledge, watchdog, windows, workers
from vahtovik import config as config_mod
from vahtovik.claude_session import Claude
from vahtovik.stages import worker_turn
from vahtovik.state import State
from vahtovik.tmux import Tmux

REPO = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).with_name("fake_claude.py")
ROLE = """---
description: Follows one product.
skills: [file, rss]
effort: xhigh
---

Assess each item.
Report everything with the file skill.
"""
SESSION_VARS = ("VAHTOVIK_WORKER", "VAHTOVIK_WORKER_NAME", "VAHTOVIK_ROLE", "VAHTOVIK_TURN", "VAHTOVIK_RUN",
                "VAHTOVIK_DRY_RUN", "VAHTOVIK_ROLES", "VAHTOVIK_CHANNEL", "VAHTOVIK_SOURCE", "VAHTOVIK_POLLER",
                "ANTHROPIC_API_KEY")


def _script(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


class Project:
    def __init__(self, root: Path, tmp: Path):
        self.root, self.tmp = root, tmp
        self.queue_file, self.log_file = tmp / "queue.json", tmp / "calls.jsonl"

    def queue(self, *responses: dict) -> None:
        queue = json.loads(self.queue_file.read_text()) if self.queue_file.exists() else []
        self.queue_file.write_text(json.dumps(queue + list(responses)))

    def calls(self) -> list[dict]:
        return self._lines(self.log_file)

    def hook_answers(self, event: str = "Stop") -> list[dict]:
        """What vahtovik answered to each hook the fake claude ran."""
        return [h["answer"] for h in self._lines(Path(f"{self.log_file}.hooks")) if h["event"] == event]

    def command_runs(self) -> list[dict]:
        """Commands the fake agent ran: the command, its exit code and output."""
        return self._lines(Path(f"{self.log_file}.runs"))

    @staticmethod
    def _lines(path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def load(self):
        cfg = config_mod.load(self.root)
        return cfg, State(cfg.db_path), Claude(cfg)

    def config(self, change) -> None:
        """Change vahtovik.yaml: `change` gets its data and changes it in place."""
        path = self.root / "vahtovik.yaml"
        data = yaml.safe_load(path.read_text())
        change(data)
        path.write_text(yaml.safe_dump(data))

    def role(self, name: str = "r1", text: str = ROLE) -> Path:
        path = self.root / "roles" / name / "ROLE.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def poller(self, name: str = "p1", **data) -> Path:
        path = self.root / "pollers" / name / "poller.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data))
        (path.parent / "poller.md").write_text("Every item is an artifact. One worker per product.\n")
        return path

    def credentials(self, name: str, text: str) -> None:
        path = self.root / "knowledge" / "credentials" / f"{name}.env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.fixture(autouse=True)
def detach_journal():
    """Tests that call cli.main() attach the journal to the "vahtovik" logger; remove it afterwards."""
    yield
    logger = logging.getLogger("vahtovik")
    for handler in [h for h in logger.handlers if getattr(h, "journal", False)]:
        logger.removeHandler(handler)
        handler.close()


@pytest.fixture(autouse=True)
def approving_watchdog(monkeypatch):
    """The watchdog's AI check finds every session right, without an AI call; so a done window closes at the
    next tick, as at the start of a real run. tests/test_watchdog.py uses the real check with fake AI answers."""
    def check(cfg, state, tmux, claude, run_id):
        meta = knowledge.watchdog_skill(cfg).meta
        for session in watchdog.sessions_to_check(cfg, state, tmux, claude):
            state.worker_checked(session.worker["id"])
            watchdog._act(cfg, state, tmux, claude, session, {"action": "none", "reason": "All is right."}, meta, {})
    monkeypatch.setattr(watchdog, "check", check)


@pytest.fixture
def project(tmp_path, monkeypatch) -> Project:
    """A vahtovik project with one role, one poller, a fake claude, and a tmux server of its own."""
    socket = f"vahtovik-test-{os.getpid()}-{tmp_path.name}"
    monkeypatch.setattr(Tmux, "use_systemd_run", False)
    monkeypatch.setattr(watchdog, "GRACE", timedelta(0))
    monkeypatch.setattr(inbox, "STARTING", timedelta(0))
    yield _project(tmp_path, monkeypatch, socket)
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)


def copy_knowledge(source: Path, target: Path) -> None:
    """Copy a project's knowledge layer for a test project. A link stays a link: a skill that is kept elsewhere is
    not copied with all it holds, and a link whose target is missing does not fail the copy, as vahtovik leaves such
    a skill out. The project's real credentials stay out of the tests: of credentials/, only example.env is copied."""
    credentials = source / "credentials"
    shutil.copytree(source, target, symlinks=True,
                    ignore=lambda folder, names: [n for n in names if Path(folder) == credentials and n != "example.env"])


def _project(tmp_path, monkeypatch, socket: str) -> Project:
    root = tmp_path / "proj"
    shutil.copytree(REPO / "skills", root / "skills")
    copy_knowledge(REPO / "knowledge", root / "knowledge")
    skills = root / "knowledge/skills"
    _script(skills / "broken/send", "#!/bin/sh\necho boom >&2\nexit 3\n")
    (skills / "broken/SKILL.md").write_text("---\nname: broken\ndescription: fails\n---\nAny text.\n")
    _script(skills / "slow/send", "#!/bin/sh\nsleep 5\n")
    (skills / "slow/SKILL.md").write_text("---\nname: slow\ndescription: slow\n---\nAny text.\n")
    fake = tmp_path / "fake-claude"
    _script(fake, f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')

    (root / "vahtovik.yaml").write_text(yaml.safe_dump({
        "claude": {"binary": str(fake), "config_dir": str(tmp_path / "claude-home"),
                   "stages": {"monitor": {"effort": "low"}, "worker": {"effort": "high", "max_budget_usd": 1}}},
        "tmux": {"socket": socket},
        "notices": ["log"],
        "channels": {
            "log": {"skill": "file", "env": {"FILE_CHANNEL_PATH": "data/outbox.md"}},
            "broken": {"skill": "broken"},
            "slow": {"skill": "slow"},
            "other": {"skill": "file", "env": {"FILE_CHANNEL_PATH": "data/other.md"}},
        },
    }))
    project = Project(root, tmp_path)
    project.role()
    project.poller(interval="1h", roles=["r1"],
                   sources=[{"id": "src-a", "skill": "rss", "params": {"url": "http://a"}},
                            {"id": "src-b", "skill": "rss", "params": {"url": "http://b"}}])

    for name in SESSION_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VAHTOVIK_HOME", str(root))
    monkeypatch.setenv("FAKE_CLAUDE_QUEUE", str(tmp_path / "queue.json"))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "calls.jsonl"))
    return project


def artifact(item_id: str, worker: str, title: str | None = None, role: str = "r1") -> dict:
    """An artifact as a monitoring session returns it."""
    return {"id": item_id, "title": title or item_id, "url": f"https://example.com/{item_id}",
            "summary": f"About {item_id}.", "role": role, "worker": worker, "worker_title": worker.title()}


def give(project, *items: dict, poller: str = "p1", source: str = "src-a"):
    """Give artifacts to workers as monitoring does. Returns (cfg, state)."""
    cfg, state, _ = project.load()
    for item in items:
        artifacts.from_poller(cfg, state, cfg.poller(poller), item, source)
    return cfg, state


def worker(cfg, state, key: str = "g", role: str = "r1", **options):
    """The active worker of the role with this key; spawned by the person if there is none."""
    return state.active_worker(role, key) or workers.create(cfg, state, role, key, key.title(), "person", **options)


def monitor_answer(*items: dict) -> dict:
    return {"mode": "print", "structured_output": {"artifacts": list(items), "notes": ""}}


def work_log(text: str = "Processed.") -> dict:
    return {"replace": "context.md", "old": "## Work log\n",
            "new": f"## Work log\n### 2026-10-17 10:00 · run test\n{text}\n"}


def status_action(status: str = "finished", importance: str | None = "low", summary: str | None = "All good.",
                  questions: tuple[str, ...] = (), extra: tuple[str, ...] = ()) -> dict:
    args = ["vahtovik", "turn", status]
    args += ["--importance", importance] if importance else []
    args += ["--summary", summary] if summary else []
    for question in questions:
        args += ["--question", question]
    return {"run": args + list(extra)}


def turn_answer(actions: list[dict], importance: str = "low", **status) -> dict:
    """A worker turn that ends with `vahtovik turn`."""
    return {"mode": "interactive", "actions": [*actions, status_action(importance=importance, **status)]}


def report_action(body: str = "Report body.", subject: str = "Test report") -> dict:
    """The agent reports with its file skill's send script, as its role says."""
    return {"run": ["env", f"VAHTOVIK_SUBJECT={subject}", ".claude/skills/file/send"], "stdin": body}


def until(condition, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def run_turns(cfg, state, claude, tmux, run_id: str = "run1", roles=None) -> list[str]:
    """A run's worker stage, then the wait until each turn it started is done or paused, and the watchdog's
    tidying, as at the next run's start: it ends the turns whose window ended, and closes failed windows."""
    started = worker_turn.run(cfg, state, claude, tmux, run_id, roles)
    for turn_id in started:
        until(lambda: _settled(state, tmux, turn_id))
    watchdog.tidy(cfg, state, tmux)
    return started


def _settled(state, tmux, turn_id: str) -> bool:
    turn = state.turn(turn_id)
    row = state.worker(turn["worker"])
    if turn["finished_at"] and (row["idle"] or not turn["ok"]):
        return True
    if row["status"] in ("failed", "maintenance") or (row["status"] == "background" and row["idle"]):
        return True
    return tmux.window(turn["worker"]) is None


def launch_turn(cfg, state, claude, tmux, turn_id: str = "t1", key: str = "g") -> str:
    """Start the window of a turn for worker r1/<key>, without waiting for it."""
    row = worker(cfg, state, key)
    spec, _ = workers.spec_for(cfg, claude, state, row)
    spec.prompt = "Process."
    spec.env["VAHTOVIK_TURN"] = turn_id
    state.start_turn(turn_id, "run1", row["id"], ["a1"], [], 0)
    state.prompt_submitted(row["id"])
    return windows.launch(cfg, state, tmux, claude, spec, "turn", turn_id)


def watchdog_answer(*sessions: dict) -> dict:
    return {"mode": "print", "structured_output": {"sessions": list(sessions)}}
