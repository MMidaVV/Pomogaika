from __future__ import annotations

from datetime import datetime


BUSY_TOKENS = (
    "забронир",
    "закрыт",
    "занято",
    "блок",
    "closed",
    "booked",
    "busy",
    "blocked",
    "reserved",
    "unavailable",
    "not available",
    "avito (",
    "гость ical",
    "guest ical",
)


def is_adjacent(a_out: datetime, b_in: datetime) -> bool:
    """Заезд в день выезда — не конфликт, если новый заезд не раньше выезда."""
    return a_out <= b_in


def overlaps(a_in: datetime, a_out: datetime, b_in: datetime, b_out: datetime) -> bool:
    if is_adjacent(a_out, b_in) or is_adjacent(b_out, a_in):
        return False
    return a_in < b_out and b_in < a_out


def is_busy_label(name: str) -> bool:
    low = (name or "").casefold()
    if not low or low in {"гость", "guest"}:
        return True
    return any(token in low for token in BUSY_TOKENS)


# Площадки кладут в один и тот же iCal и брони, и закрытые даты. Различаем по подписи:
# «Avito (Closed)» — это блокировка собственника, а не гость.
BLOCK_TOKENS = ("closed", "заблокирован", "недоступ", "not available", "blocked", "unavailable")


def is_block_label(name: str) -> bool:
    low = (name or "").casefold()
    return any(token in low for token in BLOCK_TOKENS)


def is_occupancy_mirror(
    a_platform: str,
    a_name: str,
    a_in: datetime,
    a_out: datetime,
    b_platform: str,
    b_name: str,
    b_in: datetime,
    b_out: datetime,
) -> bool:
    """Одна занятость, выгруженная с двух площадок — не двойная бронь."""
    if (a_platform or "") == (b_platform or ""):
        return False
    if not overlaps(a_in, a_out, b_in, b_out):
        return False
    a_busy = is_busy_label(a_name)
    b_busy = is_busy_label(b_name)
    if a_busy and b_busy:
        return True
    if a_in.date() == b_in.date() and a_out.date() == b_out.date() and (a_busy or b_busy):
        return True
    if a_busy and a_in.date() <= b_in.date() and a_out.date() >= b_out.date():
        return True
    if b_busy and b_in.date() <= a_in.date() and b_out.date() >= a_out.date():
        return True
    return False


def cheaper_to_cancel(amount_a: int, amount_b: int, id_a: int, id_b: int) -> int:
    if amount_a <= amount_b:
        return id_a
    return id_b
