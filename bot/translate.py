"""Машинный перевод ru → uz, когда у новости нет официальной узбекской версии и нет ключа Claude.

Бесплатные сервисы без ключа: Google Translate (публичный endpoint), запасной — MyMemory.
Результат приводится к новой латинице (to_new_latin) уже в вызывающем коде.
"""

from __future__ import annotations

import html
import logging
import re

import httpx

log = logging.getLogger(__name__)

GOOGLE = "https://translate.googleapis.com/translate_a/single"
MYMEMORY = "https://api.mymemory.translated.net/get"
MYMEMORY_CHUNK = 450  # MyMemory принимает до 500 символов за запрос


def _google(client: httpx.Client, text: str) -> str | None:
    response = client.get(GOOGLE, params={"client": "gtx", "sl": "ru", "tl": "uz", "dt": "t", "q": text})
    response.raise_for_status()
    data = response.json()
    return "".join(part[0] for part in data[0] if part and part[0]) or None


def _chunks(text: str, size: int) -> list[str]:
    sentences, chunks, current = re.split(r"(?<=[.!?])\s+", text), [], ""
    for sentence in sentences:
        if len(current) + len(sentence) + 1 > size and current:
            chunks.append(current)
            current = ""
        current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current[:size])
    return chunks


def _mymemory(client: httpx.Client, text: str) -> str | None:
    parts = []
    for chunk in _chunks(text, MYMEMORY_CHUNK):
        response = client.get(MYMEMORY, params={"q": chunk, "langpair": "ru|uz"})
        response.raise_for_status()
        data = response.json()
        if data.get("responseStatus") != 200 or data.get("quotaFinished"):
            return None
        parts.append(data["responseData"]["translatedText"])
    return " ".join(parts) or None


def translate_ru_uz(client: httpx.Client, text: str) -> str | None:
    """Перевод на узбекский (латиница) или None, если ни один сервис не ответил."""
    if not text or not text.strip():
        return ""
    for name, service in (("Google", _google), ("MyMemory", _mymemory)):
        try:
            result = service(client, text)
            result = html.unescape(result or "").strip()  # Google отдаёт апостроф как &#39;
            if result and result != text.strip():
                return result
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            log.debug("Перевод через %s не удался: %s", name, exc)
    return None
