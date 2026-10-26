"""Гость → бронь: telegram id, даты в тексте, имя, «сейчас живёт»."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from pomogaika.domain.dates import parse_guest_dates
from pomogaika.models import Booking
from pomogaika.services.bookings import ACTIVE


def match_booking(
    session: Session,
    property_id: int,
    *,
    telegram_user_id: str = "",
    text: str = "",
    booking_id: int | None = None,
    now: datetime | None = None,
) -> Booking | None:
    now = now or datetime.now()
    if booking_id:
        found = session.get(Booking, booking_id)
        if found and found.property_id == property_id and found.status != "cancelled":
            return found

    rows = (
        session.query(Booking)
        .filter(Booking.property_id == property_id, Booking.status.in_(ACTIVE))
        .order_by(Booking.check_in)
        .all()
    )
    if not rows:
        return None

    if telegram_user_id:
        uid = _as_int(telegram_user_id)
        linked = [b for b in rows if b.guest_telegram_user_id and b.guest_telegram_user_id == uid]
        if linked:
            return _pick_relevant(linked, now)

    dated = parse_guest_dates(text, now=now)
    if dated:
        start, end = dated
        by_dates = [
            b
            for b in rows
            if abs((b.check_in.date() - start.date()).days) <= 1
            or (b.check_in.date() <= start.date() <= b.check_out.date())
            or (start.date() <= b.check_in.date() <= end.date())
        ]
        if len(by_dates) == 1:
            return by_dates[0]
        if by_dates:
            return _pick_relevant(by_dates, now)

    named = _by_name(rows, text)
    if named:
        return named

    staying = [b for b in rows if b.status == "staying" or (b.check_in <= now < b.check_out)]
    if len(staying) == 1:
        return staying[0]

    upcoming = [b for b in rows if now <= b.check_in <= now + timedelta(days=3)]
    if len(upcoming) == 1:
        return upcoming[0]
    return None


def bind_guest(
    booking: Booking,
    telegram_user_id: str = "",
    guest_chat_id: str = "",
    business_connection_id: str = "",
) -> None:
    uid = _as_int(telegram_user_id)
    if uid:
        booking.guest_telegram_user_id = uid
    if guest_chat_id:
        booking.guest_chat_id = guest_chat_id
    if business_connection_id:
        booking.business_connection_id = business_connection_id
    if uid or guest_chat_id:
        booking.guest_channel = "telegram"


def bind_whatsapp(booking: Booking, chat_id: str) -> None:
    """Чат WhatsApp отдельно от Telegram, чтобы ответ не ушёл в другой мессенджер."""
    chat = (chat_id or "").strip()
    if not chat:
        return
    booking.guest_whatsapp_chat_id = chat[:200]
    booking.guest_channel = "whatsapp"


def upcoming_for_link(session: Session, property_id: int, *, now: datetime | None = None) -> list[Booking]:
    now = now or datetime.now()
    rows = (
        session.query(Booking)
        .filter(Booking.property_id == property_id, Booking.status.in_(ACTIVE))
        .order_by(Booking.check_in)
        .all()
    )
    return [b for b in rows if b.check_out >= now][:6]


def _pick_relevant(rows: list[Booking], now: datetime) -> Booking:
    current = [b for b in rows if b.check_in <= now < b.check_out]
    if current:
        return current[0]
    future = [b for b in rows if b.check_in >= now]
    if future:
        return future[0]
    return rows[-1]


def _by_name(rows: list[Booking], text: str) -> Booking | None:
    low = text.lower()
    hits = []
    for booking in rows:
        name = (booking.guest_name or "").strip()
        if len(name) >= 2 and name.lower() in low:
            hits.append(booking)
    if len(hits) == 1:
        return hits[0]
    return None


def _as_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
