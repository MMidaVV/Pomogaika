"""Правка базы знаний в чате. Коды и пароли отбрасываем."""

from __future__ import annotations

import re

from pomogaika.models import Property
from pomogaika.services.codes import looks_like_bare_code

ALIASES = {
    "парковка": "parking",
    "parking": "parking",
    "wifi": "wifi",
    "wi-fi": "wifi",
    "вайфай": "wifi",
    "интернет": "wifi",
    "заезд": "access",
    "доступ": "access",
    "access": "access",
    "вход": "entrance",
    "entrance": "entrance",
    "хост": "host",
    "host": "host",
    "кровати": "beds",
    "beds": "beds",
}

EDIT = re.compile(r"^([а-яёa-z0-9\-]+)\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)
SECRETISH = re.compile(r"(пароль|password|код\s*\d|сейф\s*\d)", re.IGNORECASE)


def dump_knowledge(prop: Property) -> str:
    k = prop.knowledge or {}
    lines = [
        f"{prop.title}",
        f"Адрес: {prop.address}",
        f"Заезд {prop.check_in_from} · выезд {prop.check_out_until} · до {prop.max_guests} гостей",
        "",
        "Можно поправить сообщением «парковка: новый текст». Коды и пароли не принимаю.",
        "",
        f"парковка: {k.get('parking', '')}",
        f"wifi: {k.get('wifi', '')}",
        f"заезд: {k.get('access', '')}",
        f"вход: {k.get('entrance', '')}",
        f"хост: {k.get('host', '')}",
    ]
    return "\n".join(lines)


def parse_knowledge_edit(text: str) -> tuple[str, str] | None:
    m = EDIT.match(text.strip())
    if not m:
        return None
    alias = m.group(1).strip().lower()
    key = ALIASES.get(alias)
    if not key:
        return None
    value = m.group(2).strip()
    return key, value


def apply_knowledge_edit(prop: Property, key: str, value: str) -> str:
    if looks_like_bare_code(value) or SECRETISH.search(value):
        return "Это похоже на код или пароль — в базу знаний не кладу. Гостю коды только через «Выдать код»."
    knowledge = dict(prop.knowledge or {})
    knowledge[key] = value
    prop.knowledge = knowledge
    if key == "entrance":
        prop.address = prop.address
    return f"Обновила «{key}»."
