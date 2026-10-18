from __future__ import annotations

from datetime import date, datetime

from app.config import get_settings


def local_now() -> datetime:
    return datetime.now(get_settings().timezone).replace(tzinfo=None)


def local_today() -> date:
    return local_now().date()
