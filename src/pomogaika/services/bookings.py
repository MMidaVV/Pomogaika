"""Ингест броней, блоки дат, конфликты. Отмену на площадке делает человек."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from pomogaika.domain.calendar import (
    cheaper_to_cancel,
    is_block_label,
    is_busy_label,
    is_occupancy_mirror,
    overlaps,
)
from pomogaika.domain.dates import fmt_span
from pomogaika.domain.policy import check
from pomogaika.models import Block, Booking, BookingConflict, Listing, Property
from pomogaika.platforms.ical import parse_ics
from pomogaika.services.seed import audit, emit

ACTIVE = {"pending", "confirmed", "paid", "staying"}
DEMO_EXTERNAL_PREFIXES = ("AV-demo", "YA-demo", "ical-demo", "ical-overlap", "ical-live")
DEMO_EXTERNAL_IDS = {"42919999", "42910245"}
PLATFORMS = {
    "avito": "Авито",
    "sutochno": "Суточно",
    "yandex": "Яндекс",
    "ostrovok": "Островок",
    "ical": "iCal",
    "block": "блок",
}
PLATFORM_ALIASES = {
    "avito": "avito",
    "авито": "avito",
    "sutochno": "sutochno",
    "суточно": "sutochno",
    "ostrovok": "ostrovok",
    "островок": "ostrovok",
    "yandex": "yandex",
    "яндекс": "yandex",
}


@dataclass
class IngestResult:
    booking: Booking
    created: bool
    conflicts: list[BookingConflict]
    closed_block: Block | None


def _active_bookings(session: Session, property_id: int, *, exclude_id: int | None = None) -> list[Booking]:
    q = session.query(Booking).filter(Booking.property_id == property_id, Booking.status.in_(ACTIVE))
    if exclude_id:
        q = q.filter(Booking.id != exclude_id)
    return q.all()


def find_overlaps(session: Session, booking: Booking) -> list[Booking]:
    """Настоящие пересечения. Критерий один и тот же с bookings_conflict:
    раньше поиск и проверка расходились, и конфликты закрывались, а потом
    тут же создавались заново.
    """
    return [
        other
        for other in _active_bookings(session, booking.property_id, exclude_id=booking.id)
        if bookings_conflict(booking, other)
    ]


def find_block_overlaps(session: Session, booking: Booking) -> list[Block]:
    blocks = session.query(Block).filter(Block.property_id == booking.property_id).all()
    return [block for block in blocks if overlaps(booking.check_in, booking.check_out, block.start, block.end)]


def _existing(session: Session, property_id: int, platform: str, external_id: str) -> Booking | None:
    if not external_id:
        return None
    return (
        session.query(Booking)
        .filter_by(property_id=property_id, platform=platform, external_id=external_id)
        .first()
    )


def ingest_booking(
    session: Session,
    *,
    property_id: int,
    platform: str,
    external_id: str = "",
    guest_name: str = "Гость",
    check_in: datetime,
    check_out: datetime,
    guests_count: int = 2,
    amount_cents: int = 0,
    paid: bool = False,
    status: str = "confirmed",
    source: str = "email",
    listing_id: int | None = None,
) -> IngestResult:
    if status == "cancelled":
        found = _existing(session, property_id, platform, external_id)
        if found:
            found.status = "cancelled"
            emit(session, "booking.cancelled", {"id": found.id, "platform": platform}, property_id)
            audit(session, "booking.cancelled", "Отмена с площадки, даты можно открыть", source, "auto", "auto")
            _close_conflicts_for(session, found.id)
            return IngestResult(found, False, [], None)
        dummy = Booking(
            property_id=property_id,
            platform=platform,
            external_id=external_id,
            guest_name=guest_name,
            check_in=check_in,
            check_out=check_out,
            status="cancelled",
            source=source,
        )
        session.add(dummy)
        session.flush()
        return IngestResult(dummy, True, [], None)

    found = _existing(session, property_id, platform, external_id)
    created = found is None
    booking = found or Booking(property_id=property_id)
    booking.listing_id = listing_id
    booking.platform = platform
    booking.external_id = external_id
    booking.guest_name = guest_name or booking.guest_name or "Гость"
    booking.check_in = check_in
    booking.check_out = check_out
    # письма про одну бронь приходят по частям: пустое значение не затирает known
    booking.guests_count = guests_count or booking.guests_count or 2
    booking.amount_cents = amount_cents or booking.amount_cents
    booking.paid = paid or booking.paid
    booking.status = status
    booking.source = source
    if created:
        session.add(booking)
    session.flush()
    emit(
        session,
        "booking.created" if created else "booking.updated",
        {"id": booking.id, "platform": platform, "guest": booking.guest_name},
        property_id,
    )

    if status in ACTIVE:
        decision = check("close_dates")
        audit(session, "close_dates", decision.reason, source, "auto", decision.mode)

    conflicts = record_conflicts(session, booking)
    return IngestResult(booking, created, conflicts, None)


def record_conflicts(session: Session, booking: Booking) -> list[BookingConflict]:
    created: list[BookingConflict] = []
    for other in find_overlaps(session, booking):
        conflict = _upsert_booking_conflict(session, booking, other)
        if conflict:
            created.append(conflict)
    for block in find_block_overlaps(session, booking):
        if block.booking_id == booking.id:
            continue
        if block.booking_id and session.get(Booking, block.booking_id):
            other = session.get(Booking, block.booking_id)
            if other and other.id != booking.id and other.status in ACTIVE:
                continue
        conflict = _upsert_block_conflict(session, booking, block)
        if conflict:
            created.append(conflict)
    return created


def _pair_exists(session: Session, a: int, b: int) -> BookingConflict | None:
    return (
        session.query(BookingConflict)
        .filter(
            BookingConflict.status == "open",
            BookingConflict.block_id.is_(None),
            ((BookingConflict.booking_a_id == a) & (BookingConflict.booking_b_id == b))
            | ((BookingConflict.booking_a_id == b) & (BookingConflict.booking_b_id == a)),
        )
        .first()
    )


def _upsert_booking_conflict(session: Session, a: Booking, b: Booking) -> BookingConflict | None:
    if is_occupancy_mirror(
        a.platform, a.guest_name, a.check_in, a.check_out,
        b.platform, b.guest_name, b.check_in, b.check_out,
    ):
        return None
    existing = _pair_exists(session, a.id, b.id)
    if existing:
        return None
    cheap = cheaper_to_cancel(a.amount_cents or 1, b.amount_cents or 1, a.id, b.id)
    conflict = BookingConflict(
        property_id=a.property_id,
        booking_a_id=a.id,
        booking_b_id=b.id,
        cheaper_to_cancel_id=cheap,
        estimate_a_cents=a.amount_cents,
        estimate_b_cents=b.amount_cents,
        status="open",
    )
    session.add(conflict)
    session.flush()
    emit(session, "booking.conflict_detected", {"id": conflict.id, "a": a.id, "b": b.id}, a.property_id)
    return conflict


def open_dates(session: Session, block_id: int) -> Block | None:
    """Снять закрытие: убрать блок и пометки пересечений, которые он породил."""
    block = session.get(Block, block_id)
    if block is None:
        return None
    session.query(BookingConflict).filter_by(block_id=block.id).delete(synchronize_session=False)
    span = f"{block.start:%d.%m}–{block.end:%d.%m}"
    property_id = block.property_id
    session.delete(block)
    session.flush()
    emit(session, "dates.opened", {"block_id": block_id}, property_id)
    audit(session, "open_dates", f"сняла закрытие {span}", "owner", "owner")
    return block


def blocks_in_range(session: Session, property_id: int, start: datetime, end: datetime) -> list[Block]:
    """Блоки, попадающие в заданный отрезок, — для команды «открой 28–30 сентября»."""
    return [
        block
        for block in session.query(Block).filter_by(property_id=property_id).all()
        if overlaps(start, end, block.start, block.end)
    ]


def _block_conflict_is_real(booking: Booking, block: Block) -> bool:
    """Закрытие, пришедшее с площадки, и безымянная занятость — одно и то же.

    Настоящее пересечение — это когда мы закрыли даты сами, а на них есть бронь.
    """
    if str(block.source or "").startswith("ical:"):
        # закрытие пришло из календаря соседней площадки — это отражение той же занятости,
        # решать человеку нечего. Настоящий повод — когда даты закрыли мы сами.
        return False
    return True


def _upsert_block_conflict(session: Session, booking: Booking, block: Block) -> BookingConflict | None:
    if not _block_conflict_is_real(booking, block):
        return None
    existing = (
        session.query(BookingConflict)
        .filter_by(status="open", booking_a_id=booking.id, block_id=block.id)
        .first()
    )
    if existing:
        return None
    conflict = BookingConflict(
        property_id=booking.property_id,
        booking_a_id=booking.id,
        booking_b_id=booking.id,
        cheaper_to_cancel_id=booking.id,
        estimate_a_cents=booking.amount_cents,
        estimate_b_cents=0,
        block_id=block.id,
        status="open",
    )
    session.add(conflict)
    session.flush()
    emit(session, "booking.conflict_detected", {"id": conflict.id, "block_id": block.id}, booking.property_id)
    return conflict


def close_dates(
    session: Session,
    property_id: int,
    start: datetime,
    end: datetime,
    *,
    reason: str = "собственник закрыл даты",
) -> tuple[Block, list[Booking]]:
    decision = check("close_dates")
    block = Block(
        property_id=property_id,
        start=start,
        end=end,
        reason=reason,
        source="owner",
    )
    session.add(block)
    session.flush()
    emit(session, "dates.closed", {"block_id": block.id}, property_id)
    audit(session, "close_dates", f"{reason}: {start:%d.%m}–{end:%d.%m}", "owner", "owner", decision.mode)
    hits = [
        booking
        for booking in _active_bookings(session, property_id)
        if overlaps(start, end, booking.check_in, booking.check_out)
    ]
    for booking in hits:
        _upsert_block_conflict(session, booking, block)
    return block, hits


def owner_cancel_in_calendar(session: Session, booking: Booking) -> None:
    """Снимаем слот у себя. На Авито/Суточно отменяет человек."""
    booking.status = "cancelled"
    _close_conflicts_for(session, booking.id)
    stay_block = (
        session.query(Block)
        .filter_by(property_id=booking.property_id, reason=f"бронь {booking.id}")
        .first()
    )
    if stay_block:
        session.delete(stay_block)
    emit(session, "booking.cancelled_local", {"id": booking.id}, booking.property_id)
    audit(
        session,
        "cancel",
        f"Собственник снял бронь {booking.guest_name} в календаре. На площадке отменяет сама.",
        "owner",
        "owner",
        "never",
    )


def dismiss_conflict(session: Session, conflict: BookingConflict) -> None:
    conflict.status = "dismissed"
    audit(session, "conflict.dismissed", "Собственник разберётся на площадках", "owner", "owner", "confirm")


def _close_conflicts_for(session: Session, booking_id: int) -> None:
    rows = (
        session.query(BookingConflict)
        .filter(
            BookingConflict.status == "open",
            (BookingConflict.booking_a_id == booking_id) | (BookingConflict.booking_b_id == booking_id),
        )
        .all()
    )
    for row in rows:
        a = session.get(Booking, row.booking_a_id)
        b = session.get(Booking, row.booking_b_id)
        still = bookings_conflict(a, b)
        if not still:
            row.status = "resolved"


def bookings_conflict(a: Booking | None, b: Booking | None) -> bool:
    if not a or not b or a.id == b.id:
        return False
    if a.status not in ACTIVE or b.status not in ACTIVE:
        return False
    if not overlaps(a.check_in, a.check_out, b.check_in, b.check_out):
        return False
    # Овербукинг мы видим по письмам: там есть имена. Календари площадок имён не отдают
    # и зеркалят занятость друг друга — считать это пересечением значит кричать впустую.
    if is_busy_label(a.guest_name) or is_busy_label(b.guest_name):
        return False
    return not is_occupancy_mirror(
        a.platform, a.guest_name, a.check_in, a.check_out,
        b.platform, b.guest_name, b.check_in, b.check_out,
    )


def is_demo_booking(booking: Booking) -> bool:
    ext = booking.external_id or ""
    if booking.source == "seed":
        return True
    if any(ext.startswith(prefix) for prefix in DEMO_EXTERNAL_PREFIXES):
        return True
    if ext in DEMO_EXTERNAL_IDS:
        return True
    name = (booking.guest_name or "").casefold()
    return name.startswith("гость ical")


def retire_ical_blocks(session: Session, property_id: int) -> int:
    """Разовая уборка: старые «брони» вида «Avito (Closed)» — это закрытые даты.

    Раньше мы тащили из iCal всё подряд, поэтому блокировки площадки лежали в бронях
    и читались в шахматке как второй гость на те же даты.
    """
    moved = 0
    rows = (
        session.query(Booking)
        .filter(Booking.property_id == property_id, Booking.source == "ical")
        .filter(Booking.status != "cancelled")
        .all()
    )
    for booking in rows:
        if not is_block_label(booking.guest_name):
            continue
        uid = (booking.external_id or "")[:200]
        block = None
        if uid:
            block = session.query(Block).filter_by(property_id=property_id, external_id=uid).one_or_none()
        if block is None:
            session.add(Block(
                property_id=property_id,
                external_id=uid,
                start=booking.check_in,
                end=booking.check_out,
                reason=f"закрыто на площадке: {booking.guest_name}"[:300],
                source=f"ical:{booking.platform}"[:32],
            ))
        booking.status = "cancelled"
        _close_conflicts_for(session, booking.id)
        moved += 1
    if moved:
        session.flush()
        audit(session, "calendar.retire_ical_blocks",
              f"Перевела {moved} закрытий площадки из броней в закрытые даты", "system", "auto", "auto")
    return moved


def retire_demo_bookings(session: Session, property_id: int) -> int:
    ical_n = (
        session.query(Booking)
        .filter(Booking.property_id == property_id, Booking.source == "ical")
        .count()
    )
    if ical_n < 3:
        return 0
    removed = 0
    rows = session.query(Booking).filter(Booking.property_id == property_id, Booking.status.in_([*ACTIVE, "checkout"])).all()
    for booking in rows:
        if not is_demo_booking(booking):
            continue
        booking.status = "cancelled"
        _close_conflicts_for(session, booking.id)
        removed += 1
    if removed:
        audit(
            session,
            "calendar.retire_demo",
            f"Сняла {removed} учебных броней: в календаре уже живой iCal",
            "system",
            "auto",
            "auto",
        )
    return removed


def reconcile_conflicts(session: Session, property_id: int) -> int:
    """Закрыть зеркала площадок и заново открыть только настоящие пересечения."""
    retire_ical_blocks(session, property_id)
    retire_demo_bookings(session, property_id)
    open_rows = session.query(BookingConflict).filter_by(property_id=property_id, status="open").all()
    closed = 0
    for row in open_rows:
        a = session.get(Booking, row.booking_a_id)
        b = None if row.block_id else session.get(Booking, row.booking_b_id)
        if row.block_id:
            block = session.get(Block, row.block_id)
            gone = block is None or a is None or a.status == "cancelled"
            if gone or not _block_conflict_is_real(a, block) or not overlaps(
                block.start, block.end, a.check_in, a.check_out
            ):
                row.status = "resolved"
                closed += 1
            continue
        if not bookings_conflict(a, b):
            row.status = "resolved"
            closed += 1
    active = _active_bookings(session, property_id)
    for booking in active:
        record_conflicts(session, booking)
    return closed


def listing_for(session: Session, prop: Property, platform: str) -> Listing | None:
    key = PLATFORM_ALIASES.get((platform or "").strip().lower(), (platform or "").strip().lower())
    return session.query(Listing).filter_by(property_id=prop.id, platform=key).first()


def guess_platform_from_url(url: str) -> str | None:
    low = (url or "").lower()
    if "avito" in low:
        return "avito"
    if "sutochno" in low:
        return "sutochno"
    if "ostrovok" in low or "101hotels" in low:
        return "ostrovok"
    if "yandex" in low:
        return "yandex"
    return None


def set_listing_ical(session: Session, prop: Property, platform: str, url: str) -> Listing:
    listing = listing_for(session, prop, platform)
    if not listing:
        guessed = guess_platform_from_url(url)
        listing = listing_for(session, prop, guessed) if guessed else None
    if not listing:
        raise ValueError("Не знаю площадку. Напишите: ical авито: https://...")
    listing.ical_url = (url or "").strip()
    return listing


def _upsert_ical_block(session: Session, property_id: int, platform: str, event) -> Block:
    """Закрытие дат с площадки. Дедуп по uid, чтобы не плодить копии при каждой сверке."""
    uid = (event.uid or "")[:200]
    block = None
    if uid:
        block = session.query(Block).filter_by(property_id=property_id, external_id=uid).one_or_none()
    if block is None:
        block = Block(property_id=property_id, external_id=uid)
        session.add(block)
    block.start = event.check_in
    block.end = event.check_out
    block.reason = f"закрыто на площадке: {event.summary}"[:300]
    block.source = f"ical:{platform}"[:32]
    session.flush()
    return block


def ingest_ics(
    session: Session,
    prop: Property,
    text: str,
    *,
    platform: str = "ical",
    listing_id: int | None = None,
) -> list[IngestResult]:
    results = []
    for event in parse_ics(text):
        listing = session.get(Listing, listing_id) if listing_id else listing_for(session, prop, platform)
        plat = listing.platform if listing else platform

        if is_block_label(event.summary):
            # это закрытые даты на площадке, а не гость: заводим блок, а не бронь
            _upsert_ical_block(session, prop.id, plat, event)
            continue

        results.append(
            ingest_booking(
                session,
                property_id=prop.id,
                platform=listing.platform if listing else platform,
                external_id=event.uid[:120],
                guest_name=event.summary.split(" — ")[0][:200] or "Гость",
                check_in=event.check_in,
                check_out=event.check_out,
                paid=event.status == "confirmed",
                status=event.status,
                source="ical",
                listing_id=listing.id if listing else listing_id,
            )
        )
    if results:
        reconcile_conflicts(session, prop.id)
    return results


def rub(cents: int) -> str:
    return f"{(cents or 0) / 100:,.0f} ₽".replace(",", " ")


def describe_conflict(session: Session, conflict: BookingConflict) -> str:
    a = session.get(Booking, conflict.booking_a_id)
    if conflict.block_id:
        block = session.get(Block, conflict.block_id)
        span = fmt_span(block.start, block.end) if block else "даты"
        guest = a.guest_name if a else "бронь"
        return (
            f"Конфликт: {guest} пересекается с закрытыми датами ({span}).\n"
            f"{block.reason if block else ''}\n"
            "Отмену на площадке делает человек. Бот только снимает слот в нашем календаре."
        )
    b = session.get(Booking, conflict.booking_b_id)
    cheap = session.get(Booking, conflict.cheaper_to_cancel_id)
    lines = [
        "Пересечение броней. Отмену на площадке делает человек.",
        "",
        _booking_line(a),
        _booking_line(b),
        "",
        f"Дешевле снять в календаре: {cheap.guest_name if cheap else '—'} ({rub(cheap.amount_cents) if cheap else ''}).",
    ]
    return "\n".join(lines)


def _booking_line(booking: Booking | None) -> str:
    if not booking:
        return "—"
    plat = PLATFORMS.get(booking.platform, booking.platform)
    return (
        f"• {booking.guest_name} · {plat} · {fmt_span(booking.check_in, booking.check_out)} · "
        f"{rub(booking.amount_cents)}"
    )


def conflict_keyboard(conflict: BookingConflict) -> list[list[dict[str, str]]]:
    if conflict.block_id:
        return [
            [{"text": "Снять бронь в календаре", "callback_data": f"cx:{conflict.id}:{conflict.booking_a_id}"}],
            [{"text": "Оставлю, разберусь", "callback_data": f"cxd:{conflict.id}"}],
        ]
    a_id, b_id = conflict.booking_a_id, conflict.booking_b_id
    return [
        [{"text": "Снять первую в календаре", "callback_data": f"cx:{conflict.id}:{a_id}"}],
        [{"text": "Снять вторую в календаре", "callback_data": f"cx:{conflict.id}:{b_id}"}],
        [{"text": "Разберусь на площадках", "callback_data": f"cxd:{conflict.id}"}],
    ]


def describe_booking(booking: Booking, *, created: bool) -> str:
    verb = "Новая бронь" if created else "Бронь обновлена"
    plat = PLATFORMS.get(booking.platform, booking.platform)
    paid = "оплачена" if booking.paid else "оплата не отмечена"
    return (
        f"{verb} · {plat}\n"
        f"{booking.guest_name}, {fmt_span(booking.check_in, booking.check_out)}, {paid}.\n"
        "Даты на остальных витринах помечаем занятыми. Отмену бот не делает."
    )
