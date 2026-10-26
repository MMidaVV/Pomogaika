from __future__ import annotations

import json
from datetime import datetime, timedelta

from pomogaika.db import SessionLocal
from pomogaika.models import MessageDraft, Property
from pomogaika.services.copilot import CopilotResult, booking_label, classify_and_draft
from pomogaika.services.guest import bind_guest, bind_whatsapp, match_booking
from pomogaika.services.nudges import capture_eta
from pomogaika.services.guest_links import is_greeting_only, resolve_code, welcome_text
from pomogaika.services.seed import audit, emit
from sqlalchemy.orm import Session


# Гость иногда отправляет одно и то же дважды: нажал «Отправить», показалось,
# что не ушло, нажал ещё раз. Двух одинаковых ответов он получить не должен.
TWIN_WINDOW = timedelta(minutes=2)


def _recent_twin(session: Session, text: str, guest_chat_id: str) -> MessageDraft | None:
    if not guest_chat_id or not text:
        return None
    since = datetime.utcnow() - TWIN_WINDOW
    return (
        session.query(MessageDraft)
        .filter(
            MessageDraft.guest_chat_id == guest_chat_id,
            MessageDraft.guest_text == text,
            MessageDraft.created_at >= since,
        )
        .order_by(MessageDraft.id.desc())
        .first()
    )


def _result_of(draft: MessageDraft, booking) -> CopilotResult:
    """Повтор уже разобранного сообщения: отдаём тот же разбор, ничего не пишем."""
    return CopilotResult(
        question_class=draft.question_class,
        policy_mode=draft.policy_mode,
        allowed_auto=False,
        draft=draft.draft_text,
        matched_faq=draft.matched_faq,
        reason="Повтор того же сообщения — ответ уже готов",
        needs_owner=draft.status == "pending_owner",
        booking_id=draft.booking_id,
        booking_label=booking_label(booking) if booking else "",
    )


def create_guest_draft(
    session: Session,
    text: str,
    *,
    guest_chat_id: str = "",
    guest_user_id: str = "",
    business_connection_id: str = "",
    channel: str = "web",
    booking_id: int | None = None,
    external_chat_id: str = "",
) -> tuple[MessageDraft, CopilotResult]:
    prop = session.query(Property).one()
    link_channel = "whatsapp" if channel == "whatsapp" else "telegram"
    linked = resolve_code(session, text, guest_user_id, link_channel)
    if linked is not None:
        booking_id = linked.id
    booking = match_booking(
        session,
        prop.id,
        telegram_user_id=guest_user_id,
        text=text,
        booking_id=booking_id,
    )
    if booking:
        if channel == "whatsapp":
            bind_whatsapp(booking, external_chat_id or guest_chat_id)
        else:
            bind_guest(booking, guest_user_id, guest_chat_id, business_connection_id)
    if booking is not None and booking.awaiting:
        said = capture_eta(session, booking, text)
        if said:
            result = CopilotResult(
                question_class="eta",
                policy_mode="auto",
                allowed_auto=True,
                draft=(f"Записала, ждём вас к {said}. "
                       "Если планы изменятся — просто напишите сюда."
                       if booking.eta_in else
                       f"Записала, выезд около {said}. Спасибо!"),
                matched_faq="время приезда",
                reason="Гость назвал время — записали в бронь",
                needs_owner=False,
                booking_id=booking.id,
                booking_label=booking_label(booking),
            )
            draft = MessageDraft(
                property_id=prop.id, guest_text=text, draft_text=result.draft,
                question_class=result.question_class, policy_mode=result.policy_mode,
                status="auto_sent", matched_faq=result.matched_faq,
                guest_chat_id=guest_chat_id, guest_user_id=guest_user_id,
                business_connection_id=business_connection_id,
                channel=channel, booking_id=booking.id,
                external_chat_id=(external_chat_id or "")[:200],
            )
            session.add(draft)
            session.flush()
            return draft, result

    twin = _recent_twin(session, text, guest_chat_id)
    if twin is not None:
        # гость нажал «Отправить» дважды — второй ответ ему не нужен
        return twin, _result_of(twin, booking)

    if linked is not None and booking is not None and is_greeting_only(text):
        # переход по нашей ссылке: отвечаем сами, без модели и без ожидания
        result = CopilotResult(
            question_class="greeting",
            policy_mode="auto",
            allowed_auto=True,
            draft=welcome_text(booking),
            matched_faq="приветствие по ссылке",
            reason="Гость перешёл по ссылке и поздоровался — отвечаем сразу",
            needs_owner=False,
            booking_id=booking.id,
            booking_label=booking_label(booking),
            bound_by_code=True,
        )
    else:
        result = classify_and_draft(text, booking)
        if linked is not None and booking is not None and booking.id == linked.id:
            result.bound_by_code = True
    draft = MessageDraft(
        property_id=prop.id,
        guest_text=text,
        draft_text=result.draft,
        question_class=result.question_class,
        policy_mode=result.policy_mode,
        status="pending_owner" if result.needs_owner else "auto_sent",
        matched_faq=result.matched_faq,
        guest_chat_id=guest_chat_id,
        guest_user_id=guest_user_id,
        business_connection_id=business_connection_id,
        channel=channel,
        external_chat_id=(external_chat_id or "")[:200],
        booking_id=result.booking_id,
    )
    session.add(draft)
    session.flush()
    emit(
        session,
        "message.draft_created",
        {"id": draft.id, "class": result.question_class, "booking_id": result.booking_id},
        prop.id,
    )
    audit(
        session,
        "typical_reply" if result.question_class == "typical" else result.question_class,
        result.reason,
        "guest_agent",
        "owner" if result.needs_owner else "auto",
        result.policy_mode,
    )
    return draft, result


