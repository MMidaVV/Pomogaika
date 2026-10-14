"""Учёт регистрации гостей в МВД.

Подачу делает внешний сервис или человек — у нас остаётся след: кто, когда, в каком
статусе. Номера документов и сканы здесь не хранятся намеренно: это персональные
данные, которые нам нечем защищать, а для следа они не нужны.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from pomogaika.models import Booking, GuestRegistration

STATUS = {
    "not_required": "не требуется",
    "to_do": "нужно подать",
    "submitted": "подано",
    "confirmed": "подтверждено",
}


def for_booking(session: Session, booking_id: int) -> list[GuestRegistration]:
    return (
        session.query(GuestRegistration)
        .filter_by(booking_id=booking_id)
        .order_by(GuestRegistration.created_at)
        .all()
    )


def add(session: Session, booking_id: int, full_name: str, birth_date: str = "",
        citizenship: str = "РФ", status: str = "to_do", note: str = "") -> GuestRegistration:
    row = GuestRegistration(
        booking_id=booking_id,
        full_name=full_name.strip()[:200],
        birth_date=birth_date.strip()[:20],
        citizenship=(citizenship or "РФ").strip()[:80],
        status=status if status in STATUS else "to_do",
        note=note.strip()[:2000],
    )
    session.add(row)
    session.flush()
    return row


def set_status(session: Session, row_id: int, status: str, by: int | None = None) -> bool:
    row = session.get(GuestRegistration, row_id)
    if row is None or status not in STATUS:
        return False
    row.status = status
    if status in ("submitted", "confirmed"):
        row.submitted_at = datetime.utcnow()
        row.submitted_by = by
    session.flush()
    return True


def remove(session: Session, row_id: int) -> bool:
    row = session.get(GuestRegistration, row_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def as_dict(row: GuestRegistration) -> dict:
    return {
        "id": row.id,
        "booking_id": row.booking_id,
        "full_name": row.full_name,
        "birth_date": row.birth_date,
        "citizenship": row.citizenship,
        "status": row.status,
        "status_title": STATUS.get(row.status, row.status),
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
        "note": row.note,
    }


def overview(session: Session, limit: int = 60, days_back: int = 14) -> dict:
    """Кого заселили и что с регистрацией: для страницы «Регистрация гостей в МВД».

    Показываем окно вокруг сегодня: ближайшие заезды первыми плюс последние две недели.
    Давние брони не нужны — по ним всё либо подано, либо уже неактуально.
    """
    window_start = datetime.utcnow() - timedelta(days=days_back)
    bookings = (
        session.query(Booking)
        .filter(Booking.status != "cancelled")
        .filter(Booking.check_out >= window_start)
        .order_by(Booking.check_in)
        .limit(limit)
        .all()
    )
    rows = []
    need = 0
    for booking in bookings:
        guests = for_booking(session, booking.id)
        pending = [g for g in guests if g.status == "to_do"]
        if pending or not guests:
            need += 1
        rows.append({
            "booking_id": booking.id,
            "guest_name": booking.guest_name,
            "check_in": booking.check_in.isoformat(),
            "check_out": booking.check_out.isoformat(),
            "guests_count": booking.guests_count,
            "platform": booking.platform,
            "registrations": [as_dict(g) for g in guests],
            "pending": len(pending),
        })
    return {"bookings": rows, "need_attention": need, "statuses": STATUS}
