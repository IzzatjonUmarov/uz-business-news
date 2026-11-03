"""Сбор новостей в очередь и публикация из очереди не чаще одного поста в POST_INTERVAL_MINUTES.

Сбор (раз в --loop секунд): новые ссылки → подготовка (текст, узбекская версия, оценка ИИ) → очередь в state.
Публикация (проверяется каждую минуту): если с последнего поста прошло достаточно времени —
отправляется один пост из очереди: сначала важнее (оценка ИИ), затем свежее. Время последнего поста
хранится в state, поэтому после перезапуска бот не публикует всё накопившееся залпом.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import httpx

from .ai import Editor
from .config import Settings, Source, load_sources
from .fetch import NewsItem, enrich, fetch_source, first_paragraph, make_client
from .format import CAPTION_LIMIT, TELEGRAM_LIMIT, build_post
from .images import build_image
from .importance import rule_importance
from .rates import build_rates_post, fetch_rates
from .state import State
from .telegram import TelegramError
from .uzbek import fetch_uzbek, to_new_latin, uzbek_version

log = logging.getLogger("bot")

PREPARE_LIMIT = 30   # сколько новых новостей готовить за одну проверку (остальные — в следующую)
MAX_ATTEMPTS = 3     # после стольких ошибок отправки пост выбрасывается из очереди


# ── Сбор ────────────────────────────────────────────────────────────

def collect(settings: Settings, sources: list[Source]) -> dict[str, list[NewsItem] | Exception]:
    results: dict[str, list[NewsItem] | Exception] = {}

    def work(source: Source):
        with make_client(settings.request_timeout) as client:
            try:
                return source.id, fetch_source(client, source)
            except Exception as exc:  # noqa: BLE001 — один упавший сайт не должен ронять весь прогон
                return source.id, exc

    with ThreadPoolExecutor(max_workers=8) as pool:
        for source_id, result in pool.map(work, sources):
            results[source_id] = result
    return results


def _generic_images(state: State, item: NewsItem) -> set[str]:
    """Картинка, которая встречается у разных новостей, — заглушка сайта (логотип, баннер), а не фото."""
    owners: dict = state.meta.setdefault("image_owner", {})
    generic: list = state.meta.setdefault("generic_images", [])
    for url in [item.image, *item.extra.get("content_images", [])]:
        if not url:
            continue
        owner = owners.get(url)
        if owner and owner != item.url and url not in generic:
            generic.append(url)
        owners.setdefault(url, item.url)
    if len(owners) > 5000:
        state.meta["image_owner"] = dict(list(owners.items())[-3000:])
    return set(generic)


def _drop_repeated_summary(state: State, item: NewsItem) -> None:
    """Одинаковое описание у разных новостей одного сайта — шаблон, заменяем первым абзацем текста."""
    if not item.summary:
        return
    owners: dict = state.meta.setdefault("summary_owner", {})
    key = f"{item.source_id}:{item.summary[:200]}"
    owner = owners.setdefault(key, item.url)
    if owner != item.url:
        item.summary = first_paragraph(item.text, item.title)
    if len(owners) > 5000:
        state.meta["summary_owner"] = dict(list(owners.items())[-3000:])


def _cutoff(settings: Settings) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=settings.max_age_days)


def prepare(settings: Settings, state: State, client: httpx.Client, item: NewsItem,
            editor: Editor | None) -> dict | None:
    """Готовит новость к публикации. None — новость не публикуется (старая или неважная)."""
    enrich(client, item)
    if item.published and item.published < _cutoff(settings):
        log.info("  пропуск (старая, %s): %s", item.published.date(), item.url)
        return None
    uz, uz_source = (fetch_uzbek(client, item), "official") if settings.uzbek_enabled else (None, "none")
    if settings.uzbek_enabled and not uz and not editor:
        # нет ни официальной версии, ни Claude — бесплатный машинный перевод
        uz, uz_source = uzbek_version(client, item)
    if settings.uzbek_enabled and not uz:
        log.info("  нет узбекской версии, пост только на русском: %s", item.url)
    review = editor.review(item, uz if uz_source == "official" else None) if editor else None
    if review and settings.uzbek_enabled and review.get("headline_uz"):
        # страховка: даже если модель сбилась на старую латиницу — приводим к новой
        uz = (to_new_latin(review["headline_uz"]), to_new_latin(review.get("summary_uz", "")))
    if review:
        importance, threshold = float(review["importance"]), settings.min_importance
    else:
        importance, threshold = rule_importance(item, uz), settings.min_importance_rules
    if importance < threshold:
        log.info("  пропуск (значимость %.1f < %s): %s", importance, threshold, item.title[:80])
        return None
    _drop_repeated_summary(state, item)
    _generic_images(state, item)
    data = asdict(item)
    data["published"] = item.published.isoformat() if item.published else None
    data["text"] = item.text[:3000]
    return {"kind": "news", "item": data, "review": review, "uz": list(uz) if uz else None, "uz_source": uz_source,
            "importance": importance, "queued_at": time.time(), "attempts": 0}


def _item_from(entry: dict) -> NewsItem:
    data = dict(entry["item"])
    data["published"] = datetime.fromisoformat(data["published"]) if data.get("published") else None
    return NewsItem(**data)


def collect_into_queue(settings: Settings, state: State, only: str | None = None,
                       initialize: bool = True) -> int:
    """Собирает новые новости и кладёт их в очередь. Возвращает, сколько добавлено."""
    sources = [s for s in load_sources(settings.sources_file) if not only or s.id == only]
    if not settings.ai_active:
        sources = [s for s in sources if not s.requires_ai]
    editor = Editor(settings.anthropic_api_key, settings.ai_model) if settings.ai_active else None
    queue: list = state.meta.setdefault("queue", [])
    queued = {e["item"]["url"] for e in queue if e.get("kind") == "news"}

    results = collect(settings, sources)
    fresh: list[NewsItem] = []
    for source in sources:
        result = results[source.id]
        if isinstance(result, Exception):
            state.record_source(source.id, ok=False, found=0, error=str(result)[:300])
            log.warning("✗ %-12s %s", source.id, result)
            continue
        new = [i for i in result if not state.is_seen(i.url) and i.url not in queued]
        first_time = not state.source_initialized(source.id)
        state.record_source(source.id, ok=bool(result), found=len(result),
                            error=None if result else "ни одной новости не найдено")
        if first_time and result and initialize:
            # первый запуск по источнику: запоминаем текущую ленту, чтобы не завалить канал архивом
            for item in result:
                state.mark(item.url)
            log.info("• %-12s инициализирован, запомнено %d ссылок", source.id, len(result))
            continue
        if new:
            log.info("✓ %-12s найдено %d, новых %d", source.id, len(result), len(new))
        fresh.extend(new)

    added = 0
    with make_client(settings.request_timeout) as client:
        for item in fresh[:PREPARE_LIMIT]:
            entry = prepare(settings, state, client, item, editor)
            state.mark(item.url)
            if entry:
                queue.append(entry)
                added += 1
    _trim_queue(settings, state)
    log.info("Проверка источников: в очередь добавлено %d, всего в очереди %d", added, len(queue))
    return added


def enqueue_rates(settings: Settings, state: State) -> None:
    """Курсы ЦБ раз в день — тоже через очередь, чтобы соблюдался интервал между постами."""
    with make_client(settings.request_timeout) as client:
        post, date = build_rates_post(fetch_rates(client))
    queue: list = state.meta.setdefault("queue", [])
    if state.meta.get("rates_posted_for") == date or any(e.get("rates_date") == date for e in queue):
        return
    queue.append({"kind": "rates", "post": post, "rates_date": date, "importance": 6,
                  "queued_at": time.time(), "attempts": 0})
    log.info("Курсы ЦБ на %s добавлены в очередь", date)


# ── Очередь ─────────────────────────────────────────────────────────

def _published_ts(entry: dict) -> float:
    published = entry.get("item", {}).get("published")
    return datetime.fromisoformat(published).timestamp() if published else entry["queued_at"]


def _ordered(queue: list, last_source: str | None = None) -> list:
    # сначала важнее; при равной важности — не тот же источник, что в прошлом посте; затем свежее
    def key(e):
        source = e.get("item", {}).get("source_id")
        return (round(e.get("importance", 3), 1), source != last_source, _published_ts(e))
    return sorted(queue, key=key, reverse=True)


def _trim_queue(settings: Settings, state: State) -> None:
    queue: list = state.meta.setdefault("queue", [])
    cutoff = _cutoff(settings).timestamp()
    alive = [e for e in queue if e["kind"] != "news" or _published_ts(e) >= cutoff]
    if len(alive) < len(queue):
        log.info("Из очереди убрано устаревших: %d", len(queue) - len(alive))
    alive = _ordered(alive, state.meta.get("last_post_source"))
    if len(alive) > settings.max_queue:
        log.info("Очередь переполнена — убраны %d наименее важных/старых", len(alive) - settings.max_queue)
        alive = alive[: settings.max_queue]
    state.meta["queue"] = alive


def seconds_until_next_post(settings: Settings, state: State) -> float:
    last = state.meta.get("last_post_at")
    if last is None:
        return 0
    return max(0.0, last + settings.post_interval_minutes * 60 - time.time())


def render(settings: Settings, state: State, client: httpx.Client, entry: dict) -> tuple[bytes | None, str]:
    """(картинка, текст) для записи очереди."""
    if entry["kind"] == "rates":
        return None, entry["post"]
    item = _item_from(entry)
    review = entry.get("review")
    uz = tuple(entry["uz"]) if entry.get("uz") else None
    if settings.uzbek_enabled and not uz and not review:
        # запись попала в очередь без узбекской версии — последняя попытка перед публикацией
        uz, _ = uzbek_version(client, item)
    image = None
    if settings.images_enabled:
        try:
            image = build_image(client, item, settings.watermark,
                                review.get("headline") if review else None, _generic_images(state, item))
        except Exception as exc:  # noqa: BLE001 — без картинки пост всё равно уйдёт
            log.warning("Не удалось подготовить картинку для %s: %s", item.url, exc)
    return image, build_post(item, review, CAPTION_LIMIT if image else TELEGRAM_LIMIT, uz)


def publish_next(settings: Settings, state: State, publisher) -> bool:
    """Публикует один пост, если подошло время. True — пост отправлен."""
    _trim_queue(settings, state)
    queue: list = state.meta["queue"]
    if not queue or seconds_until_next_post(settings, state) > 0:
        return False
    entry = queue[0]
    with make_client(settings.request_timeout) as client:
        image, post = render(settings, state, client, entry)
    try:
        if image:
            publisher.send_photo(image, post)
        else:
            url = entry.get("item", {}).get("url")
            publisher.send_message(post, preview_url=url)
    except TelegramError as exc:
        entry["attempts"] = entry.get("attempts", 0) + 1
        log.error("Не удалось отправить (%d/%d): %s", entry["attempts"], MAX_ATTEMPTS, exc)
        if entry["attempts"] >= MAX_ATTEMPTS:
            queue.remove(entry)
        return False
    queue.remove(entry)
    state.meta["last_post_at"] = time.time()
    state.meta["last_post_source"] = entry.get("item", {}).get("source_id", "rates")
    if entry["kind"] == "rates":
        state.meta["rates_posted_for"] = entry["rates_date"]
    title = entry.get("item", {}).get("title") or "курсы ЦБ"
    log.info("Опубликовано: %s (в очереди осталось %d, следующий пост не раньше чем через %d мин)",
             title[:80], len(queue), settings.post_interval_minutes)
    return True
