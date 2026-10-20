import json
import logging
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from vahtovik import config as config_mod
from vahtovik.claude_session import Claude
from vahtovik.state import State

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


@pytest.fixture
def project(tmp_path, monkeypatch) -> Project:
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
    return {"structured_output": {"artifacts": list(items), "notes": ""}}


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
    return {"actions": [*actions, status_action(importance=importance, **status)]}


def report_action(channel: str = "log", body: str = "Report body.") -> dict:
    return {"run": ["vahtovik", "report", "--channel", channel, "--subject", "Test report"], "stdin": body}
