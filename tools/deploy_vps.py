"""Развёртывание кода проекта на production-VPS.

Запускается вручную на локальной машине: python tools/deploy_vps.py [--no-live]

Что делает:
  1. Читает параметры подключения из секции `# VPS` в .env (host, port, name,
     passwd). Значения используются только в памяти и никогда не выводятся.
  2. Собирает tar.gz с кодом и документацией (без venv, var, config.yaml,
     .env — secrets и production-конфиг на VPS не перезаписываются).
  3. Заливает архив по SFTP в /tmp и распаковывает в /opt/freelance-bot.
  4. Точечно обновляет строку kwork.ru в config.yaml на VPS, добавляя
     human-паузу pause_min_seconds/pause_max_seconds (антиблокировка Qrator).
  5. Проверяет, что venv видит новый код и конфиг (без вывода секретов).
  6. Перезапускает все сервисы freelance-bot*.service через systemctl.
  7. Опционально (--no-live отключает) делает живую проверку kwork.ru с VPS.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tarfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]

# Код, тесты и фикстуры; config.yaml и .env целенаправленно исключены.
INCLUDE_DIRS = ("src", "tests", "fixtures")
INCLUDE_FILES = (
    "pyproject.toml",
    "config.example.yaml",
    ".env.example",
    "profile.yaml",
    "README.md",
    "MANUAL_RU.md",
    "DEPLOY_VPS.md",
    "TELEGRAM.md",
    "REPORT.md",
    "tools/check_kwork_live.py",
)
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}

APP_DIR = "/opt/freelance-bot"
KWORK_PAUSE = {"pause_min_seconds": "3", "pause_max_seconds": "10"}

# Точечная правка config.yaml на VPS, идемпотентная и самовосстанавливающаяся:
#   1) human-пауза в сетевой строке kwork.ru (Qrator);
#   2) poll_interval_seconds: 1800 в platforms.kwork_ru — опрос 2 раза в час;
#   3) proxy_strategy: sticky в platforms.kwork_ru — через пул прокси;
#   4) avito_ru.enabled: false — пул предназначен для kwork, не для avito.
PATCH_CONFIG_PY = r'''
import re, pathlib
p = pathlib.Path("@APP_DIR@/config.yaml")
text = p.read_text(encoding="utf-8")

# --- 1) строка kwork.ru в network.domains: пересобираем содержимое скобок ---
m = re.search(r"^(\s*)(kwork\.ru:.*)$", text, re.M)
if m is None:
    raise SystemExit("ОШИБКА: строка kwork.ru в config.yaml не найдена")
line = m.group(2)
inner = line[line.find("{") + 1:].replace("}", " ")
parts = [s.strip() for s in inner.split(",") if s.strip()]
# Идемпотентность: существующие pause-ключи убираем и добавляем заново.
parts = [s for s in parts if not s.startswith(("pause_min_seconds", "pause_max_seconds"))]
parts.append("pause_min_seconds: 3")
parts.append("pause_max_seconds: 10")
new_line = "kwork.ru:            { " + ", ".join(parts) + " }"
if new_line != line.rstrip():
    text = text[: m.start(2)] + new_line + text[m.end(2):]
    p.write_text(text, encoding="utf-8")
    print("network.kwork.ru обновлён")
else:
    print("network.kwork.ru уже корректен")


def _platform_block(lines: list, key: str) -> int:
    """Индекс строки-ключа платформы или -1."""
    return next(
        (i for i, l in enumerate(lines) if re.match(r"^  " + re.escape(key) + r":\s*$", l)),
        -1,
    )


def _block_end(lines: list, start: int) -> int:
    """Первый индекс после блока (строки с отступом 4+ или пустые)."""
    end = start + 1
    while end < len(lines) and (lines[end].startswith("    ") or not lines[end].strip()):
        end += 1
    return end


lines = p.read_text(encoding="utf-8").splitlines()

# --- 2) и 3) ключи в блоке platforms.kwork_ru ---
idx = _platform_block(lines, "kwork_ru")
if idx < 0:
    raise SystemExit("ОШИБКА: ключ platforms.kwork_ru не найден")
end = _block_end(lines, idx)
if not any("poll_interval_seconds" in lines[j] for j in range(idx + 1, end)):
    lines.insert(idx + 1, "    poll_interval_seconds: 1800")
    print("platforms.kwork_ru: добавлен poll_interval_seconds: 1800")
else:
    print("platforms.kwork_ru.poll_interval_seconds уже задан")
idx = _platform_block(lines, "kwork_ru")
end = _block_end(lines, idx)
if not any("proxy_strategy" in lines[j] for j in range(idx + 1, end)):
    lines.insert(idx + 1, "    proxy_strategy: sticky")
    print("platforms.kwork_ru: добавлен proxy_strategy: sticky")
else:
    print("platforms.kwork_ru.proxy_strategy уже задан")

# --- 4) avito выключен: пул прокси предназначен для kwork ---
idx = _platform_block(lines, "avito_ru")
if idx >= 0:
    end = _block_end(lines, idx)
    for j in range(idx + 1, end):
        if re.match(r"^\s*enabled:\s*true\s*$", lines[j]):
            lines[j] = lines[j].replace("true", "false", 1)
            print("platforms.avito_ru: enabled: true -> false")
            break
    else:
        print("platforms.avito_ru.enabled уже false")

p.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("итоговые строки kwork/avito:")
for l in p.read_text(encoding="utf-8").splitlines():
    low = l.lower()
    if ("kwork" in low and ("requests_per_minute" in l or "poll_interval" in l or "proxy_strategy" in l)) or (
        "avito" in low and "enabled" in l
    ):
        print("  " + l.strip())
'''.replace("@APP_DIR@", APP_DIR)

# Синхронизация RU_PROXY_URLS из локального .env в .env на VPS: значение
# передаётся внутри тела скрипта (канал SSH зашифрован) и никогда не выводится.
SYNC_PROXY_ENV_PY = r'''
import pathlib
p = pathlib.Path("@APP_DIR@/.env")
text = p.read_text(encoding="utf-8")
value = @PROXY_VALUE@
lines = text.splitlines()
replaced = False
for i, l in enumerate(lines):
    if l.strip().startswith("RU_PROXY_URLS="):
        lines[i] = "RU_PROXY_URLS=" + value
        replaced = True
        break
if not replaced:
    lines.append("RU_PROXY_URLS=" + value)
p.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("RU_PROXY_URLS на VPS обновлён" if replaced else "RU_PROXY_URLS на VPS добавлен")
'''.replace("@APP_DIR@", APP_DIR)

# Проверка прокси прямо на VPS: доступность каждого через его venv.
# Вывод — только количество живых, без значений.
CHECK_PROXIES_ON_VPS_PY = r'''
import asyncio, pathlib, re
text = pathlib.Path("@APP_DIR@/.env").read_text(encoding="utf-8")
urls: list = []
for l in text.splitlines():
    m = re.match(r"^RU_PROXY_URLS=(.*)$", l.strip())
    if m:
        urls = [u.strip() for u in m.group(1).split(",") if u.strip()]

async def check(url):
    from curl_cffi.requests import AsyncSession
    if "://" not in url:
        url = "http://" + url
    try:
        async with AsyncSession(impersonate="chrome131", timeout=12) as s:
            r = await s.get("https://api.ipify.org/", proxy=url)
            return r.status_code == 200
    except Exception:
        return False

async def main():
    if not urls:
        print("прокси на VPS не заданы")
        return
    results = await asyncio.gather(*(check(u) for u in urls))
    print(f"прокси живых: {sum(results)}/{len(urls)}")

asyncio.run(main())
'''.replace("@APP_DIR@", APP_DIR)

VERIFY_CONFIG_PY = r"""
from freelance_bot.config import load_config
c = load_config()
r = c.network.domains["kwork.ru"]
print("kwork rate:", r.requests_per_minute, "rpm, burst", r.burst,
      ", pause", r.pause_min_seconds, "-", r.pause_max_seconds, "s")
