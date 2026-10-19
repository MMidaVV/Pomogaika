"""Занятость и готовность брони — то, что показывает шахматка и список броней.

Две задачи:

1. **Склейка.** Площадки зеркалят занятость друг друга: бронь пришла на Авито,
   Суточно закрыло те же даты и отдало их в своём календаре как «Забронировано».
   Это одна занятость, и в шахматке она должна быть одной полосой, а не двумя.
   По нажатию видно, откуда пришло.

2. **Готовность.** Список броней должен помогать оценить, всё ли готово к заезду,
   и найти то, где нужен человек, — а не просто перечислять записи.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from pomogaika.domain.calendar import is_busy_label, overlaps
from pomogaika.models import Block, Booking, MessageDraft, Task

PLAT_TITLE = {
    "avito": "Авито",
    "sutochno": "Суточно",
    "ostrovok": "Островок",
    "yandex": "Яндекс",
    "ical": "iCal",
}


@dataclass
class Occupancy:
    """Одна занятость: что за период и из каких источников мы про неё знаем."""

    lead: Booking
    members: list[Booking] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)

    @property
    def check_in(self) -> datetime:
        return min(b.check_in for b in self.members)

    @property
    def check_out(self) -> datetime:
        return max(b.check_out for b in self.members)

    def sources(self) -> list[dict]:
        rows: list[dict] = []
        for booking in sorted(self.members, key=lambda b: (b.check_in, b.id)):
            rows.append({
                "platform": booking.platform,
                "title": PLAT_TITLE.get(booking.platform, booking.platform),
                "kind": "занято" if is_busy_label(booking.guest_name) else "бронь",
                "label": booking.guest_name,
                "id": booking.id,
            })
        for block in self.blocks:
            plat = str(block.source or "").split(":", 1)[-1]
            rows.append({
                "platform": plat,
                "title": PLAT_TITLE.get(plat, plat),
                "kind": "даты закрыты",
                "label": block.reason,
                "id": None,
            })
        return rows


def _can_merge(a: Booking, b: Booking) -> bool:
    """Склеиваем, только если хотя бы одна запись безымянная.

    Две брони с настоящими именами на пересекающихся датах — это настоящий конфликт,
    и прятать его склейкой нельзя.
    """
    if not overlaps(a.check_in, a.check_out, b.check_in, b.check_out):
        return False
    return is_busy_label(a.guest_name) or is_busy_label(b.guest_name)


def merge(bookings: list[Booking], blocks: list[Block] | None = None) -> list[Occupancy]:
    """Свести зеркала площадок в одну занятость."""
    groups: list[Occupancy] = []
    for booking in sorted(bookings, key=lambda b: (b.check_in, b.id)):
        named = not is_busy_label(booking.guest_name)
        for group in groups:
            if not any(_can_merge(member, booking) for member in group.members):
                continue
            # В группе допустима только одна бронь с именем. Иначе длинная безымянная
            # занятость сцепляет через себя двух разных гостей: группа расползается,
            # а настоящий овербукинг между ними оказывается спрятан.
            if named and any(not is_busy_label(m.guest_name) for m in group.members):
                continue
            group.members.append(booking)
            if is_busy_label(group.lead.guest_name) and named:
                group.lead = booking       # «лицом» группы становится гость с именем
            break
        else:
            groups.append(Occupancy(lead=booking, members=[booking]))

    for block in blocks or []:
        if not str(block.source or "").startswith("ical:"):
            continue          # наши собственные закрытия остаются отдельной сущностью
        for group in groups:
            if overlaps(block.start, block.end, group.check_in, group.check_out):
                group.blocks.append(block)
                break
    return groups


def platform_blocks_in_groups(groups: list[Occupancy]) -> set[int]:
    """Блоки, ставшие частью занятости: отдельной полосой их рисовать не надо."""
    return {block.id for group in groups for block in group.blocks}


# --- готовность брони ---

def _fmt_when(moment: datetime, today: date) -> str:
    days = (moment.date() - today).days
    if days == 0:
        when = "сегодня"
    elif days == 1:
        when = "завтра"
    elif days == -1:
        when = "вчера"
    elif days > 1:
        when = f"через {days} дн."
    else:
        when = f"{-days} дн. назад"
    return f"{when} в {moment:%H:%M}"


def next_event(booking: Booking, today: date) -> dict:
    """Ближайшее, что произойдёт с этой бронью."""
    if booking.check_in.date() > today:
        return {"kind": "checkin", "text": f"Заезд {_fmt_when(booking.check_in, today)}",
                "when": booking.check_in.isoformat()}
    if booking.check_out.date() >= today:
        return {"kind": "checkout", "text": f"Выезд {_fmt_when(booking.check_out, today)}",
                "when": booking.check_out.isoformat()}
    return {"kind": "done", "text": "Проживание завершено", "when": booking.check_out.isoformat()}


def readiness(booking: Booking, *, drafts: list[MessageDraft], tasks: list[Task],
              sources: list[dict], stage: str) -> list[dict]:
    """Состояние подготовки по пунктам: общение, уборка, календари.

    Про занятость без имени гостя (всё, что пришло только из календаря площадки)
    требовать нечего: мы не знаем ни оплаты, ни контактов. Показываем это прямо,
    вместо того чтобы поднимать ложную тревогу.
    """
    rows: list[dict] = []
    unknown_guest = is_busy_label(booking.guest_name)

    if unknown_guest:
        closed = len({s["platform"] for s in sources})
        return [
            {"title": "Данные гостя", "ok": True,
             "text": "календарь площадки отдаёт только даты — имя и сумма придут из письма"},
            {"title": "Календари", "ok": True,
             "text": f"даты заняты на {closed} площадк{'е' if closed == 1 else 'ах'}"},
        ]

    waiting = [d for d in drafts if d.booking_id == booking.id and d.status == "pending_owner"]
    if waiting:
        rows.append({"title": "Общение", "ok": False,
                     "text": f"ИИ ответил, ждёт вашего решения ({len(waiting)})"})
    else:
        rows.append({"title": "Общение", "ok": True, "text": "вопросов без ответа нет"})

    cleaning = [t for t in tasks if t.booking_id == booking.id]
    if cleaning:
        task = cleaning[0]
        done = task.state in ("done", "completed")
        rows.append({"title": "Уборка", "ok": done,
                     "text": f"{task.title}: {task.state}" if not done else "выполнена"})
    elif stage == "past":
        rows.append({"title": "Уборка", "ok": False, "text": "задача не поставлена"})
    else:
        rows.append({"title": "Уборка", "ok": True, "text": "будет после выезда"})

    closed = len({s["platform"] for s in sources})
    rows.append({"title": "Календари", "ok": closed >= 2 or stage == "past",
                 "text": f"даты видны на {closed} площадк{'е' if closed == 1 else 'ах'}"})

    if stage != "past":
        rows.append({"title": "Код доступа", "ok": bool(booking.code_hint),
                     "text": f"выдан ({booking.code_hint})" if booking.code_hint else "ещё не выдавали"})
    return rows


def attention(booking: Booking, *, conflict: bool, rows: list[dict], stage: str) -> dict:
    """Нужен ли человек. Отделено от этапа проживания намеренно: это разные вопросы."""
    problems: list[str] = []
    if conflict:
        problems.append("пересечение броней")
    if is_busy_label(booking.guest_name):
        # знаем только даты — требовать по такой записи нечего
        return {"level": "ok", "title": "Даты заняты", "why": []} if not conflict else {
            "level": "human", "title": "Требует внимания", "why": problems,
        }
    if not booking.paid and stage != "past":
        problems.append("оплата не отмечена")
    for row in rows:
        if not row["ok"] and row["title"] in ("Общение", "Уборка"):
            problems.append(row["text"])

    if problems:
        waiting_answer = any("ждёт вашего решения" in p for p in problems)
        return {
            "level": "human",
            "title": "Нужно решение" if waiting_answer else "Требует внимания",
            "why": problems,
        }
    if stage == "past":
        return {"level": "ok", "title": "Завершена", "why": []}
    return {"level": "ok", "title": "Всё по плану", "why": []}
