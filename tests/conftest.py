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

from vahtovik import config as config_mod
from vahtovik import skills, watchdog, windows
from vahtovik.claude_session import Claude
from vahtovik.group_session import spec_for
from vahtovik.stages import group_turn
from vahtovik.state import State
from vahtovik.tmux import Tmux

REPO = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).with_name("fake_claude.py")


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
        meta = skills.watchdog_skill(cfg.skills_dir).meta
        for session in watchdog.sessions_to_check(cfg, state, tmux, claude):
            state.group_checked(session.watch.name, session.group["key"])
            watchdog._act(cfg, state, tmux, claude, session, {"action": "none", "reason": "All is right."}, meta, {})
    monkeypatch.setattr(watchdog, "check", check)


@pytest.fixture
def project(tmp_path, monkeypatch) -> Project:
    """A vahtovik project with one watch, a fake claude, and a tmux server of its own."""
    socket = f"vahtovik-test-{os.getpid()}-{tmp_path.name}"
    monkeypatch.setattr(Tmux, "use_systemd_run", False)
    monkeypatch.setattr(watchdog, "GRACE", timedelta(0))
    yield _project(tmp_path, monkeypatch, socket)
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)


def _project(tmp_path, monkeypatch, socket: str) -> Project:
    root = tmp_path / "proj"
    shutil.copytree(REPO / "skills", root / "skills")
    _script(root / "skills/channels/broken/send", "#!/bin/sh\necho boom >&2\nexit 3\n")
    (root / "skills/channels/broken/SKILL.md").write_text("---\nname: broken\ndescription: fails\n---\nAny text.\n")
    _script(root / "skills/channels/slow/send", "#!/bin/sh\nsleep 5\n")
    (root / "skills/channels/slow/SKILL.md").write_text("---\nname: slow\ndescription: slow\n---\nAny text.\n")
    fake = tmp_path / "fake-claude"
    _script(fake, f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')

    (root / "vahtovik.yaml").write_text(yaml.safe_dump({
        "claude": {"binary": str(fake), "config_dir": str(tmp_path / "claude-home"),
                   "stages": {"monitor": {"effort": "low"}, "group": {"effort": "high", "max_budget_usd": 1}}},
        "tmux": {"socket": socket},
        "channels": {
            "log": {"skill": "file", "env": {"FILE_CHANNEL_PATH": "data/outbox.md"}},
            "broken": {"skill": "broken"},
            "slow": {"skill": "slow"},
            "other": {"skill": "file", "env": {"FILE_CHANNEL_PATH": "data/other.md"}},
        },
    }))
    watch = root / "watches/w1"
    (watch / "process").mkdir(parents=True)
    (watch / "watch.yaml").write_text(yaml.safe_dump({
        "interval": "1h",
        "sources": [{"id": "src-a", "skill": "rss", "params": {"url": "http://a"}},
                    {"id": "src-b", "skill": "rss", "params": {"url": "http://b"}}],
        "channels": ["log", "broken", "slow"],
        "claude": {"group": {"effort": "xhigh"}},
    }))
    (watch / "monitor.md").write_text("Every item is an artifact. Group by product.\n")
    (watch / "process/01-assess.md").write_text("Assess each item.\n")
    (watch / "report.md").write_text("Report everything to log.\n")

    for name in ("VAHTOVIK_WATCH", "VAHTOVIK_GROUP", "VAHTOVIK_TURN", "VAHTOVIK_DRY_RUN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VAHTOVIK_HOME", str(root))
    monkeypatch.setenv("FAKE_CLAUDE_QUEUE", str(tmp_path / "queue.json"))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "calls.jsonl"))
    return Project(root, tmp_path)


def artifact(item_id: str, group: str, title: str | None = None) -> dict:
    return {"id": item_id, "title": title or item_id, "url": f"https://example.com/{item_id}",
            "summary": f"About {item_id}.", "group_key": group, "group_title": group.title()}


def monitor_answer(*items: dict) -> dict:
    return {"mode": "print", "structured_output": {"artifacts": list(items), "notes": ""}}


def work_log(text: str = "Processed.") -> dict:
    return {"replace": "context.md", "old": "## Work log\n",
            "new": f"## Work log\n### 2026-10-17 10:00 · run test\n{text}\n"}


def status_action(status: str = "finished", importance: str | None = "low", summary: str | None = "All good.",
                  questions: tuple[str, ...] = ()) -> dict:
    args = ["vahtovik", "turn", status]
    args += ["--importance", importance] if importance else []
    args += ["--summary", summary] if summary else []
    for question in questions:
        args += ["--question", question]
    return {"run": args}


def turn_answer(actions: list[dict], importance: str = "low", **status) -> dict:
    """A group turn that ends with `vahtovik turn`."""
    return {"mode": "interactive", "actions": [*actions, status_action(importance=importance, **status)]}


def report_action(channel: str = "log", body: str = "Report body.") -> dict:
    return {"run": ["vahtovik", "report", "--channel", channel, "--subject", "Test report"], "stdin": body}


def until(condition, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def run_turns(cfg, state, claude, tmux, watch: str = "w1", run_id: str = "run1") -> list[str]:
    """A run's group stage, then the wait until each turn it started is done or paused, and the watchdog's
    tidying, as at the next run's start: it ends the turns whose window ended, and closes failed windows."""
    started = group_turn.run(cfg, state, claude, tmux, cfg.watch(watch), run_id)
    for turn_id in started:
        until(lambda: _settled(state, tmux, turn_id))
    watchdog.tidy(cfg, state, tmux)
    return started


def _settled(state, tmux, turn_id: str) -> bool:
    turn = state.turn(turn_id)
    group = state.group(turn["watch"], turn["group_key"])
    if turn["finished_at"] and (group["idle"] or not turn["ok"]):
        return True
    if group["status"] in ("failed", "maintenance") or (group["status"] == "background" and group["idle"]):
        return True
    return tmux.window(f"{turn['watch']}/{turn['group_key']}") is None


def launch_turn(cfg, state, claude, tmux, turn_id: str = "t1") -> str:
    """Start the window of a turn for group w1/g, without waiting for it."""
    spec, _ = spec_for(cfg, claude, state, cfg.watch("w1"), "g")
    spec.prompt = "Process."
    spec.env["VAHTOVIK_TURN"] = turn_id
    state.start_turn(turn_id, "run1", "w1", "g", ["a1"], 0)
    state.prompt_submitted("w1", "g")
    return windows.launch(cfg, state, tmux, claude, spec, "turn", turn_id)


def watchdog_answer(*sessions: dict) -> dict:
    return {"mode": "print", "structured_output": {"sessions": list(sessions)}}
