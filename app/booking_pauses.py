from __future__ import annotations

from datetime import date, datetime

from sqlmodel import Session, select

from app.db import get_session, init_db
from app.models import (
    ApprovalStatus,
    BookingApproval,
    BookingJob,
    BookingPause,
    BookingStatus,
)
from app.time_utils import local_now, local_today


RUNNABLE_STATUSES = {BookingStatus.PENDING, BookingStatus.NOT_YET_OPEN}


def serialize_booking_pause(pause: BookingPause) -> dict:
    return {
        "id": pause.id,
        "user_id": pause.user_id,
        "start_date": pause.start_date.isoformat(),
        "resume_date": pause.resume_date.isoformat(),
        "created_at": pause.created_at.isoformat(),
        "updated_at": pause.updated_at.isoformat(),
    }


def booking_pause_for_date(
    session: Session,
    class_date: date,
    *,
    user_id: int | None,
) -> BookingPause | None:
    statement = select(BookingPause).where(
        BookingPause.start_date <= class_date,
        BookingPause.resume_date > class_date,
    )
    if user_id is not None:
        statement = statement.where(
            (BookingPause.user_id == user_id) | BookingPause.user_id.is_(None)
        )
    return session.exec(statement.order_by(BookingPause.start_date)).first()


def current_or_upcoming_booking_pause(
    session: Session,
    *,
    user_id: int | None,
    today: date | None = None,
) -> BookingPause | None:
    today = today or local_today()
    statement = select(BookingPause).where(BookingPause.resume_date > today)
    if user_id is not None:
        statement = statement.where(
            (BookingPause.user_id == user_id) | BookingPause.user_id.is_(None)
        )
    return session.exec(statement.order_by(BookingPause.start_date)).first()


def serialize_current_booking_pause(user_id: int | None = None) -> dict:
    init_db()
    with get_session() as session:
        pause = current_or_upcoming_booking_pause(session, user_id=user_id)
        return {"pause": serialize_booking_pause(pause) if pause is not None else None}


def _pause_message(pause: BookingPause) -> str:
    return (
        f"Bookings paused for classes from {pause.start_date.isoformat()} "
        f"until bookings resume on {pause.resume_date.isoformat()}"
    )


def cancel_job_if_paused(
    session: Session,
    job: BookingJob,
    *,
    now: datetime | None = None,
) -> BookingPause | None:
    pause = booking_pause_for_date(session, job.class_date, user_id=job.user_id)
    if pause is None:
        return None
    if job.status not in RUNNABLE_STATUSES:
        return pause

    now = now or local_now()
    job.status = BookingStatus.CANCELED
    job.last_message = _pause_message(pause)
    job.capture_network = False
    job.updated_at = now
    session.add(job)

    approval = session.exec(
        select(BookingApproval).where(BookingApproval.job_id == job.id)
    ).first()
    if approval is not None and approval.status == ApprovalStatus.PENDING:
        approval.status = ApprovalStatus.EXPIRED
        approval.response_at = now
        approval.updated_at = now
        approval.last_error = "Booking was paused before the class occurred."
        session.add(approval)
    return pause


def _cancel_jobs_in_pause(session: Session, pause: BookingPause, *, now: datetime) -> list[int]:
    statement = select(BookingJob).where(
        BookingJob.class_date >= pause.start_date,
        BookingJob.class_date < pause.resume_date,
        BookingJob.status.in_(list(RUNNABLE_STATUSES)),
    )
    if pause.user_id is not None:
        statement = statement.where(BookingJob.user_id == pause.user_id)

    canceled_ids: list[int] = []
    for job in session.exec(statement).all():
        cancel_job_if_paused(session, job, now=now)
        if job.id is not None:
            canceled_ids.append(job.id)
    return canceled_ids


def set_booking_pause(
    *,
    start_date: date,
    resume_date: date,
    user_id: int | None = None,
) -> dict:
    if resume_date <= start_date:
        raise ValueError("Resume date must be after the pause start date")
    if resume_date <= local_today():
        raise ValueError("Resume date must be in the future")

    init_db()
    now = local_now()
    with get_session() as session:
        pause = current_or_upcoming_booking_pause(session, user_id=user_id)
        if pause is None:
            pause = BookingPause(
                user_id=user_id,
                start_date=start_date,
                resume_date=resume_date,
                created_at=now,
                updated_at=now,
            )
        else:
            pause.start_date = start_date
            pause.resume_date = resume_date
            pause.updated_at = now
        session.add(pause)
        session.flush()

        canceled_job_ids = _cancel_jobs_in_pause(session, pause, now=now)

        # Updating a pause can expose dates that used to be paused. Rebuild only
        # recurring jobs; one-off selections remain canceled until chosen again.
        from app.weekly_schedule import materialize_weekly_schedule_for_horizon

        materialized = materialize_weekly_schedule_for_horizon(
            session=session,
            now=now,
            user_id=user_id,
        )
        session.commit()
        session.refresh(pause)
        return {
            "pause": serialize_booking_pause(pause),
            "canceled_job_ids": canceled_job_ids,
            "materialized": materialized,
        }


def clear_booking_pause(user_id: int | None = None) -> dict:
    init_db()
    now = local_now()
    with get_session() as session:
        pause = current_or_upcoming_booking_pause(session, user_id=user_id)
        if pause is None:
            return {"removed": False, "pause": None}

        session.delete(pause)
        session.flush()

        from app.weekly_schedule import materialize_weekly_schedule_for_horizon

        materialized = materialize_weekly_schedule_for_horizon(
            session=session,
            now=now,
            user_id=user_id,
        )
        session.commit()
        return {"removed": True, "pause": None, "materialized": materialized}
