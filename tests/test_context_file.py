from vahtovik import context_file


def test_append_goes_to_the_right_section(tmp_path):
    path = tmp_path / "context.md"
    context_file.create(path, "Title", "w", "k")
    context_file.append(path, "Artifacts", context_file.artifact_row("src", "A | B", "https://x"))
    context_file.append(path, "Reports", "- report line")
    context_file.append(path, "Artifacts", context_file.artifact_row("src", "C", ""))
    text = path.read_text()
    artifacts_part = text.split("## Artifacts")[1].split("## Work log")[0]
    assert "A \\| B" in artifacts_part and "| C |" in artifacts_part
    assert text.split("## Reports")[1].strip() == "- report line"


def test_missing_section_is_added(tmp_path):
    path = tmp_path / "context.md"
    path.write_text("# Title\n\n## Summary\nx\n")
    context_file.append(path, "Reports", "- line")
    assert path.read_text().endswith("## Reports\n- line\n")


def test_fingerprint_counts_work_log_entries(tmp_path):
    path = tmp_path / "context.md"
    context_file.create(path, "Title", "w", "k")
    before = context_file.fingerprint(path)
    context_file.append(path, "Work log", "### 2026-10-17 10:00 · run r1\nDid things.")
    context_file.append(path, "Reports", "### not a work log entry")
    after = context_file.fingerprint(path)
    assert (before.work_log_entries, after.work_log_entries) == (0, 1)
    assert context_file.was_updated(before, after)
