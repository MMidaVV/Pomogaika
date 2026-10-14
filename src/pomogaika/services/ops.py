"""Демо-данные, которые нужны даже если объект уже засеян."""

from __future__ import annotations

import yaml

from sqlalchemy.orm import Session

from pomogaika.config import ROOT, settings
from pomogaika.models import ChecklistItem, Contractor, Listing, Property
from pomogaika.services.bookings import reconcile_conflicts


def ensure_answers(session: Session) -> None:
    """Библиотека ситуаций общая; нынешние тексты FAQ становятся ответами объекта.

    Так после перехода на библиотеку объект отвечает ровно то же, что отвечал:
    свои формулировки KaVa не теряются, а становятся его собственными ответами.
    """
    from pomogaika.models import PropertyAnswer, Scenario
    from pomogaika.services.copilot import load_faq
    from pomogaika.services.library import seed_library

    seed_library(session)

    prop = session.query(Property).first()
    if prop is None:
        return
    if session.query(PropertyAnswer).filter_by(property_id=prop.id).count():
        return

    scenarios = session.query(Scenario).all()
    for item in load_faq():
        answer = (item.get("a") or "").strip()
        words = [str(k).lower() for k in (item.get("keywords") or [])]
        if not answer or not words:
            continue
        hit, best = None, 0
        for scenario in scenarios:
            score = sum(
                1 for w in (scenario.keywords or [])
                if any(str(w).lower() in k or k in str(w).lower() for k in words)
            )
            if score > best:
                hit, best = scenario, score
        if hit is None:
            continue
        if session.query(PropertyAnswer).filter_by(property_id=prop.id, scenario_code=hit.code).one_or_none():
            continue
        session.add(PropertyAnswer(
            property_id=prop.id,
            scenario_code=hit.code,
            question_class=item.get("class") or hit.question_class,
            text=answer,
        ))
    session.flush()


def ensure_runtime(session: Session) -> None:
    prop = session.query(Property).first()
    if not prop:
        return
    if not session.query(Contractor).filter_by(property_id=prop.id, role="cleaner").first():
        session.add(
            Contractor(
                property_id=prop.id,
                name="клинер основной",
                role="cleaner",
                telegram_user_id=settings.telegram_cleaner_user_id,
                trust_level="new",
            )
        )
    if session.query(ChecklistItem).filter_by(property_id=prop.id).count() == 0:
        path = ROOT / "ops" / "kava" / "checklist.yaml"
        items = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        for i, item in enumerate(items, start=1):
            session.add(
                ChecklistItem(
                    property_id=prop.id,
                    room=str(item.get("room") or ""),
                    title=str(item.get("title") or ""),
                    sort=i,
                )
            )
    cleaner = session.query(Contractor).filter_by(property_id=prop.id, role="cleaner").first()
    if cleaner and settings.telegram_cleaner_user_id and not cleaner.telegram_user_id:
        cleaner.telegram_user_id = settings.telegram_cleaner_user_id
    icals = (prop.knowledge or {}).get("ical") or {}
    if not icals:
        raw = yaml.safe_load(settings.knowledge_path.read_text(encoding="utf-8")) or {}
        icals = raw.get("ical") or {}
    for platform, url in icals.items():
        if not url:
            continue
        listing = session.query(Listing).filter_by(property_id=prop.id, platform=platform).first()
        if listing and not listing.ical_url:
            listing.ical_url = str(url).strip()
    ensure_answers(session)
    reconcile_conflicts(session, prop.id)
