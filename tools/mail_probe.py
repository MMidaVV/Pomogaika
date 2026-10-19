"""Разведка ящика: можно ли отвечать гостю письмом, без API площадок.

Проверяем одну гипотезу: пересылают ли Суточно, Островок, Яндекс и Авито
сообщения гостя на почту собственника, и есть ли в таком письме адрес для
ответа, который доедет до гостя (relay вида `reply+<id>@...`).

Если relay есть — обратный канал к гостю у нас уже есть, API ждать не нужно.
Если во всех письмах стоит noreply — гипотеза закрыта, и мы это знаем за час.

Скрипт ничего не меняет: ящик открывается только для чтения (readonly),
письма не помечаются прочитанными, тела не читаются вообще — только заголовки.

Запуск:  python tools/mail_probe.py              (из корня проекта)
         python tools/mail_probe.py --limit 500 --folder INBOX
         python tools/mail_probe.py --folders     (показать список папок)

Нужны IMAP_HOST / IMAP_USER / IMAP_PASSWORD в .env — те же, что на сервере.
"""

from __future__ import annotations

import argparse
import email
import imaplib
import re
import sys
from collections import Counter
from email.utils import parseaddr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:                                      # в консоли Windows иначе ломается кириллица
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from pomogaika.config import settings                                  # noqa: E402
from pomogaika.platforms.mailbox import PLATFORM_SENDERS, _decode, _is_platform  # noqa: E402

HEADERS = "DATE FROM TO SUBJECT REPLY-TO RETURN-PATH LIST-UNSUBSCRIBE MESSAGE-ID IN-REPLY-TO"

# Письмо похоже на пересланное сообщение гостя, а не на уведомление о брони.
GUEST_WORDS = (
    "сообщен", "вопрос", "спрашива", "уточн", "написал", "пишет", "чат",
    "переписк", "ответьте", "ждёт ответа", "ждет ответа", "message", "запрос",
)
BOOKING_WORDS = ("брон", "заезд", "выезд", "оплат", "выплат", "booking", "отмен", "подтверж")

