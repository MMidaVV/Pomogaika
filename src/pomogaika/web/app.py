from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from pomogaika.bots.telegram import start_bot_background
from pomogaika.config import settings
from pomogaika.db import Base, SessionLocal, engine, get_session, migrate_sqlite
from pomogaika.domain.dates import parse_close_command
from pomogaika.models import AuditLog, Block, Booking, BookingConflict, Event, Listing, MessageDraft, Owner, Property, StayOutcome, Task
from pomogaika.platforms.email_parser import parse_platform_email
from pomogaika.platforms.ical import fetch_ics
from pomogaika.services.llm import enabled as llm_enabled
from pomogaika.services.guest_links import GUEST_PITCH, active_link, issue_link, revoke
from pomogaika.services.bookings import (
    blocks_in_range,
    close_dates,
    open_dates,
    conflict_keyboard,
    describe_booking,
    describe_conflict,
    dismiss_conflict,
    ingest_booking,
    ingest_ics,
    listing_for,
    owner_cancel_in_calendar,
    set_listing_ical,
)
from pomogaika.services.cleaning import (
    accept_task,
    add_photo,
    complete_task,
    create_cleaning_task,
    snooze_task,
    task_card,
)
from pomogaika.services.codes import issue_code
from pomogaika.services.drafts import apply_draft_action, create_guest_draft
from pomogaika.services.knowledge import apply_knowledge_edit, parse_knowledge_edit
from pomogaika.services.miniapp import build_miniapp
from pomogaika.services.notify import notify_guest, notify_owner
from pomogaika.services.ops import ensure_runtime
from pomogaika.services.outcome import as_dict as outcome_dict, save as save_outcome, summary as outcome_summary
from pomogaika.services.properties import create_property
from pomogaika.services import answers as answers_service
from pomogaika.services.copilot import classify_and_draft
from pomogaika.services.sync import sync_listings
from pomogaika.services.registration import (
    add as add_registration,
    overview as registration_overview,
    remove as remove_registration,
    set_status as set_registration_status,
)
from pomogaika.services.tourist_tax import report as tourist_tax_report
from pomogaika.services.seed import audit, seed_if_empty
from pomogaika.web import auth

STATIC = Path(__file__).resolve().parent / "static"


def configure_logging() -> None:
    """httpx пишет в лог полный URL запроса, а у Bot API токен стоит прямо в пути.

    На уровне INFO он оседал в journald открытым текстом — приглушаем.
    """
    logging.basicConfig(level=logging.INFO)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("pomogaika.web")


def fetch_bot_username() -> str:
    """Виджету входа нужно имя бота. Спрашиваем у Telegram, чтобы не держать его ещё и в .env."""
    token = (settings.telegram_bot_token or "").strip()
    if not token:
        return ""
    try:
        with httpx.Client(timeout=10) as client:
            data = client.get(f"https://api.telegram.org/bot{token}/getMe").json()
        return data.get("result", {}).get("username", "")
    except Exception as exc:
        log.warning("не удалось узнать имя бота для виджета входа: %s", exc)
        return ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    Base.metadata.create_all(engine)
    migrate_sqlite()
    session = SessionLocal()
    try:
        seed_if_empty(session)
        ensure_runtime(session)
        session.commit()
    finally:
        session.close()
    app.state.bot_username = fetch_bot_username()
    bot_task = start_bot_background()
    yield
    if bot_task:
        bot_task.cancel()


