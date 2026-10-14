"""Ссылка гостю в личку собственницы. В адресе нет номера брони — только код."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote

from sqlalchemy import func
from sqlalchemy.orm import Session

from pomogaika.config import settings
from pomogaika.models import Booking, GuestLink, Property

# Гость видит код в тексте. Префикс — код объекта, хвост короткий и случайный.
_CODE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{1,15}-[0-9a-fA-F]{4})\b")
_PREFILL = "Здравствуйте! Бронь {code}"
GUEST_PITCH = (
    "Код от двери придёт сюда за 2 часа до заезда. "
    "Напишите нам здесь, если что-то понадобится."
)


def issue_link(session: Session, booking: Booking) -> str:
    """Действующая ссылка на эту бронь. Повторный вызов не создаёт новый код."""
    username = _username()
    if not username:
        raise ValueError("В настройках нет username собственницы в Telegram")
    row = active_link(session, booking.id)
    if row is None:
        # срок всегда «выезд плюс сутки», поэтому повтор после этой даты
        # не должен плодить новые коды с тем же прошедшим сроком
        row = (
            session.query(GuestLink)
            .filter(GuestLink.booking_id == booking.id, GuestLink.revoked.is_(False))
            .order_by(GuestLink.created_at.desc())
            .first()
        )
    if row is None:
        prop = session.get(Property, booking.property_id)
        prefix = _prefix(prop.code if prop else "")
        row = GuestLink(
            id=_new_code(session, prefix),
            booking_id=booking.id,
            property_id=booking.property_id,
            expires_at=booking.check_out + timedelta(days=1),
        )
        session.add(row)
        session.flush()
    return _url(username, row.id)


def resolve_code(session: Session, text: str, user) -> Booking | None:
    """Найти код в тексте. Просроченный, отозванный и чужой аккаунт — как будто кода нет."""
    uid = _uid(user)
    if not uid:
        return None
    now = datetime.now()
    for token in _CODE.findall(text or ""):
        row = _by_code(session, token)
        if row is None or row.revoked or row.expires_at <= now:
            continue
        if row.guest_user_id and row.guest_user_id != uid:
            continue
        booking = session.get(Booking, row.booking_id)
        if booking is None or booking.status == "cancelled":
            continue
        if row.guest_user_id is None:
            row.guest_user_id = uid
            row.used_at = now
            session.flush()
        return booking
    return None


def revoke(session: Session, code: str) -> bool:
    row = _by_code(session, code)
    if row is None or row.revoked:
        return False
    row.revoked = True
    session.flush()
    return True


def active_link(session: Session, booking_id: int, *, now: datetime | None = None) -> GuestLink | None:
    now = now or datetime.now()
    return (
        session.query(GuestLink)
        .filter(
            GuestLink.booking_id == booking_id,
            GuestLink.revoked.is_(False),
            GuestLink.expires_at > now,
        )
        .order_by(GuestLink.created_at.desc())
        .first()
    )


def _username() -> str:
    return (settings.telegram_owner_username or "").strip().lstrip("@")


def _prefix(code: str) -> str:
    raw = re.sub(r"[^A-Za-z0-9]", "", code or "")
    return (raw or "KV")[:8].upper()


def _new_code(session: Session, prefix: str) -> str:
    for _ in range(8):
        code = f"{prefix}-{secrets.token_hex(2)}"
        if session.get(GuestLink, code) is None:
            return code
    raise RuntimeError("не удалось выдать код ссылки")


def _url(username: str, code: str) -> str:
    text = quote(_PREFILL.format(code=code), safe="")
    return f"https://t.me/{username}?text={text}"


def _by_code(session: Session, code: str) -> GuestLink | None:
    found = session.get(GuestLink, code)
    if found:
        return found
    return session.query(GuestLink).filter(func.lower(GuestLink.id) == code.lower()).one_or_none()


def _uid(user) -> int | None:
    try:
        return int(user)
    except (TypeError, ValueError):
        return None
