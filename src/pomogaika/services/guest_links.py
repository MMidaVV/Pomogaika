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


# Первое сообщение гостя по нашей же ссылке — это приветствие с кодом, а не
# вопрос. Прогонять его через копайлот нельзя: модель видит обрывок текста
# с непонятным кодом и отвечает «сообщение выглядит странно, не сообщайте
# никому данные брони». Для гостя, который только что перешёл по ссылке,
# это худшее из возможных первых впечатлений.
GREETING_WORDS = (
    "здравствуйте", "здравствуй", "добрый день", "доброе утро", "добрый вечер",
    "доброй ночи", "привет", "приветствую", "день добрый", "хай", "hello", "hi",
)
CODE_IN_TEXT = re.compile(r"[A-Za-zА-Яа-я]{2,10}-[0-9a-fA-F]{3,8}")


def is_greeting_only(text: str) -> bool:
    """В сообщении нет ничего, кроме приветствия и кода брони."""
    rest = CODE_IN_TEXT.sub(" ", text or "").lower()
    rest = rest.replace("бронь", " ").replace("бронирование", " ")
    for word in GREETING_WORDS:
        rest = rest.replace(word, " ")
    rest = re.sub(r"[^\w]+", " ", rest, flags=re.UNICODE).strip()
    return not rest


def welcome_text(booking: Booking) -> str:
    """Тёплый ответ на переход по ссылке: кто мы, что знаем, что будет дальше.

    Ни кодов, ни сумм, ни адреса: это приветствие, а не выдача доступа.
    """
    name = (booking.guest_name or "").strip()
    hello = f"{name}, здравствуйте!" if name else "Здравствуйте!"
    return (
        f"{hello} Вижу вашу бронь "
        f"{booking.check_in:%d.%m} {booking.check_in:%H:%M} → "
        f"{booking.check_out:%d.%m} {booking.check_out:%H:%M}.\n\n"
        "Код от двери и инструкцию по заезду пришлю сюда за 2 часа до заезда. "
        "Если что-то понадобится раньше — просто напишите в этот чат."
    )
