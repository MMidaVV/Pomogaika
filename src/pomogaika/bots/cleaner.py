"""Клинер в том же боте, другой /start. Оплату не проводим."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from pomogaika.config import settings
from pomogaika.db import SessionLocal
from pomogaika.models import Contractor, Owner, Task
from pomogaika.services.cleaning import accept_task, add_photo, complete_task, snooze_task, task_card

log = logging.getLogger("pomogaika.cleaner")
router = Router()


class CleanerStates(StatesGroup):
    snoozing = State()
    blocker = State()


def _session():
    return SessionLocal()


def _is_owner(user_id: int) -> bool:
    if settings.telegram_owner_user_id and user_id == settings.telegram_owner_user_id:
        return True
    session = _session()
    try:
        owner = session.query(Owner).first()
        return bool(owner and owner.telegram_user_id == user_id)
    finally:
        session.close()


def is_cleaner(user_id: int) -> bool:
    if _is_owner(user_id):
        return False
    if settings.telegram_cleaner_user_id and user_id == settings.telegram_cleaner_user_id:
        return True
    session = _session()
    try:
        row = session.query(Contractor).filter_by(role="cleaner", telegram_user_id=user_id).first()
        return bool(row)
    finally:
        session.close()


class IsCleaner(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return bool(user and is_cleaner(user.id))


def _kb(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Принять", callback_data=f"clok:{task_id}")],
            [InlineKeyboardButton(text="Перенос", callback_data=f"clwait:{task_id}")],
            [InlineKeyboardButton(text="Готово", callback_data=f"cldone:{task_id}")],
        ]
    )


@router.message(Command("cleaner"))
async def join_cleaner(message: Message) -> None:
    if not message.from_user:
        return
    if _is_owner(message.from_user.id):
        await message.answer(
            "Вы в пульте собственника. Клинер — другой аккаунт: пусть напишет боту /cleaner.\n"
            "На вебе у задачи есть кнопки приёмки — так можно показать без второго телефона."
        )
        return
    session = _session()
    try:
        row = session.query(Contractor).filter_by(role="cleaner").first()
        if not row:
            await message.answer("Клинер в базе ещё не заведён.")
            return
        row.telegram_user_id = message.from_user.id
        row.telegram_chat_id = message.chat.id
        session.commit()
        await message.answer(
            "Это бот задач KaVa, Гагарина 87 кв. 19.\n"
            "Пришлю уборку одним сообщением: адрес, время, чек-лист.\n"
            "Принять / перенос свободным текстом / фото по пунктам.\n"
            "Вердикт «грязно» не ставлю. Оплату здесь не провожу."
        )
    finally:
        session.close()


@router.callback_query(IsCleaner(), F.data.startswith("clok:"))
async def cb_accept(query: CallbackQuery) -> None:
    if not query.from_user or not is_cleaner(query.from_user.id) or not query.data:
        await query.answer("Только клинер")
        return
    task_id = int(query.data.split(":")[1])
    session = _session()
    try:
        task = session.get(Task, task_id)
        if not task:
            await query.answer("Уже нет")
            return
        text = accept_task(session, task)
        session.commit()
        await query.answer("Принято")
        if query.message:
            await query.message.answer(text, reply_markup=_kb(task.id))
    finally:
        session.close()


@router.callback_query(IsCleaner(), F.data.startswith("clwait:"))
async def cb_wait(query: CallbackQuery, state: FSMContext) -> None:
    if not query.from_user or not is_cleaner(query.from_user.id) or not query.data:
        await query.answer("Только клинер")
        return
    await state.set_state(CleanerStates.snoozing)
    await state.update_data(task_id=int(query.data.split(":")[1]))
    await query.answer()
    if query.message:
        await query.message.answer("Напишите, когда будете. Например: пробка, буду через час.")


@router.callback_query(IsCleaner(), F.data.startswith("cldone:"))
async def cb_done(query: CallbackQuery, state: FSMContext) -> None:
    if not query.from_user or not is_cleaner(query.from_user.id) or not query.data:
        await query.answer("Только клинер")
        return
    await state.set_state(CleanerStates.blocker)
    await state.update_data(task_id=int(query.data.split(":")[1]))
    await query.answer()
    if query.message:
        await query.message.answer("Готово. Что мешало на объекте? Можно «ничего».")


@router.message(IsCleaner(), F.photo)
async def cleaner_photo(message: Message) -> None:
    if not message.from_user or not is_cleaner(message.from_user.id):
        return
    session = _session()
    try:
        task = (
            session.query(Task)
            .filter(Task.type == "cleaning", Task.state.in_(["created", "accepted", "in_progress", "snoozed"]))
            .order_by(Task.id.desc())
            .first()
        )
        if not task:
            await message.answer("Нет открытой уборки.")
            return
        text = add_photo(session, task)
        session.commit()
        await message.answer(text, reply_markup=_kb(task.id))
    finally:
        session.close()


@router.message(IsCleaner(), F.text, F.chat.type == "private")
async def cleaner_text(message: Message, state: FSMContext) -> None:
    if not message.from_user or not message.text or not is_cleaner(message.from_user.id):
        return
    current = await state.get_state()
    if current not in {CleanerStates.snoozing, CleanerStates.blocker}:
        return
    session = _session()
    try:
        task_id = int((await state.get_data())["task_id"])
        task = session.get(Task, task_id)
        if not task:
            await message.answer("Задача уже не найдена")
            await state.clear()
            return
        if current == CleanerStates.snoozing:
            await message.answer(snooze_task(session, task, message.text))
        else:
            await message.answer(complete_task(session, task, message.text))
        session.commit()
    finally:
        session.close()
    await state.clear()
