"""Настройки из переменных окружения (.env) и список источников из config/sources.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


@dataclass
class Settings:
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    # @username канала или числовой id (-100...). Бот должен быть администратором канала.
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    # Без ключа бот работает: публикует заголовок + описание со страницы новости.
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    ai_model: str = field(default_factory=lambda: os.getenv("AI_MODEL", "claude-opus-5-5"))
    ai_enabled: bool = field(default_factory=lambda: _bool("AI_ENABLED", True))
    # Минимальная бизнес-значимость (1–5), ниже которой новость не публикуется. Работает только с ИИ.
    min_importance: int = field(default_factory=lambda: _int("MIN_IMPORTANCE", 3))
    # Порог для оценки по правилам (без ИИ), 1–5: ниже — не публикуется
    min_importance_rules: float = field(default_factory=lambda: float(os.getenv("MIN_IMPORTANCE_RULES", "1.8")))
    # Не чаще одного поста в N минут; остальное ждёт в очереди
    post_interval_minutes: int = field(default_factory=lambda: _int("POST_INTERVAL_MINUTES", 30))
    # Максимум постов в очереди: при переполнении выбрасываются наименее важные и самые старые
    max_queue: int = field(default_factory=lambda: _int("MAX_QUEUE", 100))
    # Новости старше этого срока не публикуются (если дату удалось определить).
    max_age_days: int = field(default_factory=lambda: _int("MAX_AGE_DAYS", 3))
    request_timeout: int = field(default_factory=lambda: _int("REQUEST_TIMEOUT", 25))
    sources_file: Path = field(
        default_factory=lambda: Path(os.getenv("SOURCES_FILE", ROOT / "config" / "sources.yaml"))
    )
    state_file: Path = field(
        default_factory=lambda: Path(os.getenv("STATE_FILE", ROOT / "data" / "state.json"))
    )
    # куда складывать посты, пока не задан токен Telegram
    preview_file: Path = field(
        default_factory=lambda: Path(os.getenv("PREVIEW_FILE", ROOT / "data" / "preview_posts.txt"))
    )

    # Узбекская версия (латиница) в каждом посте
    uzbek_enabled: bool = field(default_factory=lambda: _bool("UZBEK_ENABLED", True))

    # Картинки и водяной знак
    images_enabled: bool = field(default_factory=lambda: _bool("IMAGES_ENABLED", True))
    # Текст водяного знака; по умолчанию — @username канала из TELEGRAM_CHAT_ID
    watermark_text: str = field(default_factory=lambda: os.getenv("WATERMARK_TEXT", "") or (
        os.getenv("TELEGRAM_CHAT_ID", "") if os.getenv("TELEGRAM_CHAT_ID", "").startswith("@") else ""))
    # PNG-логотип канала (с прозрачным фоном), например assets/logo.png
    watermark_logo: str = field(default_factory=lambda: os.getenv("WATERMARK_LOGO", ""))
    watermark_opacity: float = field(default_factory=lambda: float(os.getenv("WATERMARK_OPACITY", "0.85")))
    # Бледный диагональный повтор надписи по всей картинке — защита от обрезки угла
    watermark_tiled: bool = field(default_factory=lambda: _bool("WATERMARK_TILED", True))

    @property
    def watermark(self):
        from .images import WatermarkConfig

        logo = Path(self.watermark_logo) if self.watermark_logo else None
        if logo and not logo.is_absolute():
            logo = ROOT / logo
        return WatermarkConfig(self.watermark_text, logo, self.watermark_opacity, self.watermark_tiled)

    @property
    def ai_active(self) -> bool:
        return self.ai_enabled and bool(self.anthropic_api_key)


@dataclass
class Source:
    id: str
    name: str
    url: str
    group: str = "gov"
    type: str = "auto"
    selectors: dict = field(default_factory=dict)
    link_pattern: str | None = None
    # для type: govuz — код организации на портале gov.uz (gov.uz/ru/<authority>/...);
    # пустая строка — общая лента портала
    authority: str | None = None
    # большой поток общих новостей — включать только вместе с ИИ-фильтром
    requires_ai: bool = False
    enabled: bool = True


def load_sources(path: Path) -> list[Source]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = [Source(**item) for item in data.get("sources", [])]
    ids = [s.id for s in sources]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"Повторяющиеся id источников: {', '.join(sorted(duplicates))}")
    return [s for s in sources if s.enabled]
