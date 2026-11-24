"""Интеграции решателей CAPTCHA (CapSolver / 2Captcha).

Решатель вызывается только после того, как challenge *подтверждён* HTTP- или
браузерным слоем. Токены никогда не кешируются между сессиями, а API-ключи
никогда не логируются.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from curl_cffi.requests import AsyncSession

CAPSOLVER_CREATE = "https://api.capsolver.com/createTask"
CAPSOLVER_RESULT = "https://api.capsolver.com/getTaskResult"
TWOCAPTCHA_IN = "https://2captcha.com/in.php"
TWOCAPTCHA_RES = "https://2captcha.com/res.php"


class CaptchaError(RuntimeError):
    pass


@dataclass
class CaptchaChallenge:
    kind: str  # recaptcha_v2 | hcaptcha | yandex_smartcaptcha
    site_key: str
    page_url: str
    # Необязательный прокси в виде host:port:user:pass, нужен некоторым типам задач.
    proxy: str | None = None


# Типы задач CapSolver. Поддержку Yandex SmartCaptcha нужно сверить с
# тариф аккаунта, прежде чем включать в продакшене.
CAPSOLVER_TYPES = {
    "recaptcha_v2": "ReCaptchaV2TaskProxyLess",
    "hcaptcha": "HCaptchaTaskProxyLess",
    "yandex_smartcaptcha": "YandexSmartCaptchaTaskProxyLess",
}

TWOCAPTCHA_METHODS = {
    "recaptcha_v2": "userrecaptcha",
    "hcaptcha": "hcaptcha",
    # 2Captcha предоставляет Yandex SmartCaptcha как method=yandexcaptcha на поддерживаемых тарифах.
    "yandex_smartcaptcha": "yandexcaptcha",
}


class CaptchaSolver:
    def __init__(
        self,
        provider: str,
        api_key: str,
        *,
        timeout_seconds: int = 180,
        poll_interval: float = 4.0,
    ) -> None:
        if not api_key:
            raise CaptchaError(f"no API key configured for provider {provider!r}")
        if provider not in ("capsolver", "twocaptcha"):
            raise CaptchaError(f"unsupported provider {provider!r}")
        self.provider = provider
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.poll_interval = poll_interval

    async def solve(self, challenge: CaptchaChallenge) -> str:
        if self.provider == "capsolver":
            return await self._solve_capsolver(challenge)
        return await self._solve_2captcha(challenge)

    # ------------------------------------------------------------------ CapSolver
    async def _solve_capsolver(self, ch: CaptchaChallenge) -> str:
        task_type = CAPSOLVER_TYPES.get(ch.kind)
        if not task_type:
            raise CaptchaError(f"CapSolver does not support {ch.kind}")
        payload: dict[str, object] = {
            "clientKey": self.api_key,
            "task": {
                "type": task_type,
                "websiteURL": ch.page_url,
                "websiteKey": ch.site_key,
            },
        }
        async with AsyncSession(timeout=30) as s:
            r = await s.post(CAPSOLVER_CREATE, json=payload)
            data = r.json()
            if data.get("errorId"):
                raise CaptchaError(f"createTask failed: {data.get('errorDescription')}")
            task_id = data.get("taskId")
            if not task_id:
                raise CaptchaError("createTask returned no taskId")

            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                await asyncio.sleep(self.poll_interval)
                r = await s.post(
                    CAPSOLVER_RESULT, json={"clientKey": self.api_key, "taskId": task_id}
                )
                data = r.json()
                if data.get("errorId"):
                    raise CaptchaError(f"getTaskResult failed: {data.get('errorDescription')}")
                if data.get("status") == "ready":
                    solution = data.get("solution") or {}
                    token = solution.get("gRecaptchaResponse") or solution.get("token")
                    if solution.get("cookies"):
                        raise CaptchaError("cookie-based solution requires browser injection")
                    if not token:
                        raise CaptchaError("solution contained no token")
                    return str(token)
        raise CaptchaError("solver timed out")

    # ------------------------------------------------------------------ 2Captcha
    async def _solve_2captcha(self, ch: CaptchaChallenge) -> str:
        method = TWOCAPTCHA_METHODS.get(ch.kind)
        if not method:
            raise CaptchaError(f"2Captcha does not support {ch.kind}")
        params = {
            "key": self.api_key,
            "method": method,
            "pageurl": ch.page_url,
            "json": 1,
        }
        if ch.kind == "recaptcha_v2":
            params["googlekey"] = ch.site_key
        elif ch.kind == "hcaptcha":
            params["sitekey"] = ch.site_key
        else:
            params["sitekey"] = ch.site_key

        async with AsyncSession(timeout=30) as s:
            r = await s.post(TWOCAPTCHA_IN, data=params)
            data = r.json()
            if data.get("status") != 1:
                raise CaptchaError(f"submit failed: {data.get('request')}")
            task_id = data["request"]

            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                await asyncio.sleep(self.poll_interval)
                r = await s.get(
                    TWOCAPTCHA_RES,
                    params={"key": self.api_key, "action": "get", "id": task_id, "json": 1},
                )
                data = r.json()
                if data.get("status") == 1:
                    return str(data["request"])
                if data.get("request") != "CAPCHA_NOT_READY":
                    raise CaptchaError(f"solve failed: {data.get('request')}")
        raise CaptchaError("solver timed out")


def build_solver(provider: str, api_key: str, timeout_seconds: int = 180) -> CaptchaSolver | None:
    if not api_key:
        return None
    return CaptchaSolver(provider, api_key, timeout_seconds=timeout_seconds)
