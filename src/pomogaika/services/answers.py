"""Ответы гостям по конкретному объекту.

Порядок, в котором ищется ответ:
  1. свой текст объекта (админ написал сам);
  2. шаблон ситуации из библиотеки с подставленными фактами объекта;
  3. если фактов не хватает — ситуация считается незаполненной, ответ отдаём человеку.

Модель подключается выше по стеку (services/copilot.py) и получает факты того же
объекта, а не файл на диске.
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy.orm import Session

from pomogaika.models import Property, PropertyAnswer, Scenario

PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")

# Факты, которые берём не из knowledge, а из самой карточки объекта.
FROM_PROPERTY = ("address", "check_in_from", "check_out_until", "max_guests", "title", "city")


def facts(prop: Property) -> dict:
    """Все факты объекта одним словарём: карточка плюс заполненная база знаний."""
    data = dict(prop.knowledge or {})
    for field in FROM_PROPERTY:
        value = getattr(prop, field, None)
        if value not in (None, ""):
            data.setdefault(field, value)
    return {k: v for k, v in data.items() if v not in (None, "")}


def missing(template: str, known: dict) -> list[str]:
    """Каких фактов не хватает шаблону."""
    return [name for name in PLACEHOLDER.findall(template) if not known.get(name)]


def render(template: str, known: dict) -> str:
    """Подставить факты. Незаполненные плейсхолдеры убираем, а не печатаем «{parking}»."""
    text = PLACEHOLDER.sub(lambda m: str(known.get(m.group(1), "")), template)
    return re.sub(r"\s{2,}", " ", text).strip()


def _score(text: str, keywords: list) -> int:
    low = text.lower()
    return sum(1 for word in keywords if str(word).lower() in low)


def catalog(session: Session, prop: Property) -> list[dict]:
    """Список ситуаций объекта со статусом — для вкладки «Ответы гостям»."""
    known = facts(prop)
    own = {
        row.scenario_code: row
        for row in session.query(PropertyAnswer).filter_by(property_id=prop.id).all()
        if row.scenario_code
    }
    custom = [
        row for row in session.query(PropertyAnswer).filter_by(property_id=prop.id).all()
        if not row.scenario_code
    ]

    rows: list[dict] = []
    for scenario in session.query(Scenario).order_by(Scenario.sort).all():
        answer = own.get(scenario.code)
        gaps = missing(scenario.template, known)
        if answer and answer.text.strip():
            state = "own" if answer.enabled else "off"
            text = answer.text
            gaps = []          # свой текст написан руками, факты шаблона ему не нужны
        elif gaps:
            state = "gap"
            text = render(scenario.template, known)
        else:
            state = "template"
            text = render(scenario.template, known)
        rows.append({
            "code": scenario.code,
            "title": scenario.title,
            "question_class": scenario.question_class,
            "state": state,
            "text": text,
            "template": scenario.template,
            "missing": gaps,
            "custom": False,
        })

    for row in custom:
        rows.append({
            "code": f"own:{row.id}",
            "id": row.id,
            "title": row.title or "Своя ситуация",
            "question_class": row.question_class,
            "state": "own" if row.enabled else "off",
            "text": row.text,
            "template": "",
            "missing": [],
            "keywords": row.keywords,
            "custom": True,
        })
    return rows


def summary(rows: list[dict]) -> dict:
    return {
        "total": len(rows),
        "own": sum(1 for r in rows if r["state"] == "own"),
        "template": sum(1 for r in rows if r["state"] == "template"),
        "gaps": sum(1 for r in rows if r["state"] == "gap"),
        "off": sum(1 for r in rows if r["state"] == "off"),
    }


def match(session: Session, prop: Property, guest_text: str) -> dict | None:
    """Подобрать ответ на вопрос гостя. Свои ситуации имеют приоритет над библиотекой."""
    known = facts(prop)
    best: dict | None = None
    best_score = 0

    for row in session.query(PropertyAnswer).filter_by(property_id=prop.id, enabled=True).all():
        if not row.text.strip():
            continue
        keywords = row.keywords or []
        if not keywords and row.scenario_code:
            scenario = session.query(Scenario).filter_by(code=row.scenario_code).one_or_none()
            keywords = scenario.keywords if scenario else []
        score = _score(guest_text, keywords)
        if score > best_score:
            title = row.title
            if not title and row.scenario_code:
                scenario = session.query(Scenario).filter_by(code=row.scenario_code).one_or_none()
                title = scenario.title if scenario else row.scenario_code
            best, best_score = {
                "text": row.text,
                "question_class": row.question_class,
                "title": title or "свой ответ",
                "source": "own",
            }, score

    for scenario in session.query(Scenario).all():
        score = _score(guest_text, scenario.keywords or [])
        if score <= best_score:
            continue
        if missing(scenario.template, known):
            continue          # факта нет — врать не будем, отдадим человеку
        best, best_score = {
            "text": render(scenario.template, known),
            "question_class": scenario.question_class,
            "title": scenario.title,
            "source": "library",
        }, score

    return best


def save_answer(session: Session, prop: Property, code: str, text: str,
                by: int | None = None, enabled: bool = True) -> PropertyAnswer:
    """Свой текст для ситуации из библиотеки."""
    row = (
        session.query(PropertyAnswer)
        .filter_by(property_id=prop.id, scenario_code=code)
        .one_or_none()
    )
    if row is None:
        scenario = session.query(Scenario).filter_by(code=code).one_or_none()
        row = PropertyAnswer(
            property_id=prop.id,
            scenario_code=code,
            question_class=scenario.question_class if scenario else "typical",
        )
        session.add(row)
    row.text = text.strip()[:4000]
    row.enabled = enabled
    row.updated_by = by
    row.updated_at = datetime.utcnow()
    session.flush()
    return row


def add_custom(session: Session, prop: Property, title: str, text: str,
               keywords: list[str], question_class: str = "typical",
               by: int | None = None) -> PropertyAnswer:
    """Своя ситуация объекта, которой нет в библиотеке."""
    row = PropertyAnswer(
        property_id=prop.id,
        scenario_code="",
        title=title.strip()[:200],
        text=text.strip()[:4000],
        keywords=[w.strip().lower() for w in keywords if w.strip()][:20],
        question_class=question_class if question_class in ("typical", "money", "conflict", "other") else "typical",
        updated_by=by,
    )
    session.add(row)
    session.flush()
    return row


def drop(session: Session, prop: Property, answer_id: int) -> bool:
    row = session.get(PropertyAnswer, answer_id)
    if row is None or row.property_id != prop.id:
        return False
    session.delete(row)
    session.flush()
    return True
