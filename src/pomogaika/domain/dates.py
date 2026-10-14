"""Разбор русских дат для блока календаря и привязки гостя."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

MONTHS = {
    "января": 1, "январь": 1, "янв": 1,
    "февраля": 2, "февраль": 2, "фев": 2,
    "марта": 3, "март": 3, "мар": 3,
    "апреля": 4, "апрель": 4, "апр": 4,
    "мая": 5, "май": 5,
    "июня": 6, "июнь": 6, "июн": 6,
    "июля": 7, "июль": 7, "июл": 7,
    "августа": 8, "август": 8, "авг": 8,
    "сентября": 9, "сентябрь": 9, "сен": 9,
    "октября": 10, "октябрь": 10, "окт": 10,
    "ноября": 11, "ноябрь": 11, "ноя": 11,
    "декабря": 12, "декабрь": 12, "дек": 12,
}

CLOSE_PREFIX = re.compile(
    r"^(?:закрой(?:те)?|закрыть|блок(?:ируй)?|заблокируй)\s+",
    re.IGNORECASE,
)
OPEN_PREFIX = re.compile(
    r"^(?:открой(?:те)?|открыть|разблокируй|разблокировать|сними|снять)\s+",
    re.IGNORECASE,
)
RANGE_NAMED = re.compile(
    r"(?:с\s+)?(\d{1,2})\s*[.\-/–—]\s*(\d{1,2})\s+([а-яё]+)",
    re.IGNORECASE,
)
RANGE_FROM_TO = re.compile(
    r"с\s+(\d{1,2})\s+([а-яё]+)\s+по\s+(\d{1,2})\s+([а-яё]+)",
    re.IGNORECASE,
)
RANGE_DOTS = re.compile(
    r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\s*[—\-–до]+\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?",
    re.IGNORECASE,
)
ONE_NAMED = re.compile(r"(?:заезд\w*|заезжаем|буду|с)\s+(\d{1,2})\s+([а-яё]+)", re.IGNORECASE)
ONE_DOT = re.compile(r"(?:заезд\w*|заезжаем|буду|с)\s+(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?")
BARE_DAY_MONTH = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\b")


def _year(month: int, day: int, now: datetime, explicit: int | None = None) -> int:
    if explicit:
        return explicit if explicit > 99 else 2000 + explicit
    candidate = datetime(now.year, month, min(day, 28))
    if candidate.date() < now.date() - timedelta(days=2):
        return now.year + 1
    return now.year


def _month(token: str) -> int | None:
    return MONTHS.get(token.lower().rstrip(".,"))


def to_stay_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    check_in = start.replace(hour=14, minute=0, second=0, microsecond=0)
    check_out = end.replace(hour=12, minute=0, second=0, microsecond=0)
    if check_out <= check_in:
        check_out = (check_in + timedelta(days=1)).replace(hour=12, minute=0)
    return check_in, check_out


def parse_range(text: str, *, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    now = now or datetime.now()
    raw = text.strip()
    m = RANGE_FROM_TO.search(raw)
    if m:
        m1, m2 = _month(m.group(2)), _month(m.group(4))
        if m1 and m2:
            d1, d2 = int(m.group(1)), int(m.group(3))
            y1, y2 = _year(m1, d1, now), _year(m2, d2, now)
            if m2 < m1:
                y2 = y1 + 1 if y2 <= y1 else y2
            return to_stay_range(datetime(y1, m1, d1), datetime(y2, m2, d2))
    m = RANGE_NAMED.search(raw)
    if m:
        month = _month(m.group(3))
        if month:
            d1, d2 = int(m.group(1)), int(m.group(2))
            year = _year(month, d1, now)
            return to_stay_range(datetime(year, month, d1), datetime(year, month, d2))
    m = RANGE_DOTS.search(raw.replace("по", "—"))
    if m:
        y1 = int(m.group(3)) if m.group(3) else None
        y2 = int(m.group(6)) if m.group(6) else None
        month1, day1 = int(m.group(2)), int(m.group(1))
        month2, day2 = int(m.group(5)), int(m.group(4))
        year1 = _year(month1, day1, now, y1)
        year2 = _year(month2, day2, now, y2)
        return to_stay_range(datetime(year1, month1, day1), datetime(year2, month2, day2))
    return None


def parse_close_command(text: str, *, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    raw = text.strip()
    if not CLOSE_PREFIX.search(raw):
        return None
    return parse_range(raw, now=now)


def parse_open_command(text: str, *, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    """«открой 28–30 сентября» — снять закрытие дат. Зеркало parse_close_command."""
    raw = text.strip()
    if not OPEN_PREFIX.search(raw):
        return None
    return parse_range(raw, now=now)


def parse_guest_dates(text: str, *, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    ranged = parse_range(text, now=now)
    if ranged:
        return ranged
    now = now or datetime.now()
    m = ONE_NAMED.search(text)
    if m:
        month = _month(m.group(2))
        if month:
            day = int(m.group(1))
            year = _year(month, day, now)
            start = datetime(year, month, day)
            return to_stay_range(start, start + timedelta(days=2))
    m = ONE_DOT.search(text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = _year(month, day, now, int(m.group(3)) if m.group(3) else None)
        start = datetime(year, month, day)
        return to_stay_range(start, start + timedelta(days=2))
    return None


def fmt_dt(value: datetime) -> str:
    return value.strftime("%d.%m %H:%M")


def fmt_span(check_in: datetime, check_out: datetime) -> str:
    return f"{check_in.strftime('%d.%m')} {check_in.strftime('%H:%M')} → {check_out.strftime('%d.%m')} {check_out.strftime('%H:%M')}"
