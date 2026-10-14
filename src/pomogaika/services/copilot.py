from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import yaml

from pomogaika.config import settings
from pomogaika.domain.dates import fmt_span
from pomogaika.domain.policy import action_for_question_class, check
from pomogaika.models import Booking
from pomogaika.services.answers import facts as object_facts, match as match_answer
from pomogaika.services.llm import enabled as llm_enabled
from pomogaika.services.llm import interpret_guest

CODE_KW = ("код", "калитки", "домофон", "пароль", "wifi", "wi-fi", "вайфай", "вай-фай", "сейф")
IN_KW = ("заезд", "заселен", "как пройти", "где квартира", "ключ", "как заехать")
OUT_KW = ("выезд", "выезжаем", "до скольки выезд", "во сколько выезд")
MONEY_KW = (
    "скидк",
    "возврат",
    "вернуть деньги",
    "отмен",
    "бесплатно",
    "дешевле",
    "залог",
    "депозит",
    "компенсац",
    "верните",
)


@dataclass
class CopilotResult:
    question_class: str
    policy_mode: str
    allowed_auto: bool
    draft: str
    matched_faq: str
    reason: str
    needs_owner: bool
    booking_id: int | None = None
    booking_label: str = ""
    issue_code: bool = False
    source: str = "faq"


def load_faq() -> list[dict]:
    return yaml.safe_load(settings.faq_path.read_text(encoding="utf-8")) or []


def _score(text: str, keywords: list[str]) -> int:
    low = text.lower()
    return sum(1 for k in keywords if str(k).lower() in low)


