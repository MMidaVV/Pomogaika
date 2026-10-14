"""Телефон гостя из письма. Служебные номера сюда не проходят и в журнал не пишутся."""

from __future__ import annotations

import re

from pomogaika.config import settings

# Ровно 10 цифр после 7 или 8, чтобы не склеить два номера из соседних строк.
_CANDIDATE = re.compile(r"(?<!\d)(?:\+7|8)(?:[\s\-()]*\d){10}")


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) != 11 or not digits.startswith("7"):
        return ""
    return "+" + digits


def _blocked() -> set[str]:
    found = set()
    for part in (settings.guest_phone_blocklist or "").split(","):
        number = normalize_phone(part)
        if number:
            found.add(number)
    return found


def keep_guest_phone(raw: str) -> str:
    """Пустая строка — номер не гостевой: кривой, 8-800 или из списка-исключения."""
    number = normalize_phone(raw)
    if not number or number.startswith("+7800") or number in _blocked():
        return ""
    return number


def first_guest_phone(text: str) -> str:
    for match in _CANDIDATE.finditer(text or ""):
        kept = keep_guest_phone(match.group(0))
        if kept:
            return kept
    return ""
