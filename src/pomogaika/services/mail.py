"""Письма площадок → брони с именем гостя, суммой и номером, и чат с гостем.

iCal отдаёт только занятые даты. Письмо о новой брони содержит остальное,
и парсер писем у нас уже есть — здесь мы просто сводим одно с другим.

Второй вид писем — сообщение гостя из чата площадки. Такое письмо даёт
Яндекс: в нём текст гостя и адрес робота для ответа. Оно не бронь, поэтому
уходит в черновик ответа, а не в календарь.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from pomogaika.config import settings
from pomogaika.models import Booking, MessageDraft, Property
from pomogaika.platforms.email_parser import parse_platform_email
from pomogaika.platforms.guest_mail import GuestMail
from pomogaika.platforms.guest_mail import parse as parse_guest_mail
from pomogaika.platforms.letters import parse as parse_letter
from pomogaika.platforms.mailbox import fetch_unseen, mark_seen
from pomogaika.services.bookings import ingest_booking
from pomogaika.services.drafts import create_guest_draft
from pomogaika.services.notify import notify_owner
from pomogaika.services.outbox import can_send_mail, send_mail_draft

log = logging.getLogger("pomogaika.mail")
NEWLINE = chr(10)


def _too_old(letter) -> bool:
    """Письмо старше окна — история, а не новый вопрос гостя.

    Дату письмо могло и не донести: тогда считаем свежим, иначе из-за кривого
    заголовка молча потеряем настоящее сообщение.
    """
    hours = int(settings.guest_mail_max_age_hours or 0)
    if hours <= 0 or letter.sent_at is None:
        return False
    return datetime.utcnow() - letter.sent_at > timedelta(hours=hours)


def handle_guest_mail(session: Session, prop: Property, guest: GuestMail) -> MessageDraft:
    """Сообщение гостя из письма площадки → черновик ответа.

    Автоответ держим на предохранителе `GUEST_MAIL_AUTOSEND`: это живая
    переписка с гостем, и включать её должен человек, а не выкатка кода.
    """
    booking = None
    if guest.external_id:
        booking = (
            session.query(Booking)
            .filter(
                Booking.property_id == prop.id,
                Booking.platform == guest.platform,
                Booking.external_id == guest.external_id,
            )
            .first()
        )

    draft, _ = create_guest_draft(
        session,
        guest.text,
        channel=f"mail_{guest.platform}",
        booking_id=booking.id if booking else None,
    )
    draft.reply_to = guest.reply_to
    draft.mail_subject = guest.subject
    draft.mail_message_id = guest.message_id
    draft.mail_quote = guest.quote
    session.flush()

    sent = False
    if draft.status == "auto_sent":
        if settings.guest_mail_autosend and can_send_mail(draft):
            try:
                send_mail_draft(draft, draft.draft_text)
                draft.status = "sent"
                sent = True
            except Exception:
                log.exception("ответ гостю письмом не ушёл, остаётся человеку")
                draft.status = "pending_owner"
        else:
            draft.status = "pending_owner"   # предохранитель или нет SMTP

    where = "отправлен" if sent else "ждёт вас"
    notify_owner(
        f"Гость написал в чат {guest.platform} (письмо)\n\n"
        f"«{guest.text}»\n\n"
        f"Бронь: {booking.external_id if booking else 'не найдена'}\n"
        f"Ответ {where}:\n{draft.draft_text}"
    )
    log.info("сообщение гостя из письма: бронь %s, черновик %s, %s",
             guest.external_id or "—", draft.id, where)
    session.flush()
    return draft


def poll(session: Session, limit: int = 20, *, unseen_only: bool = True) -> dict:
    """Разобрать письма площадок. Возвращает сводку для лога и бота.

    unseen_only=False — разбор истории при первом подключении ящика.
    """
    prop = session.query(Property).first()
    if prop is None:
        return {"letters": 0, "created": 0, "updated": 0, "skipped": 0, "messages": 0, "stale": 0}

    letters = fetch_unseen(limit=limit, unseen_only=unseen_only)
    created = updated = skipped = messages = stale = 0
    handled: list[str] = []

    for letter in letters:
        text = letter.subject + NEWLINE * 2 + letter.body

        # Сообщение гостя из чата площадки — это не бронь, и разбирать его
        # как бронь нельзя: в письме есть и «YA-…», и даты, но заезда в нём нет.
        guest = parse_guest_mail(
            letter.subject,
            letter.body,
            reply_to=letter.reply_to,
            message_id=letter.message_id,
        )
        if guest is not None:
            if not settings.guest_mail_enabled:
                skipped += 1
                continue                  # рубильник выключен — письмо не трогаем
            if _too_old(letter):
                stale += 1
                handled.append(letter.uid)  # прочитано, чтобы не возвращалось каждые 5 минут
                log.info("сообщение гостя от %s старше %s ч — пропускаем",
                         letter.sent_at, settings.guest_mail_max_age_hours)
                continue
            try:
                handle_guest_mail(session, prop, guest)
                messages += 1
                handled.append(letter.uid)
            except Exception:
                log.exception("сообщение гостя не разобрано, письмо оставляем непрочитанным")
                skipped += 1
            continue

        # сначала построчный разбор под настоящую вёрстку писем, потом старый общий
        parsed = parse_letter(text) or parse_platform_email(text)
        if parsed is None or not parsed.check_in or not parsed.check_out:
            skipped += 1
            continue                      # не разобрали — оставляем непрочитанным

        # у построчного разбора нет поля status — вид письма лежит в kind
        kind = getattr(parsed, "kind", "created")
        status = getattr(parsed, "status", None) or ("cancelled" if kind == "cancelled" else "confirmed")

        result = ingest_booking(
            session,
            property_id=prop.id,
            platform=parsed.platform,
            external_id=parsed.external_id or f"mail-{letter.uid}",
            # пустое имя не передаём: письмо-напоминание не должно стирать имя из первого письма
            guest_name=parsed.guest_name,
            check_in=parsed.check_in,
            check_out=parsed.check_out,
            guests_count=parsed.guests_count or 0,
            amount_cents=parsed.amount_cents,
            paid=parsed.paid,
            status=status,
            source="email",
        )
        created += 1 if result.created else 0
        updated += 0 if result.created else 1
        handled.append(letter.uid)

    if handled:
        session.flush()
        mark_seen(handled)
    return {
        "letters": len(letters),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "messages": messages,
        "stale": stale,
    }