p = c.platforms.get("kwork_ru")
print("kwork poll_interval_seconds:", p.poll_interval_seconds if p else "нет ключа")
print("min_price_rub:", c.category_filter.min_price_rub)
print("конфиг читается корректно")
"""


def read_vps_credentials() -> tuple[str, int, str, str]:
    """Читает host/port/name/passwd из .env; значения не логируются."""
    values: dict[str, str] = {}
    for raw in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    missing = [k for k in ("host", "port", "name", "passwd") if not values.get(k)]
    if missing:
        raise SystemExit(f"ОШИБКА: в .env нет ключей для VPS: {', '.join(missing)}")
    return values["host"], int(values["port"]), values["name"], values["passwd"]


def build_tarball() -> bytes:
    """Собирает tar.gz с кодом и документацией; права в архиве фиксированы
    (644/755), потому что Windows-права для Linux непригодны."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for dirname in INCLUDE_DIRS:
            base = ROOT / dirname
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in EXCLUDE_PARTS]
                for name in dirnames:
                    _add_dir(tar, Path(dirpath) / name, dirname)
                for name in filenames:
                    _add_file(tar, Path(dirpath) / name, dirname)
        for name in INCLUDE_FILES:
            path = ROOT / name
            if path.is_file():
                _add_file(tar, path, ".")
            else:
                print(f"  (пропущен, не найден: {name})")
    return buf.getvalue()


