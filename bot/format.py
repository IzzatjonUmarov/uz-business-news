"""Оформление поста для Telegram (HTML-разметка)."""

from __future__ import annotations

import html
import re

from .fetch import NewsItem, shorten

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


def build_post(item: NewsItem, review: dict | None = None, limit: int = TELEGRAM_LIMIT,
               uz: tuple[str, str] | None = None) -> str:
    """Пост: русский блок, затем (если есть) узбекский, ссылка на первоисточник и хэштеги."""
    e = html.escape
    if review:
        emoji = CATEGORY_EMOJI.get(review.get("category", ""), "📰")
        headline = review.get("headline") or item.title
        body = review.get("summary") or item.summary
        extra_tags = [_tag(t) for t in review.get("tags", [])[:3]]
    else:
        emoji = GROUP_EMOJI.get(item.group, "📰")
        headline, body, extra_tags = item.title, item.summary, []
    uz_headline, uz_body = uz if uz else ("", "")

    def _dedupe(head: str, text: str) -> str:
        return "" if text and text.strip().lower().startswith(head.strip().lower()[:60]) else text or ""

    body, uz_body = _dedupe(headline, body), _dedupe(uz_headline, uz_body)

    date = f" · {item.published.strftime('%d.%m.%Y')}" if item.published else ""
    tags = " ".join(dict.fromkeys(t for t in [GROUP_TAGS.get(item.group, ""), _source_tag(item), *extra_tags] if t))
    source_label = "Источник / Manba" if uz_headline else "Источник"
    footer = [f"🔗 <a href=\"{e(item.url, quote=True)}\">{source_label}: {e(item.source_name)}</a>{e(date)}"]
    if tags:
        footer.append(tags)

    def render(ru_text: str, uz_text: str) -> str:
        parts = [f"{emoji} <b>{e(headline)}</b>"]
        if ru_text:
            parts.append(e(ru_text))
        if uz_headline:
            parts.append(f"🇺🇿 <b>{e(uz_headline)}</b>")
            if uz_text:
                parts.append(e(uz_text))
        return "\n\n".join(parts + footer)

    text = render(body, uz_body)
    if len(text) > limit:
        # делим оставшееся место между описаниями поровну (пропорционально длине)
        fixed = len(render("", ""))
        budget = max(0, limit - fixed - 8)
        total = len(e(body)) + len(e(uz_body)) or 1
        ru_cut = shorten(body, int(budget * len(e(body)) / total)) if body else ""
        uz_cut = shorten(uz_body, int(budget * len(e(uz_body)) / total)) if uz_body else ""
        text = render(ru_cut if len(ru_cut) > 40 else "", uz_cut if len(uz_cut) > 40 else "")
    return text[:limit]
