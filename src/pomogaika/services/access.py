"""Кому открыт пульт и как пригласить нового человека."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from pomogaika.config import settings
from pomogaika.models import AccessUser, Invite, Owner

INVITE_HOURS = 24


def invite_expired(invite: Invite, now: datetime | None = None) -> bool:
    return (now or datetime.utcnow()) - invite.created_at > timedelta(hours=INVITE_HOURS)


def create_invite(session: Session, created_by: int) -> Invite:
    invite = Invite(id=secrets.token_hex(12), created_by=created_by)
    session.add(invite)
    session.flush()
    return invite


def accept_invite(session: Session, token: str, user_id: int, name: str = "") -> tuple[bool, str]:
    """Погасить приглашение и выдать доступ. Второй элемент — что сказать человеку."""
    invite = session.get(Invite, token)
    if invite is None:
        return False, "Такого приглашения нет."
    if invite.used_by:
        return False, "Этой ссылкой уже воспользовались. Попросите новую."
    if invite_expired(invite):
        return False, "Приглашение просрочено — они живут сутки. Попросите новое."

    invite.used_by = user_id
    invite.used_at = datetime.utcnow()
    if not has_access(session, user_id):
        session.add(AccessUser(telegram_user_id=user_id, name=name, added_by=invite.created_by))
    session.flush()
    return True, "Доступ открыт."


def has_access(session: Session, user_id: int) -> bool:
    return user_id in allowed_ids(session)


def staff_ids(session: Session) -> set[int]:
    """Собственник и приглашённые — те, для кого бот работает как пульт."""
    ids: set[int | None] = {settings.telegram_owner_user_id}
    ids.update(o.telegram_user_id for o in session.query(Owner).all())
    ids.update(a.telegram_user_id for a in session.query(AccessUser).all())
    return {i for i in ids if i}


def allowed_ids(session: Session) -> set[int]:
    """Кого пускать в мини-приложение и браузер: пульт плюс клинер из .env.

    Клинер намеренно не в staff_ids: у него свой сценарий в боте (bots/cleaner.py),
    и превращать его в собственника не надо.
    """
    ids = staff_ids(session)
    if settings.telegram_cleaner_user_id:
        ids.add(settings.telegram_cleaner_user_id)
    return ids


def is_permanent(session: Session, user_id: int) -> bool:
    """Собственника из .env и из базы убрать нельзя — иначе можно запереть самих себя."""
    if settings.telegram_owner_user_id and user_id == settings.telegram_owner_user_id:
        return True
    return any(o.telegram_user_id == user_id for o in session.query(Owner).all())


def people(session: Session) -> list[tuple[int, str, bool]]:
    """Список для команды /доступ: id, имя, можно ли убрать."""
    rows: list[tuple[int, str, bool]] = []
    for owner in session.query(Owner).all():
        if owner.telegram_user_id:
            rows.append((owner.telegram_user_id, owner.name or "собственник", False))
    if settings.telegram_owner_user_id and not any(r[0] == settings.telegram_owner_user_id for r in rows):
        rows.append((settings.telegram_owner_user_id, "собственник (.env)", False))
    if settings.telegram_cleaner_user_id:
        rows.append((settings.telegram_cleaner_user_id, "клинер (.env)", False))
    for user in session.query(AccessUser).order_by(AccessUser.created_at).all():
        rows.append((user.telegram_user_id, user.name or "без имени", True))
    return rows


def revoke(session: Session, user_id: int) -> bool:
    if is_permanent(session, user_id):
        return False
    user = session.query(AccessUser).filter_by(telegram_user_id=user_id).one_or_none()
    if user is None:
        return False
    session.delete(user)
    session.flush()
    return True
