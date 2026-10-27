"""Отправка в личный Max через локальный процесс Node-Max.

Процесс слушает только 127.0.0.1. Сюда ходим, когда черновик уже готов
и его пора отдать в чат.
"""

from __future__ import annotations

import httpx

from pomogaika.config import settings


def deliver(chat_id: str, text: str) -> str:
    chat = (chat_id or "").strip()
    body = (text or "").strip()
    if not chat or not body:
        raise RuntimeError("некуда отправлять или пустой текст")
    url = settings.max_base_url.rstrip("/") + "/send"
    try:
        with httpx.Client(timeout=40) as client:
            response = client.post(url, json={"to": chat, "text": body})
    except httpx.HTTPError as exc:
        raise RuntimeError("Max не запущен") from exc
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if response.status_code != 200 or payload.get("ok") is False:
        raise RuntimeError(str(payload.get("error") or "Max не отправил сообщение"))
    return str(payload.get("to") or "")
