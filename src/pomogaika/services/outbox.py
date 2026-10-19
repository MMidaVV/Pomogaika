"""Доставка готового ответа гостю — в тот канал, из которого он пришёл.

Telegram остаётся за ботом: там нужен живой экземпляр `Bot`. Почта площадки
уходит отсюда, потому что ответ может отправить и бот, и мини-апп, и
автоответ из фонового цикла — транспорт у всех трёх один.
"""

from __future__ import annotations

import logging

from pomogaika.models import MessageDraft
from pomogaika.platforms import mailsend

log = logging.getLogger("pomogaika.outbox")

MAIL_PREFIX = "mail"


def is_mail(draft: MessageDraft) -> bool:
    """Черновик пришёл письмом площадки — значит и ответ уходит письмом."""
    return (draft.channel or "").startswith(MAIL_PREFIX)


def can_send_mail(draft: MessageDraft) -> bool:
    return bool(draft.reply_to) and mailsend.configured()


def send_mail_draft(draft: MessageDraft, text: str) -> None:
    """Ответить гостю письмом. Исключение наверх — отправку нельзя потерять молча."""
    mailsend.send_reply(
        draft.reply_to,
        draft.mail_subject,
        text,
        in_reply_to=draft.mail_message_id,
        quote=draft.mail_quote,
    )
    log.info("черновик %s ушёл гостю письмом на %s", draft.id, draft.reply_to)
