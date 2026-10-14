"""Кто дёргает ручки.

Два входа, оба сводятся к одному telegram user id:
  * мини-приложение — Telegram подписывает initData при открытии вебвью;
  * браузер — вход через виджет Telegram, дальше своя cookie-сессия.

Формулы подписи у них разные (у мини-аппа ключ HMAC(b"WebAppData", token),
у виджета SHA256(token)) — это не описка, так устроено у Telegram.

Когда появится MAX, сюда добавится третья функция проверки подписи: остальное —
список допущенных, сессия, middleware — общее для всех платформ.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

from pomogaika.config import settings
from pomogaika.db import SessionLocal
from pomogaika.services.access import allowed_ids

log = logging.getLogger("pomogaika.auth")

# Telegram подписывает initData при открытии мини-приложения. Подпись живёт сутки:
# вебвью держат открытым подолгу, а переоткрыть его гость не может — только собственник.
MAX_AGE_SECONDS = 24 * 60 * 60

# Браузерная сессия живёт дольше: админ не хочет логиниться каждый день.
SESSION_DAYS = 30
SESSION_COOKIE = "pomogaika_session"


def enabled() -> bool:
    """Без токена бота подпись проверять нечем — локальная разработка идёт без авторизации."""
    return bool((settings.telegram_bot_token or "").strip()) and settings.telegram_auth_required


def verify_init_data(init_data: str) -> int | None:
    """Вернуть telegram user id, если подпись настоящая и свежая. Иначе None."""
    token = (settings.telegram_bot_token or "").strip()
    if not token or not init_data:
        return None

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    if not received:
        return None

    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None

    try:
        if time.time() - int(pairs.get("auth_date", "0")) > MAX_AGE_SECONDS:
            return None
        return int(json.loads(pairs.get("user", "{}"))["id"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def verify_login_widget(params: dict[str, str]) -> int | None:
    """Вернуть telegram user id из ответа виджета «Войти через Telegram». Иначе None."""
    token = (settings.telegram_bot_token or "").strip()
    pairs = {k: v for k, v in params.items() if k != "hash"}
    received = params.get("hash", "")
    if not token or not received or "id" not in pairs:
        return None

    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None

    try:
        if time.time() - int(pairs.get("auth_date", "0")) > MAX_AGE_SECONDS:
            return None
        return int(pairs["id"])
    except ValueError:
        return None


def make_session(user_id: int, days: int = SESSION_DAYS) -> str:
    """Подписанная cookie: кто и до какого времени. Хранить на сервере нечего."""
    expires = int(time.time()) + days * 24 * 60 * 60
    payload = f"{user_id}.{expires}"
    return f"{payload}.{_sign(payload)}"


def read_session(cookie: str) -> int | None:
    """User id из cookie, если подпись наша и срок не вышел."""
    try:
        user_id, expires, signature = cookie.split(".")
    except (ValueError, AttributeError):
        return None
    if not hmac.compare_digest(_sign(f"{user_id}.{expires}"), signature):
        return None
    try:
        if int(expires) < time.time():
            return None
        return int(user_id)
    except ValueError:
        return None


def _sign(payload: str) -> str:
    token = (settings.telegram_bot_token or "").strip()
    return hmac.new(token.encode(), payload.encode(), hashlib.sha256).hexdigest()


def allowed_user_ids() -> set[int]:
    """Кого пускаем: см. pomogaika.services.access — там же приглашённые."""
    session = SessionLocal()
    try:
        return allowed_ids(session)
    finally:
        session.close()


def check(init_data: str = "", session_cookie: str = "") -> int | None:
    """User id, если человеку можно внутрь: хоть из мини-аппа, хоть из браузера."""
    user_id = verify_init_data(init_data) or read_session(session_cookie)
    if user_id is None:
        return None
    if user_id not in allowed_user_ids():
        log.warning("Чужой telegram user id в мини-приложении: %s", user_id)
        return None
    return user_id