app = FastAPI(title="Pomogaika", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def telegram_auth(request: Request, call_next):
    """Ручки открыты только мини-приложению Telegram: с подписанным initData от своего человека."""
    if request.url.path.startswith("/api/") and auth.enabled():
        who = auth.check(
            request.headers.get("X-Telegram-Init-Data", ""),
            request.cookies.get(auth.SESSION_COOKIE, ""),
        )
        if who is None:
            return JSONResponse({"detail": "Нужно войти"}, status_code=401)
    return await call_next(request)


@app.get("/")
def index() -> RedirectResponse:
    """Веб-панели нет — единственный вход в систему это мини-приложение."""
    return RedirectResponse("/app")


# Вебвью Telegram кэширует страницу агрессивно: без этих заголовков после обновления
# у людей ещё сутками открывается старая версия мини-приложения.
NO_CACHE = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _page(name: str, **replacements: str) -> HTMLResponse:
    """Отдать страницу без кэша и подставить в неё метку сборки."""
    path = STATIC / name
    html = path.read_text(encoding="utf-8")
    build = datetime.fromtimestamp(path.stat().st_mtime).strftime("%d.%m %H:%M")
    html = html.replace("__APP_BUILD__", build)
    for key, value in replacements.items():
        html = html.replace(key, value)
    return HTMLResponse(html, headers=NO_CACHE)


@app.get("/app")
def miniapp_page() -> HTMLResponse:
    return _page("app.html")


@app.get("/login")
def login_page(request: Request) -> HTMLResponse:
    """Вход для браузера: виджет Telegram. В мини-приложении эта страница не нужна."""
    username = getattr(request.app.state, "bot_username", "") or ""
    return _page("login.html", __BOT_USERNAME__=username)


@app.get("/auth/telegram")
def login_callback(request: Request) -> RedirectResponse:
    """Сюда виджет возвращает подписанные данные аккаунта."""
    user_id = auth.verify_login_widget(dict(request.query_params))
    if user_id is None:
        return RedirectResponse("/login?err=bad", status_code=303)
    if user_id not in auth.allowed_user_ids():
        log.warning("вход из браузера отклонён, чужой telegram user id: %s", user_id)
        return RedirectResponse("/login?err=denied", status_code=303)

    response = RedirectResponse("/app", status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.make_session(user_id),
        max_age=auth.SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )
    return response


@app.get("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


def _booking_dict(b: Booking) -> dict:
    return {
        "id": b.id,
        "platform": b.platform,
        "guest_name": b.guest_name,
        "check_in": b.check_in.isoformat(),
        "check_out": b.check_out.isoformat(),
        "guests_count": b.guests_count,
        "amount_cents": b.amount_cents,
        "paid": b.paid,
        "status": b.status,
        "external_id": b.external_id,
        "source": b.source,
        "guest_telegram_user_id": b.guest_telegram_user_id,
        "code_hint": b.code_hint,
        "code_issued_at": b.code_issued_at.isoformat() if b.code_issued_at else None,
        "checked_in_at": b.checked_in_at.isoformat() if b.checked_in_at else None,
        "checked_out_at": b.checked_out_at.isoformat() if b.checked_out_at else None,
    }


def _block_dict(block: Block) -> dict:
    return {
        "id": block.id,
        "start": block.start.isoformat(),
        "end": block.end.isoformat(),
        "reason": block.reason,
        "source": block.source,
    }


@app.get("/api/overview")
def overview(session: Session = Depends(get_session)) -> dict:
    prop = session.query(Property).one()
    listings = session.query(Listing).filter_by(property_id=prop.id).all()
    bookings = session.query(Booking).filter_by(property_id=prop.id).order_by(Booking.check_in).all()
    conflicts = session.query(BookingConflict).filter_by(property_id=prop.id, status="open").all()
    drafts = (
        session.query(MessageDraft)
        .filter_by(property_id=prop.id)
        .order_by(MessageDraft.created_at.desc())
        .all()
    )
    tasks = session.query(Task).filter_by(property_id=prop.id).order_by(Task.created_at.desc()).all()
    events = session.query(Event).order_by(Event.ts.desc()).limit(30).all()
    journal = session.query(AuditLog).order_by(AuditLog.ts.desc()).limit(30).all()
    blocks = session.query(Block).filter_by(property_id=prop.id).order_by(Block.start).all()
    conflict_view = []
    for c in conflicts:
        a = session.get(Booking, c.booking_a_id)
        b = session.get(Booking, c.booking_b_id)
        cheap = session.get(Booking, c.cheaper_to_cancel_id)
        block = session.get(Block, c.block_id) if c.block_id else None
        conflict_view.append(
            {
                "id": c.id,
                "a": _booking_dict(a) if a else None,
                "b": _booking_dict(b) if b and b.id != (a.id if a else None) else None,
                "cheaper": cheap.guest_name if cheap else "",
                "cheaper_platform": cheap.platform if cheap else "",
                "cheaper_id": cheap.id if cheap else None,
                "block": _block_dict(block) if block else None,
                "status": c.status,
                "text": describe_conflict(session, c),
            }
        )
    return {
        "llm": {
            "enabled": llm_enabled(),
            "model": settings.tokenrouter_model if llm_enabled() else "",
        },
        "property": {
            "title": prop.title,
            "address": prop.address,
            "city": prop.city,
            "mode": prop.mode,
            "max_guests": prop.max_guests,
            "check_in_from": prop.check_in_from,
            "check_out_until": prop.check_out_until,
            "knowledge": prop.knowledge,
        },
        "listings": [
            {"platform": x.platform, "external_id": x.external_id, "url": x.url, "ical_url": x.ical_url}
            for x in listings
        ],
        "bookings": [_booking_dict(b) for b in bookings],
        "blocks": [_block_dict(b) for b in blocks],
        "conflicts": conflict_view,
        "drafts": [
            {
                "id": d.id,
                "guest_text": d.guest_text,
                "draft_text": d.draft_text,
                "question_class": d.question_class,
                "policy_mode": d.policy_mode,
                "status": d.status,
                "matched_faq": d.matched_faq,
                "booking_id": d.booking_id,
            }
            for d in drafts
        ],
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "state": t.state,
                "type": t.type,
                "assignee": t.assignee,
                "photo_count": t.photo_count,
                "photos_needed": t.photos_needed,
                "note": t.note,
                "booking_id": t.booking_id,
            }
            for t in tasks
        ],
        "events": [{"type": e.type, "payload": e.payload, "ts": e.ts.isoformat()} for e in events],
        "journal": [
            {
                "actor": j.actor,
                "action": j.action,
                "reason": j.reason,
                "authorized_by": j.authorized_by,
                "policy_decision": j.policy_decision,
                "ts": j.ts.isoformat(),
            }
            for j in journal
        ],
    }


