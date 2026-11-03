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
    item = NewsItem("testbank", "Тест Банк", "bank", "https://bank.uz/ru/news/4512", "Депозит")
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

        def send_photo(self, jpeg, caption):
            assert jpeg[:2] == b"\xff\xd8"  # JPEG
            assert len(caption) <= 1024
            sent.append(caption)

    monkeypatch.setattr(cli, "make_publisher", lambda settings: FakeTelegram())
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



def test_title_dates_and_more_links():
    html = ('<div><a href="/ru/news/tariffs-nov/">Изменение тарифов с 2 ноября 2026 года 21 октября 2026 Подробнее</a></div>'
            '<div><a href="/ru/news/davr-ebrd/">31 октября 2026 г. Davr Bank и ЕБРР расширяют поддержку МСБ</a></div>')
    src = Source(id="t", name="T", url="https://b.uz/ru/news/", link_pattern="/news/")
    items = {i.url.rsplit("/", 1)[1]: i for i in parse_html_list(html, src, src.url)}
    assert items["tariffs-nov"].title == "Изменение тарифов с 2 ноября 2026 года"
    assert items["tariffs-nov"].published.date().isoformat() == "2026-10-21"
    assert items["davr-ebrd"].title == "Davr Bank и ЕБРР расширяют поддержку МСБ"


def test_govuz_source():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["code"] == "mfa" and request.headers["language"] == "ru"
        if request.url.path == "/authorities/news/category":
            return httpx.Response(200, json={"data": [{"id": 7, "date": "2026-11-02 10:50:00",
                                                        "title": "Консультации между МИД Узбекистана и Бахрейна",
                                                        "anons": "", "anons_image": "https://x/img.jpg"}]})
        assert request.url.params["id"] == "7"
        return httpx.Response(200, json={"data": {"anons": "", "body": "<p>Первый раунд политических консультаций прошёл в Манаме.</p>"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    src = Source(id="mfa", name="МИД", url="https://gov.uz/ru/mfa/news/news", type="govuz")
    [item] = fetch_source(client, src)
    assert item.url == "https://gov.uz/ru/mfa/news/view/7"
    assert item.published.utcoffset().total_seconds() == 5 * 3600
    enrich(client, item)
    assert item.summary.startswith("Первый раунд")


def _jpeg_size(data: bytes):
    from PIL import Image
    import io
    return Image.open(io.BytesIO(data)).size


def test_images_card_photo_and_watermark():
    import io
    from PIL import Image
    from bot.images import WatermarkConfig, apply_watermark, build_image, is_generic

    assert is_generic("https://aloqabank.uz/upload/media/images/social.png")
    assert is_generic("https://garantbank.uz/assets/img/logo%20600x600.jpg")
    assert not is_generic("https://api-portal.gov.uz/uploads/1/2026/news_.jpg")

    photo = Image.new("RGB", (900, 600), (90, 120, 150))
    buf = io.BytesIO(); photo.save(buf, "PNG")
    png = buf.getvalue()
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=png) if r.url.path == "/photo.png" else httpx.Response(404)))
    item = NewsItem("testbank", "Тест Банк", "bank", "https://bank.uz/ru/news/1",
                    "Банк запустил новый онлайн-депозит со ставкой 22% годовых", image="https://bank.uz/photo.png")
    wm = WatermarkConfig(text="@my_channel")
    assert _jpeg_size(build_image(client, item, wm)) == (900, 600)       # фото новости
    item.image = "https://bank.uz/logo.png"
    assert _jpeg_size(build_image(client, item, wm)) == (1280, 720)      # заглушка → карточка
    marked = apply_watermark(photo, wm)
    assert marked.getpixel((880, 580)) != photo.getpixel((880, 580))     # плашка в углу


def test_repeated_image_is_generic(tmp_path):
    state = State(tmp_path / "s.json")
    a = NewsItem("b", "B", "bank", "https://b.uz/1", "x" * 30, image="https://b.uz/same.jpg")
    b = NewsItem("b", "B", "bank", "https://b.uz/2", "y" * 30, image="https://b.uz/same.jpg")
    assert cli._generic_images(state, a) == set()
    assert cli._generic_images(state, b) == {"https://b.uz/same.jpg"}


