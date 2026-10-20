"""Сообщения гостя, пришедшие письмом с площадки.

Единственная площадка, которая отдаёт чат с гостем по почте, — Яндекс
Путешествия. В письме «Новое сообщение от гостя по бронированию «YA-…»»
лежит текст гостя, а в заголовке `Reply-To` — адрес робота, через который
ответ возвращается в чат Экстранета. Площадка сама пишет об этом в теле:
«Чтобы отправить сообщение в чат, ответьте на письмо над этой линией».

Важная деталь: внизу письма лежит `[[CHAT_TOKEN:…]]` — по нему робот находит
нужный чат. Поэтому отвечать надо письмом-ответом с цитатой оригинала,
а не новым письмом на тот же адрес. Тело письма сохраняем целиком (`quote`).

Суточно и Авито в своих письмах текста гостя не дают — только «у вас новое
сообщение» и ссылку в кабинет, поэтому отвечать там нечему. Проверено 06.10.2026.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# «27 июля 2026 16:07 (местное время)» — строка-отбивка перед каждой репликой.
STAMP = re.compile(r"^\d{1,2}\s+[А-Яа-яёЁ]+\s+\d{4}\s+\d{1,2}:\d{2}\s*\(местное время\)$")
BOOKING = re.compile(r"(YA-[\d-]+(?::\d+)?)")
# Ниже этих строк начинается подвал письма, реплик гостя там уже нет.
FOOTER = (
    "Вы можете ответить",
    "Перейти в чат",
    "Подробнее о бронировании",
    "Номер бронирования",
)
SIGNAL = "новое сообщение от гостя"


@dataclass
class GuestMail:
    platform: str
    external_id: str              # номер брони площадки, он же в теме письма
    text: str                     # что написал гость, одной или несколькими репликами
    reply_to: str                 # адрес робота площадки — куда уходит ответ
    subject: str
    message_id: str
    quote: str = field(default="")  # оригинальное тело: в нём CHAT_TOKEN


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_yandex(subject: str, body: str, *, reply_to: str = "", message_id: str = "") -> GuestMail | None:
    """Разобрать письмо Яндекса о сообщении гостя. None — это не оно."""
    if SIGNAL not in (subject + " " + body).lower():
        return None

    number = BOOKING.search(subject) or BOOKING.search(body)
    replies: list[str] = []
    expect_text = False
    for line in _lines(body):
        if any(line.startswith(mark) for mark in FOOTER):
            break
        if STAMP.match(line):
            expect_text = True        # следующая строка — реплика гостя
            continue
        if expect_text:
            replies.append(line)
            expect_text = False       # одна реплика на одну отбивку

    text = "\n".join(replies).strip()
    if not text:
        return None                   # письмо есть, а сообщения в нём нет — не наш случай

    return GuestMail(
        platform="yandex",
        external_id=number.group(1) if number else "",
        text=text,
        reply_to=reply_to,
        subject=subject,
        message_id=message_id,
        quote=body,
    )


def parse(subject: str, body: str, *, reply_to: str = "", message_id: str = "") -> GuestMail | None:
    """Разобрать письмо с сообщением гостя. Пока умеем только Яндекс."""
    return parse_yandex(subject, body, reply_to=reply_to, message_id=message_id)