@app.get("/api/miniapp")
def miniapp(
    year: int | None = None,
    month: int | None = None,
    property_id: int | None = None,
    session: Session = Depends(get_session),
) -> dict:
    now = datetime.now()
    y = year or now.year
    m = month or now.month
    if m < 1 or m > 12:
        raise HTTPException(400, "месяц 1–12")
    try:
        return build_miniapp(session, year=y, month=m, property_id=property_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


class PropertyIn(BaseModel):
    title: str
    address: str = ""
    city: str = ""
    check_in_from: str = "14:00"
    check_out_until: str = "12:00"
    max_guests: int = 4


@app.post("/api/properties")
def add_property(body: PropertyIn, session: Session = Depends(get_session)) -> dict:
    """Новый объект собственника. Площадки заводятся пустыми — ссылки добавляются потом."""
    owner = session.query(Owner).first()
    if not owner:
        raise HTTPException(404, "собственник не заведён")
    try:
        prop = create_property(
            session,
            owner=owner,
            title=body.title,
            address=body.address,
            city=body.city,
            check_in_from=body.check_in_from,
            check_out_until=body.check_out_until,
            max_guests=body.max_guests,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "property": {
            "id": prop.id,
            "code": prop.code,
            "title": prop.title,
            "address": prop.address,
            "city": prop.city,
            "check_in_from": prop.check_in_from,
            "check_out_until": prop.check_out_until,
            "max_guests": prop.max_guests,
        },
        "note": "База знаний пустая. Добавьте ссылки iCal и факты об объекте, иначе гостю ответить будет нечем.",
    }


def _one_property(session: Session, property_id: int | None) -> Property:
    """Объект для действия. С несколькими объектами id обязателен."""
    if property_id:
        prop = session.get(Property, property_id)
        if not prop:
            raise HTTPException(404, "объект не найден")
        return prop
    props = session.query(Property).order_by(Property.id).limit(2).all()
    if not props:
        raise HTTPException(404, "объектов нет")
    if len(props) > 1:
        raise HTTPException(400, "укажите property_id: объектов несколько")
    return props[0]


class TextIn(BaseModel):
    text: str
    booking_id: int | None = None


@app.post("/api/copilot")
def copilot(body: TextIn, session: Session = Depends(get_session)) -> dict:
    draft, result = create_guest_draft(session, body.text, channel="web", booking_id=body.booking_id)
    return {
        "draft_id": draft.id,
        "question_class": result.question_class,
        "policy_mode": result.policy_mode,
        "draft": result.draft,
        "matched_faq": result.matched_faq,
        "reason": result.reason,
        "needs_owner": result.needs_owner,
        "status": draft.status,
        "booking_id": result.booking_id,
        "booking_label": result.booking_label,
        "issue_code": result.issue_code,
        "source": result.source,
    }


class DraftAction(BaseModel):
    action: str  # send | edit | self
    text: str | None = None


@app.post("/api/drafts/{draft_id}")
def draft_action(draft_id: int, body: DraftAction, session: Session = Depends(get_session)) -> dict:
    draft = session.get(MessageDraft, draft_id)
    if not draft:
        raise HTTPException(404)
    try:
        apply_draft_action(session, draft, body.action, body.text)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": draft.id, "status": draft.status, "draft_text": draft.draft_text}


@app.post("/api/email")
def ingest_email(body: TextIn, session: Session = Depends(get_session)) -> dict:
    prop = session.query(Property).one()
    parsed = parse_platform_email(body.text)
    listing = listing_for(session, prop, parsed.platform)
    check_in = parsed.check_in or datetime(2026, 9, 22, 14, 0)
    check_out = parsed.check_out or datetime(2026, 9, 25, 12, 0)
    result = ingest_booking(
        session,
        property_id=prop.id,
        platform=parsed.platform if parsed.platform != "unknown" else "avito",
        external_id=parsed.external_id,
        guest_name=parsed.guest_name,
        check_in=check_in,
        check_out=check_out,
        guests_count=parsed.guests_count,
        amount_cents=parsed.amount_cents,
        paid=parsed.paid,
        status=parsed.status,
        source="email",
        listing_id=listing.id if listing else None,
    )
    _notify_ingest(session, result)
    return {
        "ok": True,
        "parsed": parsed.__dict__,
        "created": result.created,
        "booking": _booking_dict(result.booking),
        "conflicts": [_conflict_brief(session, c) for c in result.conflicts],
    }


@app.post("/api/ical")
def ingest_ical(body: TextIn, session: Session = Depends(get_session)) -> dict:
    prop = session.query(Property).one()
    results = ingest_ics(session, prop, body.text)
    for result in results:
        _notify_ingest(session, result)
    return {
        "ok": True,
        "count": len(results),
        "bookings": [_booking_dict(r.booking) for r in results],
        "conflicts": [_conflict_brief(session, c) for r in results for c in r.conflicts],
    }


class IcalUrlIn(BaseModel):
    platform: str = ""
    url: str = ""


@app.post("/api/listings/ical")
def save_listing_ical(body: IcalUrlIn, session: Session = Depends(get_session)) -> dict:
    prop = session.query(Property).one()
    try:
        listing = set_listing_ical(session, prop, body.platform, body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    knowledge = dict(prop.knowledge or {})
    icals = dict(knowledge.get("ical") or {})
    if listing.platform:
        icals[listing.platform] = listing.ical_url
        knowledge["ical"] = icals
        prop.knowledge = knowledge
    return {"ok": True, "platform": listing.platform, "ical_url": listing.ical_url}


@app.post("/api/ical/sync")
def sync_ical(session: Session = Depends(get_session)) -> dict:
    """Кнопка «Обновить календари». Та же работа идёт сама раз в ICAL_SYNC_MINUTES."""
    return {"ok": True, "listings": sync_listings(session)}


class CloseIn(BaseModel):
    text: str = ""
    property_id: int | None = None


@app.post("/api/blocks")
def create_block(body: CloseIn, session: Session = Depends(get_session)) -> dict:
    prop = _one_property(session, body.property_id)
    rng = parse_close_command(body.text) or parse_close_command("закрой " + body.text)
    if not rng:
        raise HTTPException(400, "Не разобрала даты. Пример: закрой 20–22 сентября")
    block, hits = close_dates(session, prop.id, rng[0], rng[1], reason=body.text.strip())
    notify_owner(
        f"Закрыла даты {rng[0]:%d.%m}–{rng[1]:%d.%m}.\n"
        + (f"Пересекается с бронями: {', '.join(b.guest_name for b in hits)}" if hits else "Свободных пересечений нет.")
    )
    return {
        "ok": True,
        "block": _block_dict(block),
        "overlaps": [_booking_dict(b) for b in hits],
    }


class BlockActionIn(BaseModel):
    action: str = "open"


@app.post("/api/blocks/{block_id}")
def block_action(block_id: int, body: BlockActionIn, session: Session = Depends(get_session)) -> dict:
    """Снять закрытие дат. Бронь так не трогаем — только наш собственный блок."""
    if body.action != "open":
        raise HTTPException(400, "неизвестное действие")
    block = open_dates(session, block_id)
    if block is None:
        raise HTTPException(404, "нет такого закрытия")
    return {"ok": True, "opened": f"{block.start:%d.%m}–{block.end:%d.%m}"}


class ConflictIn(BaseModel):
    action: str  # cancel | dismiss
    booking_id: int | None = None


@app.post("/api/conflicts/{conflict_id}")
def resolve_conflict(conflict_id: int, body: ConflictIn, session: Session = Depends(get_session)) -> dict:
    conflict = session.get(BookingConflict, conflict_id)
    if not conflict:
        raise HTTPException(404)
    if body.action == "dismiss":
        dismiss_conflict(session, conflict)
        return {"ok": True, "status": conflict.status}
    if body.action == "cancel":
        booking_id = body.booking_id or conflict.cheaper_to_cancel_id
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(404)
        owner_cancel_in_calendar(session, booking)
        return {"ok": True, "cancelled": _booking_dict(booking), "status": conflict.status}
    raise HTTPException(400, "action: cancel | dismiss")


def _conflict_brief(session: Session, conflict: BookingConflict) -> dict:
    cheap = session.get(Booking, conflict.cheaper_to_cancel_id)
    return {
        "id": conflict.id,
        "cheaper_to_cancel": cheap.guest_name if cheap else "",
        "text": describe_conflict(session, conflict),
    }


def _notify_ingest(session: Session, result) -> None:
    notify_owner(describe_booking(result.booking, created=result.created))
    for conflict in result.conflicts:
        notify_owner(describe_conflict(session, conflict), conflict_keyboard(conflict))


@app.post("/api/bookings/{booking_id}/checkout")
def checkout(booking_id: int, session: Session = Depends(get_session)) -> dict:
    booking = session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404)
    task = create_cleaning_task(session, booking)
    return {"booking": _booking_dict(booking), "task": {"id": task.id, "title": task.title, "state": task.state, "card": task_card(session, task)}}


class CodeIn(BaseModel):
    text: str


@app.post("/api/bookings/{booking_id}/code")
def issue_code_api(booking_id: int, body: CodeIn, session: Session = Depends(get_session)) -> dict:
    booking = session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404)
    ok, payload = issue_code(session, booking, body.text)
    if not ok:
        return {"ok": False, "reason": payload, "code": None}
    if booking.guest_chat_id:
        notify_guest(booking.guest_chat_id, payload, booking.business_connection_id)
    return {
        "ok": True,
        "hint": booking.code_hint,
        "note": "Полный код в ответ API не отдаю. Гостю ушло, если известен чат.",
    }


