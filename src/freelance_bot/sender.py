"""Доставка сообщений.

Три режима по возрастанию риска:

* ``dry_run``  – пишет файл для проверки + запись JSONL, ничего не отправляет (по умолчанию).
* ``assist``   – ставит в очередь оператору, создаёт артефакт для копирования.
* ``live``     – требует явный транспорт для каждой площадки И одобрение
                 каждого сообщения. Транспорт по умолчанию не поставляется,
                 потому что точка рассылки каждой российской площадки закрыта
                 авторизованной сессией, а автоматизация этого вне области задачи.

Ограничение частоты обеспечивается здесь, а не вызывающим кодом.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import MessagingConfig
from .models import GeneratedMessage, Job

# Точка расширения: здесь подключается транспорт площадки с авторизацией.
# проект намеренно не поставляет его, потому что каждая точка рассылки
# закрыты авторизацией, а автоматизация входа вне области задачи.
DeliveryTransport = Callable[[GeneratedMessage, Job], None]


class SendError(RuntimeError):
    pass


@dataclass
class SendResult:
    ok: bool
    mode: str
    path: str | None = None
    reason: str | None = None
    delivered: bool = False


class RateGate:
    def __init__(self, cfg: MessagingConfig) -> None:
        self.cfg = cfg
        self._sent: list[float] = []
        self._last = 0.0

    def allow(self) -> tuple[bool, str]:
        now = time.monotonic()
        hour_ago = now - 3600
        self._sent = [t for t in self._sent if t > hour_ago]
        if len(self._sent) >= self.cfg.max_sends_per_hour:
            return False, f"hourly limit {self.cfg.max_sends_per_hour} reached"
        if self._last and (now - self._last) < self.cfg.min_seconds_between_sends:
            remaining = self.cfg.min_seconds_between_sends - (now - self._last)
            return False, f"cooldown {remaining:.0f}s remaining"
        return True, "ok"

    def record(self) -> None:
        now = time.monotonic()
        self._sent.append(now)
        self._last = now


def _safe(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", name)[:80]


class OutboxSender:
    def __init__(
        self,
        outbox: str | Path,
        cfg: MessagingConfig,
        dry_run: bool = True,
        transport: DeliveryTransport | None = None,
    ) -> None:
        self.outbox = Path(outbox)
        self.cfg = cfg
        self.dry_run = dry_run
        self.transport = transport
        self.gate = RateGate(cfg)
        self.outbox.mkdir(parents=True, exist_ok=True)
        self._jsonl = self.outbox / "messages.jsonl"

    def send(
        self,
        message: GeneratedMessage,
        job: Job,
        *,
        approved: bool = False,
        message_id: int | None = None,
    ) -> SendResult:
        if message.violations:
            return SendResult(
                ok=False,
                mode="blocked",
                reason="сообщение не прошло проверку: " + "; ".join(message.violations),
            )
        if not self.dry_run:
            if self.cfg.require_human_approval and not approved:
                return SendResult(
                    ok=False, mode="blocked", reason="требуется подтверждение оператора"
                )
            if self.transport is None:
                return SendResult(
                    ok=False,
                    mode="blocked",
                    reason="транспорт доставки не настроен (outbox-only mode)",
                )
            allowed, reason = self.gate.allow()
            if not allowed:
                return SendResult(ok=False, mode="blocked", reason=reason)

        day = datetime.now(UTC).strftime("%Y-%m-%d")
        folder = self.outbox / day
        folder.mkdir(parents=True, exist_ok=True)
        base = f"{message.platform}__{_safe(message.job_uid)}"
        md_path = folder / f"{base}.md"

        proposals = job.proposals_count if job.proposals_count is not None else "—"
        mode = "dry-run" if self.dry_run else ("assist" if not approved else "live")
        checks = "OK" if not message.violations else "; ".join(message.violations)
        md_path.write_text(
            "\n".join(
                [
                    f"# {job.title}",
                    "",
                    f"- Платформа: `{job.platform}`",
                    f"- Ссылка: {job.url}",
                    f"- Бюджет: {job.budget_amount or '—'} {job.currency}"
                    f" (≈{job.budget_rub_estimate or '—'} ₽)",
                    f"- Откликов: {proposals}",
                    f"- Режим: {mode}",
                    f"- Источник текста: {message.source}",
                    f"- Проверки: {checks}",
                    "",
                    "## Сообщение",
                    "",
                    message.text,
                    "",
                ]
            ),
            encoding="utf-8",
        )

        delivered = False
        if not self.dry_run:
            assert self.transport is not None
            try:
                self.transport(message, job)
            except Exception as e:  # noqa: BLE001 - transport failure must not lose the artifact
                return SendResult(
                    ok=False,
                    mode="blocked",
                    path=str(md_path),
                    reason=f"transport error: {type(e).__name__}: {e}",
                )
            delivered = True
            self.gate.record()

        record = {
            "ts": datetime.now(UTC).isoformat(),
            "message_id": message_id,
            "job_uid": job.uid,
            "platform": message.platform,
            "title": job.title,
            "url": job.url,
            "text": message.text,
            "source": message.source,
            "model": message.model,
            "dry_run": self.dry_run,
            "approved": approved,
            "delivered": delivered,
        }
        with self._jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        return SendResult(
            ok=True,
            mode="dry-run" if self.dry_run else ("live" if approved else "assist"),
            path=str(md_path),
            delivered=delivered,
        )
