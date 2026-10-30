"""Точка входа.

  python -m bot run              — один проход: собрать новости и опубликовать новые
  python -m bot run --dry-run    — то же, но вывести посты в консоль вместо Telegram
  python -m bot run --loop 900   — работать постоянно, проверяя источники каждые 900 секунд
  python -m bot check [--id X]   — проверить, что источники парсятся
  python -m bot rates            — опубликовать курсы ЦБ (один раз в день, повтор игнорируется)
  python -m bot test-telegram    — отправить тестовое сообщение в канал
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx

from .ai import Editor
from .config import Settings, Source, load_sources
from .fetch import NewsItem, enrich, fetch_source, make_client
from .format import build_post
from .rates import build_rates_post, fetch_rates
from .state import State
from .telegram import Telegram, TelegramError

log = logging.getLogger("bot")


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


def run_once(settings: Settings, dry_run: bool, only: str | None = None) -> int:
    sources = [s for s in load_sources(settings.sources_file) if not only or s.id == only]
    state = State(settings.state_file)
    telegram = None if dry_run else Telegram(settings.telegram_token, settings.telegram_chat_id)
    editor = Editor(settings.anthropic_api_key, settings.ai_model) if settings.ai_active else None
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.max_age_days)

    results = collect(settings, sources)
    fresh: list[NewsItem] = []
    for source in sources:
        result = results[source.id]
        if isinstance(result, Exception):
            state.record_source(source.id, ok=False, found=0, error=str(result)[:300])
            log.warning("✗ %-12s %s", source.id, result)
            continue
        new = [i for i in result if not state.is_seen(i.url)]
        first_time = not state.source_initialized(source.id)
        state.record_source(source.id, ok=bool(result), found=len(result),
                            error=None if result else "ни одной новости не найдено")
        if first_time and result and not dry_run:
            # первый запуск по источнику: запоминаем текущую ленту, чтобы не завалить канал архивом
            for item in result:
                state.mark(item.url)
            log.info("• %-12s инициализирован, запомнено %d ссылок", source.id, len(result))
            continue
        log.info("✓ %-12s найдено %d, новых %d", source.id, len(result), len(new))
        fresh.extend(new)

    posted = 0
    with make_client(settings.request_timeout) as client:
        for item in fresh:
            if posted >= settings.max_posts_per_run:
                log.info("Достигнут лимит %d постов за прогон, остальное — в следующий раз", posted)
                break
            enrich(client, item)
            if item.published and item.published < cutoff:
                log.info("  пропуск (старая, %s): %s", item.published.date(), item.url)
                state.mark(item.url)
                continue
            review = editor.review(item) if editor else None
            if review and review["importance"] < settings.min_importance:
                log.info("  пропуск (значимость %d): %s", review["importance"], item.title[:80])
                state.mark(item.url)
                continue
            post = build_post(item, review)
            if dry_run:
                print("\n" + "─" * 70 + "\n" + post)
            else:
                try:
                    telegram.send_message(post, preview_url=item.url)
                except TelegramError as exc:
                    log.error("Не удалось отправить %s: %s", item.url, exc)
                    continue
                time.sleep(settings.post_delay_seconds)
            state.mark(item.url)
            posted += 1

    if not dry_run:
        state.save()
    log.info("Готово: опубликовано %d", posted)
    return posted


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
    Telegram(settings.telegram_token, settings.telegram_chat_id).send_message(post)
    state.meta["rates_posted_for"] = date
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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()

    if args.cmd == "check":
        return cmd_check(settings, args.id)
    if args.cmd == "rates":
        cmd_rates(settings, args.dry_run)
        return 0
    if args.cmd == "test-telegram":
        tg = Telegram(settings.telegram_token, settings.telegram_chat_id)
        me = tg.get_me()
        tg.send_message(f"✅ Бот <b>@{me['username']}</b> подключён к каналу.")
        print("Сообщение отправлено")
        return 0

    if not settings.ai_active:
        log.info("ИИ-пересказ выключен (нет ANTHROPIC_API_KEY) — публикую заголовок и описание со страницы")
    while True:
        try:
            if args.with_rates:
                try:
                    cmd_rates(settings, args.dry_run)
                except (httpx.HTTPError, ValueError, KeyError, TelegramError) as exc:
                    log.warning("Курсы ЦБ: %s", exc)
            run_once(settings, args.dry_run, args.id)
        except TelegramError as exc:
            log.error("%s", exc)
            if not args.loop:
                return 1
        if not args.loop:
            return 0
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
