"""Отправка в личный Авито через локальный браузер эксперимента.

Браузер слушает только 127.0.0.1. Вызов отсюда происходит, когда человек
нажал «Отправить» в Помогайке.
"""

from __future__ import annotations

import httpx


def _post(path: str, payload: dict) -> dict:
    try:
        with httpx.Client(timeout=40) as client:
            response = client.post(f"http://127.0.0.1:8791{path}", json=payload)
    except httpx.HTTPError as exc:
        raise RuntimeError("браузер Авито не запущен") from exc
    try:
        body = response.json()
    except Exception:
        body = {}
    if response.status_code != 200:
        raise RuntimeError(str(body.get("error") or "Авито не ответил"))
    return body


def deliver(channel_id: str, text: str) -> list[dict]:
    body = _post("/send", {"channel_id": channel_id, "text": text})
    messages = body.get("messages") or []
    return messages if isinstance(messages, list) else []


def read_thread(channel_id: str) -> list[dict]:
    body = _post("/thread", {"channel_id": channel_id})
    messages = body.get("messages") or []
    return messages if isinstance(messages, list) else []
