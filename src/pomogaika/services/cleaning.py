"""Уборка: задача по выезду, чек-лист как данные, эскалация. Оплату не проводим."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from pomogaika.config import settings
from pomogaika.domain.policy import check
from pomogaika.models import Booking, ChecklistItem, Contractor, Property, Task
from pomogaika.services.notify import notify_chat, notify_owner
from pomogaika.services.seed import audit, emit

OPEN_STATES = {"created", "accepted", "in_progress", "snoozed"}


def checklist(session: Session, property_id: int) -> list[ChecklistItem]:
    return (
        session.query(ChecklistItem)
        .filter_by(property_id=property_id)
        .order_by(ChecklistItem.sort, ChecklistItem.id)
        .all()
    )


def main_cleaner(session: Session, property_id: int) -> Contractor | None:
    return (
        session.query(Contractor)
        .filter_by(property_id=property_id, role="cleaner")
        .first()
    )


def create_cleaning_task(session: Session, booking: Booking) -> Task:
    existing = (
        session.query(Task)
        .filter(
            Task.booking_id == booking.id,
            Task.type == "cleaning",
            Task.state.in_(list(OPEN_STATES)),
        )
        .first()
    )
    if existing:
        return existing
    decision = check("cleaner_task")
    items = checklist(session, booking.property_id)
    contractor = main_cleaner(session, booking.property_id)
    needed = max(4, min(6, len(items) or 6))
    now = datetime.now()
    task = Task(
        property_id=booking.property_id,
        booking_id=booking.id,
        contractor_id=contractor.id if contractor else None,
        type="cleaning",
        state="created",
        title=(
            f"Уборка после {booking.guest_name}, выезд {booking.check_out.strftime('%d.%m %H:%M')}, "
            "Гагарина 87 кв. 19"
        ),
        assignee=contractor.name if contractor else "клинер основной",
        photos_needed=needed,
        escalate_after_min=settings.cleaner_escalate_minutes,
        due_at=now + timedelta(minutes=settings.cleaner_escalate_minutes),
    )
    session.add(task)
    session.flush()
    booking.status = "checkout"
    booking.checked_out_at = now
    emit(session, "stay.checked_out", {"booking_id": booking.id}, booking.property_id)
    emit(session, "task.created", {"task_id": task.id}, booking.property_id)
    audit(session, "cleaner_task", decision.reason, "task_engine", "auto", decision.mode)
    card = task_card(session, task)
    notify_owner(f"Выезд. Задача клинеру #{task.id}:\n{card}")
    if contractor and contractor.telegram_chat_id:
        notify_chat(
            contractor.telegram_chat_id,
            card,
            [
                [{"text": "Принять", "callback_data": f"clok:{task.id}"}],
                [{"text": "Перенос", "callback_data": f"clwait:{task.id}"}],
            ],
        )
    return task


def task_card(session: Session, task: Task) -> str:
    items = checklist(session, task.property_id)
    lines = [
        task.title,
        f"Статус: {task.state} · фото {task.photo_count}/{task.photos_needed}",
        "Оплату продукт не проводит.",
        "",
        "Чек-лист (свидетель, не судья — «грязно» сама не ставлю):",
    ]
    for item in items:
        lines.append(f"• {item.room}: {item.title}")
    if task.note:
        lines += ["", f"Комментарий: {task.note}"]
    if task.due_at and task.state == "created":
        lines.append(f"Если не примут до {task.due_at.strftime('%H:%M')} — эскалация вам.")
    return "\n".join(lines)


def accept_task(session: Session, task: Task) -> str:
    task.state = "accepted"
    task.accepted_at = datetime.now()
    emit(session, "task.accepted", {"task_id": task.id}, task.property_id)
    audit(session, "cleaner_task", f"Клинер принял #{task.id}", "contractor", "auto", "auto")
    return task_card(session, task) + "\n\nПринимаю фото по пунктам. Когда снимете нужное число — завершим."


def snooze_task(session: Session, task: Task, note: str) -> str:
    task.state = "snoozed"
    task.note = note.strip()[:500]
    task.due_at = datetime.now() + timedelta(hours=1)
    emit(session, "task.snoozed", {"task_id": task.id, "note": task.note[:80]}, task.property_id)
    audit(session, "cleaner_task", f"Перенос: {task.note}", "contractor", "auto", "auto")
    notify_owner(f"Клинер переносит уборку #{task.id}: {task.note}")
    return f"Перенесла. Напишу собственнику: «{task.note}»."


def add_photo(session: Session, task: Task) -> str:
    if task.state in {"created", "snoozed"}:
        task.state = "in_progress"
    elif task.state == "accepted":
        task.state = "in_progress"
    task.photo_count += 1
    emit(session, "task.photo", {"task_id": task.id, "n": task.photo_count}, task.property_id)
    left = max(0, task.photos_needed - task.photo_count)
    if left:
        return f"Фото {task.photo_count}/{task.photos_needed}. Ещё {left}."
    return f"Фото {task.photo_count}/{task.photos_needed}. Можно завершать — кнопка «Готово»."


def complete_task(session: Session, task: Task, blocker: str = "") -> str:
    task.state = "done"
    if blocker:
        task.note = ((task.note + " | ") if task.note else "") + blocker.strip()[:300]
    emit(session, "task.done", {"task_id": task.id}, task.property_id)
    audit(session, "cleaner_task", f"Уборка #{task.id} закрыта, оплату не проводим", "contractor", "auto", "auto")
    extra = f"\nЧто мешало: {blocker}" if blocker else ""
    notify_owner(f"Уборка #{task.id} закрыта.{extra}\nОплату клинеру продукт не проводит.")
    return "Приняла. Спасибо. Если что-то мешало на объекте — напишите, передам собственнику. Оплату здесь не считаю."


def escalate_stale(session: Session, *, now: datetime | None = None) -> list[Task]:
    now = now or datetime.now()
    rows = session.query(Task).filter(Task.type == "cleaning", Task.state == "created", Task.escalated_at.is_(None)).all()
    hit = []
    for task in rows:
        start = task.created_at or now
        minutes = task.escalate_after_min or settings.cleaner_escalate_minutes
        if now >= start + timedelta(minutes=minutes):
            task.escalated_at = now
            task.state = "expired"
            emit(session, "task.escalated", {"task_id": task.id}, task.property_id)
            audit(session, "cleaner_task", f"Клинер не принял #{task.id}", "task_engine", "owner", "confirm")
            notify_owner(
                f"Клинер не принял уборку #{task.id} за {minutes} мин.\n{task.title}\n"
                "Свяжитесь сами или поставьте запасного. Штраф сама не ставлю."
            )
            hit.append(task)
    return hit


def open_tasks(session: Session, property_id: int) -> list[Task]:
    return (
        session.query(Task)
        .filter(Task.property_id == property_id, Task.type == "cleaning", Task.state.in_(OPEN_STATES | {"expired"}))
        .order_by(Task.created_at.desc())
        .all()
    )


def owner_report(session: Session, prop: Property, *, now: datetime | None = None) -> str:
    from pomogaika.models import AuditLog, BookingConflict, MessageDraft
    from pomogaika.services.bookings import ACTIVE
    from pomogaika.services.codes import bookings_in_code_window

    now = now or datetime.now()
    bookings = session.query(Booking).filter(Booking.property_id == prop.id, Booking.status.in_(ACTIVE)).all()
    conflicts = session.query(BookingConflict).filter_by(property_id=prop.id, status="open").count()
    drafts = session.query(MessageDraft).filter_by(property_id=prop.id, status="pending_owner").count()
    tasks = open_tasks(session, prop.id)
    actions = session.query(AuditLog).count()
    minutes = actions * 2
    ready = bookings_in_code_window(session, prop.id, now=now)
    lines = [
        f"Пульт {prop.title}",
        f"Активных броней: {len(bookings)}",
        f"Черновиков ждут: {drafts}",
        f"Открытых пересечений: {conflicts}",
        f"Уборок в работе: {len(tasks)}",
        f"Записей в журнале: {actions} ≈ {minutes} мин. собственника (грубая оценка)",
        "",
        "Код можно выдать:" if ready else "Сейчас окно кода ни у кого не открыто.",
    ]
    for booking in ready:
        issued = f"уже отправлен **{booking.code_hint}" if booking.code_issued_at else "ещё не отправляли"
        lines.append(f"• {booking.guest_name} · {issued}")
    return "\n".join(lines)
