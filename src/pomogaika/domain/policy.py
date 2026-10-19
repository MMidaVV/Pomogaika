"""Политики как код. LLM сюда не ходит."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pomogaika.config import settings

DEFAULTS: dict[str, str] = {
    "typical_reply": "confirm",
    "door_code": "confirm",
    "close_dates": "auto",
    "cleaner_task": "auto",
    "extra_people": "confirm",
    "upsell": "auto",
    "refund": "never",
    "cancel": "never",
}

CLASS_TO_ACTION = {
    "typical": "typical_reply",
    "money": "refund",
    "conflict": "extra_people",
    "other": "typical_reply",
}


@dataclass(frozen=True)
class Decision:
    mode: str
    action: str
    reason: str
    allowed: bool


def check(action: str, context: dict | None = None) -> Decision:
    context = context or {}
    mode = DEFAULTS.get(action, "confirm")

    # COPILOT_AUTOSEND — временный режим «отвечает сама»: обычные вопросы уходят
    # гостю без кнопки собственника. Деньги, возвраты, отмены, жалобы и коды
    # от двери остаются человеку независимо от этого переключателя.
    if settings.copilot_autosend and action == "typical_reply":
        mode = "auto"
    if context.get("property_mode") == "copilot_locked" and action == "typical_reply":
        mode = "confirm"
        return Decision(mode, action, "УК: ответы гостю всегда с подтверждением", True)

    if action == "door_code":
        paid = bool(context.get("paid"))
        now: datetime = context.get("now") or datetime.utcnow()
        check_in: datetime | None = context.get("check_in")
        check_out: datetime | None = context.get("check_out")
        if not paid:
            return Decision("never", action, "Код только для оплаченной брони", False)
        if check_in and now < check_in - timedelta(hours=2):
            return Decision("confirm", action, "Рано: код не раньше чем за 2 часа до заезда", False)
        if check_out and now >= check_out:
            return Decision("never", action, "Бронь уже закончилась", False)
        return Decision("confirm", action, "Оплачено, окно заезда открыто — собственник подтверждает код", True)

    if action == "cancel" or action == "refund":
        return Decision("never", action, "Только человек: отмены, возвраты и скидки", False)

    allowed = mode != "never"
    reasons = {
        "typical_reply": ("Типовой ответ ушёл сам — включён COPILOT_AUTOSEND"
                          if settings.copilot_autosend else
                          "Типовой ответ — копайлот, ждём кнопку собственника"),
        "close_dates": "Закрытие дат на других площадках — всегда авто",
        "cleaner_task": "Задача клинеру после выезда — всегда авто",
        "extra_people": "Жалоба или лишние люди — подтверждение",
        "upsell": "Оффер можно, лимит 1–2 за проживание",
    }
    return Decision(mode, action, reasons.get(action, ""), allowed)


def action_for_question_class(question_class: str) -> str:
    return CLASS_TO_ACTION.get(question_class, "typical_reply")
