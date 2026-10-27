"""Сообщения гостю в узлах пути: до заезда, после заезда, перед выездом.

Смысл один: гость не должен ничего спрашивать, потому что ответ пришёл
раньше вопроса. Каждое сообщение уходит один раз на бронь — отметка в самой
записи, иначе фоновый цикл будет слать их каждые сорок секунд.

Писать можем только тем, кто уже написал нам сам: без `guest_chat_id`
Telegram не даст отправить ничего. Поэтому все узлы молчат, пока гость
не перешёл по ссылке.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from pomogaika.models import Booking
from pomogaika.services.bookings import ACTIVE
from pomogaika.services.notify import deliver_to_guest, notify_owner

log = logging.getLogger("pomogaika.nudges")

# За сколько до события спрашиваем и напоминаем.
BEFORE_ARRIVAL = timedelta(hours=24)
BEFORE_DEPARTURE = timedelta(hours=16)
AFTER_ARRIVAL = timedelta(minutes=40)


def _can_write(booking: Booking) -> bool:
    if booking.guest_channel == "whatsapp":
        return bool(booking.guest_whatsapp_chat_id)
    if booking.guest_channel == "max":
        return bool(booking.guest_max_chat_id)
    return bool(booking.guest_chat_id)


def _name(booking: Booking) -> str:
    name = (booking.guest_name or "").strip()
    return f"{name}, " if name else ""


def pre_arrival_text(booking: Booking) -> str:
    return (
        f"{_name(booking)}завтра ждём вас! Заезд с {booking.check_in:%H:%M}.\n\n"
        "Подскажите, во сколько примерно будете — подстроимся и пришлём коды "
        "заранее. Если планы сдвинутся, просто напишите сюда."
    )


def settled_text(booking: Booking) -> str:
    return (
        f"{_name(booking)}всё получилось? Квартиру нашли, коды подошли?\n\n"
        "Если что-то не так — напишите прямо сейчас, решим."
    )


def pre_departure_text(booking: Booking) -> str:
    return (
        f"{_name(booking)}напоминаю: выезд завтра до {booking.check_out:%H:%M}.\n\n"
        "Во сколько планируете съехать? Это нужно, чтобы не прислать уборку, "
        "пока вы ещё собираетесь."
    )


def due(session: Session, *, now: datetime | None = None) -> list[tuple[Booking, str, str]]:
    """Что пора отправить прямо сейчас: (бронь, узел, текст)."""
    now = now or datetime.utcnow()
    rows = (
        session.query(Booking)
        .filter(Booking.status.in_(ACTIVE))
        .filter(Booking.check_out >= now - timedelta(days=1))
        .all()
    )

    plan: list[tuple[Booking, str, str]] = []
    for booking in rows:
        if not _can_write(booking):
            continue

        if (not booking.pre_arrival_notified
                and now >= booking.check_in - BEFORE_ARRIVAL
                and now < booking.check_in):
            plan.append((booking, "pre_arrival", pre_arrival_text(booking)))
            continue

        if (not booking.settled_asked
                and booking.check_in <= now - AFTER_ARRIVAL
                and now < booking.check_out):
            plan.append((booking, "settled", settled_text(booking)))
            continue

        if (not booking.pre_departure_notified
                and now >= booking.check_out - BEFORE_DEPARTURE
                and now < booking.check_out):
            plan.append((booking, "pre_departure", pre_departure_text(booking)))

    return plan


def send_due(session: Session, *, now: datetime | None = None) -> int:
    """Отправить всё, чему пришло время. Возвращает число отправленных."""
    sent = 0
    for booking, node, text in due(session, now=now):
        try:
            deliver_to_guest(booking, text)
        except Exception:  # noqa: BLE001 — одно непрошедшее сообщение не ломает цикл
            log.exception("не отправила гостю %s", node)
            continue

        if node == "pre_arrival":
            booking.pre_arrival_notified = True
            booking.awaiting = "eta_in"
        elif node == "settled":
            booking.settled_asked = True
        elif node == "pre_departure":
            booking.pre_departure_notified = True
            booking.awaiting = "eta_out"
        sent += 1
        log.info("гостю отправлено «%s» по брони %s", node, booking.id)
    return sent


def capture_eta(session: Session, booking: Booking, text: str) -> str:
    """Ответ гостя на вопрос о времени → в бронь. Возвращает, что поняли.

    Разбираем только тогда, когда сами спросили: иначе «буду в 10» в любом
    другом разговоре молча перепишет время заезда.
    """
    from pomogaika.domain.dates import parse_time_hint

    field = booking.awaiting
    if field not in {"eta_in", "eta_out"}:
        return ""
    hint = parse_time_hint(text)
    if hint is None:
        return ""

    hour, minute = hint
    base = booking.check_in if field == "eta_in" else booking.check_out
    when = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    setattr(booking, field, when)
    booking.awaiting = ""
    session.flush()

    word = "приедет" if field == "eta_in" else "съедет"
    notify_owner(f"{booking.guest_name or 'Гость'} {word} около {when:%d.%m %H:%M}.")
    log.info("бронь %s: %s = %s", booking.id, field, when)
    return f"{when:%H:%M}"
