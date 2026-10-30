from pathlib import Path

import httpx

from bot import __main__ as cli
from bot.config import Settings, Source
from bot.fetch import NewsItem, enrich, fetch_source, parse_date, parse_html_list
from bot.format import build_post
from bot.rates import build_rates_post
from bot.state import State

FIX = Path(__file__).parent / "fixtures"
BANK = Source(id="testbank", name="Тест Банк", url="https://bank.uz/ru/news/", group="bank")


def mock_client(routes: dict[str, tuple[str, str]]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url).rstrip("/")
        for url, (body, ctype) in routes.items():
            if key == url.rstrip("/"):
                return httpx.Response(200, text=body, headers={"content-type": ctype})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_parse_date_formats():
    assert parse_date("28.10.2026").date().isoformat() == "2026-10-28"
    assert parse_date("27 октября 2026").date().isoformat() == "2026-10-27"
    assert parse_date("2026-10-28T10:00:00+05:00").hour == 10
    assert parse_date("Thu, 29 Oct 2026 10:00:00 +0500").day == 29
    assert parse_date("без даты") is None


def test_html_list_heuristics():
    items = parse_html_list((FIX / "bank_list.html").read_text(), BANK, BANK.url)
    by_url = {i.url: i for i in items}
    assert set(by_url) == {"https://bank.uz/ru/news/4512", "https://bank.uz/ru/news/4511"}
    assert "22%" in by_url["https://bank.uz/ru/news/4512"].title
    assert by_url["https://bank.uz/ru/news/4512"].published.date().isoformat() == "2026-10-28"
    # заголовок взят из <h3>, а не «Подробнее»; utm-метка не создала дубль
    assert by_url["https://bank.uz/ru/news/4511"].title.startswith("Банк привлёк")
    assert by_url["https://bank.uz/ru/news/4511"].published.day == 27


def test_selectors_override():
    src = Source(id="s", name="S", url=BANK.url, selectors={"item": ".card", "title": "h3"})
    items = parse_html_list((FIX / "bank_list.html").read_text(), src, src.url)
    titles = sorted(i.title for i in items)
    assert titles == ["Банк запустил новый онлайн-депозит «Максимум» со ставкой 22% годовых",
                      "Банк привлёк кредитную линию $100 млн от АБР для поддержки МСБ"]


def test_rss_and_autodiscovery():
    page = '<html><head><link rel="alternate" type="application/rss+xml" href="/ru/rss/"></head><body></body></html>'
    client = mock_client({
        "https://cbu.uz/ru/press_center/": (page, "text/html"),
        "https://cbu.uz/ru/rss/": ((FIX / "feed.xml").read_text(), "application/rss+xml"),
    })
    items = fetch_source(client, Source(id="cbu", name="ЦБ", url="https://cbu.uz/ru/press_center/", group="regulator"))
    assert len(items) == 1
    assert items[0].title.startswith("ЦБ сохранил")
    assert items[0].summary == "Правление Центрального банка приняло решение сохранить ставку."


def test_enrich_and_post():
    client = mock_client({"https://bank.uz/ru/news/4512": ((FIX / "article.html").read_text(), "text/html")})
    item = NewsItem("testbank", "Тест Банк", "bank", "https://bank.uz/ru/news/4512", "Короткий заголовок новости банка")
    enrich(client, item)
    assert item.published.year == 2026
    assert "1 млн сумов" in item.summary
    assert item.image == "https://bank.uz/img/deposit.jpg"
    assert "проценты выплачиваются" in item.text
    post = build_post(item)
    assert "<b>Банк запустил новый онлайн-депозит" in post
    assert 'href="https://bank.uz/ru/news/4512"' in post
    assert "#банки" in post and "#testbank" in post
    ai_post = build_post(item, {"importance": 4, "category": "Новые продукты", "headline": "Депозит <22%>",
                                "summary": "Ставка 22%.", "tags": ["депозиты", "ставки банков"]})
    assert ai_post.startswith("🆕 <b>Депозит &lt;22%&gt;</b>")
    assert "#ставки_банков" in ai_post


def test_rates_post():
    post, date = build_rates_post([
        {"Ccy": "USD", "Rate": "12650.50", "Diff": "15.2", "Nominal": "1", "Date": "30.10.2026"},
        {"Ccy": "RUB", "Rate": "155.10", "Diff": "-0.4", "Nominal": "1", "Date": "30.10.2026"},
    ])
    assert date == "30.10.2026"
    assert "12 650.50" in post and "🔺" in post and "🔻" in post


def test_run_once_initializes_then_posts(tmp_path, monkeypatch):
    sources_file = tmp_path / "sources.yaml"
    sources_file.write_text("sources:\n  - {id: testbank, name: Тест Банк, group: bank, url: 'https://bank.uz/ru/news/'}\n")
    list_html = (FIX / "bank_list.html").read_text()
    routes = {"https://bank.uz/ru/news/": (list_html, "text/html"),
              "https://bank.uz/ru/news/4512": ((FIX / "article.html").read_text(), "text/html")}
    monkeypatch.setattr(cli, "make_client", lambda timeout: mock_client(routes))
    sent = []

    class FakeTelegram:
        def __init__(self, *a):
            pass

        def send_message(self, html, preview_url=None):
            sent.append(html)

    monkeypatch.setattr(cli, "Telegram", FakeTelegram)
    settings = Settings(telegram_token="t", telegram_chat_id="c", anthropic_api_key="", sources_file=sources_file,
                        state_file=tmp_path / "state.json", post_delay_seconds=0, max_age_days=10_000)

    assert cli.run_once(settings, dry_run=False) == 0  # первый запуск только запоминает ленту
    assert State(settings.state_file).source_initialized("testbank")

    routes["https://bank.uz/ru/news/"] = (list_html.replace("/ru/news/4512/", "/ru/news/4513/"), "text/html")
    routes["https://bank.uz/ru/news/4513"] = routes.pop("https://bank.uz/ru/news/4512")
    assert cli.run_once(settings, dry_run=False) == 1
    assert "онлайн-депозит" in sent[0]
    assert cli.run_once(settings, dry_run=False) == 0  # повторно не публикуется
    assert State(settings.state_file).is_seen("https://bank.uz/ru/news/4513")

