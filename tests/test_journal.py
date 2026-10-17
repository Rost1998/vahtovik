import logging

import pytest
from conftest import artifact, monitor_answer, report_action, turn_answer, work_log

from vahtovik import cli, journal


@pytest.fixture(autouse=True)
def detach_journal():
    yield
    logger = logging.getLogger("vahtovik")
    for handler in [h for h in logger.handlers if getattr(h, "journal", False)]:
        logger.removeHandler(handler)
        handler.close()


def _entries(project) -> list[tuple[str, str]]:
    """(who, message) for each journal line."""
    lines = (project.root / "data/journal.log").read_text().splitlines()
    return [(line[20:39].strip(), line[40:]) for line in lines]


def test_run_writes_one_line_per_key_event(project):
    project.queue(monitor_answer(artifact("a1", "g")), monitor_answer(),
                  turn_answer([work_log(), report_action("log", "Hi.")], importance="high"))
    assert cli.main(["run", "--force"]) == 0
    entries = _entries(project)
    messages = [m for _, m in entries]
    assert len({who for who, _ in entries}) == 1 and entries[0][0].startswith("run ")  # report lines too
    assert messages[0] == "Started run: w1 (forced)"
    assert "w1/src-a: Started monitoring (rss)" in messages
    assert 'w1/g: Created group "G"' in messages
    assert "w1/src-a: Finished monitoring in 0s: 1 found, 1 new, $0.01" in messages
    assert any(m.startswith("w1/g: Started group turn: 1 artifact, new session ") for m in messages)
    assert 'w1/g: Sent report 1 to log "Test report"' in messages
    assert "w1/g: Finished group turn in 0s: importance high, 1 report, $0.01. All good." in messages
    assert messages[-1] == "Finished run in 0s: 1 new artifact, 1 group turn, 1 report sent, $0.03"


def test_run_with_nothing_due_writes_nothing(project):
    project.queue(monitor_answer(), monitor_answer())
    cli.main(["run"])
    size = (project.root / "data/journal.log").stat().st_size
    cli.main(["run"])
    assert (project.root / "data/journal.log").stat().st_size == size


def test_failures_are_logged_with_the_attempt(project):
    project.queue(monitor_answer(artifact("a1", "g")), {"exit": 1},
                  turn_answer([report_action("broken", "Hi.")]))
    cli.main(["run", "--force"])
    messages = [m for _, m in _entries(project)]
    assert any(m.startswith("w1/src-b: Failed monitoring after") for m in messages)
    assert any(m.startswith('w1/g: Failed report 1 to broken "Test report" (attempt 1 of 5): boom')
               for m in messages)
    assert any(m.startswith("w1/g: Failed group turn after 0s (attempt 1 of 3): the agent did not add")
               for m in messages)
    assert messages[-1].endswith("1 group turn (1 failed), 0 reports sent, 1 not sent, $0.02")


def test_read_filters_and_follows(tmp_path):
    path = tmp_path / "journal.log"
    path.write_text("a one\nb two\na three\na unfinished")
    assert list(journal.read(path, 0, "a ")) == ["a one", "a three"]
    assert list(journal.read(path, 1)) == ["a three"]
    lines = journal.read(path, 1, follow=True, poll=0.01)
    assert next(lines) == "a three"
    with path.open("a") as handle:
        handle.write(" line\nb four\n")
    assert next(lines) == "a unfinished line"
    assert next(lines) == "b four"
