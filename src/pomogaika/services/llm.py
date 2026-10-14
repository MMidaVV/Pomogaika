"""TokenRouter: OpenAI-совместимый /v1/chat/completions. Политики сюда не ходят."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
import yaml

from pomogaika.config import settings

log = logging.getLogger("pomogaika.llm")

ALLOWED_CLASSES = {"typical", "money", "conflict", "other"}
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)
_LONG_NUM = re.compile(r"\d{4,}")
_KEEP_NUMS = {
    "5000",
    "2000",
    "120",
    "14",
    "12",
    "22",
    "47",
    "87",
    "19",
    "2026",
    "2025",
    "2027",
}


def enabled() -> bool:
    return bool(settings.tokenrouter_api_key.strip())


def load_knowledge() -> dict:
    return yaml.safe_load(settings.knowledge_path.read_text(encoding="utf-8")) or {}


def _safe_knowledge_blob(facts: dict | None = None) -> str:
    """Факты объекта. Без аргумента — файл: остаётся для локальной отладки и сида."""
    k = facts if facts else load_knowledge()
    notes = k.get("notes") or []
    note_lines = "\n".join(f"- {n}" for n in notes)
    return (
        f"Объект: {k.get('title')}\n"
        f"Город: {k.get('city')}\n"
        f"Адрес: {k.get('address')}\n"
        f"Вход: {k.get('entrance')}\n"
        f"Гостей до: {k.get('max_guests')}, {k.get('area_m2')} м², спальные: {k.get('beds')}\n"
        f"Заезд с {k.get('check_in_from')}, выезд до {k.get('check_out_until')}\n"
        f"Море: {k.get('sea_m')} м. Питомцы: {k.get('pets')}. Курить: {k.get('smoking')}. Вечеринки: {k.get('parties')}.\n"
        f"Залог: {k.get('deposit_rub')} ₽ (в стоимость не входит).\n"
        f"Парковка: {k.get('parking')}\n"
        f"Wi-Fi: {k.get('wifi')}\n"
        f"Доступ: {k.get('access')}\n"
        f"Хост: {k.get('host')}\n"
        f"Заметки:\n{note_lines}"
    )


def _faq_blob(faq: list[dict]) -> str:
    lines = []
    for item in faq:
        lines.append(
            f"- class={item.get('class')} | q={item.get('q')} | a={item.get('a')}"
        )
    return "\n".join(lines)


def build_messages(guest_text: str, faq: list[dict], facts: dict | None = None) -> list[dict[str, str]]:
    known = facts or {}
    title = known.get("title") or "апартаментов"
    city = known.get("city") or ""
    host = known.get("host") or "собственника"
    where = f"{title}{f' ({city})' if city else ''}"
    system = (
        f"Ты диспетчер объекта {where}. Хост: {host}. "
        "Пишешь короткий черновик ответа гостю на «вы».\n\n"
        "Класс вопроса — ровно один: typical, money, conflict, other.\n"
        "- typical: парковка, заезд/выезд, ключи, wi-fi (есть ли), море, питомцы, быт, кондиционер, трансфер.\n"
        "- money: скидка, цена, возврат, отмена брони, залог/депозит, «бесплатно», «дешевле».\n"
        "- conflict: поломка, нет света/воды, шум, жалоба, спор.\n"
        "- other: всё остальное, в базе нет.\n\n"
        "Жёсткие правила:\n"
        "- Не обещай скидку, возврат, отмену, компенсацию, бесплатное продление.\n"
        "- Не выдумывай цифры: коды калитки/домофона/сейфа, пароль Wi-Fi, телефоны.\n"
        "- Не пиши, что уже отправила код. Коды выдаёт собственник отдельно.\n"
        "- Опирайся только на базу и FAQ ниже. Если не знаешь — так и скажи, передай собственнику.\n"
        "- На money: вежливо, без обещаний, «передам собственнику».\n\n"
        "Ответ — один JSON-объект без Markdown:\n"
        '{"question_class":"typical|money|conflict|other","matched_faq":"точный q из FAQ или пусто","draft":"текст гостю"}\n\n'
        "База знаний:\n"
        f"{_safe_knowledge_blob(facts)}\n\n"
        "FAQ:\n"
        f"{_faq_blob(faq)}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": guest_text},
    ]


def sanitize_draft(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        num = match.group(0)
        if num in _KEEP_NUMS or len(num) < 4:
            return num
        if num.startswith("202") and len(num) == 4:
            return num
        return "****"

    return _LONG_NUM.sub(repl, text).strip()


def parse_model_json(raw: str) -> dict[str, str] | None:
    text = _THINK.sub("", raw or "").strip()
    if not text:
        return None
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    blob = text
    if not blob.startswith("{"):
        found = _JSON_OBJ.search(text)
        if not found:
            return None
        blob = found.group(0)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    klass = str(data.get("question_class") or "").strip().lower()
    if klass not in ALLOWED_CLASSES:
        return None
    draft = sanitize_draft(str(data.get("draft") or "").strip())
    if not draft:
        return None
    matched = str(data.get("matched_faq") or "").strip()
    return {"question_class": klass, "matched_faq": matched, "draft": draft}


def _extract_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {None, "text"}:
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(message.get("reasoning_content") or "")


def chat_json(messages: list[dict[str, str]]) -> dict[str, str] | None:
    if not enabled():
        return None
    url = settings.tokenrouter_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.tokenrouter_api_key.strip()}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": settings.tokenrouter_model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 600,
    }
    try:
        with httpx.Client(timeout=settings.tokenrouter_timeout) as client:
            try:
                response = client.post(
                    url,
                    headers=headers,
                    json={**body, "response_format": {"type": "json_object"}},
                )
                if response.status_code >= 400:
                    response = client.post(url, headers=headers, json=body)
            except httpx.HTTPError:
                response = client.post(url, headers=headers, json=body)
            response.raise_for_status()
            parsed = parse_model_json(_extract_content(response.json()))
            if parsed:
                log.info(
                    "TokenRouter ok model=%s class=%s",
                    settings.tokenrouter_model,
                    parsed["question_class"],
                )
            else:
                log.warning("TokenRouter: не разобрала JSON ответа")
            return parsed
    except Exception:
        log.exception("TokenRouter недоступен, ухожу на FAQ")
        return None


def interpret_guest(guest_text: str, faq: list[dict], facts: dict | None = None) -> dict[str, str] | None:
    return chat_json(build_messages(guest_text, faq, facts))
