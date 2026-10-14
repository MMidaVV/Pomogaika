from datetime import datetime, timedelta

import pytest

from pomogaika.domain.calendar import overlaps
from pomogaika.domain.policy import check
from pomogaika.platforms.email_parser import parse_platform_email
from pomogaika.services.copilot import classify_and_draft
from pomogaika.services.llm import parse_model_json, sanitize_draft


@pytest.fixture(autouse=True)
def _offline_llm(monkeypatch):
    monkeypatch.setattr("pomogaika.services.copilot.llm_enabled", lambda: False)
    monkeypatch.setattr("pomogaika.services.llm.enabled", lambda: False)


def test_adjacent_same_day_is_not_conflict():
    a_in = datetime(2026, 9, 15, 14, 0)
    a_out = datetime(2026, 9, 18, 12, 0)
    b_in = datetime(2026, 9, 18, 14, 0)
    b_out = datetime(2026, 9, 20, 12, 0)
    assert overlaps(a_in, a_out, b_in, b_out) is False


def test_true_overlap():
    a_in = datetime(2026, 9, 19, 14, 0)
    a_out = datetime(2026, 9, 22, 12, 0)
    b_in = datetime(2026, 9, 21, 14, 0)
    b_out = datetime(2026, 9, 24, 12, 0)
    assert overlaps(a_in, a_out, b_in, b_out) is True


def test_busy_blocks_on_two_platforms_are_mirrors():
    from pomogaika.domain.calendar import is_occupancy_mirror

    a_in = datetime(2026, 8, 15, 14, 0)
    a_out = datetime(2026, 8, 16, 12, 0)
    b_in = datetime(2026, 8, 15, 14, 0)
    b_out = datetime(2026, 8, 23, 12, 0)
    assert is_occupancy_mirror(
        "avito", "Avito (Closed)", a_in, a_out,
        "sutochno", "Забронировано", b_in, b_out,
    )
    assert is_occupancy_mirror(
        "avito", "Avito (Closed)", datetime(2026, 6, 29, 14, 0), datetime(2026, 6, 30, 12, 0),
        "sutochno", "Забронировано", datetime(2026, 6, 29, 14, 0), datetime(2026, 6, 30, 12, 0),
    )


def test_two_named_guests_same_nights_is_conflict():
    from pomogaika.domain.calendar import is_occupancy_mirror

    a_in = datetime(2026, 9, 19, 14, 0)
    a_out = datetime(2026, 9, 22, 12, 0)
    b_in = datetime(2026, 9, 21, 14, 0)
    b_out = datetime(2026, 9, 24, 12, 0)
    assert not is_occupancy_mirror(
        "yandex", "Дарья", a_in, a_out,
        "sutochno", "А.", b_in, b_out,
    )


def test_refund_never():
    d = check("refund")
    assert d.mode == "never"
    assert d.allowed is False


def test_door_code_unpaid():
    d = check("door_code", {"paid": False, "check_in": datetime.now() + timedelta(hours=1)})
    assert d.allowed is False


def test_door_code_too_early():
    now = datetime(2026, 9, 17, 12, 0)
    d = check("door_code", {
        "paid": True,
        "now": now,
        "check_in": datetime(2026, 9, 20, 14, 0),
        "check_out": datetime(2026, 9, 22, 12, 0),
    })
    assert d.allowed is False
    assert "Рано" in d.reason


def test_sutochno_email():
    p = parse_platform_email(
        """У вас новый запрос на бронь №42910245
        Бронь №42910245 | Объект №2323816
        Гость: А.
        Число гостей: 2
        Даты: 22.09.2026 — 25.09.2026
        """
    )
    assert p.platform == "sutochno"
    assert p.kind == "request"
    assert p.check_in.day == 22
    assert p.check_out.day == 25


def test_copilot_parking():
    r = classify_and_draft("Где парковка и можно ли на машине через шлагбаум?")
    assert r.question_class == "typical"
    assert r.needs_owner is True
    assert "шлагбаум" in r.draft.lower() or "Парковка" in r.draft


def test_copilot_discount_is_human():
    r = classify_and_draft("Можно скидку 20% если на три ночи?")
    assert r.question_class == "money"
    assert r.policy_mode == "never"


def test_close_command_september():
    from pomogaika.domain.dates import parse_close_command

    now = datetime(2026, 9, 17, 12, 0)
    rng = parse_close_command("закрой 20–22 сентября", now=now)
    assert rng is not None
    assert rng[0].day == 20 and rng[0].month == 9 and rng[0].hour == 14
    assert rng[1].day == 22 and rng[1].hour == 12


def test_ical_vevent():
    from pomogaika.platforms.ical import parse_ics

    events = parse_ics(
        """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:abc-1
SUMMARY:Гость К
DTSTART:20260922T140000
DTEND:20260925T120000
END:VEVENT
END:VCALENDAR"""
    )
    assert len(events) == 1
    assert events[0].uid == "abc-1"
    assert events[0].check_in.day == 22
    assert events[0].check_out.day == 25


def test_copilot_code_too_early_uses_booking():
    from pomogaika.models import Booking

    booking = Booking(
        property_id=1,
        platform="yandex",
        guest_name="Дарья",
        check_in=datetime(2026, 9, 20, 14, 0),
        check_out=datetime(2026, 9, 22, 12, 0),
        paid=True,
        status="confirmed",
    )
    r = classify_and_draft(
        "Я Дарья, какой код от калитки?",
        booking,
        now=datetime(2026, 9, 17, 12, 0),
    )
    assert r.question_class == "typical"
    assert "Рано" in r.draft or "рано" in r.draft.lower() or "2 часа" in r.draft
    assert "Дарья" in r.draft


def test_copilot_code_unpaid_booking():
    from pomogaika.models import Booking

    booking = Booking(
        property_id=1,
        platform="sutochno",
        guest_name="К.",
        check_in=datetime.now() + timedelta(hours=1),
        check_out=datetime.now() + timedelta(days=2),
        paid=False,
        status="pending",
    )
    r = classify_and_draft("Пришлите код домофона", booking)
    assert "оплат" in r.draft.lower()
    assert r.needs_owner is True
    assert r.issue_code is False


class _Mem:
    def add(self, _row):
        return None


def test_issue_code_manual_contour_keeps_only_hint():
    from pomogaika.models import Booking
    from pomogaika.services.codes import issue_code

    now = datetime(2026, 9, 17, 16, 0)
    booking = Booking(
        property_id=1,
        platform="avito",
        guest_name="Михаил",
        check_in=datetime(2026, 9, 17, 14, 0),
        check_out=datetime(2026, 9, 18, 12, 0),
        paid=True,
        status="staying",
    )
    ok, msg = issue_code(_Mem(), booking, "калитка 4412, домофон 7788, сейф 3301", now=now)
    assert ok is True
    assert "4412" in msg
    assert booking.code_hint == "01"
    assert "4412" not in (booking.code_hint or "")


def test_issue_code_refuses_too_early():
    from pomogaika.models import Booking
    from pomogaika.services.codes import issue_code

    booking = Booking(
        property_id=1,
        platform="yandex",
        guest_name="Дарья",
        check_in=datetime(2026, 9, 20, 14, 0),
        check_out=datetime(2026, 9, 22, 12, 0),
        paid=True,
        status="confirmed",
    )
    ok, reason = issue_code(_Mem(), booking, "9999", now=datetime(2026, 9, 17, 12, 0))
    assert ok is False
    assert "Рано" in reason


def test_knowledge_rejects_password():
    from pomogaika.models import Property
    from pomogaika.services.knowledge import apply_knowledge_edit, parse_knowledge_edit

    parsed = parse_knowledge_edit("парковка: карта на шлагбаум по звонку")
    assert parsed == ("parking", "карта на шлагбаум по звонку")
    prop = Property(owner_id=1, code="k", title="t", address="a", city="c", knowledge={})
    msg = apply_knowledge_edit(prop, "wifi", "123456")
    assert "не кладу" in msg or "код" in msg.lower()
    assert prop.knowledge.get("wifi") != "123456"