@app.post("/api/bookings/{booking_id}/paid")
def mark_paid(booking_id: int, session: Session = Depends(get_session)) -> dict:
    booking = session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404)
    booking.paid = True
    if booking.status == "pending":
        booking.status = "confirmed"
    audit(session, "booking.paid", f"Оплата {booking.guest_name}", "owner", "owner", "confirm")
    return {"ok": True, "booking": _booking_dict(booking)}


@app.post("/api/bookings/{booking_id}/guest-link")
def guest_link_api(booking_id: int, session: Session = Depends(get_session)) -> dict:
    booking = session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404)
    try:
        url = issue_link(session, booking)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    row = active_link(session, booking.id)
    return {
        "url": url,
        "code": row.id if row else "",
        "expires_at": row.expires_at.isoformat() if row else "",
        "message": GUEST_PITCH,
    }


@app.post("/api/bookings/{booking_id}/guest-link/revoke")
def revoke_guest_link(booking_id: int, session: Session = Depends(get_session)) -> dict:
    booking = session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404)
    row = active_link(session, booking.id)
    if row:
        revoke(session, row.id)
    return {"ok": True}


class TaskIn(BaseModel):
    action: str
    note: str = ""


@app.post("/api/tasks/{task_id}")
def task_action(task_id: int, body: TaskIn, session: Session = Depends(get_session)) -> dict:
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404)
    if body.action == "accept":
        text = accept_task(session, task)
    elif body.action == "snooze":
        text = snooze_task(session, task, body.note or "перенос с веба")
    elif body.action == "photo":
        text = add_photo(session, task)
    elif body.action == "done":
        text = complete_task(session, task, body.note)
    else:
        raise HTTPException(400, "accept | snooze | photo | done")
    return {"ok": True, "state": task.state, "photo_count": task.photo_count, "text": text}


