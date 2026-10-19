from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from pomogaika.config import ROOT, settings

(ROOT / "data").mkdir(exist_ok=True)

engine = create_engine(settings.database_url, echo=False)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def migrate_sqlite() -> None:
    insp = inspect(engine)
    tables = insp.get_table_names()
    alters: list[str] = []

    def add(table: str, name: str, ddl: str) -> None:
        if table not in tables:
            return
        cols = {c["name"] for c in insp.get_columns(table)}
        if name not in cols:
            alters.append(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    add("message_drafts", "guest_chat_id", "VARCHAR(64) DEFAULT ''")
    add("message_drafts", "guest_user_id", "VARCHAR(64) DEFAULT ''")
    add("message_drafts", "business_connection_id", "VARCHAR(128) DEFAULT ''")
    add("message_drafts", "channel", "VARCHAR(32) DEFAULT 'web'")
    add("message_drafts", "booking_id", "INTEGER")
    add("message_drafts", "reply_to", "VARCHAR(200) DEFAULT ''")
    add("message_drafts", "mail_subject", "VARCHAR(300) DEFAULT ''")
    add("message_drafts", "mail_message_id", "VARCHAR(200) DEFAULT ''")
    add("message_drafts", "mail_quote", "TEXT DEFAULT ''")
    add("owners", "telegram_user_id", "INTEGER")
    add("owners", "telegram_chat_id", "INTEGER")
    add("listings", "ical_url", "VARCHAR(500) DEFAULT ''")
    add("blocks", "external_id", "VARCHAR(200) DEFAULT ''")
    add("bookings", "guest_telegram_user_id", "INTEGER")
    add("booking_conflicts", "block_id", "INTEGER")
    add("bookings", "guest_chat_id", "VARCHAR(64) DEFAULT ''")
    add("bookings", "business_connection_id", "VARCHAR(128) DEFAULT ''")
    add("bookings", "code_hint", "VARCHAR(8) DEFAULT ''")
    add("bookings", "code_issued_at", "DATETIME")
    add("bookings", "code_window_notified", "BOOLEAN DEFAULT 0")
    add("bookings", "arrival_notified", "BOOLEAN DEFAULT 0")
    add("bookings", "departure_notified", "BOOLEAN DEFAULT 0")
    add("bookings", "checked_in_at", "DATETIME")
    add("bookings", "checked_out_at", "DATETIME")
    add("tasks", "contractor_id", "INTEGER")
    add("tasks", "note", "TEXT DEFAULT ''")
    add("tasks", "photo_count", "INTEGER DEFAULT 0")
    add("tasks", "photos_needed", "INTEGER DEFAULT 6")
    add("tasks", "escalate_after_min", "INTEGER DEFAULT 10")
    add("tasks", "accepted_at", "DATETIME")
    add("tasks", "due_at", "DATETIME")
    add("tasks", "escalated_at", "DATETIME")
    if not alters:
        return
    with engine.begin() as conn:
        for stmt in alters:
            conn.execute(text(stmt))


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
