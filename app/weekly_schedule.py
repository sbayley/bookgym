from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from sqlmodel import Session, select

from app.booking_pauses import booking_pause_for_date, cancel_job_if_paused
from app.booking_time import calculate_booking_open
from app.config import get_settings
from app.db import get_session, init_db
from app.job_sources import MANUAL_SELECTION_RULE_NAME
from app.models import (
    BookingJob,
    BookingStatus,
    DiscoveredClass,
    WeeklyScheduleItem,
    WeeklyScheduleSkip,
)
from app.time_utils import local_now, local_today


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _start_of_week(value: date) -> date:
    return value - timedelta(days=(value.weekday() + 1) % 7)


def _time_key(value: time | None) -> str:
    return value.isoformat(timespec="minutes") if value is not None else "any"


def _class_weekday(discovered_class: DiscoveredClass) -> int:
    return (discovered_class.class_date.weekday() + 1) % 7


def weekly_schedule_scope_key(
    *,
    weekday: int,
    start_time: time | None,
    title: str,
) -> str:
    return f"{weekday}:{_time_key(start_time)}:{_normalize_key(title)}"


def _scope_key_for_class(discovered_class: DiscoveredClass) -> str:
    return weekly_schedule_scope_key(
        weekday=_class_weekday(discovered_class),
        start_time=discovered_class.start_time,
        title=discovered_class.title,
    )


def _skip_key(scope_key: str, week_start: date) -> str:
    return f"{scope_key}|{week_start.isoformat()}"


def _item_from_class(
    discovered_class: DiscoveredClass,
    *,
    now: datetime,
    user_id: int | None = None,
) -> WeeklyScheduleItem:
    return WeeklyScheduleItem(
        user_id=user_id,
        scope_key=_scope_key_for_class(discovered_class),
        weekday=_class_weekday(discovered_class),
        start_time=discovered_class.start_time,
        title=discovered_class.title,
        title_key=_normalize_key(discovered_class.title),
        category=discovered_class.category,
        instructor_name=discovered_class.instructor_name,
        booking_type_id=discovered_class.booking_type_id,
        created_at=now,
        updated_at=now,
    )


def _update_item_from_class(
    item: WeeklyScheduleItem,
    discovered_class: DiscoveredClass,
    *,
    now: datetime,
) -> WeeklyScheduleItem:
    item.weekday = _class_weekday(discovered_class)
    item.start_time = discovered_class.start_time
    item.title = discovered_class.title
    item.title_key = _normalize_key(discovered_class.title)
    item.category = discovered_class.category
    item.instructor_name = discovered_class.instructor_name
    item.booking_type_id = discovered_class.booking_type_id
    item.updated_at = now
    return item


def _class_matches_item(discovered_class: DiscoveredClass, item: WeeklyScheduleItem) -> bool:
    if _class_weekday(discovered_class) != item.weekday:
        return False
    if item.start_time is not None and discovered_class.start_time != item.start_time:
        return False
    return _normalize_key(discovered_class.title) == item.title_key


def _is_selectable(discovered_class: DiscoveredClass) -> bool:
    if discovered_class.is_booked_at_gym:
        return False
    if discovered_class.is_full or "book now (full)" in discovered_class.raw_summary.lower():
        return False
    return True


def _expected_run_at(discovered_class: DiscoveredClass) -> datetime:
    return (
        discovered_class.site_booking_opens_at
        or discovered_class.expected_booking_opens_at
        or calculate_booking_open(discovered_class.class_date)
    )


