"""Код двери: только политика + человек. Цифры в журнал и репозиторий не кладём."""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy.orm import Session

from pomogaika.domain.policy import check
from pomogaika.models import Booking
from pomogaika.services.seed import audit, emit

CODEY = re.compile(r"^\d{4,8}$")


def door_decision(booking: Booking, *, now: datetime | None = None):
    now = now or datetime.now()
    return check(
        "door_code",
        {
            "paid": booking.paid,
            "now": now,
            "check_in": booking.check_in,
            "check_out": booking.check_out,
        },
    )


def code_hint(text: str) -> str:
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 2:
        return digits[-2:]
    return "??"


def looks_like_bare_code(text: str) -> bool:
    return bool(CODEY.match(text.strip()))


def bookings_in_code_window(session: Session, property_id: int, *, now: datetime | None = None) -> list[Booking]:
    now = now or datetime.now()
    rows = session.query(Booking).filter(Booking.property_id == property_id).all()
    ready = []
    for booking in rows:
        if booking.status in {"cancelled"}:
            continue
        decision = door_decision(booking, now=now)
        if decision.allowed:
            ready.append(booking)
    return ready


def issue_code(
    session: Session,
    booking: Booking,
    raw_text: str,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Принимает текст собственника. Не сохраняет код, только хвост из двух цифр."""
    now = now or datetime.now()
    decision = door_decision(booking, now=now)
    if not decision.allowed:
        audit(session, "door_code", decision.reason, "owner", "denied", decision.mode)
        return False, decision.reason
    text = raw_text.strip()
    if not text:
        return False, "Пусто: пришлите коды калитки, домофона и сейфа одним сообщением"
    hint = code_hint(text)
    booking.code_hint = hint
    booking.code_issued_at = now
    if not booking.checked_in_at and now >= booking.check_in:
        booking.checked_in_at = now
        emit(session, "stay.checked_in", {"booking_id": booking.id}, booking.property_id)
    emit(session, "stay.code_issued", {"booking_id": booking.id, "hint": hint}, booking.property_id)
    audit(
        session,
        "door_code",
        f"Собственник отправил коды гостю {booking.guest_name}, хвост **{hint}",
        "owner",
        "owner",
        decision.mode,
    )
    guest_message = (
        "Коды на сегодня (одноразовые, никому не пересылайте):\n\n"
        f"{text}\n\n"
        "Если калитка или домофон не открылись — напишите сразу, откроем удалённо."
    )
    return True, guest_message