NOREPLY = ("noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "notify@", "notification")
# Адреса поддержки и модерации площадки. Ответ туда доедет до людей площадки,
# а не до гостя — для нашей задачи это не канал, и путать их нельзя.
SUPPORT = ("support", "moderation", "partner", "help", "team@", "info@")
# Relay-адрес обычно несёт в себе идентификатор треда: reply+a1b2c3@, 12345-67@, u-xxxx@
RELAY_SHAPE = re.compile(r"[+._-][0-9a-f]{6,}|\d{6,}")


def kind(subject: str) -> str:
    low = subject.lower()
    if any(w in low for w in GUEST_WORDS):
        return "сообщение"
    if any(w in low for w in BOOKING_WORDS):
        return "бронь"
    return "прочее"


def domain(addr: str) -> str:
    _, mail = parseaddr(addr)
    return mail.split("@")[-1].lower() if "@" in mail else ""


def is_noreply(addr: str) -> bool:
    _, mail = parseaddr(addr)
    return any(w in mail.lower() for w in NOREPLY)


def is_support(addr: str) -> bool:
    _, mail = parseaddr(addr)
    return any(w in mail.lower() for w in SUPPORT)


def reply_target(reply_to: str, sender: str) -> tuple[str, str]:
    """Куда уйдёт ответ на письмо и чего от этого ждать.

    Состояния: relay — похоже на обратный канал к гостю; поддержка — адрес
    площадки для партнёров; from — заголовка Reply-To нет, ответ уйдёт
    отправителю, доедет ли он до гостя проверяется только живым письмом;
    noreply — отвечать некуда.
    """
    _, reply_mail = parseaddr(reply_to)
    _, from_mail = parseaddr(sender)
    if reply_mail:
        if is_noreply(reply_mail):
            return reply_mail, "noreply"
        if is_support(reply_mail):
            return reply_mail, "поддержка"
        if reply_mail.lower() == from_mail.lower():
            return reply_mail, "from"
        return reply_mail, "relay"
    if is_noreply(from_mail):
        return from_mail, "noreply"
    return from_mail, "from"


def show_folders(box: imaplib.IMAP4_SSL) -> None:
    status, data = box.list()
    print("Папки ящика:")
    for row in data or []:
        if isinstance(row, bytes):
            print("  ", _decode(row.decode("utf-8", "replace")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=300, help="сколько последних писем смотреть")
    ap.add_argument("--folder", default=settings.imap_folder or "INBOX")
    ap.add_argument("--folders", action="store_true", help="только показать список папок")
    args = ap.parse_args()

    if not (settings.imap_host and settings.imap_user and settings.imap_password):
        print("IMAP не настроен. Нужны в .env:")
        print("  IMAP_HOST=imap.mail.ru")
        print("  IMAP_USER=kava7@list.ru")
        print("  IMAP_PASSWORD=<пароль приложения>")
        return 2

    print(f"Ящик {settings.imap_user}, папка {args.folder}, последние {args.limit} писем.")
    print("Режим только для чтения: флаги не меняются, тела писем не читаются.\n")

    box = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    rows: list[dict] = []
    other_domains: Counter[str] = Counter()
    try:
        box.login(settings.imap_user, settings.imap_password)
        if args.folders:
            show_folders(box)
            return 0
        box.select(args.folder, readonly=True)      # readonly — ящик не трогаем
        status, data = box.search(None, "ALL")
        if status != "OK":
            print("Поиск не удался:", status)
            return 1
        uids = (data[0] or b"").split()[-args.limit:]
        print(f"Всего писем в выборке: {len(uids)}\n")

        for uid in uids:
            status, raw = box.fetch(uid, f"(BODY.PEEK[HEADER.FIELDS ({HEADERS})])")
            if status != "OK" or not raw or not raw[0]:
                continue
            head = email.message_from_bytes(raw[0][1])
            sender = _decode(head.get("From"))
            if not _is_platform(sender):
                # чужие письма не разбираем — только считаем домены,
                # чтобы не пропустить relay на незнакомом домене
                dom = domain(sender)
                if dom:
                    other_domains[dom] += 1
                continue
            rows.append({
                "date": _decode(head.get("Date"))[:31],
                "from": parseaddr(sender)[1],
                "domain": domain(sender),
                "subject": _decode(head.get("Subject")),
                "reply_to": parseaddr(_decode(head.get("Reply-To")))[1],
                "return_path": parseaddr(_decode(head.get("Return-Path")))[1],
            })
    finally:
        try:
            box.logout()
        except Exception:  # noqa: BLE001
            pass

    if not rows:
        print("Писем от площадок в выборке нет. Проверь папку: --folders")
        return 1

    for row in rows:
        row["kind"] = kind(row["subject"])
        row["target"], row["state"] = reply_target(row["reply_to"], row["from"])

    print("=" * 100)
    print("ПИСЬМА ПЛОЩАДОК")
    print("=" * 100)
    MARK = {
        "relay": "КАНАЛ К ГОСТЮ?",
        "поддержка": "адрес поддержки площадки",
        "from": "Reply-To нет, ответ уйдёт отправителю",
        "noreply": "отвечать некуда",
    }
    for row in sorted(rows, key=lambda r: (r["domain"], r["kind"])):
        print(f"[{row['kind']:9}] {row['domain']:22} {row['subject'][:58]}")
        print(f"            from: {row['from']}")
        print(f"            ответ → {row['target']:42} {MARK[row['state']]}")

    print("\n" + "=" * 100)
    print("ИТОГ ПО ПЛОЩАДКАМ")
    print("=" * 100)
    # Канал считается только по письмам с сообщением гостя: обратный адрес
    # на письме о брони или о выплате к гостю не ведёт.
    live: list[str] = []
    for dom in sorted(PLATFORM_SENDERS):
        mine = [r for r in rows if r["domain"].endswith(dom)]
        if not mine:
            print(f"{dom:22} писем нет")
            continue
        guest = [r for r in mine if r["kind"] == "сообщение"]
        print(f"{dom:22} {len(mine):3} писем, сообщений гостя: {len(guest)}")
        if not guest:
            print(f"{'':22} сообщений гостя в выборке нет — гипотеза не проверена")
            continue
        states = Counter(r["state"] for r in guest)
        for state, count in states.most_common():
            example = next(r["target"] for r in guest if r["state"] == state)
            print(f"{'':22}   {state:10} {count:3} шт., напр. {example}")
        if states.get("relay"):
            live.append(dom)

    if other_domains:
        print("\nДомены остальных отправителей (темы не читаем, только счёт) —")
        print("смотри, нет ли среди них relay площадки на незнакомом домене:")
        for dom, count in other_domains.most_common(15):
            print(f"  {count:4}  {dom}")

    print("\n" + "=" * 100)
    if live:
        print("ВЫВОД: кандидат на обратный канал есть — " + ", ".join(live) + ".")
        print("Это ещё не доказательство: адрес нашёлся в заголовках, но доедет ли ответ")
        print("до гостя, покажет только живое письмо. Дальше — SMTP с того же ящика и тест.")
    else:
        print("ВЫВОД: в письмах с сообщением гостя обратного адреса нет.")
        print("Через почту диалога не будет: остаются ссылка на бота в объявлении")
        print("и SMS по телефону из письма о брони.")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
