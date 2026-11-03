"""Точка входа.

  python -m bot run              — проверить источники и опубликовать один пост из очереди (если подошло время)
  python -m bot run --loop 900   — работать постоянно: источники каждые 900 с, посты не чаще POST_INTERVAL_MINUTES
  python -m bot run --dry-run    — показать в консоли посты, которые попали бы в очередь (ничего не сохраняет)
  python -m bot queue            — показать очередь публикаций
  python -m bot check [--id X]   — проверить, что источники парсятся
  python -m bot rates            — опубликовать курсы ЦБ сейчас (один раз в день, повтор игнорируется)
  python -m bot test-telegram    — отправить тестовое сообщение в канал
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime

import httpx

from .config import Settings, load_sources
from .fetch import make_client
from .pipeline import (collect, collect_into_queue, enqueue_rates, publish_next, render,
                       seconds_until_next_post)
from .rates import build_rates_post, fetch_rates
from .state import State
from .telegram import Telegram, TelegramError, make_publisher

log = logging.getLogger("bot")
TICK_SECONDS = 60  # как часто проверять, не пора ли публиковать следующий пост


def run_once(settings: Settings, dry_run: bool, only: str | None = None, with_rates: bool = False) -> int:
    """Одна проверка источников + не больше одного поста (если подошло время). Возвращает число постов."""
    state = State(settings.state_file)
    if dry_run:
        collect_into_queue(settings, state, only, initialize=False)
        with make_client(settings.request_timeout) as client:
            for n, entry in enumerate(state.meta.get("queue", [])):
                image, post = render(settings, state, client, entry)
                note = ""
                if image:
                    path = settings.preview_file.parent / "preview_images" / f"dry_{n}.jpg"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(image)
                    note = f"[картинка: {path}]\n"
                print("\n" + "─" * 70 + "\n" + note + post)
        return 0
    if with_rates:
        try:
            enqueue_rates(settings, state)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("Курсы ЦБ: %s", exc)
    collect_into_queue(settings, state, only)
    posted = int(publish_next(settings, state, make_publisher(settings)))
    state.save()
    return posted


def run_loop(settings: Settings, interval: int, only: str | None, with_rates: bool) -> None:
    state = State(settings.state_file)
    if "last_post_at" not in state.meta:
        # первый запуск с очередью: отсчитываем интервал от старта, чтобы не публиковать сразу
        state.meta["last_post_at"] = time.time()
        state.save()
    publisher = make_publisher(settings)
    next_check = 0.0
    while True:
        if time.time() >= next_check:
            if with_rates:
                try:
                    enqueue_rates(settings, state)
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    log.warning("Курсы ЦБ: %s", exc)
            try:
                collect_into_queue(settings, state, only)
            except Exception as exc:  # noqa: BLE001 — сбой сбора не должен останавливать публикацию
                log.exception("Ошибка при проверке источников: %s", exc)
            next_check = time.time() + interval
            state.save()
        if publish_next(settings, state, publisher):
            state.save()
        time.sleep(TICK_SECONDS)


def cmd_queue(settings: Settings) -> None:
    state = State(settings.state_file)
    queue = state.meta.get("queue", [])
    wait = seconds_until_next_post(settings, state)
    print(f"В очереди: {len(queue)}. Следующий пост: {'сейчас' if not wait else f'через {int(wait // 60)} мин'}")
    for n, e in enumerate(queue, 1):
        item = e.get("item") or {}
        date = datetime.fromisoformat(item["published"]).strftime("%d.%m %H:%M") if item.get("published") else "  —  "
        print(f"{n:>3}. [{e.get('importance', 3):.1f}] {date}  {(item.get('title') or 'Курсы ЦБ')[:80]}")


def cmd_check(settings: Settings, only: str | None) -> int:
    sources = [s for s in load_sources(settings.sources_file) if not only or s.id == only]
    results = collect(settings, sources)
    bad = 0
    for source in sources:
        result = results[source.id]
        if isinstance(result, Exception):
            bad += 1
            print(f"✗ {source.id:<12} ОШИБКА: {result}")
            continue
        mark = "✓" if result else "⚠"
        bad += 0 if result else 1
        print(f"{mark} {source.id:<12} новостей: {len(result):>3}   {source.url}")
        for item in result[: (15 if only else 3)]:
            date = item.published.strftime("%d.%m.%Y") if item.published else "  —  "
            print(f"      {date}  {item.title[:90]}\n                  {item.url}")
    print(f"\nИсточников: {len(sources)}, проблемных: {bad}")
    return 1 if bad and only else 0


def cmd_rates(settings: Settings, dry_run: bool) -> None:
    state = State(settings.state_file)
    with make_client(settings.request_timeout) as client:
        post, date = build_rates_post(fetch_rates(client))
    if state.meta.get("rates_posted_for") == date:
        log.info("Курсы на %s уже опубликованы", date)
        return
    if dry_run:
        print(post)
        return
    make_publisher(settings).send_message(post)
    state.meta["rates_posted_for"] = date
    state.meta["last_post_at"] = time.time()
    state.save()
    log.info("Курсы на %s опубликованы", date)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot", description="Бизнес-новости Узбекистана → Telegram")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--loop", type=int, metavar="SECONDS")
    p_run.add_argument("--id", help="обработать только один источник")
    p_run.add_argument("--with-rates", action="store_true", help="заодно публиковать курсы ЦБ раз в день")
    p_check = sub.add_parser("check")
    p_check.add_argument("--id")
    p_rates = sub.add_parser("rates")
    p_rates.add_argument("--dry-run", action="store_true")
    sub.add_parser("test-telegram")
    sub.add_parser("queue")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()

    if args.cmd == "check":
        return cmd_check(settings, args.id)
    if args.cmd == "queue":
        cmd_queue(settings)
        return 0
    if args.cmd == "rates":
        cmd_rates(settings, args.dry_run)
        return 0
    if args.cmd == "test-telegram":
        tg = Telegram(settings.telegram_token, settings.telegram_chat_id)
        me = tg.get_me()
        tg.send_message(f"✅ Бот <b>@{me['username']}</b> подключён к каналу.")
        print("Сообщение отправлено")
        return 0

    if settings.images_enabled and not settings.watermark.enabled:
        log.warning("Водяной знак не задан: укажите WATERMARK_TEXT или TELEGRAM_CHAT_ID вида @канал")
    if not settings.ai_active:
        log.info("ИИ-пересказ выключен (нет ANTHROPIC_API_KEY) — публикую заголовок и описание со страницы")
    if args.loop and not args.dry_run:
        log.info("Посты — не чаще одного в %d мин, проверка источников — каждые %d с",
                 settings.post_interval_minutes, args.loop)
        run_loop(settings, args.loop, args.id, args.with_rates)
        return 0
    try:
        run_once(settings, args.dry_run, args.id, args.with_rates)
    except TelegramError as exc:
        log.error("%s", exc)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