def test_parse_model_json_strips_fence_and_think():
    raw = """<think>нет</think>
```json
{"question_class":"typical","matched_faq":"Где парковка? Можно на машине?","draft":"Парковка бесплатная, карту на шлагбаум даём по запросу."}
```"""
    parsed = parse_model_json(raw)
    assert parsed is not None
    assert parsed["question_class"] == "typical"
    assert "шлагбаум" in parsed["draft"]


def test_sanitize_draft_hides_door_code_keeps_deposit():
    text = sanitize_draft("Залог 5000 ₽. Код калитки 4412 не сообщаю.")
    assert "5000" in text
    assert "4412" not in text
    assert "****" in text


def test_llm_paraphrase_parking(monkeypatch):
    monkeypatch.setattr("pomogaika.services.copilot.llm_enabled", lambda: True)
    monkeypatch.setattr(
        "pomogaika.services.copilot.interpret_guest",
        lambda *_a, **_k: {
            "question_class": "typical",
            "matched_faq": "Где парковка? Можно на машине?",
            "draft": "Парковка бесплатная за территорией, карту на шлагбаум даём по запросу.",
        },
    )
    r = classify_and_draft("куда у вас машину деть?", use_llm=True)
    assert r.source == "llm"
    assert r.question_class == "typical"
    assert "шлагбаум" in r.draft.lower() or "парков" in r.draft.lower()
    assert r.matched_faq.startswith("LLM")


def test_miniapp_nights_exclude_checkout_day():
    from datetime import date

    from pomogaika.services.miniapp import _nights

    nights = _nights(datetime(2026, 9, 20, 14, 0), datetime(2026, 9, 22, 12, 0))
    assert nights == [date(2026, 9, 20), date(2026, 9, 21)]


