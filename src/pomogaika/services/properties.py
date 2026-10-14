"""Объекты собственника: добавление нового."""

from __future__ import annotations

from sqlalchemy.orm import Session

from pomogaika.models import Listing, Owner, Property
from pomogaika.services.seed import audit

PLATFORMS = ("avito", "sutochno", "ostrovok", "yandex")


def next_code(session: Session) -> str:
    """Свободный код вида obj2, obj3 — человекочитаемый и уникальный."""
    taken = {p.code for p in session.query(Property).all()}
    number = len(taken) + 1
    code = f"obj{number}"
    while code in taken:
        number += 1
        code = f"obj{number}"
    return code


def create_property(
    session: Session,
    *,
    owner: Owner,
    title: str,
    address: str = "",
    city: str = "",
    check_in_from: str = "14:00",
    check_out_until: str = "12:00",
    max_guests: int = 4,
) -> Property:
    """Новый объект с пустыми объявлениями по площадкам.

    Базу знаний соседнего объекта не копируем: парковка, коды и правила там свои,
    и с чужими фактами Помогайка отвечала бы гостю неправду. Лучше пусто.
    """
    name = (title or "").strip()
    if not name:
        raise ValueError("нужно название объекта")

    base = session.query(Property).order_by(Property.id).first()
    prop = Property(
        owner_id=owner.id,
        code=next_code(session),
        title=name,
        address=(address or "").strip(),
        city=(city or "").strip() or (base.city if base else ""),
        timezone=base.timezone if base else "Europe/Kaliningrad",
        knowledge={},
        max_guests=max(1, int(max_guests or 1)),
        check_in_from=(check_in_from or "14:00").strip(),
        check_out_until=(check_out_until or "12:00").strip(),
    )
    session.add(prop)
    session.flush()
    for platform in PLATFORMS:
        session.add(Listing(property_id=prop.id, platform=platform, external_id="", url="", ical_url=""))
    audit(session, "property.created", f"Добавлен объект «{name}»", "owner", "owner", "confirm")
    return prop
