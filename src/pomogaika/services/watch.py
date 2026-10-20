"""Фон: календари площадок, заезды и выезды, окно кода, молчание клинера, итог проживания."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from datetime import datetime, timedelta

from pomogaika.config import settings
from pomogaika.platforms.mailbox import configured as mail_configured
from pomogaika.models import Booking, Property
from pomogaika.services.cleaning import escalate_stale
from pomogaika.services.outcome import get_or_create, pending as pending_outcomes
from pomogaika.services.mail import poll as poll_mail
from pomogaika.services.sync import sync_listings
from pomogaika.services.codes import bookings_in_code_window, door_decision
from pomogaika.services.notify import notify_owner
from pomogaika.services.nudges import send_due

log = logging.getLogger("pomogaika.watch")


_last_sync: datetime | None = None


def _sync_calendars(session: Session) -> None:
    """Сами тянем iCal, чтобы шахматка не показывала позавчерашнюю картину."""
    global _last_sync
    minutes = int(settings.ical_sync_minutes or 0)
    if minutes <= 0:
        return
    now = datetime.utcnow()
    if _last_sync and now - _last_sync < timedelta(minutes=minutes):
        return
    _last_sync = now
    report = sync_listings(session, announce="changes")
    created = sum(r.get("created", 0) for r in report if r.get("ok"))
    failed = [r["platform"] for r in report if not r.get("ok")]
    if created or failed:
        log.info("календари: новых броней %s, не ответили: %s", created, failed or "нет")


ACTIVE_STATES = ("confirmed", "checkin", "checkout")


def _stay_notices(session: Session) -> None:
    """Сказать собственнику, что гость заехал и что выехал.

    Момент берём по расписанию брони: живого замка пока нет, и это честнее,
    чем молчать до ручной отметки. Каждое событие — один раз на бронь.
    """
    now = datetime.utcnow()
    bookings = (
        session.query(Booking)
        .filter(Booking.status.in_(ACTIVE_STATES))
        .filter(Booking.check_out >= now - timedelta(days=2))
        .all()
    )
    for booking in bookings:
        if not booking.arrival_notified and booking.check_in <= now:
            booking.arrival_notified = True
            notify_owner(
                f"Заезд: {booking.guest_name}.\n"
                f"{booking.check_in:%d.%m %H:%M} — {booking.check_out:%d.%m %H:%M}"
                + (f", код выдан ({booking.code_hint})" if booking.code_hint else ", код ещё не выдавали")
            )
        if not booking.departure_notified and booking.check_out <= now:
            booking.departure_notified = True
            notify_owner(
                f"Выезд: {booking.guest_name}, {booking.check_out:%d.%m %H:%M}.\n"
                "Поставить задачу клинеру и закрыть проживание?",
                [[{"text": "Выезд и уборка", "callback_data": f"out:{booking.id}"}]],
            )


_last_mail = None


def _read_mail(session: Session) -> None:
    """Письма площадок дают то, чего нет в iCal: имя гостя, сумму, номер брони."""
    global _last_mail
    minutes = int(settings.imap_poll_minutes or 0)
    if minutes <= 0 or not mail_configured():
        return
    now = datetime.utcnow()
    if _last_mail and now - _last_mail < timedelta(minutes=minutes):
        return
    _last_mail = now
    try:
        report = poll_mail(session)
    except Exception:
        log.exception("почта недоступна")
        return
    if report.get("messages"):
        # про каждое сообщение гостя собственник уже получил отдельное письмо-карточку
        log.info("почта: сообщений гостей %s", report["messages"])
    if report["created"] or report["updated"]:
        log.info("почта: новых броней %s, обновлено %s", report["created"], report["updated"])
        if report["created"]:
            notify_owner(
                f"Из писем площадок: новых броней {report['created']}"
                + (f", обновлено {report['updated']}" if report["updated"] else "")
                + "."
                + "\n" + "Имя гостя и сумма теперь в карточке."
            )


def tick(session: Session) -> None:
    _sync_calendars(session)
    send_due(session)          # сообщения гостю в узлах пути
    _read_mail(session)
    _stay_notices(session)
    escalate_stale(session)
    _code_windows(session)
    _outcome_reminders(session)


def _outcome_reminders(session: Session) -> None:
    """Через сутки после выезда напомнить про итог: отзыв, чек, залог, конфликт.

    Напоминаем один раз на бронь — отметка в самой записи итога.
    """
    for booking in pending_outcomes(session, hours=24):
        outcome = get_or_create(session, booking.id)
        if outcome.reminded_at:
            continue
        outcome.reminded_at = datetime.utcnow()
        notify_owner(
            f"Вчера выехал {booking.guest_name}. Закройте итог проживания:\n"
            "отзыв, чек, залог, был ли конфликт.\n"
            "Мини-приложение, карточка брони, кнопка «Итог проживания».",
        )


def _code_windows(session: Session) -> None:
    prop = session.query(Property).first()
    if not prop:
        return
    for booking in bookings_in_code_window(session, prop.id):
        if booking.code_issued_at or booking.code_window_notified:
            continue
        decision = door_decision(booking)
        if not decision.allowed:
            continue
        booking.code_window_notified = True
        notify_owner(
            f"Можно выдать код: {booking.guest_name}, бронь {booking.check_in:%d.%m %H:%M}.\n"
            f"{decision.reason}\nЦифры в журнал не пишу — пришлите их после кнопки.",
            [[{"text": "Выдать код", "callback_data": f"code:{booking.id}"}]],
        )
