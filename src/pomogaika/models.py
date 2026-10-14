from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pomogaika.db import Base


def _now() -> datetime:
    return datetime.utcnow()


class Owner(Base):
    __tablename__ = "owners"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    telegram_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    properties: Mapped[list[Property]] = relationship(back_populates="owner")


class Property(Base):
    __tablename__ = "properties"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owners.id"))
    code: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(String(400))
    city: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Kaliningrad")
    knowledge: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    mode: Mapped[str] = mapped_column(String(32), default="copilot")
    max_guests: Mapped[int] = mapped_column(Integer, default=4)
    check_in_from: Mapped[str] = mapped_column(String(8), default="14:00")
    check_out_until: Mapped[str] = mapped_column(String(8), default="12:00")
    owner: Mapped[Owner] = relationship(back_populates="properties")
    listings: Mapped[list[Listing]] = relationship(back_populates="property")
    bookings: Mapped[list[Booking]] = relationship(back_populates="property")
    blocks: Mapped[list["Block"]] = relationship(back_populates="property")


class Listing(Base):
    __tablename__ = "listings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    platform: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(120), default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    ical_url: Mapped[str] = mapped_column(String(2000), default="")
    property: Mapped[Property] = relationship(back_populates="listings")


class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id"), nullable=True)
    platform: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(120), default="")
    guest_name: Mapped[str] = mapped_column(String(200), default="")
    check_in: Mapped[datetime] = mapped_column(DateTime)
    check_out: Mapped[datetime] = mapped_column(DateTime)
    guests_count: Mapped[int] = mapped_column(Integer, default=2)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    paid: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="confirmed")
    source: Mapped[str] = mapped_column(String(32), default="seed")
    guest_telegram_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    guest_chat_id: Mapped[str] = mapped_column(String(64), default="")
    business_connection_id: Mapped[str] = mapped_column(String(128), default="")
    code_hint: Mapped[str] = mapped_column(String(8), default="")
    code_issued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    code_window_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    arrival_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    departure_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_out_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    property: Mapped[Property] = relationship(back_populates="bookings")


class BookingConflict(Base):
    __tablename__ = "booking_conflicts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    booking_a_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"))
    booking_b_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"))
    cheaper_to_cancel_id: Mapped[int] = mapped_column(Integer)
    estimate_a_cents: Mapped[int] = mapped_column(Integer, default=0)
    estimate_b_cents: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="open")
    block_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Block(Base):
    __tablename__ = "blocks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[str] = mapped_column(String(300), default="")
    source: Mapped[str] = mapped_column(String(32), default="owner")
    external_id: Mapped[str] = mapped_column(String(200), default="", index=True)
    booking_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    property: Mapped[Property] = relationship(back_populates="blocks")


class MessageDraft(Base):
    __tablename__ = "message_drafts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    guest_text: Mapped[str] = mapped_column(Text)
    draft_text: Mapped[str] = mapped_column(Text)
    question_class: Mapped[str] = mapped_column(String(32))
    policy_mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default="pending_owner")
    matched_faq: Mapped[str] = mapped_column(String(300), default="")
    guest_chat_id: Mapped[str] = mapped_column(String(64), default="")
    guest_user_id: Mapped[str] = mapped_column(String(64), default="")
    business_connection_id: Mapped[str] = mapped_column(String(128), default="")
    channel: Mapped[str] = mapped_column(String(32), default="web")
    booking_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id"), nullable=True)
    contractor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    type: Mapped[str] = mapped_column(String(32), default="cleaning")
    state: Mapped[str] = mapped_column(String(32), default="created")
    title: Mapped[str] = mapped_column(String(300))
    assignee: Mapped[str] = mapped_column(String(120), default="клинер основной")
    note: Mapped[str] = mapped_column(Text, default="")
    photo_count: Mapped[int] = mapped_column(Integer, default=0)
    photos_needed: Mapped[int] = mapped_column(Integer, default=6)
    escalate_after_min: Mapped[int] = mapped_column(Integer, default=10)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Contractor(Base):
    __tablename__ = "contractors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(200), default="клинер основной")
    role: Mapped[str] = mapped_column(String(32), default="cleaner")
    telegram_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trust_level: Mapped[str] = mapped_column(String(32), default="new")


