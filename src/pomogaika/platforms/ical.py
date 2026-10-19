"""Разбор iCal площадок. Без петли экспорта: входящий UID сохраняем как external_id."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx


@dataclass
class ParsedEvent:
    uid: str
    summary: str
    check_in: datetime
    check_out: datetime
    status: str  # confirmed | cancelled


def _unfold(text: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", text)


def _parse_dt(value: str, *, is_end: bool) -> datetime | None:
    raw = value.strip()
    if ":" in raw and not re.match(r"^\d", raw):
        raw = raw.split(":", 1)[-1]
    raw = raw.replace("Z", "")
    if "T" in raw:
        body = raw.split("T", 1)[0] + "T" + re.sub(r"[^\d]", "", raw.split("T", 1)[1])[:6]
        try:
            return datetime.strptime(body[:15], "%Y%m%dT%H%M%S" if len(body) >= 15 else "%Y%m%dT%H%M")
        except ValueError:
            try:
                return datetime.strptime(body[:13], "%Y%m%dT%H%M")
            except ValueError:
                return None
    digits = re.sub(r"[^\d]", "", raw)[:8]
    if len(digits) != 8:
        return None
    day = datetime.strptime(digits, "%Y%m%d")
    if is_end:
        return day.replace(hour=12, minute=0)
    return day.replace(hour=14, minute=0)


def parse_ics(text: str) -> list[ParsedEvent]:
    body = _unfold(text)
    chunks = re.split(r"BEGIN:VEVENT", body, flags=re.IGNORECASE)
    events: list[ParsedEvent] = []
    for chunk in chunks[1:]:
        part = chunk.split("END:VEVENT", 1)[0]
        uid = _field(part, "UID") or ""
        summary = _field(part, "SUMMARY") or "Гость"
        status_raw = (_field(part, "STATUS") or "CONFIRMED").upper()
        start = _dt_field(part, "DTSTART")
        end = _dt_field(part, "DTEND", is_end=True)
        if not start:
            continue
        if not end:
            end = start + timedelta(days=1)
            end = end.replace(hour=12, minute=0)
        if end.hour == 0 and end.minute == 0:
            end = (end - timedelta(days=0)).replace(hour=12, minute=0)
            if end <= start:
                end = (start + timedelta(days=1)).replace(hour=12, minute=0)
        events.append(
            ParsedEvent(
                uid=uid or f"ics-{start.date()}-{summary}",
                summary=summary.split("\\,")[0][:200],
                check_in=start if start.hour or start.minute else start.replace(hour=14),
                check_out=end,
                status="cancelled" if status_raw == "CANCELLED" else "confirmed",
            )
        )
    return events


def _field(chunk: str, name: str) -> str | None:
    m = re.search(rf"^{name}[;:][^\n]*", chunk, re.IGNORECASE | re.MULTILINE)
    if not m:
        return None
    line = m.group(0)
    return line.split(":", 1)[-1].strip()


def _dt_field(chunk: str, name: str, *, is_end: bool = False) -> datetime | None:
    m = re.search(rf"^{name}[;:][^\n]*", chunk, re.IGNORECASE | re.MULTILINE)
    if not m:
        return None
    return _parse_dt(m.group(0).split(":", 1)[-1], is_end=is_end)


def fetch_ics(url: str, timeout: float = 45) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Accept": "text/calendar, text/plain, */*",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text