def _add_dir(tar: tarfile.TarFile, path: Path, top: str) -> None:
    arcname = path.relative_to(ROOT).as_posix()
    info = tar.gettarinfo(str(path), arcname=arcname)
    info.type = tarfile.DIRTYPE
    info.mode, info.uid, info.gid, info.uname, info.gname = 0o755, 0, 0, "root", "root"
    tar.addfile(info)


def _add_file(tar: tarfile.TarFile, path: Path, top: str) -> None:
    arcname = path.relative_to(ROOT).as_posix()
    info = tar.gettarinfo(str(path), arcname=arcname)
    info.mode, info.uid, info.gid, info.uname, info.gname = 0o644, 0, 0, "root", "root"
    with open(path, "rb") as fh:
        tar.addfile(info, fh)


class VpsSession:
    """SSH-сессия с VPS; пароль sudo передаётся через stdin, не в команде."""

    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        import paramiko

        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            host, port=port, username=username, password=password,
            look_for_keys=False, allow_agent=False, timeout=20,
        )
        self.password = password
        self.is_root = False

    def run(self, cmd: str, *, sudo: bool = False, timeout: int = 60,
            stdin_text: str | None = None) -> tuple[int, str]:
        if sudo and not self.is_root:
            cmd = f"sudo -S -p '' bash -c {shlex_quote(cmd)}"
            if stdin_text:
                stdin_text = self.password + "\n" + stdin_text
            else:
                stdin_text = self.password + "\n"
        transport = self.client.get_transport()
        assert transport is not None
        channel = transport.open_session()
        channel.settimeout(timeout)
        channel.exec_command(cmd)
        if stdin_text:
            channel.sendall(stdin_text.encode("utf-8"))
        channel.shutdown_write()
        # Читаем оба потока, пока канал не закроется; иначе сообщение об
        # ошибке, попавшее в stderr, терялось и отладка становилась слепой.
        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []
        while True:
            progressed = False
            while channel.recv_ready():
                out_chunks.append(channel.recv(32768))
                progressed = True
            while channel.recv_stderr_ready():
                err_chunks.append(channel.recv_stderr(32768))
                progressed = True
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if not progressed:
                import time

                time.sleep(0.02)
        code = channel.recv_exit_status()
        stdout = b"".join(out_chunks).decode("utf-8", errors="replace")
        stderr = b"".join(err_chunks).decode("utf-8", errors="replace")
        combined = (stdout + ("\n" if stdout and stderr else "") + stderr).strip()
        return code, combined

    def close(self) -> None:
        self.client.close()


def shlex_quote(text: str) -> str:
    import shlex

    return shlex.quote(text)


