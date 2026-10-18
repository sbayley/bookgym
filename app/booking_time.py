from __future__ import annotations

from datetime import date, datetime, timedelta

from app.config import get_settings


def calculate_booking_open(class_date: date) -> datetime:
    settings = get_settings()
    booking_date = class_date - timedelta(days=settings.classes_open_days_ahead)
    return datetime(
        year=booking_date.year,
        month=booking_date.month,
        day=booking_date.day,
        hour=settings.classes_open_hour,
        minute=settings.classes_open_minute,
    )