class ChecklistItem(Base):
    __tablename__ = "checklist_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(Integer, default=1)
    room: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(200))
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(Integer, default=1)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text, default="")
    authorized_by: Mapped[str] = mapped_column(String(64), default="system")
    policy_decision: Mapped[str] = mapped_column(String(16), default="")
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now)


class TelegramLink(Base):
    __tablename__ = "telegram_links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    business_connection_id: Mapped[str] = mapped_column(String(128), default="")
    owner_user_id: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AccessUser(Base):
    """Кому открыт пульт: мини-приложение, браузер и команды бота. Ролей нет — доступ общий."""

    __tablename__ = "access_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    added_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Invite(Base):
    """Одноразовая ссылка-приглашение. Живёт сутки, сгорает при первом использовании."""

    __tablename__ = "invites"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    used_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class StayOutcome(Base):
    """Итог проживания: отзыв, чек, залог, конфликт. Одна запись на бронь."""

    __tablename__ = "stay_outcomes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), unique=True, index=True)

    # отзыв: none | asked | received | refused
    review_status: Mapped[str] = mapped_column(String(16), default="none")
    review_note: Mapped[str] = mapped_column(Text, default="")

    # чек: none | issued | not_needed
    receipt_status: Mapped[str] = mapped_column(String(16), default="none")
    receipt_note: Mapped[str] = mapped_column(Text, default="")

    # залог: none | held | returned | withheld
    deposit_status: Mapped[str] = mapped_column(String(16), default="none")
    deposit_amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    deposit_returned_cents: Mapped[int] = mapped_column(Integer, default=0)
    deposit_reason: Mapped[str] = mapped_column(Text, default="")

    # конфликт: none | guest | client | both
    conflict_side: Mapped[str] = mapped_column(String(16), default="none")
    conflict_note: Mapped[str] = mapped_column(Text, default="")
    conflict_resolution: Mapped[str] = mapped_column(Text, default="")

    reminded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


class GuestRegistration(Base):
    """Учёт постановки гостей на регистрацию в МВД.

    Данные документа и сканы здесь намеренно не хранятся: подача идёт через внешний
    сервис, а у нас остаётся только след — кто, когда и в каком статусе подан.
    """

    __tablename__ = "guest_registrations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    birth_date: Mapped[str] = mapped_column(String(20), default="")
    citizenship: Mapped[str] = mapped_column(String(80), default="РФ")

    # not_required | to_do | submitted | confirmed
    status: Mapped[str] = mapped_column(String(16), default="to_do")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submitted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Scenario(Base):
    """Ситуация из общей библиотеки: одна на всех, ведём мы.

    Шаблон ответа с подстановками вида {parking} — факты берутся из knowledge объекта.
    Политика (можно ли отвечать автоматически) остаётся кодом, а не текстом: иначе
    первый же админ напишет «вернём деньги» и создаст обязательство от лица собственника.
    """

    __tablename__ = "scenarios"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    question_class: Mapped[str] = mapped_column(String(16), default="typical")
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    template: Mapped[str] = mapped_column(Text, default="")
    needs: Mapped[list] = mapped_column(JSON, default=list)  # какие факты объекта нужны шаблону
    sort: Mapped[int] = mapped_column(Integer, default=100)


class PropertyAnswer(Base):
    """Что отвечает конкретный объект: свой текст поверх ситуации или своя ситуация."""

    __tablename__ = "property_answers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), index=True)
    scenario_code: Mapped[str] = mapped_column(String(64), default="", index=True)
    title: Mapped[str] = mapped_column(String(200), default="")   # для своих ситуаций
    keywords: Mapped[list] = mapped_column(JSON, default=list)    # для своих ситуаций
    question_class: Mapped[str] = mapped_column(String(16), default="typical")
    text: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
