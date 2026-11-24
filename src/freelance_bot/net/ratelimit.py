"""Вёдра токенов по доменам, экспоненциальная задержка с джиттером и оркестрация повторов.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass

from ..config import BackoffConfig, RateConfig


class AsyncTokenBucket:
    """Простое асинхронное ведро токенов. Один экземпляр на домен."""

    def __init__(self, rate_per_minute: float, burst: int = 1) -> None:
        self.rate = max(rate_per_minute, 0.01) / 60.0
        self.capacity = max(burst, 1)
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        """Ждёт, пока появится токен. Возвращает число ожидаемых секунд."""
        waited = 0.0
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                delay = deficit / self.rate
                waited += delay
                await asyncio.sleep(delay)


class DomainLimiter:
    """Хранит по одному ведру на домен и небольшую вежливую паузу между запросами.
    """

    def __init__(self, default: RateConfig, per_domain: dict[str, RateConfig], jitter: bool = True):
        self._default = default
        self._config = per_domain
        self._buckets: dict[str, AsyncTokenBucket] = {}
        self._jitter = jitter

    def _bucket(self, domain: str) -> AsyncTokenBucket:
        if domain not in self._buckets:
            cfg = self._config.get(domain, self._default)
            self._buckets[domain] = AsyncTokenBucket(cfg.requests_per_minute, cfg.burst)
        return self._buckets[domain]

    def _pause_range(self, domain: str) -> tuple[float, float]:
        """Границы human-паузы домена; перепутанные границы упорядочиваются."""
        cfg = self._config.get(domain, self._default)
        lo = max(0.0, cfg.pause_min_seconds)
        return lo, max(lo, cfg.pause_max_seconds)

    async def wait(self, domain: str) -> None:
        await self._bucket(domain).acquire()
        # Доменная human-пауза (например, kwork 3-10 с): правдоподобный для
        # человека интервал между запросами поверх вёдра токенов.
        lo, hi = self._pause_range(domain)
        if hi > 0:
            await asyncio.sleep(random.uniform(lo, hi))
            return
        # Правдоподобные для человека интервалы по умолчанию; достаточно
        # детерминированы для тестов без джиттера.
        delay = random.uniform(0.35, 1.4) if self._jitter else 0.0
        if delay:
            await asyncio.sleep(delay)


def backoff_delay(attempt: int, cfg: BackoffConfig) -> float:
    """Экспоненциальная задержка с полным джиттером и потолком."""
    raw = min(cfg.base_seconds * (2**attempt), cfg.max_seconds)
    if cfg.jitter:
        return random.uniform(raw * 0.5, raw)
    return raw


@dataclass
class RetryDecision:
    retry: bool
    reason: str


async def sleep_backoff(attempt: int, cfg: BackoffConfig) -> float:
    delay = backoff_delay(attempt, cfg)
    await asyncio.sleep(delay)
    return delay