def test_build_miniapp_from_seed(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from pomogaika.db import Base
    from pomogaika.services.miniapp import build_miniapp
    from pomogaika.services.seed import seed_if_empty

    engine = create_engine(f"sqlite:///{tmp_path / 'mini.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    seed_if_empty(session)
    session.commit()
    payload = build_miniapp(session, year=2026, month=9)
    assert payload["month"]["days"] == 30
    assert len(payload["calendar"]) == 30
    plats = {p["platform"] for p in payload["platforms"]}
    assert {"avito", "sutochno", "ostrovok", "yandex"} <= plats
    assert "occupancy" in payload["month"]
    assert "arriving" in payload


def test_create_property_adds_empty_object(tmp_path):
    """Новый объект заводится с пустой базой знаний и пустыми объявлениями."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from pomogaika.db import Base
    from pomogaika.models import Listing, Owner, Property
    from pomogaika.services.properties import create_property
    from pomogaika.services.seed import seed_if_empty

    engine = create_engine(f"sqlite:///{tmp_path / 'add.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    seed_if_empty(session)
    session.commit()

    owner = session.query(Owner).one()
    first = session.query(Property).one()
    prop = create_property(
        session,
        owner=owner,
        title="  Студия на Курортном  ",
        address="Курортный просп., 12, кв. 4",
        check_in_from="15:00",
        max_guests=3,
    )
    session.commit()

    assert prop.title == "Студия на Курортном"
    assert prop.code != first.code
    assert prop.city == first.city  # город подхватывается у соседнего объекта
    assert prop.check_in_from == "15:00"
    assert prop.max_guests == 3
    # чужие факты не наследуем: иначе гостю ушёл бы чужой код от калитки
    assert prop.knowledge == {}
    plats = {listing.platform for listing in session.query(Listing).filter_by(property_id=prop.id)}
    assert plats == {"avito", "sutochno", "ostrovok", "yandex"}
    assert all(not listing.ical_url for listing in session.query(Listing).filter_by(property_id=prop.id))

    with pytest.raises(ValueError):
        create_property(session, owner=owner, title="   ")


def test_build_miniapp_scales_to_several_properties(tmp_path):
    """Второй объект не должен ронять сводку и не должен закрывать календарь первого."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from pomogaika.db import Base
    from pomogaika.models import Booking, Property
    from pomogaika.services.miniapp import build_miniapp
    from pomogaika.services.seed import seed_if_empty

    engine = create_engine(f"sqlite:///{tmp_path / 'multi.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    seed_if_empty(session)
    session.commit()

    first = session.query(Property).one()
    second = Property(
        owner_id=first.owner_id,
        code="second",
        title="Студия на Курортном",
        address="Курортный просп., 12, кв. 4",
        city=first.city,
        knowledge={},
    )
    session.add(second)
    session.flush()
    session.add(
        Booking(
            property_id=second.id,
            platform="avito",
            guest_name="Соколов",
            check_in=datetime(2026, 9, 10, 15),
            check_out=datetime(2026, 9, 12, 11),
            amount_cents=10000_00,
            paid=True,
            status="confirmed",
        )
    )
    session.commit()

    payload = build_miniapp(session, year=2026, month=9)
    assert [o["id"] for o in payload["objects"]] == [first.id, second.id]
    assert payload["month"]["slots"] == 60  # 30 ночей × 2 объекта
    assert all("property_id" in b for b in payload["bookings"])

    # бронь второго объекта не занимает первый: 10 сентября первый ещё свободен
    sep10 = payload["objects"][0]["free"][9]
    assert sep10 == 1
    assert payload["objects"][1]["free"][9] == 0
    assert payload["calendar"][9]["free_count"] == 1
    assert payload["calendar"][9]["total"] == 2

    # и фильтр по объекту отдаёт только его
    only_second = build_miniapp(session, year=2026, month=9, property_id=second.id)
    assert len(only_second["objects"]) == 1
    assert {b["property_id"] for b in only_second["bookings"]} == {second.id}


def test_llm_cannot_promise_discount(monkeypatch):
    monkeypatch.setattr("pomogaika.services.copilot.llm_enabled", lambda: True)
    monkeypatch.setattr(
        "pomogaika.services.copilot.interpret_guest",
        lambda *_a, **_k: {
            "question_class": "typical",
            "matched_faq": "",
            "draft": "Да, сделаем скидку 20%, без проблем.",
        },
    )
    r = classify_and_draft("Можно скидку 20% если на три ночи?", use_llm=True)
    assert r.question_class == "money"
    assert r.policy_mode == "never"
    assert "20%" not in r.draft
    assert "собственник" in r.draft.lower() or "не обещаю" in r.draft.lower()


# --- подпись Telegram: initData мини-приложения ---

def _init_data(token: str, user_id: int = 42, age_seconds: int = 0) -> str:
    import hashlib
    import hmac
    import json as _json
    import time
    from urllib.parse import urlencode

    pairs = {
        "auth_date": str(int(time.time()) - age_seconds),
        "query_id": "AAA",
        "user": _json.dumps({"id": user_id}, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


@pytest.fixture
def _bot_token(monkeypatch):
    from pomogaika.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", "test-token-123")
    return "test-token-123"


def test_init_data_valid(_bot_token):
    from pomogaika.web.auth import verify_init_data

    assert verify_init_data(_init_data(_bot_token, user_id=42)) == 42


def test_init_data_signed_by_another_token(_bot_token):
    from pomogaika.web.auth import verify_init_data

    assert verify_init_data(_init_data("someone-elses-token")) is None


def test_init_data_tampered(_bot_token):
    from pomogaika.web.auth import verify_init_data

    from urllib.parse import parse_qsl, urlencode

    pairs = dict(parse_qsl(_init_data(_bot_token, user_id=42)))
    pairs["user"] = '{"id":777}'  # подпись оставили прежнюю, подменили только пользователя
    assert verify_init_data(urlencode(pairs)) is None


def test_init_data_expired(_bot_token):
    from pomogaika.web.auth import verify_init_data

    assert verify_init_data(_init_data(_bot_token, age_seconds=48 * 3600)) is None


def test_init_data_empty_or_garbage(_bot_token):
    from pomogaika.web.auth import verify_init_data

    assert verify_init_data("") is None
    assert verify_init_data("hash=deadbeef&user=%7B%22id%22%3A1%7D") is None


def test_auth_disabled_without_token(monkeypatch):
    from pomogaika.config import settings
    from pomogaika.web.auth import enabled

    monkeypatch.setattr(settings, "telegram_bot_token", "")
    assert enabled() is False


# --- вход через браузер: виджет Telegram и cookie-сессия ---

def _widget_params(token: str, user_id: int = 42, age_seconds: int = 0) -> dict:
    import hashlib
    import hmac
    import time

    pairs = {
        "auth_date": str(int(time.time()) - age_seconds),
        "first_name": "Рита",
        "id": str(user_id),
    }
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hashlib.sha256(token.encode()).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return pairs


def test_login_widget_valid(_bot_token):
    from pomogaika.web.auth import verify_login_widget

    assert verify_login_widget(_widget_params(_bot_token, user_id=42)) == 42


def test_login_widget_tampered(_bot_token):
    from pomogaika.web.auth import verify_login_widget

    params = _widget_params(_bot_token, user_id=42)
    params["id"] = "777"  # подпись оставили прежнюю
    assert verify_login_widget(params) is None


def test_login_widget_wrong_token(_bot_token):
    from pomogaika.web.auth import verify_login_widget

    assert verify_login_widget(_widget_params("someone-elses-token")) is None


def test_login_widget_expired(_bot_token):
    from pomogaika.web.auth import verify_login_widget

    assert verify_login_widget(_widget_params(_bot_token, age_seconds=48 * 3600)) is None


def test_session_roundtrip(_bot_token):
    from pomogaika.web.auth import make_session, read_session

    assert read_session(make_session(42)) == 42


def test_session_tampered(_bot_token):
    from pomogaika.web.auth import make_session, read_session

    cookie = make_session(42)
    user_id, expires, signature = cookie.split(".")
    assert read_session(f"777.{expires}.{signature}") is None


def test_session_expired(_bot_token):
    from pomogaika.web.auth import make_session, read_session

    assert read_session(make_session(42, days=-1)) is None


def test_session_garbage(_bot_token):
    from pomogaika.web.auth import read_session

    assert read_session("") is None
    assert read_session("not-a-session") is None


# --- приглашения и доступ ---

@pytest.fixture
def _db(tmp_path, monkeypatch):
    """Отдельная база на тест: настоящую не трогаем."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from pomogaika.config import settings
    from pomogaika.db import Base

    monkeypatch.setattr(settings, "telegram_owner_user_id", 100)
    monkeypatch.setattr(settings, "telegram_cleaner_user_id", None)
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_invite_grants_access(_db):
    from pomogaika.services.access import accept_invite, allowed_ids, create_invite

    invite = create_invite(_db, created_by=100)
    assert 555 not in allowed_ids(_db)

    ok, _ = accept_invite(_db, invite.id, user_id=555, name="Админ")
    assert ok
    assert 555 in allowed_ids(_db)


def test_invite_is_single_use(_db):
    from pomogaika.services.access import accept_invite, create_invite

    invite = create_invite(_db, created_by=100)
    assert accept_invite(_db, invite.id, user_id=555)[0] is True

    ok, reply = accept_invite(_db, invite.id, user_id=666)
    assert ok is False
    assert "уже воспользовались" in reply


def test_invite_expires(_db):
    from datetime import datetime, timedelta

    from pomogaika.services.access import accept_invite, create_invite

    invite = create_invite(_db, created_by=100)
    invite.created_at = datetime.utcnow() - timedelta(hours=25)
    _db.flush()

    ok, reply = accept_invite(_db, invite.id, user_id=555)
    assert ok is False
    assert "просрочено" in reply


def test_unknown_invite(_db):
    from pomogaika.services.access import accept_invite

    assert accept_invite(_db, "нет-такого", user_id=555)[0] is False


def test_revoke_takes_access_away(_db):
    from pomogaika.services.access import accept_invite, allowed_ids, create_invite, revoke

    invite = create_invite(_db, created_by=100)
    accept_invite(_db, invite.id, user_id=555)
    assert revoke(_db, 555) is True
    assert 555 not in allowed_ids(_db)


def test_owner_cannot_be_revoked(_db):
    from pomogaika.services.access import allowed_ids, revoke

    assert revoke(_db, 100) is False
    assert 100 in allowed_ids(_db)


def test_cleaner_is_not_staff(_db, monkeypatch):
    from pomogaika.config import settings
    from pomogaika.services.access import allowed_ids, staff_ids

    monkeypatch.setattr(settings, "telegram_cleaner_user_id", 200)
    assert 200 in allowed_ids(_db)      # в мини-приложение клинера пускаем
    assert 200 not in staff_ids(_db)    # пультом собственника он не становится


# --- итог проживания, регистрация в МВД, турналог ---

def _booking(session, **kw):
    from datetime import datetime

    from pomogaika.models import Booking

    defaults = dict(
        property_id=1, listing_id=None, platform="avito", guest_name="Гость",
        check_in=datetime(2026, 7, 10, 14, 0), check_out=datetime(2026, 7, 13, 12, 0),
        guests_count=2, amount_cents=900000, paid=True, status="confirmed",
        external_id="x1", source="test",
    )
    defaults.update(kw)
    booking = Booking(**defaults)
    session.add(booking)
    session.flush()
    return booking


def test_outcome_saves_only_known_values(_db):
    from pomogaika.services.outcome import as_dict, save

    booking = _booking(_db)
    out = save(_db, booking.id, {
        "review_status": "received",
        "receipt_status": "какая-то чушь",   # неизвестное значение игнорируем
        "deposit_status": "withheld",
        "deposit_amount_cents": 500000,
        "deposit_returned_cents": 300000,
        "deposit_reason": "разбито стекло",
        "conflict_side": "guest",
    })
    data = as_dict(out)
    assert data["review_status"] == "received"
    assert data["receipt_status"] == "none"          # осталось прежним, а не упало
    assert data["deposit_amount_cents"] == 500000
    assert data["conflict_side"] == "guest"


def test_outcome_filled_requires_all_four(_db):
    from pomogaika.services.outcome import is_filled, save

    booking = _booking(_db)
    out = save(_db, booking.id, {"review_status": "received"})
    assert is_filled(out) is False

    out = save(_db, booking.id, {
        "review_status": "received", "receipt_status": "issued",
        "deposit_status": "returned", "conflict_side": "none", "conflict_note": "не было",
    })
    assert is_filled(out) is True


def test_outcome_reminder_only_for_old_checkouts(_db):
    from datetime import datetime, timedelta

    from pomogaika.services.outcome import pending

    fresh = _booking(_db, external_id="fresh", checked_out_at=datetime.utcnow() - timedelta(hours=2))
    old = _booking(_db, external_id="old", checked_out_at=datetime.utcnow() - timedelta(hours=30))
    ids = [b.id for b in pending(_db, hours=24)]
    assert old.id in ids
    assert fresh.id not in ids


def test_outcome_reminder_not_repeated(_db):
    from datetime import datetime, timedelta

    from pomogaika.services.outcome import get_or_create, pending

    booking = _booking(_db, checked_out_at=datetime.utcnow() - timedelta(hours=30))
    out = get_or_create(_db, booking.id)
    out.reminded_at = datetime.utcnow()
    _db.flush()
    assert [b.id for b in pending(_db, hours=24)] == []


def test_registration_flow(_db):
    from pomogaika.services.registration import add, for_booking, overview, remove, set_status

    booking = _booking(_db)
    row = add(_db, booking.id, "Иванов Иван", "01.02.1990", "РФ")
    assert for_booking(_db, booking.id)[0].status == "to_do"

    assert set_status(_db, row.id, "submitted", by=1) is True
    assert for_booking(_db, booking.id)[0].submitted_at is not None
    assert set_status(_db, row.id, "непонятно") is False

    # страница показывает окно вокруг сегодня, поэтому бронь должна быть свежей
    from datetime import datetime, timedelta

    booking.check_in = datetime.utcnow() + timedelta(days=1)
    booking.check_out = datetime.utcnow() + timedelta(days=4)
    _db.flush()
    data = overview(_db)
    assert data["bookings"][0]["registrations"][0]["full_name"] == "Иванов Иван"

    assert remove(_db, row.id) is True
    assert for_booking(_db, booking.id) == []


def test_tourist_tax_counts_nights_in_month(_db):
    from datetime import datetime

    from pomogaika.services.tourist_tax import report

    # бронь на стыке месяцев: 3 ночи в июне, 2 в июле
    _booking(_db, check_in=datetime(2026, 6, 29, 14, 0), check_out=datetime(2026, 7, 4, 12, 0),
             amount_cents=1000000, external_id="edge")
    june = report(_db, 2026, 6)
    july = report(_db, 2026, 7)
    assert june["totals"]["nights"] == 2   # 29, 30 июня
    assert july["totals"]["nights"] == 3   # 1, 2, 3 июля
    assert june["totals"]["base_rub"] + july["totals"]["base_rub"] == 10000


def test_tourist_tax_minimum_per_night(_db):
    from datetime import datetime

    from pomogaika.services.tourist_tax import report

    # дешёвая бронь: 1% меньше минимума в 100 ₽ за сутки
    _booking(_db, check_in=datetime(2026, 8, 1, 14, 0), check_out=datetime(2026, 8, 3, 12, 0),
             amount_cents=200000, external_id="cheap")
    data = report(_db, 2026, 8)
    assert data["totals"]["tax_rub"] == 200      # 2 ночи по 100 ₽
    assert data["rows"][0]["by_minimum"] is True


def test_tourist_tax_ignores_cancelled(_db):
    from datetime import datetime

    from pomogaika.services.tourist_tax import report

    _booking(_db, check_in=datetime(2026, 9, 1, 14, 0), check_out=datetime(2026, 9, 5, 12, 0),
             status="cancelled", external_id="dead")
    assert report(_db, 2026, 9)["totals"]["nights"] == 0


def test_registration_page_shows_window_around_today(_db):
    from datetime import datetime, timedelta

    from pomogaika.services.registration import overview

    soon = _booking(_db, external_id="soon",
                    check_in=datetime.utcnow() + timedelta(days=2),
                    check_out=datetime.utcnow() + timedelta(days=5))
    long_ago = _booking(_db, external_id="old-stay",
                        check_in=datetime.utcnow() - timedelta(days=90),
                        check_out=datetime.utcnow() - timedelta(days=87))
    ids = [b["booking_id"] for b in overview(_db)["bookings"]]
    assert soon.id in ids
    assert long_ago.id not in ids


# --- снятие закрытых дат ---

def test_parse_open_command():
    from pomogaika.domain.dates import parse_close_command, parse_open_command

    assert parse_open_command("открой 28-30 сентября") is not None
    assert parse_open_command("сними 28-30 сентября") is not None
    # «закрой» и «открой» не путаются между собой
    assert parse_open_command("закрой 28-30 сентября") is None
    assert parse_close_command("открой 28-30 сентября") is None


def test_open_dates_removes_block(_db):
    from datetime import datetime

    from pomogaika.models import Block
    from pomogaika.services.bookings import open_dates

    block = Block(property_id=1, start=datetime(2026, 9, 28, 14, 0),
                  end=datetime(2026, 9, 30, 12, 0), reason="ремонт", source="owner")
    _db.add(block)
    _db.flush()
    block_id = block.id

    assert open_dates(_db, block_id) is not None
    assert _db.get(Block, block_id) is None
    assert open_dates(_db, block_id) is None   # второй раз — уже нечего снимать


def test_open_dates_clears_block_conflicts(_db):
    from datetime import datetime

    from pomogaika.models import Block, BookingConflict
    from pomogaika.services.bookings import open_dates

    block = Block(property_id=1, start=datetime(2026, 9, 28, 14, 0),
                  end=datetime(2026, 9, 30, 12, 0), reason="ремонт", source="owner")
    _db.add(block)
    _db.flush()
    _db.add(BookingConflict(property_id=1, booking_a_id=1, booking_b_id=1,
                            cheaper_to_cancel_id=1, block_id=block.id, status="open"))
    _db.flush()

    open_dates(_db, block.id)
    assert _db.query(BookingConflict).filter_by(block_id=block.id).count() == 0


def test_blocks_in_range_finds_overlap(_db):
    from datetime import datetime

    from pomogaika.models import Block
    from pomogaika.services.bookings import blocks_in_range

    _db.add(Block(property_id=1, start=datetime(2026, 9, 28, 14, 0),
                  end=datetime(2026, 9, 30, 12, 0), reason="ремонт", source="owner"))
    _db.flush()

    hit = blocks_in_range(_db, 1, datetime(2026, 9, 29, 14, 0), datetime(2026, 10, 2, 12, 0))
    miss = blocks_in_range(_db, 1, datetime(2026, 10, 5, 14, 0), datetime(2026, 10, 7, 12, 0))
    assert len(hit) == 1
    assert miss == []


# --- автоматическая синхронизация календарей ---

def test_sync_runs_once_per_interval(_db, monkeypatch):
    """Фоновый цикл крутится каждые 40 секунд — тянуть площадки он должен реже."""
    from datetime import datetime, timedelta

    from pomogaika.config import settings
    from pomogaika.services import watch

    calls = []
    monkeypatch.setattr(watch, "sync_listings", lambda session, **kw: calls.append(1) or [])
    monkeypatch.setattr(settings, "ical_sync_minutes", 30)
    monkeypatch.setattr(watch, "_last_sync", None)

    watch._sync_calendars(_db)
    watch._sync_calendars(_db)          # сразу следом — не должно дёргать площадки
    assert len(calls) == 1

    watch._last_sync = datetime.utcnow() - timedelta(minutes=31)
    watch._sync_calendars(_db)
    assert len(calls) == 2


def test_sync_can_be_switched_off(_db, monkeypatch):
    from pomogaika.config import settings
    from pomogaika.services import watch

    calls = []
    monkeypatch.setattr(watch, "sync_listings", lambda session, **kw: calls.append(1) or [])
    monkeypatch.setattr(settings, "ical_sync_minutes", 0)
    monkeypatch.setattr(watch, "_last_sync", None)

    watch._sync_calendars(_db)
    assert calls == []


def test_sync_survives_dead_platform(_db, monkeypatch):
    """Одна площадка молчит — остальные всё равно забираются."""
    from pomogaika.models import Listing, Property
    from pomogaika.services import sync as sync_module

    _db.add(Property(id=1, owner_id=1, code="kava", title="KaVa", address="", city="",
                     check_in_from="14:00", check_out_until="12:00", max_guests=4))
    _db.add(Listing(property_id=1, platform="avito", ical_url="http://dead"))
    _db.add(Listing(property_id=1, platform="sutochno", ical_url="http://alive"))
    _db.flush()

    def fake_fetch(url):
        if url == "http://dead":
            raise RuntimeError("таймаут")
        return "BEGIN:VCALENDAR\nEND:VCALENDAR"

    monkeypatch.setattr(sync_module, "fetch_ics", fake_fetch)
    report = sync_module.sync_listings(_db, announce="none")
    assert [r["ok"] for r in report] == [False, True]
    assert report[0]["error"] == "таймаут"


# --- тихая синхронизация и уведомления о заезде/выезде ---

def test_sync_summary_is_one_short_message():
    from datetime import datetime

    from pomogaika.services.sync import summary_line

    class B:
        def __init__(self, name, a, b):
            self.guest_name, self.check_in, self.check_out = name, a, b

    report = [{"platform": "avito", "ok": True, "count": 12, "created": 1},
              {"platform": "ostrovok", "ok": False, "error": "таймаут"}]
    text = summary_line(report, [B("Гость", datetime(2026, 9, 27), datetime(2026, 9, 30))])
    assert text.startswith("Календари обновлены")
    assert "Не ответили: Островок" in text
    assert "Новых броней: 1" in text
    assert len(text.splitlines()) <= 3          # коротко, а не простыня карточек

    quiet = summary_line([{"platform": "avito", "ok": True, "count": 12, "created": 0}], [])
    assert "Новых броней нет." in quiet


def test_background_sync_stays_silent_without_changes(_db, monkeypatch):
    from pomogaika.services import sync as sync_module

    said = []
    monkeypatch.setattr(sync_module, "notify_owner", lambda text, kb=None: said.append(text))
    sync_module._announce([{"platform": "avito", "ok": True, "count": 5, "created": 0}], [], "changes")
    assert said == []                            # фон молчит, когда ничего не изменилось

    sync_module._announce([{"platform": "avito", "ok": True, "count": 5, "created": 0}], [], "summary")
    assert len(said) == 1                        # по кнопке — одна строка


def test_background_sync_speaks_when_platform_is_silent(_db, monkeypatch):
    from pomogaika.services import sync as sync_module

    said = []
    monkeypatch.setattr(sync_module, "notify_owner", lambda text, kb=None: said.append(text))
    sync_module._announce([{"platform": "avito", "ok": False, "error": "таймаут"}], [], "changes")
    assert len(said) == 1 and "Не ответили" in said[0]


def test_arrival_and_departure_notices_fire_once(_db, monkeypatch):
    from datetime import datetime, timedelta

    from pomogaika.services import watch

    said = []
    monkeypatch.setattr(watch, "notify_owner", lambda text, kb=None: said.append(text))

    booking = _booking(_db, guest_name="Пётр",
                       check_in=datetime.utcnow() - timedelta(hours=3),
                       check_out=datetime.utcnow() - timedelta(hours=1))
    watch._stay_notices(_db)
    assert len(said) == 2
    assert said[0].startswith("Заезд: Пётр")
    assert said[1].startswith("Выезд: Пётр")

    watch._stay_notices(_db)                     # второй проход — молчим
    assert len(said) == 2
    assert booking.arrival_notified and booking.departure_notified


def test_no_arrival_notice_before_check_in(_db, monkeypatch):
    from datetime import datetime, timedelta

    from pomogaika.services import watch

    said = []
    monkeypatch.setattr(watch, "notify_owner", lambda text, kb=None: said.append(text))
    _booking(_db, guest_name="Будущий",
             check_in=datetime.utcnow() + timedelta(days=2),
             check_out=datetime.utcnow() + timedelta(days=5))
    watch._stay_notices(_db)
    assert said == []


def test_httpx_logger_is_quiet_so_token_does_not_leak():
    """URL Bot API содержит токен: httpx на уровне INFO писал его в системный журнал."""
    import logging

    from pomogaika.web.app import configure_logging

    logging.getLogger("httpx").setLevel(logging.DEBUG)      # нарочно ломаем
    configure_logging()                                      # то, что зовёт приложение при старте
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


# --- ответы гостям: библиотека ситуаций и формулировки объекта ---

@pytest.fixture
def _prop(_db):
    from pomogaika.models import Property
    from pomogaika.services.library import seed_library

    prop = Property(id=1, owner_id=1, code="test", title="Морская 5", address="Морская, 5",
                    city="Светлогорск", check_in_from="15:00", check_out_until="11:00",
                    max_guests=3, knowledge={"parking": "Двор за шлагбаумом, карта у администратора."})
    _db.add(prop)
    seed_library(_db)
    _db.flush()
    return prop


def test_facts_merge_card_and_knowledge(_prop, _db):
    from pomogaika.services.answers import facts

    known = facts(_prop)
    assert known["parking"].startswith("Двор")     # из базы знаний
    assert known["check_in_from"] == "15:00"        # из карточки объекта
    assert known["city"] == "Светлогорск"


def test_template_renders_object_facts(_prop, _db):
    from pomogaika.services.answers import match

    hit = match(_db, _prop, "а где машину поставить?")
    assert hit is not None
    assert hit["source"] == "library"
    assert "шлагбаум" in hit["text"]                # подставился факт этого объекта


def test_scenario_without_facts_is_not_answered(_prop, _db):
    """Факта нет — лучше отдать человеку, чем выдумать."""
    from pomogaika.services.answers import catalog, match

    assert match(_db, _prop, "а wi-fi у вас есть?") is None
    gaps = [r["title"] for r in catalog(_db, _prop) if r["state"] == "gap"]
    assert "Wi-Fi и пароль" in gaps


def test_own_answer_wins_over_library(_prop, _db):
    from pomogaika.services.answers import match, save_answer

    save_answer(_db, _prop, "parking", "У нас парковки нет, ставьте на улице.")
    hit = match(_db, _prop, "где парковка?")
    assert hit["source"] == "own"
    assert "парковки нет" in hit["text"]


def test_custom_situation_is_matched(_prop, _db):
    from pomogaika.services.answers import add_custom, match

    add_custom(_db, _prop, "Собака во дворе", "Собака лает, но не кусается.", ["собака", "лает"])
    hit = match(_db, _prop, "там собака какая-то лает")
    assert hit["title"] == "Собака во дворе"
    assert hit["source"] == "own"


def test_filling_facts_closes_gaps(_prop, _db):
    from pomogaika.services.answers import catalog, summary

    before = summary(catalog(_db, _prop))["gaps"]
    _prop.knowledge = dict(_prop.knowledge, wifi="Wi-Fi есть, пароль на роутере.")
    _db.flush()
    after = summary(catalog(_db, _prop))["gaps"]
    assert after == before - 1


def test_llm_prompt_is_built_per_object():
    """Раньше промпт начинался с «апартаментов KaVa для собственника Галины»."""
    from pomogaika.services.llm import build_messages

    system = build_messages("привет", [], {"title": "Морская 5", "city": "Светлогорск", "host": "Пётр"})[0]["content"]
    assert "Морская 5" in system and "Светлогорск" in system and "Пётр" in system
    assert "KaVa" not in system and "Галины" not in system


def test_disabled_answer_is_skipped(_prop, _db):
    from pomogaika.services.answers import match, save_answer

    save_answer(_db, _prop, "parking", "Текст выключен", enabled=False)
    hit = match(_db, _prop, "где парковка?")
    assert hit is not None and hit["source"] == "library"   # вернулись к шаблону


# --- iCal: закрытия площадки, зеркала и мнимый овербукинг ---

def test_block_label_tells_closed_from_booked():
    from pomogaika.domain.calendar import is_block_label

    assert is_block_label("Avito (Closed)") is True
    assert is_block_label("Заблокировано") is True
    assert is_block_label("Avito (Booked)") is False
    assert is_block_label("Забронировано") is False


def test_ical_closed_becomes_block_not_booking(_prop, _db):
    """«Avito (Closed)» — это закрытые даты собственника, а не второй гость."""
    from pomogaika.models import Block, Booking
    from pomogaika.services.bookings import ingest_ics

    ics = (
        "BEGIN:VCALENDAR\n"
        "BEGIN:VEVENT\nUID:a1@avito.ru\nDTSTART;VALUE=DATE:20261008\nDTEND;VALUE=DATE:20261010\n"
        "SUMMARY:Avito (Closed)\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:a2@avito.ru\nDTSTART;VALUE=DATE:20261020\nDTEND;VALUE=DATE:20261022\n"
        "SUMMARY:Avito (Booked)\nEND:VEVENT\n"
        "END:VCALENDAR\n"
    )
    ingest_ics(_db, _prop, ics, platform="avito")

    bookings = _db.query(Booking).filter_by(property_id=_prop.id).all()
    blocks = _db.query(Block).filter_by(property_id=_prop.id).all()
    assert len(bookings) == 1 and bookings[0].guest_name == "Avito (Booked)"
    assert len(blocks) == 1 and "Closed" in blocks[0].reason


def test_ical_block_is_not_duplicated_on_resync(_prop, _db):
    from pomogaika.models import Block
    from pomogaika.services.bookings import ingest_ics

    ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:same@avito.ru\n"
           "DTSTART;VALUE=DATE:20261008\nDTEND;VALUE=DATE:20261010\n"
           "SUMMARY:Avito (Closed)\nEND:VEVENT\nEND:VCALENDAR\n")
    ingest_ics(_db, _prop, ics, platform="avito")
    ingest_ics(_db, _prop, ics, platform="avito")
    assert _db.query(Block).filter_by(property_id=_prop.id).count() == 1
def test_old_closed_bookings_are_moved_to_blocks(_prop, _db):
    from datetime import datetime

    from pomogaika.models import Block, Booking
    from pomogaika.services.bookings import retire_ical_blocks

    old = _booking(_db, platform="avito", guest_name="Avito (Closed)", source="ical",
                   external_id="old1", check_in=datetime(2026, 10, 8, 14, 0),
                   check_out=datetime(2026, 10, 10, 12, 0))
    real = _booking(_db, platform="avito", guest_name="Avito (Booked)", source="ical",
                    external_id="real1", check_in=datetime(2026, 10, 20, 14, 0),
                    check_out=datetime(2026, 10, 22, 12, 0))
    moved = retire_ical_blocks(_db, _prop.id)

    assert moved == 1
    assert _db.get(Booking, old.id).status == "cancelled"
    assert _db.get(Booking, real.id).status != "cancelled"
    assert _db.query(Block).filter_by(property_id=_prop.id).count() == 1


# --- склейка занятости и готовность брони ---

def test_two_platforms_become_one_occupancy(_prop, _db):
    """Зеркал больше нет: одна занятость — одна полоса, источники внутри."""
    from datetime import datetime

    from pomogaika.services.stay import merge

    a = _booking(_db, platform="avito", guest_name="Avito (Booked)", external_id="s1",
                 check_in=datetime(2026, 9, 27, 14, 0), check_out=datetime(2026, 9, 30, 12, 0))
    b = _booking(_db, platform="sutochno", guest_name="Забронировано", external_id="s2",
                 check_in=datetime(2026, 9, 27, 14, 0), check_out=datetime(2026, 9, 30, 12, 0))

    groups = merge([a, b])
    assert len(groups) == 1
    titles = {s["title"] for s in groups[0].sources()}
    assert titles == {"Авито", "Суточно"}


def test_two_named_bookings_are_not_merged(_prop, _db):
    """Две настоящие брони на одни даты — это конфликт, а не одна занятость."""
    from datetime import datetime

    from pomogaika.services.stay import merge

    a = _booking(_db, platform="avito", guest_name="Анна", external_id="c1",
                 check_in=datetime(2026, 9, 27, 14, 0), check_out=datetime(2026, 9, 30, 12, 0))
    b = _booking(_db, platform="sutochno", guest_name="Борис", external_id="c2",
                 check_in=datetime(2026, 9, 28, 14, 0), check_out=datetime(2026, 10, 1, 12, 0))
    assert len(merge([a, b])) == 2


def test_named_booking_leads_the_group(_prop, _db):
    """Если имя есть хоть у одной записи — показываем его, а не «Занято»."""
    from datetime import datetime

    from pomogaika.services.stay import merge

    busy = _booking(_db, platform="sutochno", guest_name="Забронировано", external_id="l1",
                    check_in=datetime(2026, 9, 27, 14, 0), check_out=datetime(2026, 9, 30, 12, 0))
    named = _booking(_db, platform="avito", guest_name="Анна Петрова", external_id="l2",
                     check_in=datetime(2026, 9, 27, 14, 0), check_out=datetime(2026, 9, 30, 12, 0))
    assert merge([busy, named])[0].lead.guest_name == "Анна Петрова"


def test_platform_block_joins_occupancy(_prop, _db):
    """Закрытие дат с другой площадки — тоже источник, а не отдельная полоса."""
    from datetime import datetime

    from pomogaika.models import Block
    from pomogaika.services.stay import merge, platform_blocks_in_groups

    booking = _booking(_db, platform="sutochno", guest_name="Забронировано", external_id="b1",
                       check_in=datetime(2026, 10, 8, 14, 0), check_out=datetime(2026, 10, 9, 12, 0))
    block = Block(property_id=_prop.id, start=datetime(2026, 10, 8), end=datetime(2026, 10, 10),
                  reason="закрыто на площадке: Avito (Closed)", source="ical:avito")
    _db.add(block)
    _db.flush()

    groups = merge([booking], [block])
    assert len(groups) == 1
    assert "даты закрыты" in {s["kind"] for s in groups[0].sources()}
    assert block.id in platform_blocks_in_groups(groups)


def test_our_own_block_stays_separate(_prop, _db):
    """Наше собственное закрытие дат не приклеивается к занятости площадки."""
    from datetime import datetime

    from pomogaika.models import Block
    from pomogaika.services.stay import merge, platform_blocks_in_groups

    booking = _booking(_db, platform="sutochno", guest_name="Забронировано", external_id="o1",
                       check_in=datetime(2026, 10, 8, 14, 0), check_out=datetime(2026, 10, 9, 12, 0))
    mine = Block(property_id=_prop.id, start=datetime(2026, 10, 8), end=datetime(2026, 10, 10),
                 reason="закрой 8–10 октября", source="owner")
    _db.add(mine)
    _db.flush()
    assert platform_blocks_in_groups(merge([booking], [mine])) == set()


def test_occupancy_without_guest_data_raises_no_alarm(_prop, _db):
    """По записи, где известны только даты, требовать оплату и код бессмысленно."""
    from datetime import date, datetime

    from pomogaika.services.stay import attention, readiness

    booking = _booking(_db, guest_name="Забронировано", paid=False, external_id="q1",
                       check_in=datetime(2026, 10, 8, 14, 0), check_out=datetime(2026, 10, 9, 12, 0))
    rows = readiness(booking, drafts=[], tasks=[], sources=[{"platform": "sutochno"}], stage="future")
    result = attention(booking, conflict=False, rows=rows, stage="future")
    assert result["level"] == "ok"
    assert result["why"] == []


def test_real_booking_without_payment_needs_human(_prop, _db):
    from datetime import datetime

    from pomogaika.services.stay import attention, readiness

    booking = _booking(_db, guest_name="Анна Петрова", paid=False, external_id="q2",
                       check_in=datetime(2026, 10, 8, 14, 0), check_out=datetime(2026, 10, 9, 12, 0))
    rows = readiness(booking, drafts=[], tasks=[], sources=[{"platform": "avito"}], stage="future")
    result = attention(booking, conflict=False, rows=rows, stage="future")
    assert result["level"] == "human"
    assert "оплата не отмечена" in result["why"]


def test_next_event_reads_like_a_sentence(_prop, _db):
    from datetime import date, datetime, timedelta

    from pomogaika.services.stay import next_event

    tomorrow = datetime.combine(date.today() + timedelta(days=1), datetime.min.time()).replace(hour=15)
    booking = _booking(_db, external_id="n9", check_in=tomorrow,
                       check_out=tomorrow + timedelta(days=3))
    assert next_event(booking, date.today())["text"] == "Заезд завтра в 15:00"


def test_overbooking_is_detected_between_named_bookings(_prop, _db):
    """Настоящий овербукинг — две брони с именами. Он должен ловиться."""
    from datetime import datetime

    from pomogaika.services.bookings import bookings_conflict

    a = _booking(_db, platform="avito", guest_name="Анна Петрова", external_id="ob1",
                 check_in=datetime(2026, 11, 1, 14, 0), check_out=datetime(2026, 11, 5, 12, 0))
    b = _booking(_db, platform="ostrovok", guest_name="Борис Иванов", external_id="ob2",
                 check_in=datetime(2026, 11, 3, 14, 0), check_out=datetime(2026, 11, 7, 12, 0))
    assert bookings_conflict(a, b) is True


def test_named_booking_over_calendar_busy_is_not_conflict(_prop, _db):
    """Бронь из письма и занятость из календаря на те же даты — одна бронь."""
    from datetime import datetime

    from pomogaika.services.bookings import bookings_conflict

    named = _booking(_db, platform="ostrovok", guest_name="ULADZISLAU", external_id="nb1",
                     check_in=datetime(2026, 10, 8, 14, 0), check_out=datetime(2026, 10, 9, 12, 0))
    busy = _booking(_db, platform="avito", guest_name="Забронировано", external_id="nb2",
                    check_in=datetime(2026, 10, 8, 14, 0), check_out=datetime(2026, 10, 10, 12, 0))
    assert bookings_conflict(named, busy) is False


def test_our_own_block_over_booking_still_needs_human(_prop, _db):
    """Сами закрыли даты, а там бронь — это по-прежнему повод для человека."""
    from datetime import datetime

    from pomogaika.models import Block
    from pomogaika.services.bookings import _block_conflict_is_real

    booking = _booking(_db, guest_name="Анна Петрова", external_id="bl1",
                       check_in=datetime(2026, 11, 1, 14, 0), check_out=datetime(2026, 11, 4, 12, 0))
    mine = Block(property_id=_prop.id, start=datetime(2026, 11, 1), end=datetime(2026, 11, 4),
                 reason="закрой 1–4 ноября", source="owner")
    platform = Block(property_id=_prop.id, start=datetime(2026, 11, 1), end=datetime(2026, 11, 4),
                     reason="закрыто на площадке: Avito (Closed)", source="ical:avito")
    assert _block_conflict_is_real(booking, mine) is True
    assert _block_conflict_is_real(booking, platform) is False


def test_conflict_search_and_check_agree(_prop, _db):
    """Поиск пересечений и проверка должны давать один ответ, иначе конфликты воскресают."""
    from datetime import datetime

    from pomogaika.services.bookings import bookings_conflict, find_overlaps

    named = _booking(_db, platform="yandex", guest_name="Стахина Виктория", external_id="ag1",
                     check_in=datetime(2026, 6, 29, 14, 0), check_out=datetime(2026, 7, 12, 12, 0))
    busy = _booking(_db, platform="sutochno", guest_name="Забронировано", external_id="ag2",
                    check_in=datetime(2026, 6, 30, 14, 0), check_out=datetime(2026, 7, 1, 12, 0))
    assert bookings_conflict(named, busy) is False
    assert find_overlaps(_db, named) == []


# --- письма площадок ---

SUTOCHNO_LETTER = """Гость внес предоплату за бронь
+7 916 608 01 13
— Евгения
Детали бронирования
Бронь №42911286 | Объект №2323816
Зеленоградск, Гагарина улица, 87
Заезд
2 августа 2026
в 14:00, Воскресенье
Отъезд
15 августа 2026
в 12:00, Суббота
Предоплата
37440 ₽
Оплата при заселении
149760 ₽
Количество гостей
2 гостя
Суточно.ру"""


def test_sutochno_letter_gives_name_money_and_guests():
    from pomogaika.platforms.letters import parse_sutochno

    p = parse_sutochno(SUTOCHNO_LETTER, year_hint=2026)
    assert p.external_id == "42911286"
    assert p.guest_name == "Евгения"
    assert p.guests_count == 2
    assert p.check_in.strftime("%d.%m.%Y") == "02.08.2026"
    assert p.check_out.strftime("%d.%m.%Y") == "15.08.2026"
    assert p.amount_cents == 187200 * 100      # предоплата + оплата при заселении
    assert p.paid is True
    assert p.kind == "created"


def test_sutochno_letters_share_one_booking_number():
    """Разные письма про одну бронь несут один номер — по нему они и сходятся."""
    from pomogaika.platforms.letters import parse_sutochno

    request = SUTOCHNO_LETTER.replace("Гость внес предоплату за бронь", "У вас новый запрос на бронь")
    a = parse_sutochno(SUTOCHNO_LETTER, year_hint=2026)
    b = parse_sutochno(request, year_hint=2026)
    assert a.external_id == b.external_id


def test_sutochno_refusal_is_cancellation():
    from pomogaika.platforms.letters import parse_sutochno

    text = SUTOCHNO_LETTER.replace("Гость внес предоплату за бронь", "Гость отказался от проживания")
    assert parse_sutochno(text, year_hint=2026).kind == "cancelled"


def test_boilerplate_about_cancellation_is_not_a_cancellation():
    """«Подтвердите, иначе она будет отменена» — это не отмена брони."""
    from pomogaika.platforms.letters import parse_sutochno

    text = SUTOCHNO_LETTER + "\nПодтвердите бронь, иначе она будет отменена"
    assert parse_sutochno(text, year_hint=2026).kind == "created"


def test_yandex_letter_gives_full_name_and_guests():
    from pomogaika.platforms.letters import parse_yandex

    text = """Новое бронирование в «KAVA — апартаменты у моря»
25.06.2026 - 28.06.2026. Гостей: 3. Номеров: 1.
Основной гость
Елена Юкаева
Гости
Взрослые: 2
Дети: 1 (возраст: 11 лет)
№ Яндекс
YA-0388-2035-3750:0
Статус брони
Подтверждено"""
    p = parse_yandex(text, year_hint=2026)
    assert p.guest_name == "Елена Юкаева"
    assert p.guests_count == 3
    assert p.external_id == "YA-0388-2035-3750:0"
    assert p.check_in.strftime("%d.%m") == "25.06"


def test_html_junk_never_becomes_a_guest_name():
    """Раньше в имя гостя приезжало «</td>»."""
    from pomogaika.platforms.letters import _looks_like_name

    assert _looks_like_name("</td>") is False
    assert _looks_like_name("") is False
    assert _looks_like_name("Елена Юкаева") is True


def test_two_named_bookings_do_not_merge_through_a_busy_one(_prop, _db):
    """Длинная безымянная занятость не должна сцеплять двух разных гостей.

    Иначе группа расползается, в карточке смешиваются чужие данные,
    а настоящий овербукинг между ними прячется внутри одной полосы.
    """
    from datetime import datetime

    from pomogaika.services.stay import merge

    anna = _booking(_db, platform="yandex", guest_name="Анна Петрова", external_id="t1",
                    check_in=datetime(2026, 7, 23, 14, 0), check_out=datetime(2026, 7, 28, 12, 0))
    span = _booking(_db, platform="sutochno", guest_name="Забронировано", external_id="t2",
                    check_in=datetime(2026, 7, 20, 14, 0), check_out=datetime(2026, 8, 5, 12, 0))
    maria = _booking(_db, platform="ostrovok", guest_name="Мария Иванова", external_id="t3",
                     check_in=datetime(2026, 7, 29, 14, 0), check_out=datetime(2026, 7, 31, 12, 0))

    groups = merge([anna, span, maria])
    named_per_group = [
        sum(1 for m in g.members if m.guest_name in ("Анна Петрова", "Мария Иванова"))
        for g in groups
    ]
    assert max(named_per_group) == 1      # в каждой группе максимум один гость с именем
    assert len(groups) >= 2


def test_money_formats_from_real_letters():
    """Суточно пишет «27000 ₽», Яндекс — «33,240.00 ₽». Понимать надо оба."""
    from pomogaika.platforms.letters import _money_cents

    assert _money_cents("27000 ₽") == 27000 * 100
    assert _money_cents("149 760 ₽") == 149760 * 100
    assert _money_cents("33,240.00 ₽") == 3324000
    assert _money_cents("30,079.20 ₽") == 3007920
    assert _money_cents("нет денег") == 0


def test_yandex_amount_is_what_guest_paid():
    from pomogaika.platforms.letters import parse_yandex

    text = """Новое бронирование в «KAVA»
25.06.2026 - 28.06.2026. Гостей: 2.
Основной гость
Елена Юкаева
Оплата
Оплатил гость
33,240.00 ₽
Оплата отелю
30,079.20 ₽
№ Яндекс
YA-0388-2035-3750:0
Статус брони
Подтверждено"""
    p = parse_yandex(text, year_hint=2026)
    assert p.amount_cents == 3324000       # берём то, что заплатил гость


# --- ссылка гостя в личку собственницы и телефон из письма ---

def _guest_code(session, booking, monkeypatch):
    from pomogaika.config import settings
    from pomogaika.services.guest_links import active_link, issue_link

    monkeypatch.setattr(settings, "telegram_owner_username", "galina")
    issue_link(session, booking)
    return active_link(session, booking.id)


def test_guest_code_resolves_booking_and_sets_used_at(_prop, _db, monkeypatch):
    from pomogaika.services.guest_links import resolve_code

    start = datetime.now() + timedelta(days=10)
    booking = _booking(
        _db, property_id=_prop.id, external_id="gl-1",
        check_in=start, check_out=start + timedelta(days=3),
    )
    row = _guest_code(_db, booking, monkeypatch)
    found = resolve_code(_db, f"Здравствуйте! Бронь {row.id}", 501)
    assert found.id == booking.id
    assert row.used_at is not None
    assert row.guest_user_id == 501


def test_expired_and_revoked_guest_codes_do_not_resolve(_prop, _db, monkeypatch):
    from datetime import datetime, timedelta

    from pomogaika.services.drafts import create_guest_draft
    from pomogaika.services.guest_links import resolve_code, revoke

    start = datetime.now() + timedelta(days=10)
    booking = _booking(
        _db, property_id=_prop.id, external_id="gl-2",
        check_in=start, check_out=start + timedelta(days=3),
    )
    row = _guest_code(_db, booking, monkeypatch)
    text = f"Здравствуйте! Бронь {row.id}"

    row.expires_at = datetime.now() - timedelta(minutes=1)
    _db.flush()
    assert resolve_code(_db, text, 501) is None

    row.expires_at = booking.check_out + timedelta(days=1)
    _db.flush()
    revoke(_db, row.id)
    assert resolve_code(_db, text, 501) is None
    assert resolve_code(_db, "Здравствуйте! Бронь TEST-ffff", 501) is None

    draft, result = create_guest_draft(
        _db, "Здравствуйте! Бронь TEST-ffff",
        guest_user_id="501", guest_chat_id="77", business_connection_id="bc-x",
    )
    assert result.bound_by_code is False
    assert draft.booking_id is None
    low = draft.draft_text.lower()
    assert "истекла" not in low
    assert "не найдена" not in low


def test_guest_code_is_one_person(_prop, _db, monkeypatch):
    from pomogaika.services.guest_links import resolve_code

    start = datetime.now() + timedelta(days=10)
    booking = _booking(
        _db, property_id=_prop.id, external_id="gl-3",
        check_in=start, check_out=start + timedelta(days=3),
    )
    row = _guest_code(_db, booking, monkeypatch)
    text = f"Здравствуйте! Бронь {row.id}"
    assert resolve_code(_db, text, 501).id == booking.id
    assert resolve_code(_db, text, 502) is None
    assert resolve_code(_db, text, 501).id == booking.id
    assert row.guest_user_id == 501


def test_guest_link_endpoint_reuses_code(_prop, _db, monkeypatch):
    from urllib.parse import parse_qs, unquote, urlparse

    from pomogaika.models import GuestLink
    from pomogaika.web.app import guest_link_api

    start = datetime.now() + timedelta(days=10)
    booking = _booking(
        _db, property_id=_prop.id, external_id="gl-4", guest_name="Евгения",
        check_in=start, check_out=start + timedelta(days=3),
    )
    _guest_code(_db, booking, monkeypatch)
    first = guest_link_api(booking.id, _db)
    second = guest_link_api(booking.id, _db)
    assert first["code"] == second["code"]
    assert _db.query(GuestLink).count() == 1
    text = unquote(parse_qs(urlparse(first["url"]).query)["text"][0])
    assert text == f"Здравствуйте! Бронь {first['code']}"
    assert "бот" not in first["message"].lower()
    assert first["url"].startswith("https://t.me/galina?text=")


def test_business_message_with_code_binds_draft(_prop, _db, monkeypatch):
    from datetime import datetime, timedelta

    from pomogaika.services.drafts import create_guest_draft

    later = datetime.now() + timedelta(days=30)
    booking = _booking(
        _db, property_id=_prop.id, external_id="gl-5", paid=False,
        check_in=later, check_out=later + timedelta(days=2),
    )
    row = _guest_code(_db, booking, monkeypatch)
    draft, result = create_guest_draft(
        _db,
        f"Здравствуйте! Бронь {row.id}. Пришлите код калитки",
        guest_chat_id="4242",
        guest_user_id="77",
        business_connection_id="bc-77",
        channel="telegram_business",
    )
    assert draft.business_connection_id == "bc-77"
    assert draft.booking_id == booking.id
    assert booking.guest_telegram_user_id == 77
    assert booking.guest_chat_id == "4242"
    assert booking.business_connection_id == "bc-77"
    assert result.bound_by_code is True
    assert result.issue_code is False


def test_sutochno_phone_saved_blocklist_skipped(_prop, _db, monkeypatch):
    from pomogaika.config import settings
    from pomogaika.platforms.letters import parse_sutochno
    from pomogaika.services.bookings import ingest_booking

    monkeypatch.setattr(settings, "guest_phone_blocklist", "+7 495 123-45-67")
    parsed = parse_sutochno(SUTOCHNO_LETTER, year_hint=2026)
    assert parsed.phone == "+79166080113"
    saved = ingest_booking(
        _db, property_id=_prop.id, platform=parsed.platform, external_id=parsed.external_id,
        guest_name=parsed.guest_name, check_in=parsed.check_in, check_out=parsed.check_out,
        guests_count=parsed.guests_count, amount_cents=parsed.amount_cents, paid=parsed.paid,
        guest_phone=parsed.phone,
    )
    assert saved.booking.guest_phone == "+79166080113"

    blocked = ingest_booking(
        _db, property_id=_prop.id, platform="ostrovok", external_id="listing-phone",
        guest_name="Гость", check_in=parsed.check_in, check_out=parsed.check_out,
        guest_phone="+7 (495) 123-45-67",
    )
    assert blocked.booking.guest_phone == ""

    with_hotline = SUTOCHNO_LETTER.replace(
        "+7 916 608 01 13", "+7 800 555-35-35\n+7 916 608 01 13",
    )
    assert parse_sutochno(with_hotline, year_hint=2026).phone == "+79166080113"


def test_empty_phone_does_not_erase_saved(_prop, _db):
    from pomogaika.platforms.letters import parse_sutochno
    from pomogaika.services.bookings import ingest_booking

    parsed = parse_sutochno(SUTOCHNO_LETTER, year_hint=2026)
    common = dict(
        property_id=_prop.id, platform=parsed.platform, external_id=parsed.external_id,
        guest_name=parsed.guest_name, check_in=parsed.check_in, check_out=parsed.check_out,
    )
    first = ingest_booking(_db, guest_phone=parsed.phone, **common)
    second = ingest_booking(_db, guest_phone="", **common)
    assert second.booking.id == first.booking.id
    assert second.booking.guest_phone == "+79166080113"
