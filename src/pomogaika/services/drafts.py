from __future__ import annotations

from pomogaika.db import SessionLocal
from pomogaika.models import MessageDraft, Property
from pomogaika.services.copilot import CopilotResult, classify_and_draft
from pomogaika.services.guest import bind_guest, match_booking
from pomogaika.services.seed import audit, emit
from sqlalchemy.orm import Session


def create_guest_draft(
    session: Session,
    text: str,
    *,
    guest_chat_id: str = "",
    guest_user_id: str = "",
    business_connection_id: str = "",
    channel: str = "web",
    booking_id: int | None = None,
) -> tuple[MessageDraft, CopilotResult]:
    prop = session.query(Property).one()
    booking = match_booking(
        session,
        prop.id,
        telegram_user_id=guest_user_id,
        text=text,
        booking_id=booking_id,
    )
    if booking:
        bind_guest(booking, guest_user_id, guest_chat_id, business_connection_id)
    result = classify_and_draft(text, booking)
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
