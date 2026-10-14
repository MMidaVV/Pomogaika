"""Чтение почты площадок по IMAP.

iCal отдаёт только даты — имени гостя, суммы и номера брони в нём нет. Эти данные
есть в письмах, которые площадки шлют о новой брони, а разбирать их мы уже умеем
(platforms/email_parser.py). Здесь — только доставка писем до парсера.

Читаем непрочитанные письма, разбираем, помечаем прочитанными. Ничего не удаляем:
если мы что-то разобрали неправильно, письмо остаётся в ящике.
"""

from __future__ import annotations

import email
import html
import imaplib
import re
import logging
from dataclasses import dataclass
from email.header import decode_header, make_header

from pomogaika.config import settings

log = logging.getLogger("pomogaika.mailbox")
NEWLINE = chr(10)

# Письма площадок приходят с этих доменов. Остальное не трогаем совсем —
# в ящике может лежать личная переписка, и читать её нам незачем.
PLATFORM_SENDERS = (
    "avito.ru",
    "sutochno.ru",
    "ostrovok.ru",
    "emailing.yandex.ru",
    "travel.yandex.ru",
    "yandex.ru",
)


@dataclass
class Letter:
    uid: str
    sender: str
    subject: str
    body: str
    # Адрес для ответа. У писем-уведомлений это noreply, но если площадка
    # пересылает сообщение гостя, здесь лежит её relay — обратный канал к гостю.
    reply_to: str = ""
    recipient: str = ""
    message_id: str = ""


def configured() -> bool:
    return bool(settings.imap_host and settings.imap_user and settings.imap_password)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 — кривой заголовок не повод падать
        return value


def _body(message: email.message.Message) -> str:
    if not message.is_multipart():
        payload = message.get_payload(decode=True) or b""
        body = payload.decode(message.get_content_charset() or "utf-8", "replace")
        return strip_html(body) if "<" in body else body

    chunks: list[str] = []
    for part in message.walk():
        if part.get_content_type() != "text/plain":
            continue
        payload = part.get_payload(decode=True) or b""
        chunks.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
    if chunks:
        return "\n".join(chunks)

    for part in message.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True) or b""
            return strip_html(payload.decode(part.get_content_charset() or "utf-8", "replace"))
    return ""


def strip_html(raw: str) -> str:
    """Письма площадок свёрстаны таблицами: тег превращаем в перенос строки.

    Без этого в данные попадал мусор — в имя гостя однажды приехало «</td>».
    """
    text = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", NEWLINE, text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\u00a0\u2007\u202f]{2,}", " ", text)
    return re.sub(r"\n{3,}", NEWLINE * 2, text)


def _is_platform(sender: str) -> bool:
    low = sender.lower()
    return any(domain in low for domain in PLATFORM_SENDERS)


def fetch_unseen(limit: int = 20, *, unseen_only: bool = True) -> list[Letter]:
    """Письма от площадок. Прочитанными помечает mark_seen, не мы.

    unseen_only=False — разовый проход по истории: при первом подключении ящика
    все письма уже прочитаны, и иначе мы не увидим ни одной существующей брони.
    """
    if not configured():
        return []

    letters: list[Letter] = []
    box = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        box.login(settings.imap_user, settings.imap_password)
        box.select(settings.imap_folder)
        status, data = box.search(None, "UNSEEN" if unseen_only else "ALL")
        if status != "OK":
            return []
        uids = (data[0] or b"").split()[-limit:]
        for uid in uids:
            status, raw = box.fetch(uid, "(BODY.PEEK[])")   # PEEK — не помечаем прочитанным
            if status != "OK" or not raw or not raw[0]:
                continue
            message = email.message_from_bytes(raw[0][1])
            sender = _decode(message.get("From"))
            if not _is_platform(sender):
                continue                                    # чужие письма не читаем
            letters.append(Letter(
                uid=uid.decode(),
                sender=sender,
                subject=_decode(message.get("Subject")),
                body=_body(message),
                reply_to=_decode(message.get("Reply-To")),
                recipient=_decode(message.get("To")),
                message_id=_decode(message.get("Message-ID")),
            ))
    finally:
        try:
            box.logout()
        except Exception:  # noqa: BLE001
            pass
    return letters


def mark_seen(uids: list[str]) -> None:
    """Помечаем прочитанными только то, что удалось разобрать."""
    if not configured() or not uids:
        return
    box = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        box.login(settings.imap_user, settings.imap_password)
        box.select(settings.imap_folder)
        for uid in uids:
            box.store(uid.encode(), "+FLAGS", "\\Seen")
    finally:
        try:
            box.logout()
        except Exception:  # noqa: BLE001
            pass


def check() -> tuple[bool, str]:
    """Проверка настроек: пускает ли ящик и сколько непрочитанных."""
    if not configured():
        return False, "не заданы IMAP_HOST, IMAP_USER или IMAP_PASSWORD"
    try:
        box = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
        box.login(settings.imap_user, settings.imap_password)
        box.select(settings.imap_folder)
        status, data = box.search(None, "UNSEEN")
        count = len((data[0] or b"").split()) if status == "OK" else 0
        box.logout()
        return True, f"ящик отвечает, непрочитанных писем: {count}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
