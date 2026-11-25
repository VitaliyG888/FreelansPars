"""Минимальный чат-клиент, совместимый с OpenAI (работает с OpenAI, OpenRouter, vLLM).

Поддерживает как встроенный провайдер из ``.env`` (``LLM_BASE_URL``,
``LLM_MODEL``, ``LLM_API_KEY``), так и именованные провайдеры, добавленные
через Telegram (``llm.providers``). Ключ именованного провайдера берётся из
переменной окружения, имя которой хранится в ``api_key_env``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

from curl_cffi.requests import AsyncSession

from ..config import LLMConfig, Secrets

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedProvider:
    """Итоговые параметры провайдера, выбранного для текущего цикла."""

    name: str
    base_url: str
    model: str
    api_key: str = ""
    api_key_required: bool = True
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.api_key) or not self.api_key_required


def resolve_provider(cfg: LLMConfig, secrets: Secrets) -> ResolvedProvider:
    """Выбирает активного провайдера: именованный из Telegram или встроенный."""
    provider = cfg.active_llm_provider()
    if provider is None:
        return ResolvedProvider(
            name="default (.env)",
            base_url=secrets.llm_base_url.rstrip("/"),
            model=secrets.llm_model,
            api_key=secrets.llm_api_key,
            api_key_required=True,
        )

    key = os.getenv(provider.api_key_env, "") if provider.api_key_env else ""
    if not key and provider.api_key_required:
        # Провайдер без собственного ключа может использовать общий ключ .env.
        key = secrets.llm_api_key
    return ResolvedProvider(
        name=cfg.active_provider,
        base_url=provider.base_url.rstrip("/"),
        model=provider.model,
        api_key=key,
        api_key_required=provider.api_key_required,
        headers=dict(provider.headers),
    )


class LLMClient:
    def __init__(self, cfg: LLMConfig, secrets: Secrets) -> None:
        self.cfg = cfg
        self.provider = resolve_provider(cfg, secrets)
        self.base_url = self.provider.base_url
        self.model = self.provider.model
        self._api_key = self.provider.api_key

    @property
    def available(self) -> bool:
        return self.cfg.enabled and self.provider.configured

    async def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        if not self.available:
            raise LLMError("LLM is not configured (missing API key or disabled)")
        budget = max_tokens or self.cfg.max_output_tokens
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": budget,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        content, finish = await self._post(payload)
        # Reasoning-модели (nemotron, o-series, R1) тратят весь бюджет на
        # размышления и возвращают пустой content при finish_reason=length.
        # Один повтор с запасом дешевле, чем молчаливый переход на шаблон.
        if not content.strip() and finish == "length":
            budget = min(budget * 4, 16_000)
            payload["max_tokens"] = budget
            log.info(
                "LLM returned empty content (finish=length), retrying with %s tokens",
                budget,
            )
            content, finish = await self._post(payload)
        if not content.strip():
            raise LLMError(
                f"LLM returned empty content (finish_reason={finish or 'unknown'})"
            )
        return unwrap_text(content.strip())

    async def _post(self, payload: dict[str, object]) -> tuple[str, str | None]:
        """Один запрос к провайдеру: (content, finish_reason)."""
        headers = {
            "Content-Type": "application/json",
            **self.provider.headers,
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with AsyncSession(timeout=self.cfg.timeout_seconds) as session:
            resp = await session.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers
            )
            if resp.status_code >= 400:
                body = (resp.text or "")[:300]
                raise LLMError(f"LLM HTTP {resp.status_code}: {body}")
            data = resp.json()

        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected LLM response shape: {str(data)[:200]}") from e
        if not isinstance(content, str):
            content = ""
        return content, choice.get("finish_reason")

    async def complete_json(self, system: str, user: str) -> dict:
        raw = await self.complete(system, user, json_mode=True)
        return parse_json_loose(raw)


def unwrap_text(raw: str) -> str:
    """Достаёт текст письма из ответа модели.

    Модель иногда возвращает не голый текст, а JSON вроде
    ``{"text": "..."}`` — особенно если её так обучили или сбил соседний
    json_mode. Пользователю нужен текст письма, а не фигурные скобки.
    """
    text = raw.strip()
    if not text.startswith(("{", "[")):
        return text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(data, dict):
        for key in ("text", "message", "letter", "draft", "content", "response"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return text


def parse_json_loose(raw: str) -> dict:
    """Модели иногда оборачивают JSON в ограждения или добавляют текст. Восстанавливаем аккуратно.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError as e:
                raise LLMError(f"invalid JSON from model: {e}") from e
        raise LLMError("model returned no JSON object") from None
