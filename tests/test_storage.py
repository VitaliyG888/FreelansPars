"""Хранение в SQLite: дедупликация, жизненный цикл сообщений, история циклов.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_job
from freelance_bot.categories import Classification
from freelance_bot.models import GeneratedMessage, ScoreBreakdown, ScoredJob
from freelance_bot.storage import JobStore
from freelance_bot.user_prefs import UserFilterPrefs


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    s = JobStore(tmp_path / "test.db")
    yield s
    s.close()


def test_new_job_detected_then_deduped(store: JobStore) -> None:
    job = make_job()
    assert store.seen(job.uid) is False
    assert store.upsert_job(job) is True
    assert store.seen(job.uid) is True
    assert store.upsert_job(job) is False


# --------------------------------------------------------------------------- #
# Подписки: заявка, статусы, списки
# --------------------------------------------------------------------------- #


def test_subscriber_request_created_once(store: JobStore) -> None:
    assert store.get_subscriber(777) is None
    assert store.add_subscriber_request(777, "newcomer", "Новый") is True
    assert store.add_subscriber_request(777, "newcomer", "Новый") is False

    row = store.get_subscriber(777)
    assert row is not None
    assert row["status"] == "pending"
    assert row["username"] == "newcomer"
    assert row["full_name"] == "Новый"


def test_subscriber_status_transitions(store: JobStore) -> None:
    store.add_subscriber_request(777, None, None)
    assert store.set_subscriber_status(777, "approved", decided_by=1) is True
    assert store.get_subscriber(777)["status"] == "approved"
    assert store.get_subscriber(777)["decided_by"] == 1
    assert store.set_subscriber_status(777, "rejected") is True
    assert store.get_subscriber(777)["status"] == "rejected"
    # Несуществующая заявка не обновляется.
    assert store.set_subscriber_status(42, "approved") is False


def test_subscribers_list_filters_by_status(store: JobStore) -> None:
    store.add_subscriber_request(1, "a", None)
    store.add_subscriber_request(2, "b", None)
    store.set_subscriber_status(1, "approved")

    pending = store.list_subscribers("pending")
    assert [r["chat_id"] for r in pending] == [2]
    approved = store.list_subscribers("approved")
    assert [r["chat_id"] for r in approved] == [1]
    assert {r["chat_id"] for r in store.list_subscribers()} == {1, 2}


def test_notified_marker_is_idempotent(store: JobStore) -> None:
    job = make_job()
    store.upsert_job(job)
    assert store.notified(job.uid) is False
    store.mark_notified(job.uid)
    assert store.notified(job.uid) is True
    store.mark_notified(job.uid)  # повторная запись не меняет состояние
    assert store.notified(job.uid) is True


def test_old_database_backfills_notified_state(tmp_path: Path) -> None:
    """После обновления старый бэклог не должен снова приходить в Telegram."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE jobs (
            uid TEXT PRIMARY KEY, platform TEXT NOT NULL, external_id TEXT NOT NULL,
            url TEXT NOT NULL, title TEXT NOT NULL, budget_rub REAL, score REAL,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, payload TEXT NOT NULL
        );
        INSERT INTO jobs VALUES
            ('fl_ru:1','fl_ru','1','https://x','old',1000,50,
             '2026-11-01T00:00:00+00:00','2026-11-02T00:00:00+00:00','{}');
        """
    )
    conn.commit()
    conn.close()

    store = JobStore(path)
    try:
        assert store.notified("fl_ru:1") is True
    finally:
        store.close()


def test_upsert_updates_score_and_payload(store: JobStore) -> None:
    job = make_job()
    store.upsert_job(job, score=10)
    store.upsert_job(job, score=90)
    rows = store.top_recent(1)
    assert rows[0]["score"] == 90


def test_message_saved_and_deduplicated(store: JobStore) -> None:
    msg = GeneratedMessage(platform="fl_ru", job_uid="fl_ru:1", text="Привет")
    first = store.save_message(msg)
    second = store.save_message(msg)
    assert first == second
    assert store.message_exists("fl_ru:1") is True
    assert store.message_exists("fl_ru:2") is False


def test_message_approval_flow(store: JobStore) -> None:
    msg = GeneratedMessage(platform="fl_ru", job_uid="fl_ru:1", text="Привет")
    msg_id = store.save_message(msg)
    pending = store.pending_messages()
    assert len(pending) == 1
    assert pending[0]["id"] == msg_id

    store.approve(msg_id)
    assert store.pending_messages() == []

    store.mark_sent(msg_id)
    assert store.pending_messages() == []


def test_violations_roundtrip(store: JobStore) -> None:
    import json

    msg = GeneratedMessage(
        platform="fl_ru", job_uid="fl_ru:9", text="Текст", violations=["слишком длинное"]
    )
    store.save_message(msg)
    row = store.pending_messages()[0]
    assert json.loads(row["violations"]) == ["слишком длинное"]


def test_cycle_history(store: JobStore) -> None:
    store.save_cycle(
        '{"ok": true}', "2026-11-03T00:00:00+00:00", "2026-11-03T00:01:00+00:00", dry_run=True
    )
    row = store._conn.execute("SELECT * FROM cycles").fetchone()
    assert row["dry_run"] == 1
    assert row["report"] == '{"ok": true}'


def test_scored_job_persisted(store: JobStore) -> None:
    scored = ScoredJob(job=make_job(), score=ScoreBreakdown(total=77.5, factors=[]))
    store.save_scored(scored)
    assert store.top_recent(1)[0]["score"] == 77.5


def test_eligible_jobs_are_listed_by_score(store: JobStore) -> None:
    low = ScoredJob(
        job=make_job(external_id="1", title="Слабый заказ"),
        score=ScoreBreakdown(total=21.0, factors=[]),
    )
    high = ScoredJob(
        job=make_job(external_id="2", title="Сильный заказ"),
        score=ScoreBreakdown(total=88.0, factors=[]),
    )
    skipped = ScoredJob(
        job=make_job(external_id="3", title="Неподходящий"),
        score=ScoreBreakdown(total=99.0, factors=[]),
    )
    store.save_scored(low, eligible=True)
    store.save_scored(high, eligible=True)
    store.save_scored(skipped, eligible=False)

    jobs = store.eligible_jobs(10)

    assert [job["title"] for job in jobs] == ["Сильный заказ", "Слабый заказ"]
    assert store.eligible_count() == 2
    assert all(job["notified_at"] is None for job in jobs)


def test_eligibility_and_notification_flags_are_refreshed(store: JobStore) -> None:
    scored = ScoredJob(
        job=make_job(), score=ScoreBreakdown(total=60.0, factors=[])
    )
    store.save_scored(scored, eligible=True)
    store.mark_notified(scored.job.uid)
    assert store.eligible_jobs(1)[0]["notified_at"]

    # Повторный проход с непрошедшим порогом убирает заказ из /jobs.
    store.save_scored(scored, eligible=False)
    assert store.eligible_jobs(10) == []
    assert store.eligible_count() == 0


# --------------------------------------------------------------------------- #
# Личные фильтры, избранное, персональная доставка
# --------------------------------------------------------------------------- #


def scored_with_classification(
    *, external_id: str, title: str, platform: str = "fl_ru", penalty: float = 0.0
) -> tuple[ScoredJob, Classification]:
    """ScoredJob + классификация для сохранения: category_key/подкатегория."""
    scored = ScoredJob(
        job=make_job(external_id=external_id, title=title, platform=platform),
        score=ScoreBreakdown(total=50.0, penalty=penalty, factors=[]),
    )
    classification = Classification(
        category_key="programming",
        category_name="Разработка ботов",
        group="Разработка ботов",
        subcategory="Боты и чат-боты",
        confidence=0.9,
        matched="бот",
    )
    return scored, classification


def test_user_filters_roundtrip(store: JobStore) -> None:
    prefs = store.get_user_filters(111)
    # Пустой фильтр по умолчанию: показывать всё.
    assert prefs.platforms == [] and prefs.categories == []

    saved = store.set_user_filters(
        111,
        platforms=["fl_ru"],
        categories=["programming"],
        subcategories={"programming": ["Боты и чат-боты"]},
        min_price_rub=5000,
    )
    assert saved.platforms == ["fl_ru"]
    assert saved.subcategories == {"programming": ["Боты и чат-боты"]}
    assert store.get_user_filters(111).min_price_rub == 5000

    # Частичное обновление не трогает остальные поля.
    store.set_user_filters(111, min_price_rub=7000)
    again = store.get_user_filters(111)
    assert again.platforms == ["fl_ru"] and again.min_price_rub == 7000

    store.reset_user_filters(111)
    assert store.get_user_filters(111).platforms == []


def test_reclassify_keeps_subscriber_chosen_categories(store: JobStore) -> None:
    """Переклассификация не отсекает рубрики, выбранные подписчиком.

    Админский фильтр собирает только программирование, но подписчик выбрал
    тексты — после пересчёта такие заказы должны остаться подходящими.
    """
    from freelance_bot.categories import CategoryFilter

    job = make_job(title="Написать три статьи", description="Тексты для блога")
    store.upsert_job(job, eligible=0)
    store.set_user_filters(555, categories=["writing_translations"])

    cat_filter = CategoryFilter(enabled=True, categories=("programming",))
    stats = store.reclassify_jobs(cat_filter=cat_filter)

    assert stats["checked"] == 1
    row = store._conn.execute(  # noqa: SLF001 - проверка состояния базы
        "SELECT category_key, eligible FROM jobs WHERE uid = ?", (job.uid,)
    ).fetchone()
    assert row["category_key"] == "writing_translations"
    assert row["eligible"] == 1


def test_reclassify_drops_disabled_platform_jobs(store: JobStore) -> None:
    """Заказы с отключённой биржи перестают быть подходящими.

    Когда владелец оставляет только одну площадку, старые заказы остальных
    бирж не должны всплывать в /jobs.
    """
    from freelance_bot.categories import CategoryFilter

    site = Classification(category_key="programming", category_name="Разработка и IT")
    kept = make_job(platform="kwork_ru", external_id="k1", title="Создание сайта")
    dropped = make_job(platform="fl_ru", external_id="f1", title="Создание сайта")
    store.upsert_job(kept, eligible=1, classification=site)
    store.upsert_job(dropped, eligible=1, classification=site)

    store.reclassify_jobs(
        cat_filter=CategoryFilter(enabled=True, categories=("programming",)),
        enabled_platforms={"kwork_ru"},
    )

    rows = {
        row["platform"]: row["eligible"]
        for row in store._conn.execute(  # noqa: SLF001 - проверка состояния базы
            "SELECT platform, eligible FROM jobs"
        )
    }
    assert rows["kwork_ru"] == 1
    assert rows["fl_ru"] == 0


def test_reclassify_drops_job_nobody_wants(store: JobStore) -> None:
    """Заказ вне админского фильтра и вне выбора подписчиков остаётся вне выдачи."""
    from freelance_bot.categories import CategoryFilter

    job = make_job(title="Монтаж видео для блога", description="Видеоролик")
    store.upsert_job(job, eligible=0)
    store.set_user_filters(555, categories=["writing_translations"])

    cat_filter = CategoryFilter(enabled=True, categories=("programming",))
    store.reclassify_jobs(cat_filter=cat_filter)

    row = store._conn.execute(  # noqa: SLF001 - проверка состояния базы
        "SELECT category_key, eligible FROM jobs WHERE uid = ?", (job.uid,)
    ).fetchone()
    assert row["category_key"] == "audio_video"
    assert row["eligible"] == 0


def test_reclassify_respects_confidence_threshold(store: JobStore) -> None:
    """Слабое совпадение не становится подходящим из-за выбора подписчика.

    «Настройка VPS» совпадала только по слову «сервер» из описания (балл
    0.23). Выбор рубрики подписчиком отменял порог уверенности, и такой
    заказ попадал в выдачу вместе с настоящими заказами.
    """
    from freelance_bot.categories import CategoryFilter

    job = make_job(
        title="Составить смету по готовому проекту",
        description="Расчёт для проекта, нейросети не нужны",
    )
    store.upsert_job(job, eligible=0)
    store.set_user_filters(555, categories=["programming"])
    store._conn.execute(  # noqa: SLF001 - готовим запись с известным слабым совпадением
        "UPDATE jobs SET category_key = 'programming', category_group = NULL,"
        " subcategory = NULL WHERE uid = ?",
        (job.uid,),
    )

    store.reclassify_jobs(
        cat_filter=CategoryFilter(
            enabled=True, categories=("programming",), min_confidence=0.35
        )
    )

    row = store._conn.execute(  # noqa: SLF001 - проверка состояния базы
        "SELECT eligible FROM jobs WHERE uid = ?", (job.uid,)
    ).fetchone()
    assert row["eligible"] == 0


def test_reclassify_price_is_hard_limit_even_for_subscriber(store: JobStore) -> None:
    """Цена ниже порога — жёсткое условие, выбор подписчика его не отменяет.

    Раньше `allows_price` проверялся только для общего фильтра, а ветка
    «рубрика выбрана подписчиком» цену игнорировала: заказы за 500 ₽ снова
    становились подходящими и попадали в /jobs.
    """
    from freelance_bot.categories import CategoryFilter
    from freelance_bot.models import BudgetKind

    cheap = make_job(
        external_id="c1", title="Создание сайта-визитки", budget=500.0
    )
    negotiable = make_job(
        external_id="c2",
        title="Создание сайта под ключ",
        budget=None,  # type: ignore[arg-type]
        kind=BudgetKind.UNKNOWN,
    )
    store.upsert_job(cheap, eligible=1)
    store.upsert_job(negotiable, eligible=1)
    store.set_user_filters(555, categories=["programming"])

    store.reclassify_jobs(
        cat_filter=CategoryFilter(
            enabled=True, categories=("programming",), min_price_rub=1000
        )
    )

    rows = {
        row["uid"]: row["eligible"]
        for row in store._conn.execute(  # noqa: SLF001 - проверка состояния базы
            "SELECT uid, eligible FROM jobs"
        )
    }
    assert rows[cheap.uid] == 0
    # «По договорённости» — цена неизвестна, такой заказ нужен всегда.
    assert rows[negotiable.uid] == 1


def test_user_jobs_counts_match_platform_counts(store: JobStore) -> None:
    """Счётчики бирж в меню совпадают с числом заказов в списке.

    Раньше выборка обрезалась до 500 строк до применения личных фильтров,
    поэтому меню обещало больше заказов, чем показывал список.
    """
    site = Classification(category_key="programming", category_name="Разработка и IT")
    texts = Classification(category_key="writing_translations", category_name="Тексты")
    # Заказов намеренно больше прежнего ограничения выборки (500 строк):
    # иначе тест проходил бы и с ошибкой.
    site_total = 620
    for index in range(site_total):
        job = make_job(
            platform="kwork_ru",
            external_id=f"k{index}",
            title=f"Создание сайта номер {index}",
            description="Нужен сайт под ключ",
        )
        store.upsert_job(job, eligible=1, classification=site)
    for index in range(5):
        job = make_job(
            platform="fl_ru",
            external_id=f"f{index}",
            title=f"Написать статью {index}",
            description="Текст для блога",
        )
        store.upsert_job(job, eligible=1, classification=texts)

    prefs = UserFilterPrefs(chat_id=555, categories=["programming"])
    counts = store.user_platform_counts(prefs)
    assert counts["kwork_ru"] == site_total

    _, total = store.user_jobs(prefs, limit=1)
    assert total == site_total


def test_user_jobs_filtering_and_priority(store: JobStore) -> None:
    """user_jobs применяет личные фильтры; штрафные заказы идут последними."""
    clean, classification = scored_with_classification(external_id="1", title="Чистый")
    flagged, flag_class = scored_with_classification(
        external_id="2", title="С флагами", penalty=0.7
    )
    store.save_scored(clean, eligible=True, classification=classification)
    store.save_scored(flagged, eligible=True, classification=flag_class)

    prefs = store.get_user_filters(111)
    rows, total = store.user_jobs(prefs, 10)
    assert total == 2
    # Приоритет: без штрафа — первым, со штрафом — последним.
    assert [row["title"] for row in rows] == ["Чистый", "С флагами"]

    # Фильтр по платформе скрывает заказ.
    prefs = store.set_user_filters(111, platforms=["kwork_ru"])
    rows, total = store.user_jobs(prefs, 10)
    assert (rows, total) == ([], 0)


def test_user_jobs_respects_min_price(store: JobStore) -> None:
    scored, classification = scored_with_classification(external_id="1", title="Дешёвый")
    store.save_scored(scored, eligible=True, classification=classification)

    prefs = store.set_user_filters(111, min_price_rub=10**9)
    rows, total = store.user_jobs(prefs, 10)
    assert (rows, total) == ([], 0)


def test_favorites_roundtrip(store: JobStore) -> None:
    scored, classification = scored_with_classification(external_id="1", title="Любимый")
    store.save_scored(scored, eligible=True, classification=classification)

    assert store.add_favorite(111, scored.job.uid) is True
    assert store.add_favorite(111, scored.job.uid) is False  # уже в избранном
    assert store.is_favorite(111, scored.job.uid) is True
    assert store.is_favorite(222, scored.job.uid) is False  # чужое избранное

    rows = store.favorites(111)
    assert len(rows) == 1 and rows[0]["uid"] == scored.job.uid

    assert store.remove_favorite(111, scored.job.uid) is True
    assert store.remove_favorite(111, scored.job.uid) is False
    assert store.favorites(111) == []


def test_delivery_marks_are_per_chat(store: JobStore) -> None:
    scored, classification = scored_with_classification(external_id="1", title="Заказ")
    store.save_scored(scored, eligible=True, classification=classification)

    store.mark_delivered_to(111, scored.job.uid)
    assert store.delivered_to(111, scored.job.uid) is True
    assert store.delivered_to(222, scored.job.uid) is False
