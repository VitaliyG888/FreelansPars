"""Хранение в SQLite: просмотренные заказы, баллы, сгенерированные сообщения, состояние outbox.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .models import GeneratedMessage, Job, OrderStatus, ScoredJob
from .order_links import resolve_order_url

if TYPE_CHECKING:
    from .categories import Classification
    from .user_prefs import UserFilterPrefs

#: Разрешённые переходы статусов карточки. Повторный callback идемпотентен:
#: переход в тот же статус не считается ошибкой, обратный — запрещён.
STATUS_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset(
        {OrderStatus.SHOWN, OrderStatus.SENT_CLICKED, OrderStatus.SKIPPED}
    ),
    OrderStatus.SHOWN: frozenset({OrderStatus.SENT_CLICKED, OrderStatus.SKIPPED}),
    OrderStatus.SENT_CLICKED: frozenset({OrderStatus.SKIPPED}),
    OrderStatus.SKIPPED: frozenset(),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    uid TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT NOT NULL,
    order_url TEXT,
    title TEXT NOT NULL,
    budget_rub REAL,
    score REAL,
    eligible INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',
    draft_text TEXT,
    card_text TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    notified_at TEXT,
    payload TEXT NOT NULL,
    category_key TEXT,
    category_group TEXT,
    subcategory TEXT,
    penalty REAL,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_platform ON jobs(platform);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score DESC);

CREATE TABLE IF NOT EXISTS user_filters (
    chat_id INTEGER PRIMARY KEY,
    platforms TEXT NOT NULL DEFAULT '[]',
    categories TEXT NOT NULL DEFAULT '[]',
    subcategories TEXT NOT NULL DEFAULT '{}',
    min_price_rub REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_favorites (
    user_id INTEGER NOT NULL,
    job_uid TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, job_uid)
);

CREATE TABLE IF NOT EXISTS user_deliveries (
    chat_id INTEGER NOT NULL,
    job_uid TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, job_uid)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_job ON user_deliveries(job_uid);

CREATE TABLE IF NOT EXISTS user_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    job_uid TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_job ON user_actions(job_uid);
CREATE INDEX IF NOT EXISTS idx_actions_created ON user_actions(created_at);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uid TEXT NOT NULL,
    platform TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    model TEXT,
    violations TEXT NOT NULL DEFAULT '[]',
    approved INTEGER,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(job_uid, text)
);
CREATE INDEX IF NOT EXISTS idx_messages_job ON messages(job_uid);

CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    dry_run INTEGER NOT NULL,
    report TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_admins (
    chat_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    created_at TEXT NOT NULL,
    is_super INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bot_subscribers (
    chat_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_subscribers_status ON bot_subscribers(status);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class JobStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "notified_at" not in columns:
                # Старые базы не знали о показе в Telegram. Всё, что уже лежит
                # в них, было обработано прошлыми циклами, поэтому помечаем его
                # показанным: после обновления бот не пришлёт старый бэклог.
                self._conn.execute("ALTER TABLE jobs ADD COLUMN notified_at TEXT")
                self._conn.execute("UPDATE jobs SET notified_at = last_seen")
            if "eligible" not in columns:
                # Старые базы не хранили результат порогов. Признак появится
                # после ближайшего прохода, который пересчитает все заказы.
                self._conn.execute(
                    "ALTER TABLE jobs ADD COLUMN eligible INTEGER NOT NULL DEFAULT 0"
                )
            # --- миграция под карточки заказов (ссылка, черновик, статус) ---
            if "order_url" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN order_url TEXT")
            if "status" not in columns:
                self._conn.execute(
                    "ALTER TABLE jobs ADD COLUMN status TEXT NOT NULL DEFAULT 'new'"
                )
                # Всё, что уже показывалось в Telegram, получает shown.
                self._conn.execute(
                    "UPDATE jobs SET status = 'shown' WHERE notified_at IS NOT NULL"
                )
            if "draft_text" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN draft_text TEXT")
            if "card_text" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN card_text TEXT")
            # --- миграция под личные фильтры: классификация и приоритет ---
            # category_key/group/subcategory нужны фильтрам пользователей,
            # penalty/published_at — сортировке «свежие и чистые первыми».
            for name, decl in (
                ("category_key", "TEXT"),
                ("category_group", "TEXT"),
                ("subcategory", "TEXT"),
                ("penalty", "REAL"),
                ("published_at", "TEXT"),
            ):
                if name not in columns:
                    self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")

            self._backfill_order_urls()
            # --- миграция под роли админов: супер-админ назначается /set_admin ---
            # Колонка is_super отделяет администратора настроек от обычного
            # подписчика, добавленного через /adduser или одобрение заявки.
            admin_columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(bot_admins)").fetchall()
            }
            if "is_super" not in admin_columns:
                self._conn.execute(
                    "ALTER TABLE bot_admins ADD COLUMN is_super INTEGER NOT NULL DEFAULT 0"
                )
            # --- миграция статусов подписки: approved → active ---
            # Раньше одобренная подписка называлась 'approved'. Теперь статусы
            # pending/active/rejected; старые строки приводим к новому имени,
            # иначе одобренные пользователи потеряли бы доступ.
            self._conn.execute(
                "UPDATE bot_subscribers SET status = 'active' WHERE status = 'approved'"
            )
            # Индекс создаётся после миграции: в старой базе колонки status
            # ещё нет, и CREATE INDEX из SCHEMA упал бы.
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
            self._conn.commit()

    def _backfill_order_urls(self) -> None:
        """Заполняет order_url для строк, сохранённых до обновления.

        Валидация выполняется здесь, а не в SQL: whitelist доменов живёт в
        Python-модуле и не дублируется в схеме.
        """
        rows = self._conn.execute(
            "SELECT uid, platform, external_id, url FROM jobs WHERE order_url IS NULL"
        ).fetchall()
        updates: list[tuple[str, str]] = []
        for row in rows:
            url = resolve_order_url(row["platform"], row["external_id"], row["url"])
            if url is not None:
                updates.append((url, row["uid"]))
        if updates:
            self._conn.executemany("UPDATE jobs SET order_url = ? WHERE uid = ?", updates)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -------------------------------------------------------------------- заказы
    def seen(self, uid: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM jobs WHERE uid = ?", (uid,)).fetchone()
        return row is not None

    def notified(self, uid: str) -> bool:
        """Был ли заказ уже показан в Telegram (или обработан CLI-циклом)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT notified_at FROM jobs WHERE uid = ?", (uid,)
            ).fetchone()
        return bool(row and row["notified_at"])

    def mark_notified(self, uid: str) -> None:
        """Помечает заказ показанным, не откатывая более поздний статус."""
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET notified_at = COALESCE(notified_at, ?),"
                " status = CASE WHEN status = 'new' THEN 'shown' ELSE status END"
                " WHERE uid = ?",
                (_now(), uid),
            )
            self._conn.commit()

    # ------------------------------------------------------------------ админы
    def list_bot_admins(self) -> set[int]:
        """ID чатов, добавленные через /adduser (живут в базе, переживают рестарт)."""
        with self._lock:
            rows = self._conn.execute("SELECT chat_id FROM bot_admins").fetchall()
        return {int(row["chat_id"]) for row in rows}

    def add_bot_admin(self, chat_id: int, added_by: int | None) -> bool:
        """Добавляет админа; False, если такой уже есть."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO bot_admins (chat_id, added_by, created_at) VALUES (?, ?, ?)",
                (chat_id, added_by, _now()),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def remove_bot_admin(self, chat_id: int) -> bool:
        """Убирает админа; False, если его не было."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM bot_admins WHERE chat_id = ?", (chat_id,)
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def list_super_admins(self) -> set[int]:
        """ID чатов, назначенных администраторами командой /set_admin.

        Это отдельный слой поверх ``bot_admins``: обычный подписчик видит
        карточки и личные фильтры, а разделы настроек открыты только супер-админам
        (плюс администраторам из ``TELEGRAM_ADMIN_CHAT_IDS``).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT chat_id FROM bot_admins WHERE is_super = 1"
            ).fetchall()
        return {int(row["chat_id"]) for row in rows}

    def set_super_admin(self, chat_id: int, enabled: bool, added_by: int | None = None) -> bool:
        """Включает или снимает роль администратора настроек.

        Возвращает True, если состояние изменилось. Назначение сразу создаёт
        запись в ``bot_admins``: супер-админ обязан иметь доступ к боту, иначе
        он не сможет получить ни карточек, ни ответов на команды.
        """
        with self._lock:
            existing = self._conn.execute(
                "SELECT is_super FROM bot_admins WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if existing is None:
                if not enabled:
                    return False
                self._conn.execute(
                    "INSERT INTO bot_admins (chat_id, added_by, created_at, is_super)"
                    " VALUES (?, ?, ?, 1)",
                    (chat_id, added_by, _now()),
                )
                self._conn.commit()
                return True
            if bool(existing["is_super"]) == enabled:
                return False
            self._conn.execute(
                "UPDATE bot_admins SET is_super = ? WHERE chat_id = ?",
                (1 if enabled else 0, chat_id),
            )
            self._conn.commit()
        return True

    # ------------------------------------------------------------------ подписки
    def add_subscriber_request(
        self, chat_id: int, username: str | None, full_name: str | None
    ) -> bool:
        """Создаёт заявку на подписку; False, если заявка уже существует.

        Поля username/full_name заполняются из профиля Telegram и нужны админу,
        чтобы решить, кого одобрять. Повторный /start не создаёт дубликат.
        """
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO bot_subscribers"
                " (chat_id, username, full_name, status, requested_at)"
                " VALUES (?,?,?,'pending',?)",
                (chat_id, username, full_name, _now()),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def get_subscriber(self, chat_id: int) -> dict | None:
        """Возвращает заявку/подписку пользователя или None, если её нет."""
        with self._lock:
            row = self._conn.execute(
                "SELECT chat_id, username, full_name, status, requested_at,"
                " decided_at, decided_by FROM bot_subscribers WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def set_subscriber_status(
        self, chat_id: int, status: str, decided_by: int | None = None
    ) -> bool:
        """Обновляет статус заявки (approved/rejected); False, если записи нет."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE bot_subscribers SET status = ?, decided_at = ?, decided_by = ?"
                " WHERE chat_id = ?",
                (status, _now(), decided_by, chat_id),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def upsert_subscriber_status(
        self, chat_id: int, status: str, decided_by: int | None = None
    ) -> None:
        """Ставит статус заявки, создавая запись при её отсутствии.

        Нужен /deluser: отзыв доступа у пользователя без заявки должен
        блокировать повторный запрос через /start.
        """
        with self._lock:
            updated = self._conn.execute(
                "UPDATE bot_subscribers SET status = ?, decided_at = ?, decided_by = ?"
                " WHERE chat_id = ?",
                (status, _now(), decided_by, chat_id),
            ).rowcount
            if not updated:
                self._conn.execute(
                    "INSERT INTO bot_subscribers (chat_id, status, requested_at)"
                    " VALUES (?,?,?)",
                    (chat_id, status, _now()),
                )
            self._conn.commit()

    def list_subscribers(self, status: str | None = None) -> list[dict]:
        """Заявки на подписку; с фильтром по статусу, новые первыми."""
        with self._lock:
            if status is not None:
                rows = self._conn.execute(
                    "SELECT chat_id, username, full_name, status, requested_at"
                    " FROM bot_subscribers WHERE status = ? ORDER BY requested_at",
                    (status,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT chat_id, username, full_name, status, requested_at"
                    " FROM bot_subscribers ORDER BY requested_at"
                ).fetchall()
        return [dict(r) for r in rows]

    def get_order(self, uid: str) -> dict | None:
        """Возвращает карточку заказа для callback-обработчика.

        Читается всё, что нужно кнопкам: ссылка, черновик, текст карточки и
        текущий статус. Так состояние переживает рестарт бота.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT uid, platform, external_id, url, order_url, title, status,"
                " draft_text, card_text, score, budget_rub, payload FROM jobs WHERE uid = ?",
                (uid,),
            ).fetchone()
        return dict(row) if row is not None else None

    def set_draft(self, uid: str, draft_text: str, card_text: str | None = None) -> None:
        """Сохраняет сгенерированное письмо.

        Если строка заказа ещё не существует (тесты, direct API), вставляет
        минимальную строку с uid. Это делает set_draft idempotentotentным.
        """
        with self._lock:
            rows = self._conn.execute("SELECT 1 FROM jobs WHERE uid = ?", (uid,)).fetchall()
            if rows:
                self._conn.execute(
                    "UPDATE jobs SET draft_text = ?, card_text = COALESCE(?, card_text)"
                    " WHERE uid = ?",
                    (draft_text, card_text, uid),
                )
            else:
                # Вставляем заглушку — все обязательные поля кроме uid пусты.
                self._conn.execute(
                    "INSERT INTO jobs (uid, platform, external_id, url, title,"
                    " first_seen, last_seen, payload, status, draft_text)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (uid, "", "", "", "", _now(), _now(), "{}", "shown", draft_text),
                )
            self._conn.commit()

    def set_status(self, uid: str, status: OrderStatus) -> bool:
        """Переводит заказ в новый статус с проверкой допустимости перехода.

        Возвращает True, если состояние изменилось. Повторный перевод в уже
        установленный статус считается успешным идемпотентным вызовом.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM jobs WHERE uid = ?", (uid,)
            ).fetchone()
            if row is None:
                return False
            current = OrderStatus(row["status"])
            if current == status:
                return True
            if status not in STATUS_TRANSITIONS[current]:
                return False
            self._conn.execute(
                "UPDATE jobs SET status = ? WHERE uid = ?", (status.value, uid)
            )
            self._conn.commit()
            return True

    def status_of(self, uid: str) -> OrderStatus | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM jobs WHERE uid = ?", (uid,)
            ).fetchone()
        return OrderStatus(row["status"]) if row is not None else None

    def mark_sent_clicked(self, uid: str) -> bool:
        """Фиксирует нажатие «Отправить». Повторное нажатие не меняет статус."""
        return self.set_status(uid, OrderStatus.SENT_CLICKED)

    def mark_skipped(self, uid: str) -> bool:
        return self.set_status(uid, OrderStatus.SKIPPED)

    def log_action(
        self, user_id: int | None, job_uid: str, action: str, details: dict | None = None
    ) -> None:
        """Журнал действий оператора: id, заказ, действие, время.

        В логи приложения (logging) PII не попадает: черновики и заголовки
        остаются только в этой таблице.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO user_actions (user_id, job_uid, action, details, created_at)"
                " VALUES (?,?,?,?,?)",
                (
                    user_id,
                    job_uid,
                    action,
                    json.dumps(details or {}, ensure_ascii=False),
                    _now(),
                ),
            )
            self._conn.commit()

    def action_history(self, job_uid: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, action, details, created_at FROM user_actions"
                " WHERE job_uid = ? ORDER BY id DESC LIMIT ?",
                (job_uid, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def upsert_job(
        self,
        job: Job,
        score: float | None = None,
        eligible: bool | None = None,
        *,
        penalty: float | None = None,
        classification: Classification | None = None,
    ) -> bool:
        """Вставляет или обновляет заказ. Возвращает True, если заказ новый.

        ``eligible`` — прошёл ли заказ общий фильтр категорий/цены на текущем
        проходе. ``None`` оставляет прежнее значение нетронутым.
        ``classification`` — сопоставление с таксономией Kwork, по которому
        работают личные фильтры пользователей; ``penalty`` — штраф красных
        флагов, понижающий приоритет доставки.
        """
        payload = job.model_dump_json()
        eligible_value = None if eligible is None else int(eligible)
        published = (
            job.published_at.isoformat() if job.published_at is not None else None
        )
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM jobs WHERE uid = ?", (job.uid,)).fetchone()
            is_new = row is None
            if is_new:
                self._conn.execute(
                    "INSERT INTO jobs (uid, platform, external_id, url, title, budget_rub,"
                    " score, eligible, first_seen, last_seen, payload,"
                    " category_key, category_group, subcategory, penalty, published_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        job.uid,
                        job.platform,
                        job.external_id,
                        job.url,
                        job.title,
                        job.budget_rub_estimate,
                        score,
                        eligible_value or 0,
                        _now(),
                        _now(),
                        payload,
                        classification.category_key if classification else None,
                        classification.group if classification else None,
                        classification.subcategory if classification else None,
                        penalty,
                        published,
                    ),
                )
            else:
                self._conn.execute(
                    "UPDATE jobs SET last_seen = ?, budget_rub = ?, score = COALESCE(?, score),"
                    " eligible = COALESCE(?, eligible), payload = ?,"
                    " category_key = COALESCE(?, category_key),"
                    " category_group = COALESCE(?, category_group),"
                    " subcategory = COALESCE(?, subcategory),"
                    " penalty = COALESCE(?, penalty),"
                    " published_at = COALESCE(?, published_at) WHERE uid = ?",
                    (
                        _now(),
                        job.budget_rub_estimate,
                        score,
                        eligible_value,
                        payload,
                        classification.category_key if classification else None,
                        classification.group if classification else None,
                        classification.subcategory if classification else None,
                        penalty,
                        published,
                        job.uid,
                    ),
                )
            self._conn.commit()
        return is_new

    def save_scored(
        self,
        scored: ScoredJob,
        *,
        eligible: bool | None = None,
        classification: Classification | None = None,
    ) -> None:
        self.upsert_job(
            scored.job,
            scored.score.total,
            eligible,
            penalty=scored.score.penalty,
            classification=classification,
        )

    def top_recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT uid, platform, title, score, budget_rub, url, last_seen"
                " FROM jobs ORDER BY last_seen DESC, score DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def eligible_jobs(self, limit: int = 10) -> list[dict]:
        """Подходящие заказы для команды /jobs, сначала с лучшим баллом."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT uid, platform, title, score, budget_rub, url, first_seen,"
                " last_seen, notified_at FROM jobs WHERE eligible = 1"
                " ORDER BY score DESC, last_seen DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def eligible_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM jobs WHERE eligible = 1"
            ).fetchone()
        return int(row["c"])

    # ------------------------------------------------------------------ личные фильтры
    def get_user_filters(self, chat_id: int) -> UserFilterPrefs:
        """Фильтры пользователя; для отсутствующей записи — «показывать всё»."""
        from .user_prefs import UserFilterPrefs

        with self._lock:
            row = self._conn.execute(
                "SELECT chat_id, platforms, categories, subcategories, min_price_rub"
                " FROM user_filters WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        if row is None:
            return UserFilterPrefs.default_for(chat_id)
        return UserFilterPrefs.from_row(dict(row))

    def set_user_filters(
        self,
        chat_id: int,
        *,
        platforms: list[str] | None = None,
        categories: list[str] | None = None,
        subcategories: dict[str, list[str]] | None = None,
        min_price_rub: float | None = None,
    ) -> UserFilterPrefs:
        """Обновляет поля фильтров; None — поле не трогать.

        Возвращает свежий снимок фильтров. Пустой список = «все биржи/рубрики».
        """
        from .user_prefs import _json_loads_dict, _json_loads_list

        with self._lock:
            current = self._conn.execute(
                "SELECT platforms, categories, subcategories, min_price_rub"
                " FROM user_filters WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            if current is None:
                base_platforms: list[str] = []
                base_categories: list[str] = []
                base_subcats: dict[str, list[str]] = {}
                base_price = 0.0
                self._conn.execute(
                    "INSERT OR IGNORE INTO user_filters (chat_id, updated_at) VALUES (?,?)",
                    (chat_id, _now()),
                )
            else:
                base_platforms = _json_loads_list(current["platforms"])
                base_categories = _json_loads_list(current["categories"])
                base_subcats = _json_loads_dict(current["subcategories"])
                base_price = float(current["min_price_rub"] or 0.0)

            new_platforms = base_platforms if platforms is None else sorted(set(platforms))
            new_categories = (
                base_categories if categories is None else list(dict.fromkeys(categories))
            )
            new_subcats = base_subcats if subcategories is None else {
                key: list(dict.fromkeys(value)) for key, value in subcategories.items() if value
            }
            new_price = base_price if min_price_rub is None else max(0.0, float(min_price_rub))

            self._conn.execute(
                "UPDATE user_filters SET platforms = ?, categories = ?,"
                " subcategories = ?, min_price_rub = ?, updated_at = ? WHERE chat_id = ?",
                (
                    json.dumps(new_platforms, ensure_ascii=False),
                    json.dumps(new_categories, ensure_ascii=False),
                    json.dumps(new_subcats, ensure_ascii=False),
                    new_price,
                    _now(),
                    chat_id,
                ),
            )
            self._conn.commit()
        return self.get_user_filters(chat_id)

    def reset_user_filters(self, chat_id: int) -> None:
        """Удаляет личные фильтры: пользователь снова получает всё."""
        with self._lock:
            self._conn.execute("DELETE FROM user_filters WHERE chat_id = ?", (chat_id,))
            self._conn.commit()

    def all_user_filters(self) -> dict[int, UserFilterPrefs]:
        """Снимки фильтров всех пользователей, задавших хотя бы один ограничение."""
        from .user_prefs import UserFilterPrefs

        with self._lock:
            rows = self._conn.execute("SELECT * FROM user_filters").fetchall()
        return {int(row["chat_id"]): UserFilterPrefs.from_row(dict(row)) for row in rows}

    def user_jobs(
        self, prefs: UserFilterPrefs, limit: int = 10
    ) -> tuple[list[dict], int]:
        """Последние заказы по фильтрам пользователя.

        Приоритет выдачи: без штрафа — первыми, заказы со штрафом красных
        флагов — последними; внутри каждой группы свежие выше старых.
        Возвращает (список карточек, всего подходящих).
        """
        from .user_prefs import UserFilterPrefs

        if not isinstance(prefs, UserFilterPrefs):  # pragma: no cover - защита от типа
            raise TypeError("prefs must be UserFilterPrefs")
        with self._lock:
            rows = self._conn.execute(
                "SELECT uid, platform, title, budget_rub, score, penalty, url,"
                " order_url, category_key, category_group, subcategory,"
                " first_seen, last_seen, published_at, notified_at, status"
                " FROM jobs WHERE eligible = 1"
                " ORDER BY (COALESCE(penalty, 0) > 0) ASC,"
                " COALESCE(published_at, last_seen) DESC, last_seen DESC"
                " LIMIT 500"
            ).fetchall()
        matched = [
            dict(row)
            for row in rows
            if prefs.matches(
                platform=row["platform"],
                category_key=row["category_key"],
                subcategory=row["subcategory"],
                group=row["category_group"],
                budget_rub_estimate=row["budget_rub"],
            )
        ]
        return matched[:limit], len(matched)

    # ------------------------------------------------------------------ избранное
    def add_favorite(self, user_id: int, job_uid: str) -> bool:
        """Кладёт заказ в избранное пользователя; False, если уже там."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO user_favorites (user_id, job_uid, created_at)"
                " VALUES (?,?,?)",
                (user_id, job_uid, _now()),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def remove_favorite(self, user_id: int, job_uid: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM user_favorites WHERE user_id = ? AND job_uid = ?",
                (user_id, job_uid),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def is_favorite(self, user_id: int, job_uid: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM user_favorites WHERE user_id = ? AND job_uid = ?",
                (user_id, job_uid),
            ).fetchone()
        return row is not None

    def favorites(self, user_id: int, limit: int = 50) -> list[dict]:
        """Избранные заказы с данными карточки; свежие добавления первыми."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT j.uid, j.platform, j.title, j.budget_rub, j.score, j.url,"
                " j.order_url, j.status, j.notified_at, f.created_at AS fav_at"
                " FROM user_favorites f LEFT JOIN jobs j ON j.uid = f.job_uid"
                " WHERE f.user_id = ? ORDER BY f.created_at DESC, j.last_seen DESC"
                " LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ доставка пользователям
    def mark_delivered_to(self, chat_id: int, job_uid: str) -> None:
        """Помечает заказ доставленным конкретному пользователю (один раз)."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO user_deliveries (chat_id, job_uid, delivered_at)"
                " VALUES (?,?,?)",
                (chat_id, job_uid, _now()),
            )
            self._conn.commit()

    def delivered_to(self, chat_id: int, job_uid: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM user_deliveries WHERE chat_id = ? AND job_uid = ?",
                (chat_id, job_uid),
            ).fetchone()
        return row is not None

    # ---------------------------------------------------------------- сообщения
    def save_message(self, msg: GeneratedMessage) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO messages"
                " (job_uid, platform, text, source, model, violations, approved, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    msg.job_uid,
                    msg.platform,
                    msg.text,
                    msg.source,
                    msg.model,
                    json.dumps(msg.violations, ensure_ascii=False),
                    1 if msg.approved else (0 if msg.approved is False else None),
                    msg.created_at.isoformat(),
                ),
            )
            self._conn.commit()
            if cur.lastrowid:
                return int(cur.lastrowid)
            row = self._conn.execute(
                "SELECT id FROM messages WHERE job_uid = ? AND text = ?",
                (msg.job_uid, msg.text),
            ).fetchone()
            return int(row["id"]) if row else 0

    def message_exists(self, job_uid: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM messages WHERE job_uid = ? LIMIT 1", (job_uid,)
            ).fetchone()
        return row is not None

    def approve(self, message_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE messages SET approved = 1 WHERE id = ?", (message_id,))
            self._conn.commit()

    def reject(self, message_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE messages SET approved = 0 WHERE id = ?", (message_id,))
            self._conn.commit()

    def mark_sent(self, message_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET sent_at = ?, approved = 1 WHERE id = ?",
                (_now(), message_id),
            )
            self._conn.commit()

    def pending_messages(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, job_uid, platform, text, violations FROM messages"
                " WHERE sent_at IS NULL AND (approved IS NULL OR approved = 0)"
                " ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def save_cycle(
        self, report_json: str, started_at: str, finished_at: str, dry_run: bool
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO cycles (started_at, finished_at, dry_run, report) VALUES (?,?,?,?)",
                (started_at, finished_at, 1 if dry_run else 0, report_json),
            )
            self._conn.commit()

    # ------------------------------------------------------------------- статистика
    def stats_since(self, days: int = 7, min_score: float = 0.0) -> dict[str, int]:
        """Счётчики для карточки /stats в Telegram.

        ``min_score`` оставлен для совместимости и больше не используется:
        подходящие заказы считаются по флагу eligible (прошли фильтры).
        """
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._lock:
            jobs = self._conn.execute(
                "SELECT COUNT(*) AS c FROM jobs WHERE first_seen >= ?", (cutoff,)
            ).fetchone()["c"]
            eligible = self._conn.execute(
                "SELECT COUNT(*) AS c FROM jobs WHERE first_seen >= ? AND eligible = 1",
                (cutoff,),
            ).fetchone()["c"]
            messages = self._conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE created_at >= ?", (cutoff,)
            ).fetchone()["c"]
            sent = self._conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE sent_at IS NOT NULL AND sent_at >= ?",
                (cutoff,),
            ).fetchone()["c"]
            rejected = self._conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE approved = 0 AND created_at >= ?",
                (cutoff,),
            ).fetchone()["c"]
        return {
            "total_jobs": int(jobs),
            "total_new": int(jobs),
            "total_eligible": int(eligible),
            "total_messages": int(messages),
            "total_sent": int(sent),
            "total_rejected": int(rejected),
        }
