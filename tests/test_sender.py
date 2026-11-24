"""Гарантии доставки: артефакты dry-run, блокировки, ограничение частоты."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_job
from freelance_bot.config import MessageLimit, MessagingConfig
from freelance_bot.models import GeneratedMessage
from freelance_bot.sender import OutboxSender, RateGate


@pytest.fixture
def cfg() -> MessagingConfig:
    return MessagingConfig(
        limits={"fl_ru": MessageLimit(max_chars=1000)},
        require_human_approval=True,
        max_sends_per_hour=3,
        min_seconds_between_sends=0,
    )


def msg(text: str = "Здравствуйте! По парсеру каталога всё понятно.") -> GeneratedMessage:
    return GeneratedMessage(platform="fl_ru", job_uid="fl_ru:1", text=text)


def test_dry_run_writes_markdown_and_jsonl(tmp_path: Path, cfg: MessagingConfig) -> None:
    sender = OutboxSender(tmp_path / "outbox", cfg, dry_run=True)
    result = sender.send(msg(), make_job())
    assert result.ok and result.mode == "dry-run"
    assert Path(result.path).exists()
    content = Path(result.path).read_text(encoding="utf-8")
    assert "Здравствуйте" in content
    assert "dry-run" in content

    lines = (tmp_path / "outbox" / "messages.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    assert record["dry_run"] is True
    assert record["job_uid"] == "fl_ru:1"
    assert record["approved"] is False


def test_message_with_violations_is_never_written(tmp_path: Path, cfg: MessagingConfig) -> None:
    sender = OutboxSender(tmp_path / "outbox", cfg, dry_run=True)
    bad = msg()
    bad.violations = ["содержит ссылку или внешний контакт"]
    result = sender.send(bad, make_job())
    assert result.ok is False
    assert result.mode == "blocked"
    assert not (tmp_path / "outbox" / "messages.jsonl").exists()


def test_live_requires_approval(tmp_path: Path, cfg: MessagingConfig) -> None:
    sender = OutboxSender(tmp_path / "outbox", cfg, dry_run=False)
    result = sender.send(msg(), make_job(), approved=False)
    assert result.ok is False
    assert "подтвержд" in (result.reason or "")


def test_live_without_transport_is_blocked(tmp_path: Path, cfg: MessagingConfig) -> None:
    sender = OutboxSender(tmp_path / "outbox", cfg, dry_run=False)
    result = sender.send(msg(), make_job(), approved=True)
    assert result.ok is False
    assert "транспорт" in (result.reason or "")


def test_live_with_approval_invokes_transport(tmp_path: Path, cfg: MessagingConfig) -> None:
    delivered: list[str] = []
    sender = OutboxSender(
        tmp_path / "outbox",
        cfg,
        dry_run=False,
        transport=lambda message, job: delivered.append(job.uid),
    )
    result = sender.send(msg(), make_job(), approved=True)
    assert result.ok and result.mode == "live" and result.delivered
    assert delivered == ["fl_ru:1"]
    record = json.loads(
        (tmp_path / "outbox" / "messages.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert record["dry_run"] is False
    assert record["approved"] is True
    assert record["delivered"] is True


def test_rate_gate_enforces_hourly_limit() -> None:
    gate = RateGate(MessagingConfig(max_sends_per_hour=2, min_seconds_between_sends=0))
    assert gate.allow()[0] is True
    gate.record()
    assert gate.allow()[0] is True
    gate.record()
    allowed, reason = gate.allow()
    assert allowed is False
    assert "hourly limit" in reason


def test_rate_gate_enforces_cooldown() -> None:
    gate = RateGate(MessagingConfig(max_sends_per_hour=10, min_seconds_between_sends=300))
    gate.record()
    allowed, reason = gate.allow()
    assert allowed is False
    assert "cooldown" in reason


def test_rate_gate_does_not_record_in_dry_run(tmp_path: Path, cfg: MessagingConfig) -> None:
    sender = OutboxSender(tmp_path / "outbox", cfg, dry_run=True)
    for _ in range(10):
        assert sender.send(msg(), make_job()).ok
    assert sender.gate.allow()[0] is True