def avito_thread(draft: MessageDraft) -> list[dict]:
    try:
        rows = json.loads(draft.thread_json or "[]")
    except json.JSONDecodeError:
        return []
    return rows if isinstance(rows, list) else []


def _save_thread(draft: MessageDraft, rows: list[dict]) -> None:
    draft.thread_json = json.dumps(rows, ensure_ascii=False)


def remember_avito_line(draft: MessageDraft, role: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    rows = avito_thread(draft)
    if rows and rows[-1].get("role") == role and rows[-1].get("text") == text:
        return
    rows.append({"role": role, "text": text})
    _save_thread(draft, rows)


def apply_avito_remote_thread(draft: MessageDraft, messages: list[dict]) -> None:
    rows: list[dict] = []
    for message in messages:
        text = str(message.get("text") or "").strip()
        if not text:
            continue
        role = "out" if message.get("mine") else "in"
        if rows and rows[-1]["role"] == role and rows[-1]["text"] == text:
            continue
        rows.append({"role": role, "text": text})
    if rows:
        _save_thread(draft, rows)
        last_in = next((row["text"] for row in reversed(rows) if row["role"] == "in"), "")
        if last_in:
            draft.guest_text = last_in


def create_avito_draft(
    session: Session,
    *,
    channel_id: str,
    peer: str,
    item: str,
    text: str,
) -> tuple[MessageDraft, bool]:
    """Карточка для личного Авито. Всегда ждёт кнопку, сама никуда не отправляется.

    Второй элемент True, если карточка создана сейчас, а не найдена прежняя.
    """
    channel_id = (channel_id or "").strip()
    text = (text or "").strip()
    if not channel_id or not text:
        raise ValueError("нужны чат и текст сообщения")
    existing = (
        session.query(MessageDraft)
        .filter(
            MessageDraft.channel == "avito",
            MessageDraft.external_chat_id == channel_id[:200],
        )
        .order_by(MessageDraft.id.desc())
        .first()
    )
    if existing is not None:
        known = any(row.get("role") == "in" and row.get("text") == text for row in avito_thread(existing))
        if not known and not avito_thread(existing) and existing.guest_text == text:
            known = True
        if not known:
            remember_avito_line(existing, "in", text)
            if existing.guest_text != text:
                result = classify_and_draft(text, None, session=session, prop=session.query(Property).one())
                existing.guest_text = text
                existing.draft_text = result.draft
                existing.question_class = result.question_class
                existing.matched_faq = result.matched_faq
                existing.status = "pending_owner"
        elif not avito_thread(existing):
            remember_avito_line(existing, "in", text)
        return existing, False
    prop = session.query(Property).one()
    result = classify_and_draft(text, None, session=session, prop=prop)
    draft = MessageDraft(
        property_id=prop.id,
        guest_text=text,
        draft_text=result.draft,
        question_class=result.question_class,
        policy_mode="confirm",
        status="pending_owner",
        matched_faq=result.matched_faq,
        guest_chat_id=channel_id[:64],
        guest_user_id=(peer or "Собеседник")[:64],
        business_connection_id=(item or "")[:128],
        channel="avito",
        external_chat_id=channel_id[:200],
        thread_json=json.dumps([{"role": "in", "text": text}], ensure_ascii=False),
    )
    session.add(draft)
    session.flush()
    emit(
        session,
        "message.draft_created",
        {"id": draft.id, "class": result.question_class, "channel": "avito"},
        prop.id,
    )
    audit(session, "typical_reply", "Черновик из Авито ждёт кнопку отправки", "guest_agent", "owner", "confirm")
    return draft, True


def save_guest_draft(
    text: str,
    *,
    guest_chat_id: str = "",
    guest_user_id: str = "",
    business_connection_id: str = "",
    channel: str = "telegram",
    booking_id: int | None = None,
) -> tuple[int, CopilotResult]:
    session = SessionLocal()
    try:
        draft, result = create_guest_draft(
            session,
            text,
            guest_chat_id=guest_chat_id,
            guest_user_id=guest_user_id,
            business_connection_id=business_connection_id,
            channel=channel,
            booking_id=booking_id,
        )
        session.commit()
        return draft.id, result
    finally:
        session.close()


def apply_draft_action(session: Session, draft: MessageDraft, action: str, text: str | None = None) -> MessageDraft:
    if action == "send":
        draft.status = "sent"
        reason = "Собственник нажал «отправить»"
    elif action == "edit":
        if text:
            draft.draft_text = text
        draft.status = "sent_edited"
        reason = "Собственник поправил черновик и отправил"
    elif action == "self":
        if text:
            draft.draft_text = text
        draft.status = "owner_replied"
        reason = "Собственник ответил сам"
    else:
        raise ValueError(action)
    emit(session, "message.sent", {"id": draft.id, "via": action}, draft.property_id)
    audit(session, "approve_action", reason, "owner", "owner", draft.policy_mode)
    return draft
