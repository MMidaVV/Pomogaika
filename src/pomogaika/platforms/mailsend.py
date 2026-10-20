"""Отправка письма гостю — обратная сторона `platforms/mailbox.py`.

Нужна ровно для одного: ответить на письмо робота площадки, чтобы текст
доехал до гостя в чат. Поэтому письмо должно быть именно ответом: та же тема,
`In-Reply-To` на исходное письмо и цитата оригинала под нашим текстом —
в цитате лежит `[[CHAT_TOKEN:…]]`, по которому робот находит чат.

Логин и пароль по умолчанию берём те же, что для чтения: у mail.ru один
пароль приложения на IMAP и SMTP. Хост тоже выводим из imap-хоста
(`imap.mail.ru` → `smtp.mail.ru`), чтобы не держать в .env два почти
одинаковых значения.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from pomogaika.config import settings

log = logging.getLogger("pomogaika.mailsend")


def host() -> str:
    if settings.smtp_host:
        return settings.smtp_host
    imap = (settings.imap_host or "").strip()
    return "smtp." + imap[5:] if imap.startswith("imap.") else ""


def login() -> str:
    return settings.smtp_user or settings.imap_user


def _secret() -> str:
    return settings.smtp_password or settings.imap_password


def configured() -> bool:
    return bool(host() and login() and _secret())


def reply_subject(subject: str) -> str:
    subject = (subject or "").strip()
    if not subject:
        return "Re:"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def build(to: str, subject: str, body: str, *, in_reply_to: str = "", quote: str = "") -> EmailMessage:
    """Собрать письмо-ответ. Отдельно от отправки, чтобы проверять в тестах."""
    message = EmailMessage()
    message["From"] = login()
    message["To"] = to
    message["Subject"] = reply_subject(subject)
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    # Наш текст — сверху, цитата — под ним: площадка берёт то, что над линией.
    message.set_content(f"{body}\n\n{quote}" if quote else body)
    return message


def send_reply(to: str, subject: str, body: str, *, in_reply_to: str = "", quote: str = "") -> None:
    """Отправить ответ. Исключение наверх: отправку нельзя «тихо не сделать»."""
    if not configured():
        raise RuntimeError("SMTP не настроен: нужны SMTP_HOST/SMTP_USER/SMTP_PASSWORD или IMAP-доступы")
    if not to:
        raise RuntimeError("нет адреса для ответа")
    message = build(to, subject, body, in_reply_to=in_reply_to, quote=quote)
    with smtplib.SMTP_SSL(host(), int(settings.smtp_port or 465), timeout=30) as server:
        server.login(login(), _secret())
        server.send_message(message)
    log.info("ответ отправлен на %s, тема %r", to, message["Subject"])