def _has(text: str, keys: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(k in low for k in keys)


def booking_label(booking: Booking) -> str:
    paid = "оплачена" if booking.paid else "оплата не отмечена"
    return f"{booking.guest_name} · {booking.platform} · {fmt_span(booking.check_in, booking.check_out)} · {paid}"


def _faq_match(guest_text: str, faq: list[dict]) -> tuple[dict | None, int]:
    best = None
    best_score = 0
    for item in faq:
        score = _score(guest_text, item.get("keywords") or [])
        if score > best_score:
            best, best_score = item, score
    return best, best_score


def _faq_by_q(faq: list[dict], q: str) -> dict | None:
    needle = (q or "").strip().lower()
    if not needle:
        return None
    for item in faq:
        if str(item.get("q") or "").strip().lower() == needle:
            return item
    for item in faq:
        title = str(item.get("q") or "").strip().lower()
        if needle in title or title in needle:
            return item
    return None


def _money_item(faq: list[dict]) -> dict | None:
    for item in faq:
        if item.get("class") == "money" and "скидк" in str(item.get("q") or "").lower():
            return item
    for item in faq:
        if item.get("class") == "money":
            return item
    return None


def _from_faq(guest_text: str, faq: list[dict]) -> tuple[str, str, str]:
    best, best_score = _faq_match(guest_text, faq)
    if best is None or best_score == 0:
        return (
            "other",
            (
                "Не уверена, что это типовой вопрос по квартире. "
                "Не обещаю скидку, возврат или отмену. Передаю собственнику."
            ),
            "",
        )
    return best["class"], best["a"], best["q"]


def classify_and_draft(
    guest_text: str,
    booking: Booking | None = None,
    *,
    now: datetime | None = None,
    use_llm: bool | None = None,
    session=None,
    prop=None,
) -> CopilotResult:
    """Черновик ответа гостю.

    Если переданы session и prop — отвечаем по ответам этого объекта и его фактам.
    Без них остаётся старый путь по файлу: так работают тесты и локальная отладка.
    """
    now = now or datetime.now()
    faq = load_faq()
    source = "faq"
    matched = ""
    want_llm = llm_enabled() if use_llm is None else bool(use_llm)

    known = object_facts(prop) if prop is not None else None
    own = None
    if session is not None and prop is not None:
        own = match_answer(session, prop, guest_text)

    parsed = interpret_guest(guest_text, faq, known) if want_llm else None
    if parsed:
        question_class = parsed["question_class"]
        draft = parsed["draft"]
        faq_row = _faq_by_q(faq, parsed["matched_faq"])
        matched = faq_row["q"] if faq_row else parsed["matched_faq"]
        source = "llm"
    else:
        question_class, draft, matched = _from_faq(guest_text, faq)

    if own and own["source"] == "own":
        # админ объекта написал это сам — его формулировка главнее модели
        question_class, draft, matched, source = own["question_class"], own["text"], own["title"], "object"
    elif own and not parsed:
        question_class, draft, matched, source = own["question_class"], own["text"], own["title"], "library"

    if _has(guest_text, MONEY_KW):
        money = _money_item(faq)
        question_class = "money"
        source = "faq"
        if money:
            draft = money["a"]
            matched = money["q"]

    action = action_for_question_class(question_class)
    decision = check(action)
    stay = ""
    issue_code = False
    if booking:
        stay = booking_label(booking)
        draft, action, decision, question_class, issue_code = _with_booking(
            guest_text, draft, booking, now, question_class, action, decision
        )

    needs_owner = decision.mode in {"confirm", "never"} or not decision.allowed
    if question_class == "typical" and decision.mode == "confirm":
        needs_owner = True
    label = matched
    if source == "llm" and matched:
        label = f"LLM · {matched}"
    elif source == "llm":
        label = "LLM"
    return CopilotResult(
        question_class=question_class,
        policy_mode=decision.mode,
        allowed_auto=decision.mode == "auto" and decision.allowed,
        draft=draft,
        matched_faq=label,
        reason=decision.reason,
        needs_owner=needs_owner,
        booking_id=booking.id if booking else None,
        booking_label=stay,
        issue_code=issue_code,
        source=source,
    )


def _with_booking(guest_text, draft, booking: Booking, now: datetime, question_class, action, decision):
    head = f"{booking.guest_name}, у вас бронь {fmt_span(booking.check_in, booking.check_out)}."
    paid_note = " Бронь отмечена как оплаченная." if booking.paid else " В календаре оплата пока не стоит."
    if question_class == "money":
        return draft, action, decision, question_class, False

    if _has(guest_text, CODE_KW):
        code = check(
            "door_code",
            {
                "paid": booking.paid,
                "now": now,
                "check_in": booking.check_in,
                "check_out": booking.check_out,
            },
        )
        if not code.allowed:
            text = f"{head}{paid_note} {code.reason}."
            if not booking.paid:
                text += " Код калитки, домофона и пароль Wi-Fi не отправляю — сначала оплата, это решит собственник."
            elif "Рано" in code.reason:
                text += " Коды пришлём в этот чат не раньше чем за 2 часа до заезда."
            else:
                text += " Передаю собственнику."
            return text, "door_code", code, "typical", False
        text = (
            f"{head}{paid_note} Окно заезда открыто. Код калитки, код домофона подъезда и код сейфа "
            "собственник подтвердит и отправит в этот чат. Сама цифры в черновик не ставлю."
        )
        return text, "door_code", code, "typical", True

    if _has(guest_text, OUT_KW):
        extra = ""
        if now.date() == booking.check_out.date():
            extra = " Сегодня выезд до 12:00. Ключ — в сейф у двери."
        text = f"{head} Выезд до 12:00.{extra} {draft}"
        return text, action, decision, question_class, False

    if _has(guest_text, IN_KW) or question_class == "typical":
        extra = ""
        if now.date() == booking.check_in.date() or (
            booking.check_in <= now < booking.check_out
        ):
            extra = " Вы в окне проживания."
        elif now < booking.check_in:
            extra = f" До заезда ещё есть время — с {booking.check_in.strftime('%H:%M')}."
        text = f"{head}{paid_note}{extra} {draft}"
        return text, action, decision, question_class, False

    return f"{head} {draft}", action, decision, question_class, False
