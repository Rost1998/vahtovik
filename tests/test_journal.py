import re
import time

from conftest import artifact, monitor_answer, report_action, turn_answer, work_log

from vahtovik import cli, journal
from vahtovik.util import file_lock


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
    assert "w1/g: Status finished, importance high: All good." in messages
    assert any(re.fullmatch(r"w1/g: Started group turn: 1 artifact, new session \w{8}, tmux window @\d+", m)
               for m in messages)
    assert any(re.fullmatch(r"w1/g: Finished group turn in \ds: status finished, 1 report", m) for m in messages)
    assert re.fullmatch(r"Finished run in \ds: 1 new artifact, 1 group turn, 1 report sent, \$0.02", messages[-1])


def test_run_with_nothing_due_writes_one_line(project):
    project.queue(monitor_answer(), monitor_answer())
    cli.main(["run"])
    before = len(_entries(project))
    cli.main(["run"])
    entries = _entries(project)
    assert len(entries) == before + 1 and entries[-1][1] in ("Nothing due: w1 in 59m59s", "Nothing due: w1 in 1h00m")


def test_overlapping_run_is_skipped_and_names_the_running_one(project):
    with file_lock(project.root / "data/.run.lock") as handle:
        handle.write(f"20261020-140312 {time.time() - 47 * 60:.0f}\n")
        handle.flush()
        assert cli.main(["run", "--force"]) == 0
    assert _entries(project)[-1][1] == "Skipped run: run 20261020-140312 has been going for 47m00s"
    assert project.calls() == []


def test_run_writes_its_id_into_the_lock(project):
    project.queue(monitor_answer(), monitor_answer())
    cli.main(["run"])
    run_id, _ = (project.root / "data/.run.lock").read_text().split()
    assert _entries(project)[-1][0] == f"run {run_id}"


def test_failures_are_logged_with_the_attempt(project):
    project.queue(monitor_answer(artifact("a1", "g")), {"exit": 1},
                  turn_answer([report_action("broken", "Hi.")]))
    cli.main(["run", "--force"])
    messages = [m for _, m in _entries(project)]
    assert any(m.startswith("w1/src-b: Failed monitoring after") for m in messages)
    assert any(m.startswith('w1/g: Failed report 1 to broken "Test report" (attempt 1 of 5): boom')
               for m in messages)
    assert any(re.fullmatch(r"w1/g: Failed group turn after \ds \(attempt 1 of 3\): "
                            r"the agent stopped 4 times without running `vahtovik turn`", m) for m in messages)
    assert messages[-1].endswith("1 group turn (1 failed), 0 reports sent, 1 not sent, $0.01")


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