@app.post("/api/knowledge")
def knowledge_edit(body: TextIn, session: Session = Depends(get_session)) -> dict:
    prop = session.query(Property).one()
    parsed = parse_knowledge_edit(body.text)
    if not parsed:
        raise HTTPException(400, "Формат: парковка: новый текст")
    note = apply_knowledge_edit(prop, parsed[0], parsed[1])
    return {"ok": True, "note": note, "knowledge": prop.knowledge}


# --- итог проживания, регистрация в МВД, турналог ---


class OutcomeIn(BaseModel):
    review_status: str | None = None
    review_note: str | None = None
    receipt_status: str | None = None
    receipt_note: str | None = None
    deposit_status: str | None = None
    deposit_amount_cents: int | None = None
    deposit_returned_cents: int | None = None
    conflict_side: str | None = None
    conflict_note: str | None = None
    conflict_resolution: str | None = None


@app.get("/api/bookings/{booking_id}/outcome")
def get_outcome(booking_id: int, session: Session = Depends(get_session)) -> dict:
    booking = session.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(404, "нет такой брони")
    outcome = session.query(StayOutcome).filter_by(booking_id=booking_id).one_or_none()
    return {
        "booking": {
            "id": booking.id,
            "guest_name": booking.guest_name,
            "check_in": booking.check_in.isoformat(),
            "check_out": booking.check_out.isoformat(),
            "amount_cents": booking.amount_cents,
        },
        "outcome": outcome_dict(outcome),
        "summary": outcome_summary(outcome),
    }


