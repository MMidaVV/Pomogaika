"""Telegram: официальный Bot API. Гости — через Telegram Business (Premium), не userbot."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BusinessConnection,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from pomogaika.bots.cleaner import router as cleaner_router
from pomogaika.config import settings
from pomogaika.db import SessionLocal
from pomogaika.domain.dates import fmt_span, parse_close_command, parse_open_command
from pomogaika.models import Block, Booking, BookingConflict, Invite, Listing, MessageDraft, Owner, Property, TelegramLink
from pomogaika.platforms.ical import fetch_ics
from pomogaika.services.bookings import (
    blocks_in_range,
    close_dates,
    open_dates,
    dismiss_conflict,
    ingest_ics,
    owner_cancel_in_calendar,
    set_listing_ical,
)
from pomogaika.services.access import (
    accept_invite,
    create_invite,
    people as access_people,
    revoke as access_revoke,
    staff_ids,
)
from pomogaika.services.cleaning import create_cleaning_task, owner_report
from pomogaika.services.codes import bookings_in_code_window, door_decision, issue_code
from pomogaika.services.copilot import classify_and_draft
from pomogaika.services.drafts import apply_draft_action, save_guest_draft
from pomogaika.services.guest import bind_guest, upcoming_for_link
from pomogaika.services.knowledge import apply_knowledge_edit, dump_knowledge, parse_knowledge_edit
from pomogaika.services.notify import notify_chat, notify_guest
from pomogaika.services.outbox import can_send_mail, is_mail, send_mail_draft
from pomogaika.services.seed import audit
from pomogaika.services.watch import tick

log = logging.getLogger("pomogaika.telegram")
router = Router()
ICAL_SET = re.compile(r"^ical(?:\s+([а-яёa-z]+))?\s*:\s*(https?://\S+)", re.IGNORECASE)
ICAL_BARE = re.compile(r"^ical\s+(https?://\S+)", re.IGNORECASE)


class OwnerStates(StatesGroup):
    editing = State()
    answering = State()
    issuing_code = State()


def _session():
    return SessionLocal()


def _is_owner(user_id: int) -> bool:
    """Пульт открыт собственнику и тем, кого он пригласил (см. services/access.py)."""
    if settings.telegram_owner_user_id and user_id == settings.telegram_owner_user_id:
        return True
    session = _session()
    try:
        return user_id in staff_ids(session)
    finally:
        session.close()


def _business_owner_id() -> int | None:
    """Чей это business-аккаунт: только его собственные сообщения пропускаем.

    Раньше здесь стоял `_is_owner`, и бот молча игнорировал любого, у кого есть
    доступ в мини-апп. Человек с доступом, написавший как гость, не получал
    ни ответа, ни черновика, и в журнале не оставалось ни строки.
    """
    session = _session()
    try:
        row = session.query(TelegramLink).filter_by(kind="business").first()
        if row and row.owner_user_id:
            return row.owner_user_id
        return settings.telegram_owner_user_id
    finally:
        session.close()


def _display_name(user) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    if user.username:
        name = f"{name} @{user.username}".strip()
    return name[:200]


def _webapp_url() -> str:
    return (settings.telegram_webapp_url or "").strip()


def _browser_login_url() -> str:
    """Вход для компьютера — тот же сайт, но страница /login вместо мини-приложения."""
    url = _webapp_url()
    if not url.startswith("https://"):
        return ""
    return url.rsplit("/", 1)[0] + "/login"


def _owner_app_markup() -> ReplyKeyboardMarkup | None:
    url = _webapp_url()
    if not url.startswith("https://"):
        return None
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Календарь", web_app=WebAppInfo(url=url))]],
        resize_keyboard=True,
    )


def _owner_chat_id() -> int | None:
    session = _session()
    try:
        owner = session.query(Owner).first()
        if owner and owner.telegram_chat_id:
            return owner.telegram_chat_id
        # В личной переписке chat_id совпадает с user_id, поэтому известный
        # user_id — рабочий запасной адрес. Без этого карточка с черновиком
        # молча пропадала, если собственник ни разу не нажимал /start.
        if owner and owner.telegram_user_id:
            return owner.telegram_user_id
        return settings.telegram_owner_user_id
    finally:
        session.close()


def _keyboard(
    draft_id: int,
    bind_rows: list | None = None,
    *,
    issue_code_booking: int | None = None,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="Отправить", callback_data=f"send:{draft_id}"),
            InlineKeyboardButton(text="Поправить", callback_data=f"edit:{draft_id}"),
        ],
        [InlineKeyboardButton(text="Ответить самой", callback_data=f"self:{draft_id}")],
    ]
    if issue_code_booking:
        rows.append(
            [InlineKeyboardButton(text="Выдать код", callback_data=f"code:{issue_code_booking}")]
        )
    if bind_rows:
        rows.extend(bind_rows)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _bind_rows(draft_id: int, bookings: list[Booking]) -> list:
    return [
        [
            InlineKeyboardButton(
                text=f"Это {b.guest_name} {b.check_in:%d.%m}–{b.check_out:%d.%m}",
                callback_data=f"bind:{draft_id}:{b.id}",
            )
        ]
        for b in bookings
    ]


def _card(draft: MessageDraft, result_reason: str = "", booking_label: str = "", *, identified: bool = False) -> str:
    cls = {"typical": "типовой", "money": "деньги", "conflict": "конфликт", "other": "нестандарт"}.get(
        draft.question_class, draft.question_class
    )
    lines = [
        f"Гость пишет ({draft.channel})",
        "",
        f"«{draft.guest_text}»",
        "",
        f"Класс: {cls} · политика: {draft.policy_mode}"
        + (" · модель" if (draft.matched_faq or "").startswith("LLM") else " · FAQ"),
        f"Бронь: {booking_label}" if booking_label else "Бронь: не привязана — выберите, кто пишет.",
    ]
    if identified:
        lines.append("Гость опознан по коду и привязан к этой брони.")
    lines += [
        "",
        "Черновик:",
        draft.draft_text,
    ]
    if result_reason:
        lines += ["", result_reason]
    return "\n".join(lines)


async def _notify_owner(bot: Bot, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    chat_id = _owner_chat_id()
    if not chat_id:
        log.warning("Собственник ещё не нажал /start — черновик в журнале, в Telegram не ушёл")
        return
    await bot.send_message(chat_id, text, reply_markup=markup)


async def _send_to_guest(bot: Bot, draft: MessageDraft, text: str) -> None:
    # Гость из чата площадки пришёл письмом — туда же и отвечаем.
    if is_mail(draft):
        if not can_send_mail(draft):
            raise RuntimeError("нет адреса для ответа или не настроен SMTP")
        await asyncio.to_thread(send_mail_draft, draft, text)
        return
    if not draft.guest_chat_id:
        raise RuntimeError("нет чата гостя")
    kwargs: dict = {"chat_id": int(draft.guest_chat_id), "text": text}
    if draft.business_connection_id:
        kwargs["business_connection_id"] = draft.business_connection_id
    await bot.send_message(**kwargs)


@router.message(CommandStart())
async def start(message: Message) -> None:
    user = message.from_user
    if not user:
        return

    payload = (message.text or "").split(maxsplit=1)
    token = payload[1][7:] if len(payload) > 1 and payload[1].startswith("invite_") else ""
    if token:
        session = _session()
        try:
            ok, reply = accept_invite(session, token, user.id, _display_name(user))
            inviter = None
            if ok:
                invite = session.get(Invite, token)
                inviter = invite.created_by if invite else None
            session.commit()
        finally:
            session.close()
        if ok:
            browser = _browser_login_url()
            await message.answer(
                "Доступ открыт. Это пульт KaVa: календарь, задачи, ответы гостям.\n\n"
                "На телефоне: /app — мини-приложение прямо здесь.\n"
                + (f"На компьютере: {browser} — кнопка «Log in with Telegram», "
                   "войти этим же аккаунтом.\n" if browser else "")
                + "\n/status — очередь\n/доступ — кто ещё внутри",
                reply_markup=_owner_app_markup(),
            )
            if inviter and inviter != user.id:
                notify_chat(inviter, f"{_display_name(user)} принял приглашение — доступ открыт.")
        else:
            await message.answer(reply)
        return

    session = _session()
    try:
        owner = session.query(Owner).first()
        claimed = owner and owner.telegram_user_id
        is_this_owner = (
            (settings.telegram_owner_user_id and user.id == settings.telegram_owner_user_id)
            or (claimed and owner.telegram_user_id == user.id)
            or (not claimed and not settings.telegram_owner_user_id)
        )
        if is_this_owner and owner:
            owner.telegram_user_id = user.id
            owner.telegram_chat_id = message.chat.id
            session.commit()
            await message.answer(
                "Это пульт собственника KaVa.\n\n"
                "Гости пишут вам в личку — бот готовит ответ, вы жмёте кнопку.\n\n"
                "Если Telegram пишет «doesn't support secretary mode yet»:\n"
                "откройте @BotFather → /mybots → этот бот → Bot Settings → "
                "Business Mode (Secretary Mode) → Turn on.\n"
                "Потом: Настройки → Telegram Business → Чат-боты → подключить бота.\n"
                "Права: читать сообщения и отвечать.\n\n"
                "Пока Business не включён, гость может написать этому боту напрямую.\n\n"
                "Команды:\n"
                "/status · /calendar · /report — очередь и часы\n"
                "/knowledge — база; «парковка: текст» — правка\n"
                "/code — кому можно выдать код\n"
                "/checkout — выезд и задача клинеру\n"
                "/ical — ссылки календарей; «ical авито: https://...» — сохранить\n"
                "/app — календарь и площадки в мини-приложении\n"
                "«закрой 20–22 сентября» — блок дат, «открой 20–22 сентября» — снять."
            ,
                reply_markup=_owner_app_markup(),
            )
            return
    finally:
        session.close()
    await message.answer(
        "Здравствуйте, это Pomogaika, апартаменты KaVa в Зеленоградске. "
        "Напишите вопрос про заезд, парковку, Wi-Fi, море — подготовлю ответ."
    )


@router.message(Command("access"))
@router.message(F.text.lower().in_({"/доступ", "доступ"}))
async def access_list(message: Message) -> None:
    """Кто имеет доступ к пульту, и кнопки пригласить / убрать."""
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    session = _session()
    try:
        rows = access_people(session)
    finally:
        session.close()

    lines = ["Доступ к пульту:"]
    buttons: list[list[InlineKeyboardButton]] = []
    for user_id, name, removable in rows:
        lines.append(f"· {name} — {user_id}" + ("" if removable else " · убрать нельзя"))
        if removable:
            buttons.append([InlineKeyboardButton(text=f"Убрать {name}", callback_data=f"acc:del:{user_id}")])
    buttons.append([InlineKeyboardButton(text="Пригласить", callback_data="acc:new")])
    lines.append("")
    lines.append("Приглашение — одноразовая ссылка на сутки.")
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == "acc:new")
async def cb_access_new(query: CallbackQuery) -> None:
    if not query.from_user or not _is_owner(query.from_user.id):
        await query.answer("Только собственник")
        return
    session = _session()
    try:
        invite = create_invite(session, query.from_user.id)
        session.commit()
        token = invite.id
    finally:
        session.close()

    me = await query.bot.get_me()
    await query.answer()
    if query.message:
        await query.message.answer(
            f"https://t.me/{me.username}?start=invite_{token}\n\n"
            "Перешлите её человеку в личку. Ссылка откроет Telegram: он нажмёт «Запустить» — "
            "и доступ откроется.\n"
            "Одноразовая, живёт сутки. Кто первым нажал, тот и вошёл, — в общий чат не кидайте.\n\n"
            + (f"Потом на компьютере: {_browser_login_url()}, кнопка «Log in with Telegram»."
               if _browser_login_url() else "")
        )


@router.callback_query(F.data.startswith("acc:del:"))
async def cb_access_del(query: CallbackQuery) -> None:
    if not query.from_user or not _is_owner(query.from_user.id) or not query.data:
        await query.answer("Только собственник")
        return
    user_id = int(query.data.split(":")[2])
    session = _session()
    try:
        done = access_revoke(session, user_id)
        session.commit()
    finally:
        session.close()
    await query.answer("Убрала" if done else "Этого убрать нельзя")
    if done and query.message:
        await query.message.answer(f"{user_id} больше не имеет доступа. Активная сессия в браузере отвалится сразу.")


@router.message(Command("status"))
async def status(message: Message) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    session = _session()
    try:
        from pomogaika.models import BookingConflict, Task

        open_c = session.query(BookingConflict).filter_by(status="open").count()
        pending = session.query(MessageDraft).filter_by(status="pending_owner").count()
        tasks = session.query(Task).filter(Task.state.in_(["created", "assigned"])).count()
        blocks = session.query(Block).count()
        link = session.query(TelegramLink).filter_by(kind="business", enabled=True).first()
        biz = "Business подключён" if link else "Business пока нет — гости могут писать боту"
        await message.answer(
            f"{biz}\nЧерновиков ждут: {pending}\nОткрытых пересечений: {open_c}\n"
            f"Закрытых окон: {blocks}\nЗадач клинеру: {tasks}"
        )
    finally:
        session.close()


@router.message(Command("calendar"))
async def calendar(message: Message) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    session = _session()
    try:
        prop = session.query(Property).first()
        if not prop:
            await message.answer("Объект ещё не создан")
            return
        bookings = (
            session.query(Booking)
            .filter(Booking.property_id == prop.id, Booking.status != "cancelled")
            .order_by(Booking.check_in)
            .all()
        )
        blocks = session.query(Block).filter_by(property_id=prop.id).order_by(Block.start).all()
        lines = ["Календарь KaVa", ""]
        for b in bookings:
            paid = "оплачена" if b.paid else "не оплачена"
            lines.append(f"• {b.guest_name} · {b.platform} · {fmt_span(b.check_in, b.check_out)} · {paid} · {b.status}")
        if blocks:
            lines += ["", "Закрыто собственником:"]
            for block in blocks:
                lines.append(f"• {fmt_span(block.start, block.end)} — {block.reason}")
        open_c = session.query(BookingConflict).filter_by(status="open").all()
        if open_c:
            lines += ["", f"Открытых пересечений: {len(open_c)}"]
        lines += ["", "Удобнее сеткой: /app"]
        await message.answer("\n".join(lines)[:3500])
    finally:
        session.close()


@router.message(Command("app"))
async def app_cmd(message: Message) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    markup = _owner_app_markup()
    if markup:
        await message.answer("Календарь, загрузка и очередь — в мини-приложении.", reply_markup=markup)
        return
    await message.answer(
        "Календарь удобнее здесь: http://127.0.0.1:8000/app\n"
        "С телефона в Telegram нужна https-ссылка: в .env TELEGRAM_WEBAPP_URL."
    )


@router.message(Command("ical"))
async def ical_cmd(message: Message) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    session = _session()
    try:
        prop = session.query(Property).one()
        listings = session.query(Listing).filter_by(property_id=prop.id).all()
        lines = [
            "Ссылки календарей (.ics с площадки, не страница объявления).",
            "Сохранить: ical авито: https://...",
            "",
        ]
        for listing in listings:
            lines.append(f"{listing.platform}: {listing.ical_url or 'нет'}")
        to_sync = [x for x in listings if x.ical_url]
        if not to_sync:
            await message.answer("\n".join(lines))
            return
        lines += ["", "Забираю брони…"]
        for listing in to_sync:
            try:
                text = fetch_ics(listing.ical_url)
                results = ingest_ics(
                    session, prop, text, platform=listing.platform, listing_id=listing.id
                )
                lines.append(f"{listing.platform}: {len(results)} событий")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"{listing.platform}: ошибка {exc}")
        session.commit()
        await message.answer("\n".join(lines)[:3500])
    finally:
        session.close()


@router.message(Command("close"))
async def close_cmd(message: Message) -> None:
    if not message.from_user or not _is_owner(message.from_user.id) or not message.text:
        return
    text = message.text.replace("/close", "закрой", 1)
    await _owner_close(message, text)


async def _owner_close(message: Message, text: str) -> None:
    rng = parse_close_command(text)
    if not rng:
        await message.answer("Не разобрала даты. Пример: закрой 20–22 сентября")
        return
    session = _session()
    try:
        prop = session.query(Property).one()
        block, hits = close_dates(session, prop.id, rng[0], rng[1], reason=text.strip())
        session.commit()
        extra = (
            "Пересекается с: " + ", ".join(b.guest_name for b in hits)
            if hits
            else "Свободных пересечений нет."
        )
        await message.answer(
            f"Закрыла {fmt_span(block.start, block.end)}.\n{extra}\nОтмену на площадке бот не делает."
        )
    finally:
        session.close()


async def _owner_open(message: Message, text: str) -> None:
    """«открой 28–30 сентября» — снять свои закрытия в этом отрезке."""
    rng = parse_open_command(text)
    if not rng:
        await message.answer("Не разобрала даты. Пример: открой 28–30 сентября")
        return
    session = _session()
    try:
        prop = session.query(Property).one()
        found = blocks_in_range(session, prop.id, rng[0], rng[1])
        if not found:
            await message.answer(
                f"На {fmt_span(rng[0], rng[1])} закрытий нет. "
                "Если даты заняты — это бронь, её снимают на площадке."
            )
            return
        spans = [fmt_span(b.start, b.end) for b in found]
        for block in found:
            open_dates(session, block.id)
        session.commit()
        await message.answer("Открыла: " + ", ".join(spans) + ".\nДаты снова свободны в нашем календаре.")
    finally:
        session.close()


@router.message(Command("knowledge"))
async def knowledge_cmd(message: Message) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    session = _session()
    try:
        prop = session.query(Property).one()
        await message.answer(dump_knowledge(prop)[:3500])
    finally:
        session.close()


@router.message(Command("report"))
async def report_cmd(message: Message) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    session = _session()
    try:
        prop = session.query(Property).one()
        await message.answer(owner_report(session, prop)[:3500])
    finally:
        session.close()


@router.message(Command("code"))
async def code_cmd(message: Message) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    session = _session()
    try:
        prop = session.query(Property).one()
        ready = bookings_in_code_window(session, prop.id)
        if not ready:
            await message.answer("Сейчас никому нельзя выдать код: нет оплаченной брони в окне 2 часа.")
            return
        rows = [
            [InlineKeyboardButton(text=f"{b.guest_name} {b.check_in:%d.%m %H:%M}", callback_data=f"code:{b.id}")]
            for b in ready
        ]
        await message.answer(
            "Окно открыто. Цифры пришлите следующим сообщением после кнопки. В журнал уйдёт только хвост **.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    finally:
        session.close()


@router.message(Command("checkout"))
async def checkout_cmd(message: Message) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    session = _session()
    try:
        staying = (
            session.query(Booking)
            .filter(Booking.status.in_(["staying", "confirmed", "paid"]))
            .order_by(Booking.check_in)
            .all()
        )
        now = datetime.now()
        current = [b for b in staying if b.check_in <= now <= b.check_out] or staying
        if not current:
            await message.answer("Нет брони для выезда.")
            return
        booking = current[0]
        task = create_cleaning_task(session, booking)
        session.commit()
        await message.answer(f"Выезд {booking.guest_name}. Задача клинеру #{task.id}. Оплату не провожу.")
    finally:
        session.close()


@router.message(Command("paid"))
async def paid_cmd(message: Message) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    session = _session()
    try:
        rows = session.query(Booking).filter(Booking.paid.is_(False), Booking.status != "cancelled").all()
        if not rows:
            await message.answer("Все активные брони уже с отметкой оплаты.")
            return
        kb = [
            [InlineKeyboardButton(text=f"{b.guest_name} {b.check_in:%d.%m}", callback_data=f"paid:{b.id}")]
            for b in rows[:8]
        ]
        await message.answer("Отметить оплату:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    finally:
        session.close()


@router.business_connection()
async def on_business(event: BusinessConnection) -> None:
    session = _session()
    try:
        row = session.query(TelegramLink).filter_by(kind="business").first()
        if not row:
            row = TelegramLink(kind="business")
            session.add(row)
        row.business_connection_id = event.id
        row.owner_user_id = event.user.id
        row.enabled = bool(event.is_enabled)
        owner = session.query(Owner).first()
        if owner:
            owner.telegram_user_id = event.user.id
        session.commit()
    finally:
        session.close()
    log.info("Telegram Business: %s enabled=%s", event.id, event.is_enabled)


async def _handle_guest_text(
    bot: Bot,
    text: str,
    chat_id: int,
    user_id: int,
    business_connection_id: str,
    channel: str,
) -> None:
    draft_id, result = await asyncio.to_thread(
        save_guest_draft,
        text,
        guest_chat_id=str(chat_id),
        guest_user_id=str(user_id),
        business_connection_id=business_connection_id,
        channel=channel,
    )
    session = _session()
    try:
        draft = session.get(MessageDraft, draft_id)
        if not draft:
            return
        card = _card(draft, result.reason, result.booking_label, identified=result.bound_by_code)
        bind = [] if result.booking_id else _bind_rows(draft.id, upcoming_for_link(session, draft.property_id))
        markup = (
            _keyboard(draft.id, bind, issue_code_booking=result.booking_id if result.issue_code else None)
            if result.needs_owner or bind or result.issue_code
            else None
        )
        await _notify_owner(bot, card, markup)
        if not result.needs_owner:
            await _send_to_guest(bot, draft, draft.draft_text)
    finally:
        session.close()


@router.business_message(F.text)
async def guest_business(message: Message, bot: Bot) -> None:
    if not message.text or not message.from_user:
        return
    # Telegram шлёт business_message и на исходящие — свои сообщения пропускаем.
    # Все остальные, включая людей с доступом в пульт, пишут нам как гости.
    if message.from_user.id == _business_owner_id():
        log.debug("business: своё исходящее сообщение, пропускаем")
        return
    await _handle_guest_text(
        bot,
        message.text,
        message.chat.id,
        message.from_user.id,
        message.business_connection_id or "",
        "telegram_business",
    )


@router.message(F.text, F.chat.type == "private")
async def private_text(message: Message, bot: Bot, state: FSMContext) -> None:
    if not message.from_user or not message.text:
        return
    if message.text.startswith("/"):
        return
    current = await state.get_state()
    if _is_owner(message.from_user.id):
        if current == OwnerStates.editing or current == OwnerStates.answering:
            action = "edit" if current == OwnerStates.editing else "self"
            draft_id = int((await state.get_data())["draft_id"])
            session = _session()
            try:
                draft = session.get(MessageDraft, draft_id)
                if not draft:
                    await message.answer("Черновик уже не найден")
                    await state.clear()
                    return
                apply_draft_action(session, draft, action, message.text)
                session.commit()
                try:
                    await _send_to_guest(bot, draft, draft.draft_text)
                    await message.answer("Отправила гостю.")
                except Exception as exc:  # noqa: BLE001
                    await message.answer(f"Текст сохранила, но в чат гостя не ушло: {exc}")
            finally:
                session.close()
            await state.clear()
            return
        if current == OwnerStates.issuing_code:
            booking_id = int((await state.get_data())["booking_id"])
            await _owner_send_code(message, booking_id, message.text)
            await state.clear()
            return
        edited = parse_knowledge_edit(message.text)
        if edited:
            session = _session()
            try:
                prop = session.query(Property).one()
                note = apply_knowledge_edit(prop, edited[0], edited[1])
                session.commit()
                await message.answer(note)
            finally:
                session.close()
            return
        if parse_close_command(message.text):
            await _owner_close(message, message.text)
            return
        if parse_open_command(message.text):
            await _owner_open(message, message.text)
            return
        ical_set = ICAL_SET.match(message.text.strip())
        ical_bare = ICAL_BARE.match(message.text.strip())
        if ical_set or ical_bare:
            session = _session()
            try:
                prop = session.query(Property).one()
                if ical_set:
                    platform = ical_set.group(1) or ""
                    url = ical_set.group(2)
                else:
                    platform = ""
                    url = ical_bare.group(1)
                listing = set_listing_ical(session, prop, platform, url)
                session.commit()
                await message.answer(
                    f"Сохранила календарь {listing.platform}. "
                    "Напишите /ical — заберу брони. Отмену на площадке не делаю."
                )
            except ValueError as exc:
                await message.answer(str(exc))
            finally:
                session.close()
            return
        if "BEGIN:VCALENDAR" in message.text.upper():
            session = _session()
            try:
                prop = session.query(Property).one()
                results = ingest_ics(session, prop, message.text)
                session.commit()
                n = len(results)
                conflicts = sum(len(r.conflicts) for r in results)
                await message.answer(f"Приняла iCal: {n} событий, пересечений: {conflicts}.")
            finally:
                session.close()
            return
        await message.answer(
            "Это пульт. /status · /calendar · /report · /knowledge · /code · /checkout · /ical\n"
            "Закрыть даты: «закрой 20–22 сентября». База: «парковка: текст». Календарь: «ical авито: https://...»."
        )
        return

    await _handle_guest_text(
        bot,
        message.text,
        message.chat.id,
        message.from_user.id,
        "",
        "telegram_bot",
    )


@router.callback_query(F.data.startswith("send:"))
async def cb_send(query: CallbackQuery, bot: Bot) -> None:
    if not query.from_user or not _is_owner(query.from_user.id):
        await query.answer("Только собственник")
        return
    draft_id = int(query.data.split(":")[1])
    session = _session()
    try:
        draft = session.get(MessageDraft, draft_id)
        if not draft:
            await query.answer("Уже нет")
            return
        apply_draft_action(session, draft, "send")
        session.commit()
        await _send_to_guest(bot, draft, draft.draft_text)
        await query.message.edit_reply_markup(reply_markup=None)
        await query.answer("Отправлено")
        await query.message.reply("Отправила гостю.")
    except Exception as exc:  # noqa: BLE001
        await query.answer("Ошибка")
        await query.message.reply(f"Не смогла отправить гостю: {exc}")
    finally:
        session.close()


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(query: CallbackQuery, state: FSMContext) -> None:
    if not query.from_user or not _is_owner(query.from_user.id):
        await query.answer("Только собственник")
        return
    draft_id = int(query.data.split(":")[1])
    await state.set_state(OwnerStates.editing)
    await state.update_data(draft_id=draft_id)
    await query.answer()
    await query.message.reply("Пришлите исправленный текст — уйдёт гостю.")


@router.callback_query(F.data.startswith("self:"))
async def cb_self(query: CallbackQuery, state: FSMContext) -> None:
    if not query.from_user or not _is_owner(query.from_user.id):
        await query.answer("Только собственник")
        return
    draft_id = int(query.data.split(":")[1])
    await state.set_state(OwnerStates.answering)
    await state.update_data(draft_id=draft_id)
    await query.answer()
    await query.message.reply("Напишите свой ответ гостю. Черновик бота не отправлю.")


@router.callback_query(F.data.startswith("bind:"))
async def cb_bind(query: CallbackQuery) -> None:
    if not query.from_user or not _is_owner(query.from_user.id) or not query.data:
        await query.answer("Только собственник")
        return
    _, draft_id, booking_id = query.data.split(":")
    session = _session()
    try:
        draft = session.get(MessageDraft, int(draft_id))
        booking = session.get(Booking, int(booking_id))
        if not draft or not booking:
            await query.answer("Уже нет")
            return
        bind_guest(booking, draft.guest_user_id, draft.guest_chat_id, draft.business_connection_id)
        result = classify_and_draft(draft.guest_text, booking)
        draft.draft_text = result.draft
        draft.booking_id = booking.id
        draft.question_class = result.question_class
        draft.policy_mode = result.policy_mode
        draft.matched_faq = result.matched_faq
        session.commit()
        if query.message:
            await query.message.edit_text(
                _card(draft, result.reason, result.booking_label),
                reply_markup=_keyboard(
                    draft.id,
                    issue_code_booking=booking.id if result.issue_code else None,
                ),
            )
        await query.answer(f"Это {booking.guest_name}")
    finally:
        session.close()


@router.callback_query(F.data.startswith("cxd:"))
async def cb_conflict_dismiss(query: CallbackQuery) -> None:
    if not query.from_user or not _is_owner(query.from_user.id) or not query.data:
        await query.answer("Только собственник")
        return
    conflict_id = int(query.data.split(":")[1])
    session = _session()
    try:
        conflict = session.get(BookingConflict, conflict_id)
        if not conflict:
            await query.answer("Уже нет")
            return
        dismiss_conflict(session, conflict)
        session.commit()
        await query.answer("Оставила вам")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.message.reply("Разберётесь на площадках. Бот ничего не отменяет.")
    finally:
        session.close()


@router.callback_query(F.data.startswith("cx:"))
async def cb_conflict_cancel(query: CallbackQuery) -> None:
    if not query.from_user or not _is_owner(query.from_user.id) or not query.data:
        await query.answer("Только собственник")
        return
    _, conflict_id, booking_id = query.data.split(":")
    session = _session()
    try:
        booking = session.get(Booking, int(booking_id))
        if not booking:
            await query.answer("Уже нет")
            return
        owner_cancel_in_calendar(session, booking)
        session.commit()
        await query.answer("Сняла в календаре")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.message.reply(
                f"Сняла {booking.guest_name} в нашем календаре. "
                f"На {booking.platform} отмените сами — бот этого не делает."
            )
    finally:
        session.close()


async def _owner_send_code(message: Message, booking_id: int, text: str) -> None:
    session = _session()
    try:
        booking = session.get(Booking, booking_id)
        if not booking:
            await message.answer("Бронь не найдена")
            return
        ok, payload = issue_code(session, booking, text)
        session.commit()
        if not ok:
            await message.answer(payload)
            return
        chat = booking.guest_chat_id
        if not chat:
            draft = (
                session.query(MessageDraft)
                .filter(MessageDraft.booking_id == booking.id, MessageDraft.guest_chat_id != "")
                .order_by(MessageDraft.id.desc())
                .first()
            )
            if draft:
                chat = draft.guest_chat_id
                booking.guest_chat_id = chat
                booking.business_connection_id = draft.business_connection_id
                session.commit()
        if chat:
            notify_guest(chat, payload, booking.business_connection_id)
            await message.answer(f"Отправила гостю. В журнале только хвост **{booking.code_hint}.")
        else:
            await message.answer(
                f"Коды приняла, хвост **{booking.code_hint}. Гостевого чата нет — отправьте сами на площадке."
            )
    finally:
        session.close()


@router.callback_query(F.data.startswith("code:"))
async def cb_code(query: CallbackQuery, state: FSMContext) -> None:
    if not query.from_user or not _is_owner(query.from_user.id) or not query.data:
        await query.answer("Только собственник")
        return
    booking_id = int(query.data.split(":")[1])
    session = _session()
    try:
        booking = session.get(Booking, booking_id)
        if not booking:
            await query.answer("Уже нет")
            return
        decision = door_decision(booking)
        if not decision.allowed:
            await query.answer()
            if query.message:
                await query.message.reply(decision.reason)
            return
        await state.set_state(OwnerStates.issuing_code)
        await state.update_data(booking_id=booking_id)
        await query.answer()
        if query.message:
            await query.message.reply(
                f"Пришлите коды для {booking.guest_name} одним сообщением "
                "(калитка, домофон подъезда, сейф). В базу и журнал цифры не кладу — только хвост из двух знаков."
            )
    finally:
        session.close()


@router.callback_query(F.data.startswith("out:"))
async def cb_checkout(query: CallbackQuery) -> None:
    """Кнопка под уведомлением о выезде: задача клинеру одним нажатием."""
    if not query.from_user or not _is_owner(query.from_user.id) or not query.data:
        await query.answer("Только собственник")
        return
    booking_id = int(query.data.split(":")[1])
    session = _session()
    try:
        booking = session.get(Booking, booking_id)
        if not booking:
            await query.answer("Брони уже нет")
            return
        task = create_cleaning_task(session, booking)
        booking.checked_out_at = booking.checked_out_at or datetime.utcnow()
        session.commit()
        await query.answer("Задача клинеру поставлена")
        if query.message:
            await query.message.answer(
                f"Выезд {booking.guest_name} отмечен. Задача клинеру #{task.id}.\n"
                "Через сутки напомню закрыть итог проживания."
            )
    finally:
        session.close()


@router.callback_query(F.data.startswith("paid:"))
async def cb_paid(query: CallbackQuery) -> None:
    if not query.from_user or not _is_owner(query.from_user.id) or not query.data:
        await query.answer("Только собственник")
        return
    booking_id = int(query.data.split(":")[1])
    session = _session()
    try:
        booking = session.get(Booking, booking_id)
        if not booking:
            await query.answer("Уже нет")
            return
        booking.paid = True
        if booking.status == "pending":
            booking.status = "confirmed"
        audit(session, "booking.paid", f"Оплата {booking.guest_name}", "owner", "owner", "confirm")
        session.commit()
        await query.answer("Оплачено")
        if query.message:
            await query.message.reply(f"{booking.guest_name}: оплата отмечена. Код всё ещё только в окне 2 часа.")
    finally:
        session.close()


async def _watch_loop() -> None:
    while True:
        await asyncio.sleep(40)
        session = _session()
        try:
            tick(session)
            session.commit()
        except Exception:
            log.exception("watch")
            session.rollback()
        finally:
            session.close()


async def run_bot() -> None:
    token = (settings.telegram_bot_token or "").strip()
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN пуст — бот не запускается")
        return
    bot = Bot(token)
    dp = Dispatcher()
    dp.include_router(cleaner_router)
    dp.include_router(router)
    url = _webapp_url()
    if url.startswith("https://"):
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Календарь", web_app=WebAppInfo(url=url))
            )
        except Exception:
            log.exception("не выставила кнопку меню Mini App")
    log.info("Telegram-бот запущен (polling)")
    watch = asyncio.create_task(_watch_loop(), name="pomogaika-watch")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        watch.cancel()


def start_bot_background() -> asyncio.Task | None:
    token = (settings.telegram_bot_token or "").strip()
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN пуст — бот не запускается. См. docs/telegram.md")
        return None
    return asyncio.create_task(run_bot(), name="pomogaika-telegram")
