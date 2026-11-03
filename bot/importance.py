"""Оценка бизнес-значимости новости по правилам (1–5), когда нет ИИ.

Складывается из веса источника, ключевых тем (русский + узбекский, старая и новая латиница)
и конкретики (суммы, проценты). Нужна, чтобы в канал первыми шли важные новости, а проходные
(поздравления, графики работы, конкурсы на должности) не публиковались вовсе.
"""

from __future__ import annotations

import re

from .fetch import NewsItem

# Базовый вес источника: регулятор и экономический блок важнее отраслевых ведомств.
SOURCE_WEIGHT = {
    "cbu": 3.8, "cbu_press": 3.8, "president": 3.6, "imv": 3.2, "miit": 3.0, "soliq": 3.0,
    "davaktiv": 3.0, "invest": 3.0, "uzse": 3.0, "raqobat": 2.8, "gov": 2.8, "stat": 2.8, "mfa": 2.8,
}
GROUP_WEIGHT = {"regulator": 3.2, "invest": 3.0, "market": 3.0, "bank": 2.6, "gov": 2.4, "media": 2.0}

# (шаблон, баллы). Шаблоны ищутся в заголовке и описании (RU + UZ), без учёта регистра.
RULES: list[tuple[str, float]] = [
    # денежно-кредитная политика, законы, налоги
    (r"основн\w* ставк|ставк\w* рефинанс|асосий ставка|asosiy stavka|денежно-кредитн|pul-kredit|инфляц|inflyats", 2.0),
    (r"\bуказ\w*|постановлени\w*|закон\w*|\bфармон|\bqaror|\bqonun|nizom|регламент", 1.2),
    (r"налог|ндс|ндфл|таможен|пошлин|soliq|bojxona|boj\b|qqs\b", 1.2),
    # инвестиции, финансирование, рынок капитала
    (r"инвест\w*|investits|прям\w+ иностран", 1.0),
    (r"\bеврооблигац|облигац|obligats|ipo\b|spo\b|размещени\w+ акци|акци\w+ на бирж|eurobond|дивиденд|dividend", 1.5),
    (r"кредитн\w+ лини|kredit liniya|ебрр|ebrd|yeттb|yttb|\bабр\b|\badb\b|всемирн\w+ банк|jahon banki|\bмвф\b|\bimf\b|"
     r"\bifc\b|\baiib\b|\bаб(р|ии)\b|jica|kfw|opec fund", 1.5),
    (r"приватизац|xususiylashtir|аукцион\w*|auksion|госактив|davlat aktiv|свободн\w+ (экономическ|торгов)\w* зон|"
     r"erkin (iqtisodiy|savdo) zona", 1.0),
    (r"экспорт|импорт|eksport|import|внешн\w+ торгов|tashqi savdo|товарооборот|tovar ayirboshlash", 1.0),
    (r"ввп|yaim|yalpi ichki|макроэконом|бюджет|byudjet|госдолг|davlat qarzi|резерв", 1.2),
    (r"соглашени\w+.{0,40}(подписан|заключен)|подписал\w* соглашени|kelishuv\w*.{0,30}imzola|меморандум|memorandum", 1.0),
    (r"(торгово|экономическ|инвестицион|промышленн|энергетическ|финансов)\w*[- ]\w*\s*(сотрудничеств|партн[её]рств|связ|проект)|"
     r"savdo-iqtisodiy|iqtisodiy hamkorlik|investitsiyaviy hamkorlik", 1.0),
    (r"двусторонн\w+ сотрудничеств|ikki tomonlama hamkorlik|бизнес-форум|biznes-forum|деловой форум", 0.5),
    # продукты банков и условия для клиентов
    (r"запуст\w+|новы\w+ (продукт|услуг|тариф|карт|депозит|вклад|кредит)|yangi (mahsulot|xizmat|tarif|karta)|"
     r"ishga tush|joriy et|станов\w+ доступ|открыл\w* доступ|теперь доступ|встречайте|представил\w* нов|"
     r"презентовал|endi mavjud|taqdim et", 1.0),
    (r"изменени\w+.{0,25}(ставк|тариф|комисси|оферт|услови|стоимост)|(повыш|сниж)\w+.{0,15}(ставк|тариф|комисси)|"
     r"stavka\w*.{0,20}o.zgar|tarif\w*.{0,20}o.zgar", 1.0),
    (r"депозит|вклад|кредит|ипотек|лизинг|автокредит|микрозайм|рассрочк|кэшбэк|cashback|apple pay|google pay|"
     r"omonat|kredit|ipoteka|lizing", 0.8),
    (r"тариф|комисси|процентн\w+ ставк|ставк\w+ по|foiz stavka|tarif", 0.8),
    (r"предпринимател|бизнес|мсб|\bип\b|малого бизнеса|tadbirkor|biznes|yatt\b|kichik biznes", 0.5),
    # проходное
    (r"поздравл|с днём|с днем|праздник|bayram|tabrik|юбилей", -2.5),
    (r"график работы|режим работы|выходн\w+ дн|рабочи\w+ дн|ish tartibi|dam olish kun|технически\w+ работ|"
     r"профилактическ|texnik ishlar", -2.5),
    (r"конкурс\w* на (замещение )?должност|\bваканси|lavozimiga tanlov|bo.sh ish o.rin|ishga taklif", -2.5),
    (r"\bспорт|\bтурнир|\bкуб(ок|ка)\b|\bчемпионат|\bсоревновани|\bфутбол|\bsport\b|\bturnir|\bkubog|\bmusobaqa", -2.0),
    (r"диктант|субботник|благотвор|концерт|фестивал|выставк\w+ картин|библиотек|духовн|молодеж|молодёж|"
     r"ветеран|памят\w+|соболезн|ma.naviyat|yoshlar kun|xayriya", -1.5),
    (r"тренинг|семинар|вебинар|мастер-класс|открыт\w+ двер|выездн\w+ при[её]м|при[её]м граждан|обращени\w+ граждан|"
     r"trening|seminar|sayyor qabul|murojaat", -1.0),
    (r"мошенни|осторожн|будьте бдительн|firibgar|ehtiyot bo", -0.5),
]
COMPILED = [(re.compile(p, re.I), w) for p, w in RULES]

MONEY = re.compile(r"\d[\d\s.,]*\s*(млн|млрд|трлн|mln|mlrd|trln|тыс\.?\s*(долл|сум)|\$|долл|usd|евро|eur|сум\b|so.m\b)|"
                   r"\$\s*\d", re.I)
BIG_MONEY = re.compile(r"\d[\d\s.,]*\s*(млрд|трлн|mlrd|trln)|\d{3,}[\d\s.,]*\s*(млн|mln)\s*(долл|\$|usd|евро)", re.I)
PERCENT = re.compile(r"\d+([.,]\d+)?\s*%")


def rule_importance(item: NewsItem, uz: tuple[str, str] | None = None) -> float:
    text = " ".join([item.title, item.summary, *(uz or ())])
    head = " ".join([item.title, (uz or ("", ""))[0]])
    score = SOURCE_WEIGHT.get(item.source_id, GROUP_WEIGHT.get(item.group, 2.5))
    for pattern, weight in COMPILED:
        if pattern.search(text):
            # тема в заголовке весит полностью, только в описании — наполовину
            score += weight if pattern.search(head) else weight / 2
    if MONEY.search(text):
        score += 0.6
    if BIG_MONEY.search(text):
        score += 0.6
    if PERCENT.search(text):
        score += 0.4
    return round(max(1.0, min(5.0, score - 1.5)), 2)
