"""Сбор новостей: RSS, автопоиск RSS на странице и эвристический разбор HTML-списков."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urldefrag, urljoin, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from .config import Source

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "ru,uz;q=0.8,en;q=0.5",
}

# Части URL, по которым узнаём страницу конкретной новости.
NEWS_HINTS = re.compile(
    r"/(news|novosti|yangiliklar|press|press[-_]center|press[-_]releases?|releases?|post|posts|"
    r"article|articles|view|lists/view|item|media|mediacenter|events|announcements?|"
    r"press[-_]sluzhba|matbuot)(/|-|_|$)",
    re.I,
)
ID_HINT = re.compile(r"(/\d{2,}(/|$|-|\.html?))|([-_/]\d{4,}(/|$|\.html?))|(/\d{4}/\d{2}/)")
SKIP_LINK = re.compile(
    r"(\.(pdf|docx?|xlsx?|zip|rar|jpe?g|png|gif|mp4)$)|(/(tag|tags|category|categories|page|"
    r"search|login|auth|lang)/)|([?&]page=)|(mailto:|tel:|javascript:)",
    re.I,
)
MIN_TITLE_LEN = 20
TRAILING_MORE = re.compile(r"[\s|·–-]*(подробнее|читать далее|читать|batafsil|read more|далее)\s*[→»>]*\s*$", re.I)
TRAILING_DATE = re.compile(
    r"[\s|·,–-]*(\d{1,2}[./]\d{1,2}[./]20\d{2}|\d{1,2}\s+[а-яё]+\s+20\d{2})(\s*(г\.?|года))?\s*$", re.I)
LEADING_DATE = re.compile(
    r"^\s*(\d{1,2}[./]\d{1,2}[./]20\d{2}|\d{1,2}\s+[а-яё]+\s+20\d{2}|[A-Z][a-z]+\s+\d{1,2},\s+20\d{2})"
    r"(\s*(г\.?|года))?[\s,.|:–-]*", re.I)
EN_DATE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
                     r"\s+(\d{1,2}),\s+(20\d{2})")
EN_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
             "november", "december"]
TASHKENT = timezone(timedelta(hours=5))
GOVUZ_API = "https://api-portal.gov.uz"

DATE_PATTERNS = [
    (re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b"), ("d", "m", "y")),
    (re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b"), ("y", "m", "d")),
]
RU_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
RU_DATE = re.compile(r"\b(\d{1,2})\s+(январ\w*|феврал\w*|март\w*|апрел\w*|ма[яй]\w*|июн\w*|июл\w*|"
                     r"август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)\s+(20\d{2})", re.I)


@dataclass
class NewsItem:
    source_id: str
    source_name: str
    group: str
    url: str
    title: str
    published: datetime | None = None
    summary: str = ""
    text: str = ""
    image: str | None = None
    extra: dict = field(default_factory=dict)


def make_client(timeout: int) -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True, http2=False)


def normalize_url(url: str) -> str:
    url, _ = urldefrag(url.strip())
    parsed = urlparse(url)
    # utm-метки не должны порождать дубли
    query = "&".join(p for p in parsed.query.split("&") if p and not p.lower().startswith("utm_"))
    return parsed._replace(query=query, netloc=parsed.netloc.lower()).geturl().rstrip("/")


def _same_site(a: str, b: str) -> bool:
    ha, hb = urlparse(a).netloc.lower(), urlparse(b).netloc.lower()
    strip = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
    return strip(ha) == strip(hb) or strip(ha).endswith("." + strip(hb)) or strip(hb).endswith("." + strip(ha))


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        pass
    for pattern, order in DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            parts = dict(zip(order, map(int, m.groups())))
            try:
                return datetime(parts["y"], parts["m"], parts["d"], tzinfo=timezone.utc)
            except ValueError:
                continue
    m = EN_DATE.search(text)
    if m:
        return datetime(int(m.group(3)), EN_MONTHS.index(m.group(1).lower()) + 1, int(m.group(2)), tzinfo=timezone.utc)
    m = RU_DATE.search(text)
    if m:
        word = m.group(2).lower()
        month = next((n for k, n in RU_MONTHS.items() if word.startswith(k)), None)
        if month:
            try:
                return datetime(int(m.group(3)), month, int(m.group(1)), tzinfo=timezone.utc)
            except ValueError:
                return None
    return None


BOILERPLATE = re.compile(
    r"(узнайте (первым|больше)|официальн\w* (веб)?сайт|официальный портал|все новости|последние новости|"
    r"читайте (на|в)|подписывайтесь|добро пожаловать|rasmiy veb-sayt|official website)",
    re.I,
)


def is_boilerplate(text: str) -> bool:
    """Общий для всего сайта текст в meta description вместо описания конкретной новости."""
    if not text:
        return True
    chunks = [c for c in text.split(",") if c.strip()]
    if len(chunks) >= 6 and sum(len(c) for c in chunks) / len(chunks) < 40:
        return True  # список ключевых слов
    return bool(BOILERPLATE.search(text))


def strip_site_suffix(title: str) -> str:
    """«Тариф «Конструктор» | Новости | Hamkorbank» → «Тариф «Конструктор»»."""
    return re.split(r"\s+[|—–]\s+", title)[0].strip() if title else title


def shorten(text: str, limit: int = 450) -> str:
    """Обрезает по концу предложения, а если так не выйдет — по слову с многоточием."""
    text = _clean(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= limit // 2:
        return cut[: end + 1]
    return cut.rsplit(" ", 1)[0].rstrip(" ,.;:—–-") + "…"


def first_paragraph(text: str, title: str = "") -> str:
    head = title.lower()[:50]
    for paragraph in text.split("\n"):
        if len(paragraph) >= 60 and not (head and paragraph.lower().startswith(head)) and not is_boilerplate(paragraph):
            return shorten(paragraph)
    rest = [p for p in text.split("\n") if len(p) > 20 and not is_boilerplate(p)]
    return shorten(rest[0]) if rest else ""


# ── RSS ─────────────────────────────────────────────────────────────

def parse_feed(content: bytes, source: Source) -> list[NewsItem]:
    feed = feedparser.parse(content)
    items = []
    for entry in feed.entries:
        link = entry.get("link")
        title = _clean(entry.get("title"))
        if not link or not title:
            continue
        published = None
        for key in ("published_parsed", "updated_parsed"):
            if entry.get(key):
                published = datetime(*entry[key][:6], tzinfo=timezone.utc)
                break
        summary = _clean(BeautifulSoup(entry.get("summary", ""), "lxml").get_text(" "))
        items.append(NewsItem(source.id, source.name, source.group, normalize_url(link), title,
                              published, shorten(summary)))
    return items


def looks_like_feed(response: httpx.Response) -> bool:
    ctype = response.headers.get("content-type", "").lower()
    head = response.content[:500].lstrip().lower()
    return "xml" in ctype or "rss" in ctype or head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head


def discover_feed(soup: BeautifulSoup, base_url: str) -> str | None:
    for link in soup.find_all("link", rel=lambda v: v and "alternate" in v):
        if "rss" in (link.get("type") or "") or "atom" in (link.get("type") or ""):
            href = link.get("href")
            if href:
                return urljoin(base_url, href)
    return None


# ── HTML ────────────────────────────────────────────────────────────

def _nearby_date(tag) -> datetime | None:
    """Ищет дату рядом со ссылкой: в <time>, атрибутах или тексте родительских блоков."""
    node = tag
    for _ in range(4):
        if node is None:
            break
        time_tag = node.find("time") if hasattr(node, "find") else None
        if time_tag and len(node.get_text(" ", strip=True)) <= 800:
            dt = parse_date(time_tag.get("datetime") or time_tag.get_text())
            if dt:
                return dt
        text = node.get_text(" ", strip=True) if hasattr(node, "get_text") else ""
        if len(text) > 800:
            break  # дошли до общего контейнера списка — дальше даты чужих новостей
        dt = parse_date(text)
        if dt:
            return dt
        node = node.parent
    return None


def _link_title(a) -> str:
    title = _clean(a.get_text(" "))
    if len(title) < MIN_TITLE_LEN:
        title = _clean(a.get("title") or "")
    if len(title) < MIN_TITLE_LEN:
        # заголовок часто лежит в соседнем h2/h3 внутри той же карточки
        card = a.find_parent(["article", "li", "div"])
        heading = card.find(["h1", "h2", "h3", "h4", "h5"]) if card else None
        if heading:
            title = _clean(heading.get_text(" "))
    return title


def parse_html_list(html: str, source: Source, page_url: str) -> list[NewsItem]:
    soup = BeautifulSoup(html, "lxml")
    base = soup.find("base", href=True)
    if base:
        page_url = urljoin(page_url, base["href"])
    for junk in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        junk.decompose()

    pattern = re.compile(source.link_pattern) if source.link_pattern else None
    sel = source.selectors or {}
    candidates = []

    if sel.get("item"):
        for block in soup.select(sel["item"]):
            a = block.select_one(sel.get("link", "a[href]"))
            if not a or not a.get("href"):
                continue
            title_tag = block.select_one(sel["title"]) if sel.get("title") else None
            title = _clean(title_tag.get_text(" ")) if title_tag else _link_title(a)
            date_tag = block.select_one(sel["date"]) if sel.get("date") else None
            published = parse_date(date_tag.get("datetime") or date_tag.get_text()) if date_tag else _nearby_date(block)
            candidates.append((a["href"], title, published))
    else:
        for a in soup.find_all("a", href=True):
            candidates.append((a["href"], _link_title(a), None, a))

    items: dict[str, NewsItem] = {}
    for cand in candidates:
        href, title = cand[0], cand[1]
        url = normalize_url(urljoin(page_url, href))
        if not url.startswith("http") or SKIP_LINK.search(url) or not _same_site(url, page_url):
            continue
        if url == normalize_url(page_url):
            continue
        path = urlparse(url).path + ("?" + urlparse(url).query if urlparse(url).query else "")
        if pattern:
            if not pattern.search(url):
                continue
        elif not sel.get("item") and not (NEWS_HINTS.search(path) and ID_HINT.search(path)) \
                and not (NEWS_HINTS.search(path) and path.count("/") >= 3 and len(path) > 25):
            continue
        published = cand[2] if len(cand) == 3 else _nearby_date(cand[3])
        # «31 октября 2026 г. Заголовок» — дату переносим в published
        # «… 21 октября 2026 Подробнее» — то же для даты в конце карточки
        title = TRAILING_MORE.sub("", title)
        for rx, cut in ((LEADING_DATE, lambda t, m: t[m.end():]), (TRAILING_DATE, lambda t, m: t[:m.start()])):
            m = rx.search(title)
            if m and parse_date(m.group(1)):
                published = parse_date(m.group(1))  # дата из самой карточки надёжнее соседних блоков
                title = cut(title, m).strip()
        if len(title) < MIN_TITLE_LEN:
            continue
        if url not in items or len(title) > len(items[url].title):
            items[url] = NewsItem(source.id, source.name, source.group, url, title[:300], published)
    return list(items.values())


# ── Портал gov.uz ───────────────────────────────────────────────────
# Сайты министерств на gov.uz рендерятся в браузере, поэтому читаем тот же JSON API,
# что и сам сайт. Организация передаётся заголовком `code`, язык — заголовком `language`.

def _govuz_headers(authority: str | None) -> dict:
    headers = {"language": "ru", "Accept": "application/json"}
    if authority:
        headers["code"] = authority
    return headers


def _govuz_prefix(authority: str | None) -> str:
    return "/authorities/news" if authority else "/news"


def fetch_govuz(client: httpx.Client, source: Source) -> list[NewsItem]:
    authority = source.id if source.authority is None else source.authority
    response = client.get(f"{GOVUZ_API}{_govuz_prefix(authority)}/category",
                          params={"code_name": "news", "page": 1}, headers=_govuz_headers(authority))
    response.raise_for_status()
    site = f"https://gov.uz/ru/{authority}" if authority else "https://gov.uz/ru"
    items = []
    for row in response.json().get("data", []):
        title = _clean(row.get("title"))
        if not title or not row.get("id"):
            continue
        published = None
        if row.get("date"):
            try:
                published = datetime.strptime(row["date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TASHKENT)
            except ValueError:
                published = parse_date(row["date"])
        items.append(NewsItem(
            source.id, source.name, source.group, f"{site}/news/view/{row['id']}", title[:300], published,
            shorten(row.get("anons") or ""), image=row.get("anons_image"),
            extra={"govuz_id": row["id"], "govuz_authority": authority},
        ))
    return items


def _enrich_govuz(client: httpx.Client, item: NewsItem, max_chars: int) -> None:
    authority = item.extra["govuz_authority"]
    try:
        response = client.get(f"{GOVUZ_API}{_govuz_prefix(authority)}/view",
                              params={"id": item.extra["govuz_id"]}, headers=_govuz_headers(authority))
        response.raise_for_status()
        data = response.json().get("data") or {}
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("gov.uz: не удалось получить %s: %s", item.url, exc)
        return
    body = BeautifulSoup(data.get("body") or "", "lxml")
    paragraphs = [_clean(p.get_text(" ")) for p in body.find_all(["p", "li"])] or [_clean(body.get_text(" "))]
    item.text = "\n".join(p for p in paragraphs if len(p) > 20)[:max_chars]
    if not item.summary:
        item.summary = shorten(data.get("anons") or "") or first_paragraph(item.text, item.title)


def fetch_source(client: httpx.Client, source: Source) -> list[NewsItem]:
    if source.type == "govuz":
        return fetch_govuz(client, source)
    response = client.get(source.url)
    response.raise_for_status()
    if source.type == "rss" or (source.type == "auto" and looks_like_feed(response)):
        return parse_feed(response.content, source)

    if source.type == "auto":
        feed_url = discover_feed(BeautifulSoup(response.text, "lxml"), str(response.url))
        if feed_url:
            try:
                feed_resp = client.get(feed_url)
                feed_resp.raise_for_status()
                items = parse_feed(feed_resp.content, source)
                if items:
                    log.debug("%s: использую RSS %s", source.id, feed_url)
                    return items
            except httpx.HTTPError as exc:
                log.debug("%s: RSS %s недоступен: %s", source.id, feed_url, exc)
    return parse_html_list(response.text, source, str(response.url))


# ── Страница новости ────────────────────────────────────────────────

UZ_SWITCH = re.compile(r"^(uz|o.?z(bek(cha)?)?|ўзб(екча)?)$", re.I)


def uz_alternates(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Ссылки на узбекскую версию этой же страницы: <link hreflang="uz…"> и переключатель языка."""
    found = []
    for link in soup.find_all("link", hreflang=True, href=True):
        if link["hreflang"].lower().startswith("uz"):
            found.append(urljoin(page_url, link["href"]))
    depth = len([p for p in urlparse(page_url).path.split("/") if p])
    for a in soup.find_all("a", href=True):
        if UZ_SWITCH.match(_clean(a.get_text())):
            url = urljoin(page_url, a["href"])
            # переключатель, ведущий на общий список, а не на эту новость, не годится
            if len([p for p in urlparse(url).path.split("/") if p]) >= depth:
                found.append(url)
    return list(dict.fromkeys(u for u in found if u.startswith("http") and normalize_url(u) != normalize_url(page_url)))


