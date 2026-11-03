"""Узбекская версия новости на латинице в НОВОЙ редакции алфавита.

Закон № ЗРУ-1184 от 21.10.2026 (вступает в силу 22.04.2027): 28 букв и апостроф;
вместо Sh, Ch, Oʻ, Gʻ — буквы Ş, Ç, Ö, Ğ; апостроф ʼ — вместо твёрдого знака (ta’lim → taʼlim).

Откуда берётся текст:
1. gov.uz — тот же API с языком `oz` (портал уже частично перешёл на новый алфавит).
2. Остальные сайты — та же новость по адресу с /uz/ (или поддоменом uz.).
3. Если узбекской версии нет — перевод Claude (см. ai.py), иначе пост выходит только на русском.
Любой найденный текст (старая латиница, кириллица, смесь) приводится к новому алфавиту функцией to_new_latin.
"""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import urlparse, urlunparse

import httpx

from .fetch import (GOVUZ_API, NewsItem, _clean, _govuz_prefix, enrich, first_paragraph, shorten,
                    strip_site_suffix)

log = logging.getLogger(__name__)

TUTUQ = "ʼ"  # апостроф (тутуқ белгиси): taʼlim, eʼtibor, sunʼiy

CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ё": "yo", "ж": "j", "з": "z", "и": "i", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "x", "ц": "s", "ч": "ç", "ш": "ş", "щ": "ş", "ъ": TUTUQ, "ы": "i", "ь": "", "э": "e",
    "ю": "yu", "я": "ya", "ў": "ö", "қ": "q", "ғ": "ğ", "ҳ": "h",
}
VOWELS = set("аеёиоуўэюяaeiou")

# всевозможные «апострофы», которыми на сайтах пишут oʻ, gʻ и тутуқ
QUOTES = "‘’ʻʼ'`´"
OLD_O = re.compile(rf"([Oo])[{QUOTES}]")
OLD_G = re.compile(rf"([Gg])[{QUOTES}]")
SEPARATED_SH = re.compile(rf"([Ss])[{QUOTES}]([Hh])")  # Is’hoq: s и h раздельно → в новом алфавите просто «sh»
OTHER_QUOTE = re.compile(rf"(?<=\w)[{QUOTES}](?=\w)")
# Иностранные слова и бренды (в узбекском алфавите нет w, c без h и т. п.) не трогаем: Deutsche, Wyndham, Bosch
FOREIGN = re.compile(r"[wW]|[cC](?![hH])|sch|ph|th|ck|ee|oo", re.I)


def cyr_to_latin(text: str) -> str:
    out = []
    for i, ch in enumerate(text):
        low = ch.lower()
        if low == "ц":  # «ts» после гласной (migratsiya), иначе «s» (sirk)
            prev = text[i - 1].lower() if i else " "
            lat = "ts" if prev in VOWELS else "s"
        elif low == "е":  # в начале слова и после гласной — «ye»
            prev = text[i - 1].lower() if i else " "
            lat = "ye" if (not prev.isalpha() or prev in VOWELS or prev in "ъь") else "e"
        elif low in CYR_TO_LAT:
            lat = CYR_TO_LAT[low]
        else:
            out.append(ch)
            continue
        if ch.isupper() and lat:
            nxt = text[i + 1] if i + 1 < len(text) else ""
            lat = lat.upper() if (nxt.isupper() and len(lat) > 1) else lat[0].upper() + lat[1:]
        out.append(lat)
    return "".join(out)


def _word_to_new(word: str) -> str:
    if FOREIGN.search(word.replace("ç", "").replace("ş", "")):
        return word
    word = word.replace("SH", "Ş").replace("Sh", "Ş").replace("sh", "ş")
    word = word.replace("CH", "Ç").replace("Ch", "Ç").replace("ch", "ç")
    return word.replace("\x00", "h")


def old_latin_to_new(text: str) -> str:
    """Старая латиница (oʻ, gʻ, sh, ch) → новая (ö, ğ, ş, ç)."""
    text = OLD_O.sub(lambda m: "Ö" if m.group(1) == "O" else "ö", text)
    text = OLD_G.sub(lambda m: "Ğ" if m.group(1) == "G" else "ğ", text)
    text = SEPARATED_SH.sub(lambda m: m.group(1) + "\x00", text)  # временно прячем «h», чтобы не стало «ş»
    text = re.sub(r"[^\W\d_]+", lambda m: _word_to_new(m.group(0)), text)
    text = text.replace("\x00", "h")
    return OTHER_QUOTE.sub(TUTUQ, text)


def _letters(text: str, pattern: str) -> int:
    return len(re.findall(pattern, text))


def is_mostly_cyrillic(text: str) -> bool:
    cyr, lat = _letters(text, r"[А-Яа-яЁёЎўҚқҒғҲҳ]"), _letters(text, r"[A-Za-z]")
    return cyr > lat


def is_russian_not_uzbek(text: str) -> bool:
    """Кириллица без узбекских букв (ў қ ғ ҳ), зато с русскими (ы щ э) — это русский текст."""
    if not is_mostly_cyrillic(text):
        return False
    return not re.search(r"[ЎўҚқҒғҲҳ]", text) and bool(re.search(r"[ЫыЩщ]|\b(в|и|на|с|по)\b", text))


HOMOGLYPHS = str.maketrans("аеорсухкмтАЕОРСУХКМТВН", "aeopcyxkmtAEOPCYXKMTBH")
MIXED_WORD = re.compile(r"\b(?=\w*[A-Za-z])(?=\w*[А-Яа-я])\w+\b")


def fix_homoglyphs(text: str) -> str:
    """«Rеspublikasi» с кириллической «е» внутри латинского слова → чистая латиница."""
    return MIXED_WORD.sub(lambda m: m.group(0).translate(HOMOGLYPHS), text)