def upsert_manual_selection_job(
    session: Session,
    discovered_class: DiscoveredClass,
    *,
    now: datetime | None = None,
    last_message: str = "Selected from admin calendar",
    user_id: int | None = None,
) -> tuple[BookingJob, bool]:
    now = now or local_now()
    statement = select(BookingJob).where(
        BookingJob.discovered_class_id == discovered_class.id,
        BookingJob.rule_name == MANUAL_SELECTION_RULE_NAME,
    )
    if user_id is not None:
        statement = statement.where(BookingJob.user_id == user_id)
    job = session.exec(statement).first()
    created = job is None

    if job is None:
        job = BookingJob(
            user_id=user_id,
            rule_name=MANUAL_SELECTION_RULE_NAME,
            target_match_text=discovered_class.title,
            class_date=discovered_class.class_date,
            window_start_time=discovered_class.start_time,
            window_end_time=discovered_class.start_time,
            expected_run_at=_expected_run_at(discovered_class),
            calendar_event_id=discovered_class.calendar_event_id,
            site_booking_opens_at=discovered_class.site_booking_opens_at,
            discovered_class_id=discovered_class.id,
            status=BookingStatus.PENDING,
            last_message=last_message,
            notification_email_override=None,
            notification_sms_override=None,
            created_at=now,
            updated_at=now,
        )
    else:
        if user_id is not None:
            job.user_id = user_id
        job.rule_name = MANUAL_SELECTION_RULE_NAME
        job.target_match_text = discovered_class.title
        job.class_date = discovered_class.class_date
        job.window_start_time = discovered_class.start_time
        job.window_end_time = discovered_class.start_time
        job.expected_run_at = _expected_run_at(discovered_class)
        job.calendar_event_id = discovered_class.calendar_event_id
        job.site_booking_opens_at = discovered_class.site_booking_opens_at
        job.discovered_class_id = discovered_class.id
        job.status = BookingStatus.PENDING
        job.last_message = last_message
        job.notification_email_override = None
        job.notification_sms_override = None
        job.updated_at = now

    session.add(job)
    return job, created


def active_weekly_schedule_items(
    session: Session,
    user_id: int | None = None,
) -> list[WeeklyScheduleItem]:
    statement = select(WeeklyScheduleItem)
    if user_id is not None:
        statement = statement.where(WeeklyScheduleItem.user_id == user_id)
    statement = statement.order_by(
        WeeklyScheduleItem.weekday,
        WeeklyScheduleItem.start_time,
        WeeklyScheduleItem.title,
    )
    return list(session.exec(statement).all())


def active_weekly_schedule_skips(
    session: Session,
    user_id: int | None = None,
) -> list[WeeklyScheduleSkip]:
    statement = select(WeeklyScheduleSkip)
    if user_id is not None:
        statement = statement.where(WeeklyScheduleSkip.user_id == user_id)
    statement = statement.order_by(
        WeeklyScheduleSkip.week_start,
        WeeklyScheduleSkip.scope_key,
    )
    return list(session.exec(statement).all())


