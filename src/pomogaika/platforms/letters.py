"""Разбор писем площадок по их настоящей вёрстке.

Письма приходят таблицами: подпись на одной строке, значение на следующей.

    Заезд
    четверг, 25 июня
    Основной гость
    Елена Юкаева

Поэтому разбираем построчно «метка → следующая непустая строка», а не регулярками
по всему тексту: так меньше шансов поймать чужое число.

Проверено на живом ящике 30.09.2026:
  * Яндекс   — имя гостя, даты, взрослые и дети, номер брони;
  * Суточно  — номер брони, даты, предоплата, телефон и имя из подписи;
  * Островок — работает старый разбор в email_parser.py;
  * Авито    — **деталей в письмах нет вообще**: ни имени, ни дат, ни номера брони.
    Только «Вы сможете получить N ₽». Из писем Авито бронь собрать нельзя.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}

DATE_DMY = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
DATE_WORDS = re.compile(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", re.IGNORECASE)
TIME = re.compile(r"в\s+(\d{1,2}):(\d{2})")
MONEY = re.compile(r"(\d[\d\s  ,]*(?:\.\d{2})?)\s*₽")
PHONE = re.compile(r"\+7[\s\d]{10,}")


@dataclass
class LetterBooking:
    platform: str
    external_id: str = ""
    guest_name: str = ""
    guests_count: int = 0
    check_in: datetime | None = None
    check_out: datetime | None = None
    amount_cents: int = 0
    paid: bool = False
    kind: str = "created"          # created | cancelled | request
    phone: str = ""


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _after(lines: list[str], label: str, within: int = 3) -> str:
    """Значение, идущее после подписи. Пустые строки уже отброшены."""
    low = label.casefold()
    for i, line in enumerate(lines):
        if line.casefold() == low or line.casefold().startswith(low + " "):
            for nxt in lines[i + 1:i + 1 + within]:
                if nxt.casefold() != low:
                    return nxt
    return ""


def _month(word: str) -> int | None:
    low = word.casefold()
    for stem, num in MONTHS.items():
        if low.startswith(stem):
            return num
    return None


def _date(text: str, *, year_hint: int, hour: int, minute: int = 0) -> datetime | None:
    m = DATE_DMY.search(text)
    if m:
        day, month, year = (int(x) for x in m.groups())
        return _safe(year, month, day, hour, minute)
    m = DATE_WORDS.search(text)
    if m:
        day = int(m.group(1))
        month = _month(m.group(2))
        year = int(m.group(3)) if m.group(3) else year_hint
        if month:
            return _safe(year, month, day, hour, minute)
    return None


def _safe(year: int, month: int, day: int, hour: int, minute: int) -> datetime | None:
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def _money_cents(text: str) -> int:
    """Суммы приходят по-разному: «27000 ₽» у Суточно, «33,240.00 ₽» у Яндекса."""
    m = MONEY.search(text)
    if not m:
        return 0
    raw = m.group(1).strip()
    # копейки отделены точкой, тысячи — запятой или пробелом
    cents = 0
    if re.search(r"\.\d{2}$", raw):
        cents = int(raw[-2:])
        raw = raw[:-3]
    digits = re.sub(r"\D", "", raw)
    return (int(digits) * 100 + cents) if digits else 0


def parse_yandex(text: str, *, year_hint: int) -> LetterBooking | None:
    """«Новое бронирование в «KAVA…»» — самое полное письмо из всех."""
    lines = _lines(text)
    joined = "\n".join(lines)
    if "Яндекс" not in joined and "YA-" not in joined:
        return None

    cancelled = "Отмена бронирования" in joined
    check_in = check_out = None

    # «25.06.2026 - 28.06.2026» в шапке — самый надёжный вариант
    span = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4})\s*[-–—]\s*(\d{1,2}\.\d{1,2}\.\d{4})", joined)
    if span:
        check_in = _date(span.group(1), year_hint=year_hint, hour=14)
        check_out = _date(span.group(2), year_hint=year_hint, hour=12)
    else:
        check_in = _date(_after(lines, "Заезд"), year_hint=year_hint, hour=14)
        check_out = _date(_after(lines, "Выезд"), year_hint=year_hint, hour=12)
    if not check_in or not check_out:
        return None

    guest = _after(lines, "Основной гость")
    adults = re.search(r"Взрослые:\s*(\d+)", joined)
    kids = re.search(r"Дети:\s*(\d+)", joined)
    guests = (int(adults.group(1)) if adults else 0) + (int(kids.group(1)) if kids else 0)
    if not guests:
        m = re.search(r"Гостей:\s*(\d+)", joined)
        guests = int(m.group(1)) if m else 0

    number = re.search(r"(YA-[\d-]+(?::\d+)?)", joined)
    return LetterBooking(
        platform="yandex",
        external_id=number.group(1) if number else "",
        guest_name=guest if _looks_like_name(guest) else "",
        guests_count=guests,
        check_in=check_in,
        check_out=check_out,
        amount_cents=_money_cents(_after(lines, "Оплатил гость") or joined),
        paid="Подтверждено" in joined and not cancelled,
        kind="cancelled" if cancelled else "created",
    )


def parse_sutochno(text: str, *, year_hint: int) -> LetterBooking | None:
    """Суточно шлёт несколько писем про одну бронь, и в каждом есть её номер.

    «Новый запрос на бронь», «Гость внес предоплату», «Гость отказался» — все несут
    блок «Детали бронирования» с номером, датами, суммами и числом гостей. Поэтому
    связывать письма вручную не нужно: они сходятся по номеру брони сами.
    """
    lines = _lines(text)
    joined = "\n".join(lines)
    if "Суточно" not in joined and "Сдавайся" not in joined and "Бронь №" not in joined:
        return None

    number = re.search(r"Бронь\s*№\s*(\d+)", joined)
    check_in = _date(_after(lines, "Заезд"), year_hint=year_hint, hour=14)
    check_out = _date(_after(lines, "Отъезд") or _after(lines, "Выезд"), year_hint=year_hint, hour=12)
    if not check_in or not check_out:
        return None

    # полная стоимость = предоплата + оплата при заселении
    prepaid = _money_cents(_after(lines, "Предоплата"))
    on_arrival = _money_cents(_after(lines, "Оплата при заселении"))
    total = prepaid + on_arrival

    guests = 0
    people = re.search(r"(\d+)\s+гост", _after(lines, "Количество гостей") or joined)
    if people:
        guests = int(people.group(1))

    phone = PHONE.search(joined)
    guest = _name_near_phone(lines)

    # «отменена» встречается в дежурных фразах вроде «иначе она будет отменена»,
    # поэтому ловим только явные формулировки о судьбе брони
    cancelled = bool(re.search(r"Гость отказался|бронь отменена|бронирование отменено", joined, re.I))
    requested = "запрос на бронь" in joined.casefold()

    return LetterBooking(
        platform="sutochno",
        external_id=number.group(1) if number else "",
        guest_name=guest,
        guests_count=guests,
        check_in=check_in,
        check_out=check_out,
        amount_cents=total or prepaid,
        paid=prepaid > 0 and "внес предоплату" in joined.casefold(),
        kind="cancelled" if cancelled else ("request" if requested else "created"),
        phone=phone.group(0).strip() if phone else "",
    )


def _name_near_phone(lines: list[str]) -> str:
    """Суточно подписывает телефон именем гостя: «+7 916 ... — Евгения»."""
    for i, line in enumerate(lines):
        if not PHONE.search(line):
            continue
        for nxt in lines[i:i + 4]:
            m = re.search(r"[—–-]\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)", nxt)
            if m:
                return m.group(1).strip()
    return ""


def _looks_like_name(value: str) -> bool:
    """Отсекаем мусор вроде «</td>», который раньше попадал в имя гостя."""
    if not value or len(value) > 80 or "<" in value or ">" in value:
        return False
    return bool(re.search(r"[A-Za-zА-Яа-яЁё]{2,}", value))


def parse(text: str, *, year_hint: int | None = None) -> LetterBooking | None:
    """Разобрать письмо площадки. None — значит, данных о брони в нём нет."""
    year_hint = year_hint or datetime.now().year
    for reader in (parse_yandex, parse_sutochno):
        found = reader(text, year_hint=year_hint)
        if found:
            return found
    return None
