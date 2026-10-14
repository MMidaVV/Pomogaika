"""Синхронизация календарей площадок.

Одна и та же работа для двух вызывающих: кнопки «Обновить календари» в мини-приложении
и фонового цикла. Раньше логика жила только в веб-ручке, поэтому календарь обновлялся
исключительно по нажатию — и показывал то, что забрали в прошлый раз.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from pomogaika.models import Listing, Property
from pomogaika.platforms.ical import fetch_ics
from pomogaika.services.bookings import (
    PLATFORMS,
    conflict_keyboard,
    describe_conflict,
    ingest_ics,
)
from pomogaika.services.notify import notify_owner

log = logging.getLogger("pomogaika.sync")


def sync_listings(session: Session, *, announce: str = "summary") -> list[dict]:
    """Забрать iCal по всем площадкам с ссылкой. Возвращает отчёт по каждой.

    announce:
      * "summary" — одна короткая строка в бота (кнопка «Обновить календари»);
      * "changes" — писать, только если что-то появилось или площадка молчит (фон);
      * "none"    — молча.

    Карточку на каждую бронь больше не шлём: при обновлении их прилетали десятки.
    Про пересечения сообщаем отдельно — там нужны кнопки и решение человека.
    """
    prop = session.query(Property).first()
    if prop is None:
        return []

    listings = (
        session.query(Listing)
        .filter(Listing.property_id == prop.id, Listing.ical_url != "")
        .all()
    )
    report: list[dict] = []
    fresh: list = []
    for listing in listings:
        try:
            text = fetch_ics(listing.ical_url)
        except Exception as exc:  # noqa: BLE001 — площадка может молчать, это не повод падать
            log.warning("iCal %s не забрался: %s", listing.platform, exc)
            report.append({"platform": listing.platform, "ok": False, "error": str(exc)})
            continue

        results = ingest_ics(session, prop, text, platform=listing.platform, listing_id=listing.id)
        created = [r for r in results if r.created]
        fresh.extend(r.booking for r in created)
        if announce != "none":
            for result in results:
                for conflict in result.conflicts:
                    notify_owner(describe_conflict(session, conflict), conflict_keyboard(conflict))
        report.append({
            "platform": listing.platform,
            "ok": True,
            "count": len(results),
            "created": len(created),
        })

    _announce(report, fresh, announce)
    return report


def summary_line(report: list[dict], fresh: list) -> str:
    """Короткий текст про то, чем кончилось обновление."""
    ok = [r for r in report if r["ok"]]
    bad = [r for r in report if not r["ok"]]
    parts = [f"{PLATFORMS.get(r['platform'], r['platform'])} {r['count']}" for r in ok]
    text = "Календари обновлены" + (": " + " · ".join(parts) if parts else "")
    if bad:
        text += "\nНе ответили: " + ", ".join(PLATFORMS.get(r["platform"], r["platform"]) for r in bad)
    if fresh:
        listed = ", ".join(f"{b.guest_name} {b.check_in:%d.%m}–{b.check_out:%d.%m}" for b in fresh[:5])
        more = f" и ещё {len(fresh) - 5}" if len(fresh) > 5 else ""
        text += f"\nНовых броней: {len(fresh)} — {listed}{more}"
    elif not bad:
        text += "\nНовых броней нет."
    return text


def _announce(report: list[dict], fresh: list, mode: str) -> None:
    if mode == "none" or not report:
        return
    failed = any(not r["ok"] for r in report)
    if mode == "changes" and not fresh and not failed:
        return  # фон молчит, когда ничего не изменилось
    notify_owner(summary_line(report, fresh))