def local_proxy_value() -> str:
    """Сырая строка RU_PROXY_URLS из локального .env (без нормализации схемы).

    Значение используется только для встраивания в тело скрипта синхронизации;
    в вывод не попадает никогда.
    """
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return ""
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^RU_PROXY_URLS=(.*)$", raw.strip())
        if m:
            return m.group(1).strip()
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Развёртывание freelance-bot на VPS")
    parser.add_argument("--no-live", action="store_true",
                        help="не запускать живую проверку kwork после деплоя")
    args = parser.parse_args()

    host, port, username, password = read_vps_credentials()
    print(f"1) Подключение к VPS (пользователь: {username}, порт: {port}) ...")
    vps = VpsSession(host, port, username, password)
    try:
        code, out = vps.run("id -u")
        vps.is_root = code == 0 and out.strip() == "0"
        prefix = "" if vps.is_root else " (sudo)"

        code, out = vps.run(f"test -d {APP_DIR}/src && echo yes || echo no")
        if out.strip() != "yes":
            raise SystemExit(f"ОШИБКА: на VPS нет {APP_DIR}/src — деплоить некуда")
        code, out = vps.run(f"stat -c '%U:%G' {APP_DIR}/src")
        owner = out.strip() or "unknown"
        print(f"   каталог {APP_DIR} найден, владелец: {owner}")

        print("2) Сборка архива с кодом ...")
        payload = build_tarball()
        print(f"   размер: {len(payload) / 1_048_576:.1f} МиБ")

        print("3) Загрузка по SFTP ...")
        sftp = vps.client.open_sftp()
        remote_tar = f"/tmp/freelance-bot-deploy-{os.getpid()}.tar.gz"
        with sftp.open(remote_tar, "wb") as fh:
            fh.write(payload)
        sftp.close()

        print(f"4) Распаковка в {APP_DIR}{prefix} ...")
        code, out = vps.run(
            f"tar -xzf {remote_tar} -C {APP_DIR} && echo extracted",
            sudo=not vps.is_root,
        )
        if "extracted" not in out:
            raise SystemExit(f"ОШИБКА распаковки: {out.strip()[:400]}")

        print(f"5) Права и владелец ({owner}){prefix} ...")
        dirs = " ".join(f"{APP_DIR}/{d}" for d in (*INCLUDE_DIRS, "tools"))
        files = " ".join(f"{APP_DIR}/{f}" for f in INCLUDE_FILES if (ROOT / f).is_file())
        cmd = f"chown -R {owner} {dirs} {files} && chmod -R u=rwX,go=rX {dirs} {files}"
        code, out = vps.run(cmd, sudo=not vps.is_root)
        if code != 0:
            raise SystemExit(f"ОШИБКА chown/chmod: {out.strip()[:400]}")

        print("6) Точечная правка config.yaml (kwork.ru + пауза + расписание + прокси) ...")
        code, out = vps.run("python3 -", stdin_text=PATCH_CONFIG_PY)
        if code != 0:
            raise SystemExit(f"ОШИБКА правки конфига: {out.strip()[:400]}")
        print("   " + out.strip().replace("\n", "\n   "))

        proxy_value = local_proxy_value()
        if proxy_value:
            print("6b) Синхронизация RU_PROXY_URLS в .env на VPS (значение скрыто) ...")
            sync_script = SYNC_PROXY_ENV_PY.replace("@PROXY_VALUE@", repr(proxy_value))
            code, out = vps.run("python3 -", stdin_text=sync_script)
            if code != 0:
                raise SystemExit(f"ОШИБКА синхронизации прокси: {out.strip()[:400]}")
            print("   " + out.strip())

            print("6c) Проверка прокси с VPS ...")
            code, out = vps.run(
                f"cd {APP_DIR} && venv/bin/python -",
                stdin_text=CHECK_PROXIES_ON_VPS_PY, timeout=120,
            )
            print("   " + out.strip().replace("\n", "\n   "))
            if code != 0:
                print("   ВНИМАНИЕ: проверка прокси завершилась с ошибкой (см. выше)")
        else:
            print("6b) Локальный RU_PROXY_URLS пуст — синхронизация не требуется")

        print("7) Проверка нового кода и конфига через venv на VPS ...")
        verify = f"cd {APP_DIR} && venv/bin/python -"
        code, out = vps.run(verify, sudo=not vps.is_root, stdin_text=VERIFY_CONFIG_PY,
                            timeout=120)
        if code != 0:
            raise SystemExit(f"ОШИБКА проверки: {out.strip()[:600]}")
        print("   " + out.strip().replace("\n", "\n   "))

        print("8) Перезапуск сервисов freelance-bot*.service ...")
        code, out = vps.run(
            "systemctl list-units --all --type=service --no-legend 2>/dev/null "
            "| grep -oE 'freelance-bot[a-zA-Z0-9_-]*\\.service' | sort -u",
            sudo=not vps.is_root,
        )
        services = sorted(set(out.split()))
        if not services:
            print("   сервисы freelance-bot не найдены — код обновлён, запуск вручную")
        for svc in services:
            code, out = vps.run(f"systemctl restart {svc} && systemctl is-active {svc}",
                                sudo=not vps.is_root, timeout=90)
            status = out.strip().splitlines()[-1] if out.strip() else f"exit {code}"
            print(f"   {svc}: {status}")
            if status != "active":
                code, out = vps.run(f"journalctl -u {svc} -n 12 --no-pager",
                                    sudo=not vps.is_root)
                print("      последние логи:\n      "
                      + out.strip().replace("\n", "\n      "))

        if not args.no_live and services:
            print("9) Живая проверка kwork.ru с VPS (прогрев → листинг → парсинг) ...")
            code, out = vps.run(
                f"cd {APP_DIR} && venv/bin/python tools/check_kwork_live.py",
                sudo=not vps.is_root, timeout=180,
            )
            print("   " + out.strip().replace("\n", "\n   "))
            if code != 0:
                print("   ВНИМАНИЕ: живая проверка завершилась с ошибкой (см. выше)")

        print("10) Очистка временного архива ...")
        vps.run(f"rm -f {remote_tar}", sudo=not vps.is_root)
        print("Готово: код на VPS обновлён.")
    finally:
        vps.close()


if __name__ == "__main__":
    main()
