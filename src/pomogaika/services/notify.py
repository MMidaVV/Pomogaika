"""Короткое уведомление собственнику через Bot API (из веб-ингеста тоже)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from pomogaika.config import settings
from pomogaika.db import SessionLocal
from pomogaika.models import Owner


log = logging.getLogger("pomogaika.notify")


def notify_chat(chat_id: int | None, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> None:
    token = (settings.telegram_bot_token or "").strip()
    if not token or not chat_id:
        return
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
            if response.status_code != 200:
                log.warning("notify failed: %s %s", response.status_code, response.text[:300])
    except Exception:
        log.exception("notify")


def deliver_to_guest(booking, text: str) -> None:
    """Написать гостю в тот канал, где он в последний раз ответил."""
    if getattr(booking, "guest_channel", "") == "whatsapp" and booking.guest_whatsapp_chat_id:
        from pomogaika.platforms.whatsapp_local import deliver

        deliver(booking.guest_whatsapp_chat_id, text)
        return
    if getattr(booking, "guest_channel", "") == "max" and booking.guest_max_chat_id:
        from pomogaika.platforms.max_local import deliver

        deliver(booking.guest_max_chat_id, text)
        return
    notify_guest(booking.guest_chat_id, text, booking.business_connection_id or "")


def notify_guest(chat_id: str, text: str, business_connection_id: str = "") -> None:
    if not chat_id:
        return
    token = (settings.telegram_bot_token or "").strip()
    if not token:
        return
    payload: dict[str, Any] = {"chat_id": int(chat_id), "text": text}
    if business_connection_id:
        payload["business_connection_id"] = business_connection_id
    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
            if response.status_code != 200:
                log.warning("notify guest failed: %s %s", response.status_code, response.text[:300])
    except Exception:
        log.exception("notify guest")


def owner_chat_id() -> int | None:
    session = SessionLocal()
    try:
        owner = session.query(Owner).first()
        if owner and owner.telegram_chat_id:
            return owner.telegram_chat_id
        return None
    finally:
        session.close()


def notify_owner(text: str, keyboard: list[list[dict[str, str]]] | None = None) -> None:
    notify_chat(owner_chat_id(), text, keyboard)
