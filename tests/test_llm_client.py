"""Клиент LLM: устойчивость к reasoning-моделям и JSON-обёрткам."""

from __future__ import annotations

import pytest

from freelance_bot.llm.client import LLMClient, LLMError, ResolvedProvider, unwrap_text


class _StubClient(LLMClient):
    """Клиент с подменённым HTTP-слоем: без сети."""

    def __init__(self, responses):
        self.cfg = type("Cfg", (), {"enabled": True, "temperature": 0.4,
                                    "max_output_tokens": 900, "timeout_seconds": 5})()
        self.provider = ResolvedProvider(
            name="stub", base_url="http://stub", model="m", api_key="k"
        )
        self.base_url = "http://stub"
        self.model = "m"
        self._api_key = "k"
        self._responses = list(responses)
        self.payloads = []

    async def _post(self, payload):
        self.payloads.append(dict(payload))
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_retries_with_bigger_budget_when_reasoning_ate_it() -> None:
    """Пустой content при finish=length — это бюджет на размышления, не поломка."""
    client = _StubClient([("", "length"), ("Готов обсудить задачу.", "stop")])

    out = await client.complete("sys", "user")

    assert out == "Готов обсудить задачу."
    assert len(client.payloads) == 2
    assert client.payloads[1]["max_tokens"] > client.payloads[0]["max_tokens"]


@pytest.mark.asyncio
async def test_retry_ladder_when_reasoning_needs_more_than_one_step() -> None:
    """Длинный промпт съедает и первый запас: поднимаем бюджет дальше."""
    client = _StubClient([("", "length"), ("", "length"), ("Письмо.", "stop")])

    out = await client.complete("sys", "user")

    assert out == "Письмо."
    budgets = [p["max_tokens"] for p in client.payloads]
    assert budgets == sorted(budgets), budgets
    assert budgets[-1] > budgets[1]


@pytest.mark.asyncio
async def test_empty_content_without_length_raises() -> None:
    client = _StubClient([("", "stop")])

    with pytest.raises(LLMError, match="empty content"):
        await client.complete("sys", "user")


def test_unwrap_text_extracts_json_payload() -> None:
    """Модель иногда оборачивает письмо в JSON — пользователю нужен текст."""
    assert unwrap_text('{"text": "Здравствуйте, готов обсудить."}') == (
        "Здравствуйте, готов обсудить."
    )
    assert unwrap_text('{"letter": "  Письмо  "}') == "Письмо"


def test_unwrap_text_keeps_plain_text_and_unknown_json() -> None:
    assert unwrap_text("Обычный текст письма.") == "Обычный текст письма."
    # Неизвестная структура — не выдумываем и оставляем как есть.
    assert unwrap_text('{"foo": "bar"}').startswith("{")
