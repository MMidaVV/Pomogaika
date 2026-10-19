"""Отчёт по туристическому налогу.

Считаем по ночам и суммам броней за месяц. Ставка и минимум за сутки берутся из
настроек, потому что закон менялся и будет меняться: мы даём арифметику и выгрузку,
а не юридическую позицию. Применим ли налог к конкретному объекту — решает собственник
со своим бухгалтером.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from pomogaika.config import settings
from pomogaika.models import Booking


def _nights_in_month(booking: Booking, first: date, after_last: date) -> int:
    """Ночи этой брони, попавшие в месяц. Ночь привязана к дате, когда гость лёг спать.

    Граница месяца — исключающая: ночь с 30 июня на 1 июля считается июньской.
    """
    start = max(booking.check_in.date(), first)
    end = min(booking.check_out.date(), after_last)
    return max(0, (end - start).days)


def report(session: Session, year: int, month: int) -> dict:
    days = monthrange(year, month)[1]
    first = date(year, month, 1)
    after_last = date(year, month, days) + timedelta(days=1)

    bookings = (
        session.query(Booking)
        .filter(Booking.status != "cancelled")
        .filter(Booking.check_out >= datetime(year, month, 1))
        .filter(Booking.check_in <= datetime(year, month, days, 23, 59))
        .order_by(Booking.check_in)
        .all()
    )

    percent = float(settings.tourist_tax_percent)
    min_per_night = int(settings.tourist_tax_min_rub_per_night)

    rows = []
    total_nights = 0
    total_base = 0
    total_tax = 0
    for booking in bookings:
        nights = _nights_in_month(booking, first, after_last)
        if nights <= 0:
            continue
        stay_nights = max(1, (booking.check_out.date() - booking.check_in.date()).days)
        # доля суммы, приходящаяся на ночи внутри месяца
        base = round(booking.amount_cents / 100 * nights / stay_nights)
        by_percent = base * percent / 100
        by_minimum = min_per_night * nights
        tax = round(max(by_percent, by_minimum))

        total_nights += nights
        total_base += base
        total_tax += tax
        rows.append({
            "booking_id": booking.id,
            "guest_name": booking.guest_name,
            "platform": booking.platform,
            "check_in": booking.check_in.isoformat(),
            "check_out": booking.check_out.isoformat(),
            "nights_in_month": nights,
            "base_rub": base,
            "tax_rub": tax,
            "by_minimum": by_minimum > by_percent,
        })

    return {
        "year": year,
        "month": month,
        "percent": percent,
        "min_rub_per_night": min_per_night,
        "rows": rows,
        "totals": {
            "bookings": len(rows),
            "nights": total_nights,
            "base_rub": total_base,
            "tax_rub": total_tax,
        },
    }
