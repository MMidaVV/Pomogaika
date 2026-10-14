"""Сводка для Mini App собственника: объекты, календарь и площадки.

Многообъектный по построению: один объект — частный случай списка из одного.
Фильтр по собственнику (`owner_id`) и по объекту (`property_id`) — снаружи.
"""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from pomogaika.domain.calendar import is_busy_label
from pomogaika.services.stay import (
    attention,
    merge as merge_occupancy,
    next_event,
    platform_blocks_in_groups,
    readiness,
)
from pomogaika.models import Block, Booking, BookingConflict, Listing, MessageDraft, Property, Task
from pomogaika.services.bookings import describe_conflict

PLAT_TITLE = {
    "avito": "Авито",
    "sutochno": "Суточно",
    "yandex": "Яндекс",
    "ostrovok": "Островок",
    "ical": "iCal",
}

ACTIVE = {"pending", "confirmed", "paid", "staying", "checkout"}

STATUS_TITLE = {
    "pending": "Ждёт подтверждения",
    "confirmed": "Подтверждена",
    "paid": "Оплачена",
    "staying": "Проживает",
    "checkout": "Выезд",
}


def _nights(check_in: datetime, check_out: datetime) -> list[date]:
    days: list[date] = []
    cur = check_in.date()
    last = check_out.date()
    while cur < last:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def _covers(day: date, start: datetime, end: datetime) -> bool:
    return start.date() <= day < end.date()


def properties_for(session: Session, *, owner_id: int | None = None, property_id: int | None = None) -> list[Property]:
    """Объекты, по которым собираем сводку. Пусто — значит показывать нечего."""
    query = session.query(Property).order_by(Property.id)
    if owner_id:
        query = query.filter(Property.owner_id == owner_id)
    if property_id:
        query = query.filter(Property.id == property_id)
    return query.all()


def _booking_card(booking: Booking, *, conflict_ids: set[int], today: date,
                  sources: list[dict] | None = None, drafts: list | None = None,
                  tasks: list | None = None, merged_ids: list[int] | None = None) -> dict:
    """Карточка брони в том виде, в каком её рисует шахматка и список броней."""
    nights = len(_nights(booking.check_in, booking.check_out))
    amount = round(booking.amount_cents / 100)
    if booking.check_out.date() <= today:
        stage = "past"
    elif booking.check_in.date() <= today:
        stage = "staying"
    else:
        stage = "future"
    src = sources or [{
        "platform": booking.platform,
        "title": PLAT_TITLE.get(booking.platform, booking.platform),
        "kind": "занято" if is_busy_label(booking.guest_name) else "бронь",
        "label": booking.guest_name,
        "id": booking.id,
    }]
    rows = readiness(booking, drafts=drafts or [], tasks=tasks or [], sources=src, stage=stage)
    paid_amount = amount if booking.paid else 0
    return {
        "id": booking.id,
        "property_id": booking.property_id,
        "code": booking.external_id or f"KV{booking.id:04d}",
        "platform": booking.platform,
        "platform_title": PLAT_TITLE.get(booking.platform, booking.platform),
        "guest_name": booking.guest_name,
        "busy": is_busy_label(booking.guest_name),
        "check_in": booking.check_in.isoformat(),
        "check_out": booking.check_out.isoformat(),
        "nights": nights,
        "guests": booking.guests_count,
        "amount": amount,
        "balance": 0 if booking.paid else -amount,
        "paid": booking.paid,
        "status": booking.status,
        "status_title": STATUS_TITLE.get(booking.status, booking.status),
        "stage": stage,
        "conflict": booking.id in conflict_ids,
        "code_hint": booking.code_hint,
        # откуда знаем про эту занятость: площадки, которые её показывают
        "sources": src,
        "merged_ids": merged_ids or [booking.id],
        "next_event": next_event(booking, today),
        "readiness": rows,
        "attention": attention(booking, conflict=booking.id in conflict_ids, rows=rows, stage=stage),
        "paid_amount": paid_amount,
        "left_amount": amount - paid_amount,
    }