@app.post("/api/bookings/{booking_id}/outcome")
def post_outcome(booking_id: int, body: OutcomeIn, request: Request,
                 session: Session = Depends(get_session)) -> dict:
    booking = session.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(404, "нет такой брони")
    who = auth.check(
        request.headers.get("X-Telegram-Init-Data", ""),
        request.cookies.get(auth.SESSION_COOKIE, ""),
    )
    outcome = save_outcome(session, booking_id, body.model_dump(exclude_none=True), by=who)
    audit(session, "stay.outcome", f"бронь {booking_id}: {outcome_summary(outcome)}", authorized_by=str(who or "web"))
    return {"ok": True, "outcome": outcome_dict(outcome), "summary": outcome_summary(outcome)}


@app.get("/api/registrations")
def registrations(session: Session = Depends(get_session)) -> dict:
    return registration_overview(session)


class RegistrationIn(BaseModel):
    full_name: str
    birth_date: str = ""
    citizenship: str = "РФ"
    status: str = "to_do"
    note: str = ""


@app.post("/api/bookings/{booking_id}/registrations")
def add_registration_route(booking_id: int, body: RegistrationIn,
                           session: Session = Depends(get_session)) -> dict:
    if session.get(Booking, booking_id) is None:
        raise HTTPException(404, "нет такой брони")
    if not body.full_name.strip():
        raise HTTPException(400, "нужно имя гостя")
    row = add_registration(session, booking_id, body.full_name, body.birth_date,
                           body.citizenship, body.status, body.note)
    return {"ok": True, "id": row.id}