def serialize_weekly_schedule_item(item: WeeklyScheduleItem) -> dict:
    return {
        "id": item.id,
        "user_id": item.user_id,
        "scope_key": item.scope_key,
        "weekday": item.weekday,
        "start_time": item.start_time.isoformat(timespec="seconds") if item.start_time else None,
        "title": item.title,
        "title_key": item.title_key,
        "category": item.category,
        "instructor_name": item.instructor_name,
        "booking_type_id": item.booking_type_id,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def serialize_weekly_schedule_skip(skip: WeeklyScheduleSkip) -> dict:
    return {
        "id": skip.id,
        "user_id": skip.user_id,
        "scope_key": skip.scope_key,
        "week_start": skip.week_start.isoformat(),
        "created_at": skip.created_at.isoformat(),
    }


def serialize_weekly_schedule(
    session: Session | None = None,
    user_id: int | None = None,
) -> dict:
    if session is None:
        init_db()
        with get_session() as owned_session:
            return serialize_weekly_schedule(owned_session, user_id=user_id)

    items = active_weekly_schedule_items(session, user_id=user_id)
    skips = active_weekly_schedule_skips(session, user_id=user_id)
    return {
        "active": bool(items),
        "items": [serialize_weekly_schedule_item(item) for item in items],
        "skips": [serialize_weekly_schedule_skip(skip) for skip in skips],
    }


def clear_weekly_schedule_skip(
    session: Session,
    discovered_class: DiscoveredClass,
    user_id: int | None = None,
) -> bool:
    scope_key = _scope_key_for_class(discovered_class)
    week_start = _start_of_week(discovered_class.class_date)
    statement = select(WeeklyScheduleSkip).where(
        WeeklyScheduleSkip.skip_key == _skip_key(scope_key, week_start)
    )
    if user_id is not None:
        statement = statement.where(WeeklyScheduleSkip.user_id == user_id)
    skip = session.exec(statement).first()
    if skip is None:
        return False
    session.delete(skip)
    return True


def _cancel_job_for_skipped_class(
    session: Session,
    discovered_class: DiscoveredClass,
    *,
    week_start: date,
    now: datetime,
    user_id: int | None = None,
) -> list[int]:
    clearable_statuses = [
        BookingStatus.PENDING,
        BookingStatus.NOT_YET_OPEN,
        BookingStatus.FAILED,
        BookingStatus.NEEDS_CONFIRMATION,
        BookingStatus.WAITLISTED,
        BookingStatus.CANCELED,
    ]
    statement = select(BookingJob).where(
        BookingJob.discovered_class_id == discovered_class.id,
        BookingJob.rule_name == MANUAL_SELECTION_RULE_NAME,
        BookingJob.status.in_(clearable_statuses),
    )
    if user_id is not None:
        statement = statement.where(BookingJob.user_id == user_id)
    jobs = list(session.exec(statement).all())
    canceled_ids = []
    skip_message = f"Skipped recurring class for week of {week_start.isoformat()}"
    for job in jobs:
        if (
            job.status == BookingStatus.CANCELED
            and job.last_message == skip_message
            and not job.capture_network
        ):
            continue
        job.status = BookingStatus.CANCELED
        job.last_message = skip_message
        job.capture_network = False
        job.updated_at = now
        session.add(job)
        if job.id is not None:
            canceled_ids.append(job.id)
    return canceled_ids


def skip_weekly_schedule_occurrence(
    discovered_class_id: int,
    user_id: int | None = None,
) -> dict:
    init_db()
    now = local_now()
    with get_session() as session:
        discovered_class = session.get(DiscoveredClass, discovered_class_id)
        if discovered_class is None:
            raise ValueError("Discovered class not found")

        scope_key = _scope_key_for_class(discovered_class)
        item_statement = select(WeeklyScheduleItem).where(WeeklyScheduleItem.scope_key == scope_key)
        if user_id is not None:
            item_statement = item_statement.where(WeeklyScheduleItem.user_id == user_id)
        item = session.exec(item_statement).first()
        if item is None:
            raise ValueError("This class is not part of the recurring schedule")

        week_start = _start_of_week(discovered_class.class_date)
        skip_key = _skip_key(scope_key, week_start)
        skip_statement = select(WeeklyScheduleSkip).where(WeeklyScheduleSkip.skip_key == skip_key)
        if user_id is not None:
            skip_statement = skip_statement.where(WeeklyScheduleSkip.user_id == user_id)
        skip = session.exec(skip_statement).first()
        if skip is None:
            skip = WeeklyScheduleSkip(
                user_id=user_id,
                skip_key=skip_key,
                scope_key=scope_key,
                week_start=week_start,
                created_at=now,
            )
            session.add(skip)

        canceled_job_ids = _cancel_job_for_skipped_class(
            session,
            discovered_class,
            week_start=week_start,
            now=now,
            user_id=user_id,
        )
        session.commit()
        session.refresh(skip)
        return {
            "skip": serialize_weekly_schedule_skip(skip),
            "canceled_job_ids": canceled_job_ids,
            "schedule": serialize_weekly_schedule(session, user_id=user_id),
        }


def resume_weekly_schedule_occurrence(
    discovered_class_id: int,
    user_id: int | None = None,
) -> dict:
    init_db()
    now = local_now()
    with get_session() as session:
        discovered_class = session.get(DiscoveredClass, discovered_class_id)
        if discovered_class is None:
            raise ValueError("Discovered class not found")

        scope_key = _scope_key_for_class(discovered_class)
        item_statement = select(WeeklyScheduleItem).where(WeeklyScheduleItem.scope_key == scope_key)
        if user_id is not None:
            item_statement = item_statement.where(WeeklyScheduleItem.user_id == user_id)
        item = session.exec(item_statement).first()
        if item is None:
            raise ValueError("This class is not part of the recurring schedule")

        removed = clear_weekly_schedule_skip(session, discovered_class, user_id=user_id)
        job_id = None
        if discovered_class.class_date >= local_today() and _is_selectable(discovered_class):
            job, _created = upsert_manual_selection_job(
                session,
                discovered_class,
                now=now,
                last_message="Resumed recurring class for this week",
                user_id=user_id,
            )
            session.flush()
            job_id = job.id

        session.commit()
        return {
            "removed": removed,
            "job_id": job_id,
            "schedule": serialize_weekly_schedule(session, user_id=user_id),
        }


def add_class_to_weekly_schedule(
    discovered_class_id: int,
    user_id: int | None = None,
) -> dict:
    init_db()
    now = local_now()
    with get_session() as session:
        discovered_class = session.get(DiscoveredClass, discovered_class_id)
        if discovered_class is None:
            raise ValueError("Discovered class not found")

        scope_key = _scope_key_for_class(discovered_class)
        item_statement = select(WeeklyScheduleItem).where(WeeklyScheduleItem.scope_key == scope_key)
        if user_id is not None:
            item_statement = item_statement.where(WeeklyScheduleItem.user_id == user_id)
        item = session.exec(item_statement).first()
        if item is None:
            item = _item_from_class(discovered_class, now=now, user_id=user_id)
        else:
            item = _update_item_from_class(item, discovered_class, now=now)

        session.add(item)
        materialized = materialize_weekly_schedule_for_horizon(session=session, now=now)
        session.commit()
        session.refresh(item)
        return {
            "item": serialize_weekly_schedule_item(item),
            "schedule": serialize_weekly_schedule(session, user_id=user_id),
            "materialized": materialized,
        }


def remove_weekly_schedule_item(item_id: int, user_id: int | None = None) -> dict:
    init_db()
    with get_session() as session:
        item = session.get(WeeklyScheduleItem, item_id)
        if item is None:
            raise ValueError("Weekly schedule item not found")
        if user_id is not None and item.user_id != user_id:
            raise ValueError("Weekly schedule item not found")

        for skip in list(
            session.exec(
                select(WeeklyScheduleSkip).where(
                    WeeklyScheduleSkip.scope_key == item.scope_key,
                    *([WeeklyScheduleSkip.user_id == user_id] if user_id is not None else []),
                )
            ).all()
        ):
            session.delete(skip)
        session.delete(item)
        session.commit()
        return {"schedule": serialize_weekly_schedule(session, user_id=user_id)}


def stop_weekly_schedule(user_id: int | None = None) -> dict:
    init_db()
    with get_session() as session:
        items = active_weekly_schedule_items(session, user_id=user_id)
        for item in items:
            session.delete(item)
        for skip in active_weekly_schedule_skips(session, user_id=user_id):
            session.delete(skip)
        session.commit()
        return {"removed_count": len(items), "schedule": serialize_weekly_schedule(session, user_id=user_id)}


def replace_weekly_schedule_from_week(week_start: date, user_id: int | None = None) -> dict:
    init_db()
    week_end = week_start + timedelta(days=6)
    now = local_now()

    with get_session() as session:
        jobs_statement = (
                select(BookingJob)
                .where(
                    BookingJob.rule_name == MANUAL_SELECTION_RULE_NAME,
                    BookingJob.class_date >= week_start,
                    BookingJob.class_date <= week_end,
                    BookingJob.status != BookingStatus.CANCELED,
                    BookingJob.discovered_class_id.is_not(None),
                )
                .order_by(BookingJob.class_date, BookingJob.window_start_time)
        )
        if user_id is not None:
            jobs_statement = jobs_statement.where(BookingJob.user_id == user_id)
        jobs = list(session.exec(jobs_statement).all())
        discovered_classes = []
        seen_class_ids = set()
        for job in jobs:
            if job.discovered_class_id is None or job.discovered_class_id in seen_class_ids:
                continue
            discovered_class = session.get(DiscoveredClass, job.discovered_class_id)
            if discovered_class is None:
                continue
            seen_class_ids.add(discovered_class.id)
            discovered_classes.append(discovered_class)

        if not discovered_classes:
            raise ValueError("No watched classes found for this week")

        for item in active_weekly_schedule_items(session, user_id=user_id):
            session.delete(item)
        for skip in active_weekly_schedule_skips(session, user_id=user_id):
            session.delete(skip)
        session.flush()

        items = []
        seen_scope_keys = set()
        for discovered_class in discovered_classes:
            scope_key = _scope_key_for_class(discovered_class)
            if scope_key in seen_scope_keys:
                continue
            seen_scope_keys.add(scope_key)
            item = _item_from_class(discovered_class, now=now, user_id=user_id)
            session.add(item)
            items.append(item)

        materialized = materialize_weekly_schedule_for_horizon(session=session, now=now)
        session.commit()
        for item in items:
            session.refresh(item)

        return {
            "schedule": serialize_weekly_schedule(session, user_id=user_id),
            "materialized": materialized,
        }


def materialize_weekly_schedule_for_horizon(
    *,
    session: Session,
    now: datetime | None = None,
    user_id: int | None = None,
) -> dict:
    now = now or local_now()
    today = local_today()
    horizon_end = today + timedelta(days=get_settings().discovery_days_ahead)
    items = active_weekly_schedule_items(session, user_id=user_id)
    skip_keys = {
        (skip.user_id, skip.skip_key)
        for skip in active_weekly_schedule_skips(session, user_id=user_id)
    }
    if not items:
        return {
            "range_start": today.isoformat(),
            "range_end": horizon_end.isoformat(),
            "created_count": 0,
            "already_count": 0,
            "skipped_count": 0,
            "paused_count": 0,
            "missing_count": 0,
        }

    candidates = list(
        session.exec(
            select(DiscoveredClass)
            .where(
                DiscoveredClass.class_date >= today,
                DiscoveredClass.class_date <= horizon_end,
            )
            .order_by(DiscoveredClass.class_date, DiscoveredClass.start_time, DiscoveredClass.title)
        ).all()
    )

    created_count = 0
    already_count = 0
    skipped_count = 0
    paused_count = 0
    missing_count = 0
    selected_class_ids: set[int] = set()

    for item in items:
        matches = [candidate for candidate in candidates if _class_matches_item(candidate, item)]
        if not matches:
            missing_count += 1
            continue

        for discovered_class in matches:
            if booking_pause_for_date(
                session,
                discovered_class.class_date,
                user_id=item.user_id,
            ) is not None:
                paused_jobs_statement = select(BookingJob).where(
                    BookingJob.discovered_class_id == discovered_class.id,
                    BookingJob.rule_name == MANUAL_SELECTION_RULE_NAME,
                )
                if item.user_id is None:
                    paused_jobs_statement = paused_jobs_statement.where(
                        BookingJob.user_id.is_(None)
                    )
                else:
                    paused_jobs_statement = paused_jobs_statement.where(
                        BookingJob.user_id == item.user_id
                    )
                for paused_job in session.exec(paused_jobs_statement).all():
                    cancel_job_if_paused(session, paused_job, now=now)
                paused_count += 1
                continue

            class_week_start = _start_of_week(discovered_class.class_date)
            if (item.user_id, _skip_key(item.scope_key, class_week_start)) in skip_keys:
                _cancel_job_for_skipped_class(
                    session,
                    discovered_class,
                    week_start=class_week_start,
                    now=now,
                    user_id=item.user_id,
                )
                skipped_count += 1
                continue

            if discovered_class.id is None or discovered_class.id in selected_class_ids:
                already_count += 1
                continue
            if not _is_selectable(discovered_class):
                skipped_count += 1
                continue

            existing_statement = select(BookingJob).where(
                    BookingJob.discovered_class_id == discovered_class.id,
                    BookingJob.rule_name == MANUAL_SELECTION_RULE_NAME,
                    BookingJob.status != BookingStatus.CANCELED,
                )
            if item.user_id is not None:
                existing_statement = existing_statement.where(BookingJob.user_id == item.user_id)
            existing_job = session.exec(existing_statement).first()
            if existing_job is not None:
                selected_class_ids.add(discovered_class.id)
                already_count += 1
                continue

            upsert_manual_selection_job(
                session,
                discovered_class,
                now=now,
                last_message="Selected from weekly schedule",
                user_id=item.user_id,
            )
            selected_class_ids.add(discovered_class.id)
            created_count += 1

    return {
        "range_start": today.isoformat(),
        "range_end": horizon_end.isoformat(),
        "created_count": created_count,
        "already_count": already_count,
        "skipped_count": skipped_count,
        "paused_count": paused_count,
        "missing_count": missing_count,
    }


def materialize_weekly_schedule() -> dict:
    init_db()
    with get_session() as session:
        result = materialize_weekly_schedule_for_horizon(session=session)
        session.commit()
        return result
