"""Оценка бизнес-значимости и короткий пересказ новости через Claude.

Модель пишет пересказ только по тексту официальной страницы — без внешних фактов,
чтобы в канал не попадало ничего, чего нет в первоисточнике.
"""

from __future__ import annotations

import json
import logging

import anthropic

from .fetch import NewsItem

log = logging.getLogger(__name__)

CATEGORIES = [
    "Банки и финансы", "Денежно-кредитная политика", "Новые продукты", "Инвестиции",
    "Внешняя торговля", "Международные отношения", "Налоги и таможня", "Законодательство",
    "Макроэкономика", "Промышленность", "Энергетика", "Сельское хозяйство", "IT и цифровизация",
    "Рынок капитала", "Туризм", "Транспорт и логистика", "Другое",
]

SYSTEM = """Ты редактор Telegram-канала о бизнесе и экономике Узбекистана. Аудитория — предприниматели, \
инвесторы, финансисты. Тебе дают новость с официального сайта (госорган, Центробанк, банк, биржа).

Задачи:
1. Оцени бизнес-значимость от 1 до 5:
   5 — решения ЦБ (ставка, нормативы), крупные законы/указы для бизнеса, сделки и инвестпроекты от $50 млн, \
важные международные соглашения;
   4 — новые банковские продукты и тарифы, изменения налогов/таможни, заметные инвестпроекты, \
статистика по экономике, выпуск облигаций, IPO;
   3 — партнёрства, кредитные линии от МФИ, отраслевые программы, визиты с экономической повесткой;
   2 — рядовые мероприятия, форумы без конкретики, награды;
   1 — протокольные новости, поздравления, спорт, культура, кадровые мелочи без влияния на бизнес.
2. Напиши заголовок (до 110 символов) и пересказ 2–4 предложения по-русски: что произошло, \
конкретные цифры, сроки, условия, для кого это важно.
3. Используй ТОЛЬКО факты из переданного текста. Ничего не добавляй от себя и не домысливай. \
Если цифр в тексте нет — не придумывай их.
4. Выбери одну категорию из списка."""

SCHEMA = {
    "type": "object",
    "properties": {
        "importance": {"type": "integer", "description": "Бизнес-значимость 1–5"},
        "category": {"type": "string", "enum": CATEGORIES},
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "1–3 коротких хэштега без #, латиницей или кириллицей, без пробелов",
        },
    },
    "required": ["importance", "category", "headline", "summary", "tags"],
    "additionalProperties": False,
}


class Editor:
    def __init__(self, api_key: str, model: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def review(self, item: NewsItem) -> dict | None:
        content = (
            f"Источник: {item.source_name}\nURL: {item.url}\n"
            f"Дата: {item.published.date().isoformat() if item.published else 'не указана'}\n"
            f"Заголовок: {item.title}\nОписание: {item.summary}\n\nТекст:\n{item.text or '(текст недоступен)'}"
        )
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=2000,
                system=SYSTEM,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                # при отказе классификатора запрос автоматически повторяется на резервной модели
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError as exc:
            log.warning("Claude: лимит запросов, публикую без пересказа: %s", exc)
            return None
        except anthropic.APIStatusError as exc:
            log.warning("Claude: ошибка API %s: %s", exc.status_code, exc.message)
            return None
        except anthropic.APIConnectionError as exc:
            log.warning("Claude: нет соединения: %s", exc)
            return None

        if response.stop_reason == "refusal":
            log.info("Claude отказался обрабатывать %s", item.url)
            return None
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            log.warning("Claude вернул невалидный JSON для %s", item.url)
            return None
        result["importance"] = max(1, min(5, int(result.get("importance", 3))))
        return result