class RegistrationActionIn(BaseModel):
    action: str
    status: str = ""


@app.post("/api/registrations/{row_id}")
def registration_action(row_id: int, body: RegistrationActionIn, request: Request,
                        session: Session = Depends(get_session)) -> dict:
    who = auth.check(
        request.headers.get("X-Telegram-Init-Data", ""),
        request.cookies.get(auth.SESSION_COOKIE, ""),
    )
    if body.action == "delete":
        if not remove_registration(session, row_id):
            raise HTTPException(404, "нет такой записи")
        return {"ok": True}
    if body.action == "status":
        if not set_registration_status(session, row_id, body.status, by=who):
            raise HTTPException(400, "неизвестный статус")
        return {"ok": True}
    raise HTTPException(400, "неизвестное действие")


@app.get("/api/tourist-tax")
def tourist_tax(year: int | None = None, month: int | None = None,
                session: Session = Depends(get_session)) -> dict:
    now = datetime.now()
    y = year or now.year
    m = month or now.month
    if m < 1 or m > 12:
        raise HTTPException(400, "месяц 1–12")
    return tourist_tax_report(session, y, m)


# --- ответы гостям: библиотека ситуаций и свои формулировки объекта ---

# Что спрашиваем при подключении объекта. Заполнил — и большинство ситуаций
# библиотеки начинают отвечать сами, без ручного написания текстов.
ONBOARDING = [
    {"key": "access", "title": "Как гость попадает внутрь", "hint": "код калитки, сейф, кейбокс, встреча"},
    {"key": "parking", "title": "Парковка", "hint": "где ставить машину, есть ли шлагбаум"},
    {"key": "wifi", "title": "Wi-Fi", "hint": "есть ли, когда сообщаете пароль"},
    {"key": "around", "title": "Что рядом", "hint": "далеко ли море, центр, магазины"},
    {"key": "beds", "title": "Спальные места", "hint": "какие кровати, есть ли диван"},
    {"key": "household", "title": "Быт и техника", "hint": "кондиционер, стиральная машина, посуда"},
    {"key": "pets_rule", "title": "Животные", "hint": "можно ли, на каких условиях"},
    {"key": "deposit", "title": "Залог", "hint": "сколько, когда возвращаете"},
    {"key": "transfer", "title": "Как добраться", "hint": "трансфер, аэропорт, вокзал"},
    {"key": "cleaning", "title": "Уборка и бельё", "hint": "меняете ли в середине проживания"},
    {"key": "noise", "title": "Тишина", "hint": "стройка, соседи, часы тишины — честно"},
    {"key": "host", "title": "Кто на связи", "hint": "имя администратора и режим"},
]


def _prop_or_404(session: Session, property_id: int | None) -> Property:
    prop = _one_property(session, property_id)
    if prop is None:
        raise HTTPException(404, "нет такого объекта")
    return prop


@app.get("/api/answers")
def answers_catalog(property_id: int | None = None, session: Session = Depends(get_session)) -> dict:
    prop = _prop_or_404(session, property_id)
    rows = answers_service.catalog(session, prop)
    known = answers_service.facts(prop)
    return {
        "property": {"id": prop.id, "title": prop.title},
        "rows": rows,
        "summary": answers_service.summary(rows),
        "onboarding": [
            {**item, "value": known.get(item["key"], "")} for item in ONBOARDING
        ],
    }


class AnswerIn(BaseModel):
    code: str
    text: str = ""
    enabled: bool = True
    property_id: int | None = None


@app.post("/api/answers")
def answers_save(body: AnswerIn, request: Request, session: Session = Depends(get_session)) -> dict:
    prop = _prop_or_404(session, body.property_id)
    who = auth.check(
        request.headers.get("X-Telegram-Init-Data", ""),
        request.cookies.get(auth.SESSION_COOKIE, ""),
    )
    answers_service.save_answer(session, prop, body.code, body.text, by=who, enabled=body.enabled)
    audit(session, "answers.edit", f"{prop.title}: ситуация «{body.code}»", authorized_by=str(who or "web"))
    return {"ok": True}


