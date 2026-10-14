"""Итог проживания: отзыв, чек, залог, конфликт.

Заполняет админ — руками, после выезда. Ничего не выводится автоматически:
здесь факты, которые знает только человек.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from pomogaika.models import Booking, StayOutcome

REVIEW = {"none": "не спрашивали", "asked": "попросили", "received": "получен", "refused": "отказался"}
RECEIPT = {"none": "не оформлен", "issued": "оформлен", "not_needed": "не нужен"}
DEPOSIT = {"none": "не брали", "held": "у нас", "returned": "возвращён", "withheld": "удержан"}
CONFLICT = {"none": "без конфликта", "guest": "со стороны гостя", "client": "со стороны заказчика", "both": "с обеих сторон"}

FIELDS = {
    "review_status": REVIEW,
    "receipt_status": RECEIPT,
    "deposit_status": DEPOSIT,
    "conflict_side": CONFLICT,
}
TEXT_FIELDS = ("review_note", "receipt_note", "deposit_reason", "conflict_note", "conflict_resolution")
MONEY_FIELDS = ("deposit_amount_cents", "deposit_returned_cents")


def get_or_create(session: Session, booking_id: int) -> StayOutcome:
    outcome = session.query(StayOutcome).filter_by(booking_id=booking_id).one_or_none()
    if outcome is None:
        outcome = StayOutcome(booking_id=booking_id)
        session.add(outcome)
        session.flush()
    return outcome


def save(session: Session, booking_id: int, data: dict, by: int | None = None) -> StayOutcome:
    """Обновить только то, что пришло. Неизвестные значения игнорируем, а не падаем."""
    outcome = get_or_create(session, booking_id)
    for field, allowed in FIELDS.items():
        value = data.get(field)
        if value in allowed:
            setattr(outcome, field, value)
    for field in TEXT_FIELDS:
        if field in data:
            setattr(outcome, field, str(data[field] or "")[:2000])
    for field in MONEY_FIELDS:
        if field in data:
            try:
                setattr(outcome, field, max(0, int(data[field] or 0)))
            except (TypeError, ValueError):
                pass
    outcome.updated_by = by
    outcome.updated_at = datetime.utcnow()
    session.flush()
    return outcome


def is_filled(outcome: StayOutcome | None) -> bool:
    """Итог считается закрытым, когда отвечено про отзыв, чек, залог и конфликт."""
    if outcome is None:
        return False
    return all([
        outcome.review_status != "none",
        outcome.receipt_status != "none",
        outcome.deposit_status != "none",
        outcome.conflict_side != "none" or bool(outcome.conflict_note),
    ])


def as_dict(outcome: StayOutcome | None) -> dict:
    if outcome is None:
        return {
            "review_status": "none", "review_note": "",
            "receipt_status": "none", "receipt_note": "",
            "deposit_status": "none", "deposit_amount_cents": 0,
            "deposit_returned_cents": 0, "deposit_reason": "",
            "conflict_side": "none", "conflict_note": "", "conflict_resolution": "",
            "filled": False,
        }
    return {
        "review_status": outcome.review_status,
        "review_note": outcome.review_note,
        "receipt_status": outcome.receipt_status,
        "receipt_note": outcome.receipt_note,
        "deposit_status": outcome.deposit_status,
        "deposit_amount_cents": outcome.deposit_amount_cents,
        "deposit_returned_cents": outcome.deposit_returned_cents,
        "deposit_reason": outcome.deposit_reason,
        "conflict_side": outcome.conflict_side,
        "conflict_note": outcome.conflict_note,
        "conflict_resolution": outcome.conflict_resolution,
        "filled": is_filled(outcome),
        "updated_at": outcome.updated_at.isoformat() if outcome.updated_at else None,
    }


def summary(outcome: StayOutcome | None) -> str:
    """Короткая строка для списка и для бота."""
    if outcome is None:
        return "итог не заполнен"
    parts = [f"отзыв: {REVIEW[outcome.review_status]}", f"чек: {RECEIPT[outcome.receipt_status]}"]
    if outcome.deposit_status != "none":
        money = outcome.deposit_amount_cents // 100
        parts.append(f"залог {money} ₽: {DEPOSIT[outcome.deposit_status]}")
    if outcome.conflict_side != "none":
        parts.append(f"конфликт {CONFLICT[outcome.conflict_side]}")
    return " · ".join(parts)


def pending(session: Session, hours: int = 24) -> list[Booking]:
    """Выехавшие больше N часов назад, у кого итог ещё не закрыт и напоминание не ушло."""
    now = datetime.utcnow()
    bookings = (
        session.query(Booking)
        .filter(Booking.checked_out_at.isnot(None))
        .order_by(Booking.checked_out_at.desc())
        .limit(200)
        .all()
    )
    out = []
    for booking in bookings:
        if (now - booking.checked_out_at).total_seconds() < hours * 3600:
            continue
        outcome = session.query(StayOutcome).filter_by(booking_id=booking.id).one_or_none()
        if is_filled(outcome) or (outcome and outcome.reminded_at):
            continue
        out.append(booking)
    return out
