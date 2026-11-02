"""Оформление поста для Telegram (HTML-разметка)."""

from __future__ import annotations

import html
import re

from .fetch import NewsItem

GROUP_TAGS = {
    "gov": "#госорганы",
    "regulator": "#ЦБ",
    "bank": "#банки",
    "market": "#биржа",
    "invest": "#инвестиции",
    "media": "#официально",
}
CATEGORY_EMOJI = {
    "Банки и финансы": "🏦", "Денежно-кредитная политика": "📊", "Новые продукты": "🆕",
    "Инвестиции": "💼", "Внешняя торговля": "🚢", "Международные отношения": "🌐",
    "Налоги и таможня": "🧾", "Законодательство": "⚖️", "Макроэкономика": "📈",
    "Промышленность": "🏭", "Энергетика": "⚡", "Сельское хозяйство": "🌾",
    "IT и цифровизация": "💻", "Рынок капитала": "📉", "Туризм": "✈️",
    "Транспорт и логистика": "🚆", "Другое": "📰",
}
GROUP_EMOJI = {"gov": "🏛", "regulator": "🏦", "bank": "🏦", "market": "📉", "invest": "💼", "media": "📰"}
TELEGRAM_LIMIT = 4096
CAPTION_LIMIT = 1024  # подпись к фото в Telegram короче обычного сообщения


def _tag(text: str) -> str:
    cleaned = re.sub(r"[^\w]", "", text.replace(" ", "_"), flags=re.UNICODE)
    return f"#{cleaned}" if cleaned else ""


def _source_tag(item: NewsItem) -> str:
    return _tag(item.source_id)


def build_post(item: NewsItem, review: dict | None = None, limit: int = TELEGRAM_LIMIT) -> str:
    e = html.escape
    if review:
        emoji = CATEGORY_EMOJI.get(review.get("category", ""), "📰")
        headline = review.get("headline") or item.title
        body = review.get("summary") or item.summary
        extra_tags = [_tag(t) for t in review.get("tags", [])[:3]]
    else:
        emoji = GROUP_EMOJI.get(item.group, "📰")
        headline, body, extra_tags = item.title, item.summary, []

    if body and body.strip().lower().startswith(headline.strip().lower()[:60]):
        body = ""  # описание дублирует заголовок

    date = f" · {item.published.strftime('%d.%m.%Y')}" if item.published else ""
    tags = " ".join(dict.fromkeys(t for t in [GROUP_TAGS.get(item.group, ""), _source_tag(item), *extra_tags] if t))

    parts = [f"{emoji} <b>{e(headline)}</b>"]
    if body:
        parts.append(e(body))
    parts.append(f"🔗 <a href=\"{e(item.url, quote=True)}\">Источник: {e(item.source_name)}</a>{e(date)}")
    if tags:
        parts.append(tags)
    text = "\n\n".join(parts)

    while len(text) > limit and body:
        # укорачиваем пересказ по словам, пока пост не влезет в лимит
        cut = max(0, len(body) - (len(text) - limit) - 10)
        body = body[:cut].rsplit(" ", 1)[0].rstrip(" ,.;:—-") if cut else ""
        parts[1] = e(body + "…") if body else None
        text = "\n\n".join(p for p in parts if p)
    return text[:limit]