def build_miniapp(
    session: Session,
    *,
    year: int,
    month: int,
    owner_id: int | None = None,
    property_id: int | None = None,
) -> dict:
    props = properties_for(session, owner_id=owner_id, property_id=property_id)
    if not props:
        raise LookupError("объектов нет")
    ids = [p.id for p in props]

    listings = session.query(Listing).filter(Listing.property_id.in_(ids)).all()
    bookings = (
        session.query(Booking)
        .filter(Booking.property_id.in_(ids), Booking.status != "cancelled")
        .order_by(Booking.check_in)
        .all()
    )
    blocks = session.query(Block).filter(Block.property_id.in_(ids)).all()
    conflicts = session.query(BookingConflict).filter(BookingConflict.property_id.in_(ids), BookingConflict.status == "open").all()
    drafts = (
        session.query(MessageDraft)
        .filter(MessageDraft.property_id.in_(ids), MessageDraft.status == "pending_owner")
        .order_by(MessageDraft.created_at.desc())
        .limit(20)
        .all()
    )
    tasks = (
        session.query(Task)
        .filter(Task.property_id.in_(ids), Task.state.notin_(["done", "failed"]))
        .order_by(Task.created_at.desc())
        .all()
    )

    days_in_month = monthrange(year, month)[1]
    month_nights = [date(year, month, d) for d in range(1, days_in_month + 1)]

    # занятость считаем по каждому объекту отдельно — иначе с двумя объектами
    # «занято» одного закрывало бы календарь второго
    busy_by_prop: dict[int, set[date]] = defaultdict(set)
    blocked_by_prop: dict[int, set[date]] = defaultdict(set)
    night_plats: dict[date, set[str]] = defaultdict(set)
    nights_by_plat: dict[str, set[date]] = defaultdict(set)
    stays_by_plat: dict[str, int] = defaultdict(int)

    for booking in bookings:
        stays_by_plat[booking.platform] += 1
        for night in _nights(booking.check_in, booking.check_out):
            busy_by_prop[booking.property_id].add(night)
            night_plats[night].add(booking.platform)
            nights_by_plat[booking.platform].add(night)
    for block in blocks:
        for night in _nights(block.start, block.end):
            blocked_by_prop[block.property_id].add(night)

    objects = []
    for prop in props:
        busy = busy_by_prop[prop.id]
        blocked = blocked_by_prop[prop.id]
        taken = busy | blocked
        occupied_days = [d for d in month_nights if d in taken]
        objects.append(
            {
                "id": prop.id,
                "code": prop.code,
                "title": prop.title,
                "address": prop.address,
                "city": prop.city,
                "check_in_from": prop.check_in_from,
                "check_out_until": prop.check_out_until,
                "max_guests": prop.max_guests,
                "days": days_in_month,
                "occupied": len(occupied_days),
                "occupancy": round(100 * len(occupied_days) / days_in_month) if days_in_month else 0,
                # 1 — объект свободен в эту ночь, 0 — занят или закрыт
                "free": [0 if d in taken else 1 for d in month_nights],
            }
        )

    slots = days_in_month * len(props)
    occupied_total = sum(o["occupied"] for o in objects)
    occ_pct = round(100 * occupied_total / slots) if slots else 0
    occupied_any = [d for d in month_nights if any(d in busy_by_prop[p.id] for p in props)]

    by_platform = []
    for plat in sorted({listing.platform for listing in listings}):
        rows = [listing for listing in listings if listing.platform == plat]
        nights = nights_by_plat.get(plat) or set()
        month_hit = [d for d in occupied_any if plat in night_plats[d]]
        named = sum(1 for b in bookings if b.platform == plat and not is_busy_label(b.guest_name))
        # на чём канал реально работает: iCal даёт только даты, письма — имя, сумму и номер
        sources = {b.source for b in bookings if b.platform == plat}
        feeds = []
        if "email" in sources:
            feeds.append("письма")
        if any(bool(r.ical_url) for r in rows):
            feeds.append("iCal")
        how = " + ".join(feeds) if feeds else "не подключён"
        by_platform.append(
            {
                "platform": plat,
                "title": PLAT_TITLE.get(plat, plat),
                "url": rows[0].url if rows else "",
                "ical": any(bool(r.ical_url) for r in rows),
                "listings": len(rows),
                "stays": stays_by_plat.get(plat, 0),
                "stays_named": named,
                "nights": len(nights),
                "nights_month": len(month_hit),
                "share": round(100 * len(month_hit) / len(occupied_any)) if occupied_any else 0,
                "how": how,
                "has_names": named > 0,
                "api": False,  # прямого API у площадок пока нет ни у одной
            }
        )

    cal = []
    for index, day in enumerate(month_nights):
        stays = [
            {
                "id": b.id,
                "property_id": b.property_id,
                "platform": b.platform,
                "guest_name": b.guest_name,
                "busy": is_busy_label(b.guest_name),
                "check_in": b.check_in.isoformat(),
                "check_out": b.check_out.isoformat(),
                "paid": b.paid,
                "status": b.status,
            }
            for b in bookings
            if _covers(day, b.check_in, b.check_out)
        ]
        named = [s for s in stays if not s["busy"]]
        owner_blocks = [
            {
                "id": bl.id,
                "property_id": bl.property_id,
                "reason": bl.reason,
                "start": bl.start.isoformat(),
                "end": bl.end.isoformat(),
            }
            for bl in blocks
            if _covers(day, bl.start, bl.end)
        ]
        free_count = sum(o["free"][index] for o in objects)
        plats = sorted({s["platform"] for s in stays})
        cal.append(
            {
                "date": day.isoformat(),
                "free_count": free_count,
                "total": len(objects),
                "occupied": free_count == 0,
                "blocked": bool(owner_blocks),
                "platforms": plats,
                "label": named[0]["guest_name"] if named else ("занято" if stays else ("блок" if owner_blocks else "")),
                "stays": stays,
                "blocks": owner_blocks,
            }
        )

    now = datetime.now()
    today = now.date()

    def _stay(booking: Booking, *, kind: str) -> dict:
        return {
            "id": booking.id,
            "property_id": booking.property_id,
            "platform": booking.platform,
            "guest_name": booking.guest_name,
            "busy": is_busy_label(booking.guest_name),
            "check_in": booking.check_in.isoformat(),
            "check_out": booking.check_out.isoformat(),
            "kind": kind,
        }

    upcoming = []
    for booking in bookings:
        if booking.check_in.date() < today:
            continue
        upcoming.append(_stay(booking, kind="in"))
        if len(upcoming) >= 8:
            break

    staying = [_stay(b, kind="stay") for b in bookings if b.check_in.date() <= today < b.check_out.date()]
    arriving = [_stay(b, kind="in") for b in bookings if b.check_in.date() == today]
    leaving = [_stay(b, kind="out") for b in bookings if b.check_out.date() == today]

    conflict_ids = {c.booking_a_id for c in conflicts} | {c.booking_b_id for c in conflicts}
    groups = merge_occupancy(bookings, blocks)
    used_blocks = platform_blocks_in_groups(groups)
    cards = [
        _booking_card(
            group.lead,
            conflict_ids=conflict_ids,
            today=today,
            sources=group.sources(),
            drafts=drafts,
            tasks=tasks,
            merged_ids=[m.id for m in group.members],
        )
        for group in groups
    ]
    owner_blocks = [
        {
            "id": bl.id,
            "property_id": bl.property_id,
            "reason": bl.reason or "закрыто собственником",
            "start": bl.start.isoformat(),
            "end": bl.end.isoformat(),
            "source": bl.source,
        }
        for bl in blocks
        if bl.id not in used_blocks
    ]

    first = objects[0]
    return {
        # первый объект — для шапки; список объектов — в objects
        "property": {
            "id": first["id"],
            "title": first["title"],
            "address": first["address"],
            "city": first["city"],
            "check_in_from": first["check_in_from"],
            "check_out_until": first["check_out_until"],
        },
        "objects": objects,
        "month": {
            "year": year,
            "month": month,
            "days": days_in_month,
            "slots": slots,
            "occupied": occupied_total,
            "occupancy": occ_pct,
        },
        "platforms": by_platform,
        "bookings": cards,
        "blocks": owner_blocks,
        "calendar": cal,
        "today": staying,
        "arriving": arriving,
        "leaving": leaving,
        "upcoming": upcoming,
        "attention": {
            "drafts": len(drafts),
            "conflicts": len(conflicts),
            "tasks": len(tasks),
        },
        "drafts": [
            {
                "id": d.id,
                "property_id": d.property_id,
                "guest_text": d.guest_text,
                "draft_text": d.draft_text,
                "question_class": d.question_class,
                "matched_faq": d.matched_faq,
            }
            for d in drafts
        ],
        "conflicts": [
            {
                "id": c.id,
                "property_id": c.property_id,
                "text": describe_conflict(session, c),
            }
            for c in conflicts
        ],
        "tasks": [
            {
                "id": t.id,
                "property_id": t.property_id,
                "title": t.title,
                "state": t.state,
                "photo_count": t.photo_count,
                "photos_needed": t.photos_needed,
            }
            for t in tasks
        ],
    }
