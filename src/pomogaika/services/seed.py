from __future__ import annotations

from datetime import datetime, timedelta

import yaml
from sqlalchemy.orm import Session

from pomogaika.config import settings
from pomogaika.models import (
    AuditLog,
    Booking,
    BookingConflict,
    Event,
    Listing,
    MessageDraft,
    Owner,
    Property,
    Task,
)
from pomogaika.domain.calendar import cheaper_to_cancel, overlaps


def emit(session: Session, type_: str, payload: dict, property_id: int = 1) -> None:
    session.add(Event(property_id=property_id, type=type_, payload=payload))


def audit(session: Session, action: str, reason: str, actor: str = "agent", authorized_by: str = "system", policy: str = "") -> None:
    session.add(
        AuditLog(
            actor=actor,
            action=action,
            reason=reason,
            authorized_by=authorized_by,
            policy_decision=policy,
        )
    )


def seed_if_empty(session: Session) -> None:
    if session.query(Property).first():
        return
    knowledge = yaml.safe_load(settings.knowledge_path.read_text(encoding="utf-8"))
    owner = Owner(name="Галина")
    session.add(owner)
    session.flush()
    prop = Property(
        owner_id=owner.id,
        code="kava",
        title=knowledge["title"],
        address=knowledge["address"],
        city=knowledge["city"],
        timezone=knowledge["timezone"],
        knowledge=knowledge,
        max_guests=knowledge["max_guests"],
        check_in_from=knowledge["check_in_from"],
        check_out_until=knowledge["check_out_until"],
    )
    session.add(prop)
    session.flush()
    platforms = [
        ("avito", "8044426045", knowledge["platforms"]["avito"]),
        ("sutochno", "2323816", knowledge["platforms"]["sutochno"]),
        ("ostrovok", "13926838", knowledge["platforms"]["ostrovok"]),
        ("yandex", "kava-apartamenty-u-moria-zelenogradsk", knowledge["platforms"]["yandex"]),
    ]
    listings = {}
    for name, ext, url in platforms:
        listing = Listing(
            property_id=prop.id,
            platform=name,
            external_id=ext,
            url=url,
            ical_url=str((knowledge.get("ical") or {}).get(name) or ""),
        )
        session.add(listing)
        session.flush()
        listings[name] = listing

    def b(platform, guest, cin, cout, guests, amount, paid, status, ext=""):
        booking = Booking(
            property_id=prop.id,
            listing_id=listings[platform].id,
            platform=platform,
            external_id=ext,
            guest_name=guest,
            check_in=cin,
            check_out=cout,
            guests_count=guests,
            amount_cents=amount,
            paid=paid,
            status=status,
        )
        session.add(booking)
        session.flush()
        return booking

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    current = b(
        "avito", "Михаил",
        today.replace(hour=14) - timedelta(days=2),
        today.replace(hour=12) + timedelta(days=1),
        2, 24000_00, True, "staying", "AV-demo-1",
    )
    next_stay = b(
        "yandex", "Дарья",
        today.replace(hour=14) + timedelta(days=2),
        today.replace(hour=12) + timedelta(days=5),
        4, 31872_00, True, "confirmed", "YA-demo-2",
    )
    overlap_new = b(
        "sutochno", "А.",
        today.replace(hour=14) + timedelta(days=4),
        today.replace(hour=12) + timedelta(days=7),
        2, 18000_00, True, "confirmed", "42910245",
    )
    session.add(
        BookingConflict(
            property_id=prop.id,
            booking_a_id=next_stay.id,
            booking_b_id=overlap_new.id,
            cheaper_to_cancel_id=cheaper_to_cancel(next_stay.amount_cents, overlap_new.amount_cents, next_stay.id, overlap_new.id),
            estimate_a_cents=next_stay.amount_cents,
            estimate_b_cents=overlap_new.amount_cents,
            status="open",
        )
    )
    emit(session, "booking.created", {"guest": current.guest_name, "platform": "avito"}, prop.id)
    emit(session, "booking.conflict_detected", {"a": next_stay.id, "b": overlap_new.id}, prop.id)
    audit(session, "close_dates", "Даты закрыты на остальных площадках после брони Михаила", "system", "auto", "auto")
    session.add(
        Task(
            property_id=prop.id,
            booking_id=current.id,
            type="cleaning",
            state="assigned",
            title=f"Уборка после выезда Михаила {current.check_out.strftime('%d.%m')}, 12:00, Гагарина 87 кв. 19",
        )
    )
    session.add(
        MessageDraft(
            property_id=prop.id,
            guest_text="Галина, код от калитки не подходит, мы не можем попасть во двор",
            draft_text="Код одноразовый. Сейчас вышлю новый и если снова не откроется — открою удалённо. Не оставляем вас у калитки.",
            question_class="typical",
            policy_mode="confirm",
            status="pending_owner",
            matched_faq="Какой код от калитки / домофона?",
        )
    )


def find_overlaps(session: Session, booking: Booking) -> list[Booking]:
    others = (
        session.query(Booking)
        .filter(
            Booking.property_id == booking.property_id,
            Booking.id != booking.id,
            Booking.status.notin_(["cancelled"]),
        )
        .all()
    )
    return [
        other
        for other in others
        if overlaps(booking.check_in, booking.check_out, other.check_in, other.check_out)
    ]
