"""Ежедневный пост с официальными курсами валют ЦБ Узбекистана (cbu.uz)."""

from __future__ import annotations

import html

import httpx

CBU_RATES_URL = "https://cbu.uz/ru/arkhiv-kursov-valyut/json/"
CBU_RATES_PAGE = "https://cbu.uz/ru/arkhiv-kursov-valyut/"
CURRENCIES = ["USD", "EUR", "RUB", "GBP", "CNY", "KZT", "JPY", "CHF", "TRY", "AED"]
FLAGS = {"USD": "🇺🇸", "EUR": "🇪🇺", "RUB": "🇷🇺", "GBP": "🇬🇧", "CNY": "🇨🇳", "KZT": "🇰🇿",
         "JPY": "🇯🇵", "CHF": "🇨🇭", "TRY": "🇹🇷", "AED": "🇦🇪"}


def fetch_rates(client: httpx.Client) -> list[dict]:
    response = client.get(CBU_RATES_URL)
    response.raise_for_status()
    return response.json()


def build_rates_post(rates: list[dict]) -> tuple[str, str]:
    """Возвращает (текст поста, дата курсов)."""
    by_code = {r["Ccy"]: r for r in rates}
    date = rates[0]["Date"] if rates else ""
    lines = [f"💱 <b>Официальные курсы ЦБ Узбекистана на {html.escape(date)}</b>", ""]
    for code in CURRENCIES:
        r = by_code.get(code)
        if not r:
            continue
        diff = float(r.get("Diff") or 0)
        arrow = "🔺" if diff > 0 else "🔻" if diff < 0 else "▫️"
        nominal = r.get("Nominal", "1")
        rate = float(r["Rate"])
        lines.append(f"{FLAGS.get(code, '')} {nominal} {code} = <b>{rate:,.2f}</b> сум {arrow} {diff:+.2f}".replace(",", " "))
    lines += ["", f"🔗 <a href=\"{CBU_RATES_PAGE}\">Источник: Центральный банк</a>", "#курсы #ЦБ #валюта"]
    return "\n".join(lines), date
