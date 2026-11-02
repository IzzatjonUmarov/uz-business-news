"""Минимальный клиент Telegram Bot API."""

from __future__ import annotations

import logging
import time
from datetime import datetime

import httpx

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, chat_id: str, timeout: int = 30):
        if not token or not chat_id:
            raise TelegramError("Не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHAT_ID")
        self.chat_id = chat_id
        self.base = f"https://api.telegram.org/bot{token}"
        self.http = httpx.Client(timeout=timeout)

    def _call(self, method: str, payload: dict, files: dict | None = None) -> dict:
        for attempt in range(4):
            if files:
                response = self.http.post(f"{self.base}/{method}", data=payload, files=files)
            else:
                response = self.http.post(f"{self.base}/{method}", json=payload)
            data = response.json()
            if data.get("ok"):
                return data["result"]
            retry_after = data.get("parameters", {}).get("retry_after")
            if response.status_code == 429 and retry_after:
                log.info("Telegram просит подождать %s c", retry_after)
                time.sleep(int(retry_after) + 1)
                continue
            if response.status_code >= 500 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise TelegramError(f"{method}: {data.get('description', response.text)}")
        raise TelegramError(f"{method}: превышено число попыток")

    def send_message(self, html: str, preview_url: str | None = None) -> dict:
        payload = {"chat_id": self.chat_id, "text": html, "parse_mode": "HTML"}
        if preview_url:
            payload["link_preview_options"] = {"url": preview_url, "prefer_large_media": True}
        else:
            payload["link_preview_options"] = {"is_disabled": True}
        return self._call("sendMessage", payload)

    def send_photo(self, jpeg: bytes, caption_html: str) -> dict:
        payload = {"chat_id": self.chat_id, "caption": caption_html, "parse_mode": "HTML"}
        return self._call("sendPhoto", payload, files={"photo": ("news.jpg", jpeg, "image/jpeg")})

    def get_me(self) -> dict:
        return self._call("getMe", {})


class FilePublisher:
    """Режим предпросмотра: пока не задан токен Telegram, посты пишутся в файл."""

    def __init__(self, path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send_message(self, html: str, preview_url: str | None = None) -> dict:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(f"\n{'─' * 70}\n[{datetime.now():%d.%m.%Y %H:%M}]\n{html}\n")
        return {}

    def send_photo(self, jpeg: bytes, caption_html: str) -> dict:
        images = self.path.parent / "preview_images"
        images.mkdir(exist_ok=True)
        name = images / f"{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"
        name.write_bytes(jpeg)
        return self.send_message(f"[картинка: {name}]\n{caption_html}")


def make_publisher(settings):
    if settings.telegram_token and settings.telegram_chat_id:
        return Telegram(settings.telegram_token, settings.telegram_chat_id)
    log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — режим предпросмотра, посты пишутся в %s",
                settings.preview_file)
    return FilePublisher(settings.preview_file)