def to_new_latin(text: str) -> str:
    """Любой узбекский текст (кириллица, старая латиница, смесь) → латиница в новой редакции."""
    text = html.unescape(text or "")
    if is_mostly_cyrillic(text):
        text = cyr_to_latin(text)
    return old_latin_to_new(fix_homoglyphs(text))


def looks_uzbek_latin(text: str, russian: str) -> bool:
    if not text or len(text) < 15:
        return False
    if _clean(text).lower() == _clean(russian).lower():
        return False
    lat, cyr = _letters(text, r"[A-Za-z]"), _letters(text, r"[А-Яа-яЁё]")
    return lat > 3 * max(cyr, 1)


def uz_url_candidates(url: str) -> list[str]:
    parsed = urlparse(url)
    host, path = parsed.netloc, parsed.path
    candidates = []
    if "/ru/" in path or path.endswith("/ru"):
        candidates.append(path.replace("/ru/", "/uz/", 1) if "/ru/" in path else path[:-3] + "/uz")
        if path.startswith("/ru/"):
            candidates.append(path[3:])  # узбекский — язык сайта по умолчанию: /ru/news/x → /news/x
    elif host.startswith("ru."):
        candidates.append(("uz." + host[3:], path))
    else:
        candidates.append("/uz" + path)
    result = []
    for cand in candidates:
        new_host, new_path = cand if isinstance(cand, tuple) else (host, cand)
        result.append(urlunparse(parsed._replace(netloc=new_host, path=new_path)))
    return result


def _fetch_govuz(client: httpx.Client, item: NewsItem) -> tuple[str, str] | None:
    authority = item.extra["govuz_authority"]
    headers = {"language": "oz", "Accept": "application/json"}
    if authority:
        headers["code"] = authority
    response = client.get(f"{GOVUZ_API}{_govuz_prefix(authority)}/view",
                          params={"id": item.extra["govuz_id"]}, headers=headers)
    response.raise_for_status()
    data = response.json().get("data") or {}
    title = _clean(data.get("title"))
    if not title:
        return None
    from bs4 import BeautifulSoup

    body = BeautifulSoup(data.get("body") or "", "lxml")
    text = "\n".join(_clean(p.get_text(" ")) for p in body.find_all(["p", "li"]))
    summary = shorten(data.get("anons") or "") or first_paragraph(text, title)
    return title, summary


NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def same_news(ru_text: str, uz_text: str) -> bool:
    """Защита от чужой статьи по ссылке: в новости совпадают хотя бы некоторые числа (даты, суммы)."""
    ru_numbers = {n.replace(",", ".") for n in NUMBER.findall(ru_text) if len(n) >= 2}
    if len(ru_numbers) < 2:
        return True  # чисел мало — сравнивать не по чему
    uz_numbers = {n.replace(",", ".") for n in NUMBER.findall(uz_text)}
    return len(ru_numbers & uz_numbers) >= min(2, len(ru_numbers))


def _fetch_page(client: httpx.Client, item: NewsItem) -> tuple[str, str] | None:
    candidates = list(dict.fromkeys([*item.extra.get("uz_alternates", []), *uz_url_candidates(item.url)]))
    for url in candidates:
        probe = NewsItem(item.source_id, item.source_name, item.group, url, "")
        try:
            response = client.head(url)
            if response.status_code == 405:
                response = client.get(url)
            if response.status_code != 200:
                continue
            # редирект на общий список новостей — это не та же новость
            if urlparse(str(response.url)).path.rstrip("/") != urlparse(url).path.rstrip("/"):
                continue
        except httpx.HTTPError:
            continue
        enrich(client, probe)
        title = strip_site_suffix(probe.title)
        ru_text = " ".join([item.title, item.summary, item.text[:1500]])
        if title and not same_news(ru_text, " ".join([title, probe.summary, probe.text[:3000]])):
            log.debug("Узбекская страница %s не совпадает с новостью %s", url, item.url)
            continue
        if title:
            # meta description на узбекских страницах часто общая для сайта — надёжнее первый абзац
            return title, first_paragraph(probe.text, title) or probe.summary
    return None


def fetch_uzbek(client: httpx.Client, item: NewsItem) -> tuple[str, str] | None:
    """(заголовок, описание) на узбекском (латиница) или None."""
    try:
        found = _fetch_govuz(client, item) if "govuz_id" in item.extra else _fetch_page(client, item)
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("Узбекская версия %s недоступна: %s", item.url, exc)
        return None
    if not found:
        return None
    if is_russian_not_uzbek(found[0]):
        return None  # узбекская страница отдала русский текст
    title, summary = to_new_latin(found[0]), ("" if is_russian_not_uzbek(found[1]) else to_new_latin(found[1]))
    if not looks_uzbek_latin(title, item.title):
        return None
    if summary and (not looks_uzbek_latin(summary, item.summary) or summary == title):
        summary = ""
    return title, summary


def uzbek_version(client: httpx.Client, item: NewsItem) -> tuple[tuple[str, str] | None, str]:
    """(заголовок, описание) на новой латинице и откуда взято: official | machine | none."""
    official = fetch_uzbek(client, item)
    if official:
        return official, "official"
    from .translate import translate_ru_uz

    title = translate_ru_uz(client, item.title)
    if not title:
        return None, "none"
    summary = translate_ru_uz(client, item.summary) if item.summary else ""
    if not item.title.rstrip().endswith("."):
        title = title.rstrip().rstrip(".")  # переводчик добавляет точку в конце заголовка
    return (to_new_latin(title), to_new_latin(summary or "")), "machine"