def test_boilerplate_descriptions():
    from bot.fetch import first_paragraph, is_boilerplate
    assert is_boilerplate("Узнайте первым о последних новостях, обновлениях и достижениях Aloqabank")
    assert is_boilerplate("statistika, Uzstat, статистика, YaIM, inflyatsiya indeksi, palov indeksi, statistics")
    assert not is_boilerplate("Ставка по депозиту составит 22% годовых, минимальная сумма — 1 млн сумов.")
    text = "Короткая строка\nПо предварительным данным Госкомстата, в январе-сентябре выловлено 270,8 тысячи тонн рыбы."
    assert first_paragraph(text).startswith("По предварительным")


def test_shorten():
    from bot.fetch import shorten
    assert shorten("Первое предложение достаточно длинное. Второе предложение " + "слово " * 100, 70) \
        == "Первое предложение достаточно длинное."
    assert shorten("слово " * 100, 50).endswith("…") and len(shorten("слово " * 100, 50)) <= 51


def test_uzbek_latin_helpers():
    from bot.uzbek import cyr_to_latin, is_russian_not_uzbek, looks_uzbek_latin, to_new_latin, uz_url_candidates
    # новый алфавит (ЗРУ-1184): sh→ş, ch→ç, oʻ→ö, gʻ→ğ, тутуқ — ʼ
    assert cyr_to_latin("Меҳнат миграцияси бўйича ғолиб") == "Mehnat migratsiyasi böyiça ğolib"
    assert cyr_to_latin("Ёшлар ва ер, таълим") == "Yoşlar va yer, taʼlim"
    assert to_new_latin("O‘zbekiston bo‘yicha ilg‘or tashkil etish uchun") == "Özbekiston böyiça ilğor taşkil etiş uçun"
    assert to_new_latin("Oʻzbekiston boʻyicha soligʻi") == "Özbekiston böyiça soliği"
    assert to_new_latin("YaTTlar uçun böyiça topshiriş") == "YaTTlar uçun böyiça topşiriş"   # смесь (как на gov.uz)
    assert to_new_latin("O‘ZBEKISTON, SHAHAR, Shavkat, Chorvoq") == "ÖZBEKISTON, ŞAHAR, Şavkat, Çorvoq"
    assert to_new_latin("ta’lim, e'tibor, sun`iy, Is’hoq") == "taʼlim, eʼtibor, sunʼiy, Ishoq"
    assert to_new_latin("eksportchilarni qo‘llab-quvvatlash, ishchi") == "eksportçilarni qöllab-quvvatlaş, işçi"
    assert to_new_latin("Deutsche Bank, Wyndham, Bosch, Shanghai") == "Deutsche Bank, Wyndham, Bosch, Şanghai"
    assert is_russian_not_uzbek("Саммит в Анкаре: новые направления сотрудничества")
    assert not is_russian_not_uzbek("Ўзбекистон ва Германия ҳамкорлиги")
    assert looks_uzbek_latin("Biznes uchun «Konstruktor» tarifi", "Тариф «Конструктор»")
    assert not looks_uzbek_latin("Тариф «Конструктор» для бизнеса", "Тариф")
    assert uz_url_candidates("https://bank.uz/ru/news/abc") == ["https://bank.uz/uz/news/abc"]
    assert uz_url_candidates("https://ru.bank.uz/news/abc") == ["https://uz.bank.uz/news/abc"]
    assert uz_url_candidates("https://bank.uz/press-center/news/abc") == ["https://bank.uz/uz/press-center/news/abc"]


def test_bilingual_post_fits_caption():
    item = NewsItem("soliq", "Налоговый комитет", "gov", "https://gov.uz/ru/soliq/news/view/1",
                    "Упрощён порядок отчётности НДФЛ по аренде", summary="Длинное описание. " * 60)
    post = build_post(item, None, 1024, ("Ijara böyiça hisobot tartibi soddalaştirildi", "Uzun tavsif. " * 60))
    assert len(post) <= 1024
    assert "🇺🇿 <b>Ijara böyiça hisobot tartibi soddalaştirildi</b>" in post
    assert "Источник / Manba: Налоговый комитет" in post
    assert post.index("<b>Упрощён") < post.index("🇺🇿")
    assert "Длинное описание." in post and "Uzun tavsif." in post   # обе версии сохранили описание
    assert "Источник: " in build_post(item)                          # без узбекского — как раньше
