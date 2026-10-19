"""Тестовая бронь для проверки ссылки гостя.

Заводит одну будущую оплаченную бронь, по которой можно выдать ссылку гостю
и пройти весь сценарий: гость открыл личку, отправил сообщение, бот опознал
бронь по коду и ответил от лица собственницы.

Бронь именно будущая: у архивной ссылка считается просроченной, и половину
проверки пройти не выйдет.

Запуск на сервере:
    cd /opt/pomogaika
    sudo -u pomogaika .venv/bin/python tools/make_test_booking.py

Отвязать гостя и выдать ссылку заново:
    sudo -u pomogaika .venv/bin/python tools/make_test_booking.py --unbind

Убрать после проверки:
    sudo -u pomogaika .venv/bin/python tools/make_test_booking.py --remove

Повторный запуск не плодит дубли: бронь ищется по external_id и обновляется.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from pomogaika.db import SessionLocal                      # noqa: E402
from pomogaika.models import Booking, Property             # noqa: E402

EXTERNAL_ID = "TEST-LINK-1"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true", help="удалить тестовую бронь")
    ap.add_argument("--unbind", action="store_true",
                    help="отвязать гостя: вернуть кнопку «Ссылка для гостя» в карточку")
    ap.add_argument("--days", type=int, default=3, help="через сколько дней заезд")
    ap.add_argument("--nights", type=int, default=2, help="сколько ночей")
    ap.add_argument("--name", default="Юрий", help="имя гостя в карточке")
    args = ap.parse_args()

    session = SessionLocal()
    try:
        prop = session.query(Property).first()
        if prop is None:
            print("В базе нет ни одного объекта — сначала засейте объект.")
            return 1

        booking = (
            session.query(Booking)
            .filter(Booking.property_id == prop.id, Booking.external_id == EXTERNAL_ID)
            .first()
        )

        if args.unbind:
            if booking is None:
                print("Тестовой брони нет.")
                return 1
            booking.guest_telegram_user_id = None
            booking.guest_chat_id = ""
            booking.business_connection_id = ""
            session.commit()
            print("Гость отвязан — ссылку можно выдать заново.")
            return 0

        if args.remove:
            if booking is None:
                print("Тестовой брони нет, удалять нечего.")
                return 0
            session.delete(booking)
            session.commit()
            print(f"Тестовая бронь {EXTERNAL_ID} удалена.")
            return 0

        check_in = (datetime.now() + timedelta(days=args.days)).replace(
            hour=14, minute=0, second=0, microsecond=0
        )
        check_out = (check_in + timedelta(days=args.nights)).replace(hour=12, minute=0)

        if booking is None:
            booking = Booking(property_id=prop.id, platform="yandex", external_id=EXTERNAL_ID)
            session.add(booking)
            action = "создана"
        else:
            action = "обновлена"

        booking.guest_name = args.name
        booking.check_in = check_in
        booking.check_out = check_out
        booking.guests_count = 2
        booking.amount_cents = 1_000_000          # 10 000 ₽, чтобы карточка выглядела живой
        booking.paid = True                       # иначе код от двери не выдаётся никогда
        booking.status = "confirmed"
        booking.source = "manual"
        session.commit()

        print(f"Тестовая бронь {action}: id={booking.id}, объект «{prop.title}»")
        print(f"  заезд:  {check_in:%d.%m.%Y %H:%M}")
        print(f"  выезд:  {check_out:%d.%m.%Y %H:%M}")
        print(f"  гость:  {booking.guest_name}, оплачено: да")
        print()
        print("Дальше: мини-апп → Брони → Подробнее → «Ссылка для гостя».")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
