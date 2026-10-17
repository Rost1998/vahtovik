import json

import pytest
from conftest import artifact

from vahtovik import artifacts, delivery


def _group(project):
    cfg, state, _ = project.load()
    artifacts.register(cfg, state, cfg.watch("w1"), artifact("a1", "g"), "src-a")
    return cfg, state


def test_channel_outside_the_watch_is_refused(project):
    cfg, state = _group(project)
    with pytest.raises(delivery.DeliveryError, match="not allowed"):
        delivery.report(cfg, state, "w1", "g", "other", "s", "body", "manual")


def test_dry_run_does_not_write(project):
    cfg, state = _group(project)
    row = delivery.report(cfg, state, "w1", "g", "log", "s", "body", "manual", dry_run=True)
    assert row["status"] == "sent" and row["external_id"] == "dry-run"
    assert not (cfg.root / "data/outbox.md").exists()
    assert "sent (dry run)" in (cfg.group_dir("w1", "g") / "context.md").read_text()


def test_failed_delivery_is_retried_up_to_the_limit(project):
    cfg, state = _group(project)
    row = delivery.report(cfg, state, "w1", "g", "broken", "s", "body", "manual")
    assert row["status"] == "failed" and "boom" in row["last_error"]
    for _ in range(10):
        delivery.retry_failed(cfg, state, dry_run=False)
    assert state.delivery(row["id"])["attempts"] == delivery.MAX_ATTEMPTS


def test_slow_send_script_times_out(project, monkeypatch):
    cfg, state = _group(project)
    monkeypatch.setattr(delivery, "SEND_TIMEOUT", 1)
    row = delivery.report(cfg, state, "w1", "g", "slow", "s", "body", "manual")
    assert row["status"] == "failed" and "timed out" in row["last_error"]


def test_invalid_and_duplicate_agent_files(project):
    cfg, state = _group(project)
    folder = cfg.group_dir("w1", "g") / "artifacts"
    (folder / "bad.json").write_text(json.dumps({"title": "no id"}))
    (folder / "dup.json").write_text(json.dumps({"id": "a1", "title": "again", "summary": "x"}))
    notes = artifacts.register_agent_files(cfg, state, cfg.watch("w1"), "g")
    assert (folder / "bad.json.invalid").exists() and not (folder / "dup.json").exists()
    assert any("already known" in n for n in notes)