def enrich(client: httpx.Client, item: NewsItem, max_chars: int = 6000) -> None:
    """Догружает описание, дату, картинку и основной текст со страницы новости."""
    if "govuz_id" in item.extra:
        _enrich_govuz(client, item, max_chars)
        return
    try:
        response = client.get(item.url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.debug("Не удалось открыть %s: %s", item.url, exc)
        return
    soup = BeautifulSoup(response.text, "lxml")
    item.extra["uz_alternates"] = uz_alternates(soup, str(response.url))

    def meta(*names: str) -> str | None:
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    if not item.summary:
        description = _clean(meta("og:description", "description", "twitter:description"))
        if not is_boilerplate(description):
            item.summary = shorten(description)
    if not item.image:
        image = meta("og:image", "twitter:image")
        item.image = urljoin(item.url, image) if image else None
    if not item.published:
        item.published = parse_date(meta("article:published_time", "og:updated_time", "pubdate", "date"))
        if not item.published:
            time_tag = soup.find("time")
            if time_tag:
                item.published = parse_date(time_tag.get("datetime") or time_tag.get_text())
    og_title = strip_site_suffix(_clean(meta("og:title")))
    # заголовок из списка обычно полный; og:title берём, только если из списка его не достали
    if og_title and og_title.lower() not in {"новости", "news"}:
        short = len(item.title) < MIN_TITLE_LEN + 5 or item.title.endswith(("…", "..."))
        # в карточке списка к заголовку бывает приклеен анонс: «Заголовок Ташкент, 29 октября…»
        glued = len(og_title) >= MIN_TITLE_LEN and len(item.title) > len(og_title) + 10 \
            and item.title.lower().startswith(og_title.lower()[:40])
        if (short and len(og_title) > len(item.title)) or glued:
            item.title = og_title[:300]

    for junk in soup(["script", "style", "noscript", "header", "footer", "nav", "aside", "form"]):
        junk.decompose()
    body = soup.find("article") or soup.find("main") or soup.body
    if body:
        images = []
        for img in body.find_all("img"):
            src = img.get("data-src") or img.get("src") or ""
            if not src or src.startswith("data:") or re.search(r"\.(svg|gif)(\?|$)", src, re.I):
                continue
            url = urljoin(item.url, src)
            if url not in images:
                images.append(url)
        item.extra["content_images"] = images[:5]
    if body:
        paragraphs = [_clean(p.get_text(" ")) for p in body.find_all(["p", "li", "h2", "h3"])]
        text = "\n".join(p for p in paragraphs if len(p) > 40)
        item.text = text[:max_chars]
    if not item.summary and item.text:
        item.summary = first_paragraph(item.text, item.title)
