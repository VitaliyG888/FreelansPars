"""Сетевые примитивы: прокси, троттлинг, robots, имитация TLS, браузер."""

from .browser import BrowserFetcher
from .captcha import CaptchaChallenge, CaptchaError, CaptchaSolver, build_solver
from .proxies import ProxyPool, ProxyState
from .ratelimit import AsyncTokenBucket, DomainLimiter, backoff_delay
from .robots import RobotsCache
from .stealth import FetchResult, StealthHttpClient, classify_response

__all__ = [
    "AsyncTokenBucket",
    "BrowserFetcher",
    "CaptchaChallenge",
    "CaptchaError",
    "CaptchaSolver",
    "DomainLimiter",
    "FetchResult",
    "ProxyPool",
    "ProxyState",
    "RobotsCache",
    "StealthHttpClient",
    "backoff_delay",
    "build_solver",
    "classify_response",
]
