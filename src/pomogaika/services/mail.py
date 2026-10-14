"""Письма площадок → брони с именем гостя, суммой и номером.

iCal отдаёт только занятые даты. Письмо о новой брони содержит остальное,
и парсер писем у нас уже есть — здесь мы просто сводим одно с другим.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from pomogaika.models import Property
from pomogaika.platforms.email_parser import parse_platform_email
from pomogaika.platforms.letters import parse as parse_letter
from pomogaika.platforms.mailbox import fetch_unseen, mark_seen
from pomogaika.services.bookings import ingest_booking

log = logging.getLogger("pomogaika.mail")
NEWLINE = chr(10)


def poll(session: Session, limit: int = 20, *, unseen_only: bool = True) -> dict:
    """Разобрать письма площадок. Возвращает сводку для лога и бота.

    unseen_only=False — разбор истории при первом подключении ящика.
    """
    prop = session.query(Property).first()
    if prop is None:
        return {"letters": 0, "created": 0, "updated": 0, "skipped": 0}

    letters = fetch_unseen(limit=limit, unseen_only=unseen_only)
    created = updated = skipped = 0
    handled: list[str] = []

    for letter in letters:
        text = letter.subject + NEWLINE * 2 + letter.body
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
            guest_phone=getattr(parsed, "phone", "") or "",
        )
        created += 1 if result.created else 0
        updated += 0 if result.created else 1
        handled.append(letter.uid)

    if handled:
        session.flush()
        mark_seen(handled)
    return {"letters": len(letters), "created": created, "updated": updated, "skipped": skipped}
