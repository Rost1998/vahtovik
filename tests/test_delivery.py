import pytest

from vahtovik import delivery


def test_a_notice_goes_through_the_channels_send_script(project):
    cfg, state, _ = project.load()
    row = delivery.notice(cfg, state, "log", "Subject", "The body.")
    assert row["status"] == "sent" and row["origin"] == "notice" and row["external_id"].startswith("data/outbox.md:")
    text = (cfg.root / "data/outbox.md").read_text()
    assert "· Subject\n_vahtovik_\n\nThe body." in text


def test_dry_run_does_not_write(project):
    cfg, state, _ = project.load()
    row = delivery.notice(cfg, state, "log", "s", "body", dry_run=True)
    assert row["status"] == "sent" and row["external_id"] == "dry-run"
    assert not (cfg.root / "data/outbox.md").exists()


def test_a_reply_names_the_message_it_answers(project):
    cfg, state, _ = project.load()
    delivery.reply(cfg, state, "log", "", "Done.", "m7")
    assert "_reply to message m7_\n\nDone." in (cfg.root / "data/outbox.md").read_text()


def test_failed_delivery_is_retried_up_to_the_limit(project):
    cfg, state, _ = project.load()
    row = delivery.notice(cfg, state, "broken", "s", "body")
    assert row["status"] == "failed" and "boom" in row["last_error"]
    for _ in range(10):
        delivery.retry_failed(cfg, state, dry_run=False)
    assert state.delivery(row["id"])["attempts"] == delivery.MAX_ATTEMPTS
    assert state.undelivered() == 1


def test_slow_send_script_times_out(project, monkeypatch):
    cfg, state, _ = project.load()
    monkeypatch.setattr(delivery, "SEND_TIMEOUT", 1)
    row = delivery.notice(cfg, state, "slow", "s", "body")
    assert row["status"] == "failed" and "timed out" in row["last_error"]


def test_a_notice_to_an_unknown_channel_is_refused(project):
    cfg, state, _ = project.load()
    with pytest.raises(delivery.DeliveryError, match="unknown channel 'nope'"):
        delivery.notice(cfg, state, "nope", "s", "body")