class CustomAnswerIn(BaseModel):
    title: str
    text: str
    keywords: list[str] = []
    question_class: str = "typical"
    property_id: int | None = None


@app.post("/api/answers/custom")
def answers_add_custom(body: CustomAnswerIn, request: Request,
                       session: Session = Depends(get_session)) -> dict:
    prop = _prop_or_404(session, body.property_id)
    if not body.title.strip() or not body.text.strip():
        raise HTTPException(400, "нужны название ситуации и текст ответа")
    if not [w for w in body.keywords if w.strip()]:
        raise HTTPException(400, "нужны слова-приметы, по которым узнать вопрос гостя")
    who = auth.check(
        request.headers.get("X-Telegram-Init-Data", ""),
        request.cookies.get(auth.SESSION_COOKIE, ""),
    )
    row = answers_service.add_custom(session, prop, body.title, body.text,
                                     body.keywords, body.question_class, by=who)
    return {"ok": True, "id": row.id}


@app.post("/api/answers/{answer_id}/delete")
def answers_delete(answer_id: int, property_id: int | None = None,
                   session: Session = Depends(get_session)) -> dict:
    prop = _prop_or_404(session, property_id)
    if not answers_service.drop(session, prop, answer_id):
        raise HTTPException(404, "нет такой записи")
    return {"ok": True}


class TryIn(BaseModel):
    text: str
    property_id: int | None = None


@app.post("/api/answers/try")
def answers_try(body: TryIn, session: Session = Depends(get_session)) -> dict:
    """Проверка: что уйдёт гостю на этот вопрос. Ничего не отправляет и не сохраняет."""
    prop = _prop_or_404(session, body.property_id)
    hit = answers_service.match(session, prop, body.text)
    result = classify_and_draft(body.text, session=session, prop=prop)
    return {
        "matched": hit,
        "draft": result.draft,
        "question_class": result.question_class,
        "policy_mode": result.policy_mode,
        "needs_owner": result.needs_owner,
        "source": result.source,
    }


class FactsIn(BaseModel):
    facts: dict
    property_id: int | None = None


@app.post("/api/answers/facts")
def answers_facts(body: FactsIn, session: Session = Depends(get_session)) -> dict:
    """Мастер подключения объекта: заполняем факты, из них живут шаблоны библиотеки."""
    prop = _prop_or_404(session, body.property_id)
    known = dict(prop.knowledge or {})
    for key, value in body.facts.items():
        if key in {item["key"] for item in ONBOARDING}:
            known[key] = str(value or "").strip()[:2000]
    prop.knowledge = known
    session.flush()
    rows = answers_service.catalog(session, prop)
    return {"ok": True, "summary": answers_service.summary(rows)}


class ListingUrlIn(BaseModel):
    platform: str
    url: str = ""
    property_id: int | None = None


@app.post("/api/listings/url")
def set_listing_url(body: ListingUrlIn, session: Session = Depends(get_session)) -> dict:
    """Ссылка на объявление витрины — чтобы не искать, где что висит."""
    prop = _one_property(session, body.property_id)
    listing = session.query(Listing).filter_by(property_id=prop.id, platform=body.platform).first()
    if listing is None:
        listing = Listing(property_id=prop.id, platform=body.platform)
        session.add(listing)
    url = body.url.strip()
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(400, "ссылка должна начинаться с http:// или https://")
    listing.url = url[:500]
    session.flush()
    return {"ok": True, "platform": body.platform, "url": listing.url}


@app.get("/api/mail/check")
def mail_check() -> dict:
    """Отвечает ли почтовый ящик с письмами площадок."""
    from pomogaika.platforms.mailbox import check as check_mailbox

    ok, note = check_mailbox()
    return {"ok": ok, "note": note, "host": settings.imap_host, "user": settings.imap_user}


@app.post("/api/mail/poll")
def mail_poll(history: bool = False, limit: int = 20,
              session: Session = Depends(get_session)) -> dict:
    """Разобрать письма прямо сейчас. history=true — разовый проход по всему ящику."""
    from pomogaika.services.mail import poll as poll_mail

    return {"ok": True, **poll_mail(session, limit=limit, unseen_only=not history)}
