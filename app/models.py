from __future__ import annotations

from datetime import date, datetime, time
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel

from app.time_utils import local_now


class BookingStatus(str, Enum):
    PENDING = "pending"
    CANCELED = "canceled"
    BOOKED = "booked"
    WAITLISTED = "waitlisted"
    FAILED = "failed"
    DRY_RUN = "dry_run"
    NOT_YET_OPEN = "not_yet_open"
    NEEDS_CONFIRMATION = "needs_confirmation"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class UserRole(str, Enum):
    MEMBER = "member"
    DELEGATE = "delegate"
    ADMIN = "admin"


class DiscoveredClass(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    source_key: str = Field(index=True, unique=True)
    calendar_event_id: Optional[int] = Field(default=None, index=True)
    layout_id: Optional[int] = None
    title: str
    class_date: date
    start_time: Optional[time] = None
    instructor_name: str = ""
    category: str = ""
    duration_minutes: Optional[int] = None
    event_type: str = ""
    booking_type_id: Optional[int] = None
    resource_id: Optional[int] = None
    can_show_layout: Optional[bool] = None
    seat_title: str = ""
    total_spots: Optional[int] = None
    total_available_spots: Optional[int] = None
    total_booked: Optional[int] = None
    is_full: Optional[bool] = None
    can_book_event: Optional[bool] = None
    gym_booking_status: str = ""
    is_booked_at_gym: Optional[bool] = None
    gym_booking_spot_labels: str = ""
    gym_booking_actions: str = ""
    deep_link_url: str = ""
    expected_booking_opens_at: Optional[datetime] = None
    site_booking_opens_at: Optional[datetime] = None
    booking_probe_status: str = ""
    booking_probe_message: str = ""
    last_probed_at: Optional[datetime] = None
    raw_summary: str = ""
    api_payload: str = ""
    first_seen_at: datetime = Field(default_factory=local_now, nullable=False)
    last_seen_at: datetime = Field(default_factory=local_now, nullable=False)


class BookingJob(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="appuser.id", index=True)
    rule_name: str = Field(index=True)
    target_match_text: str
    class_date: date = Field(index=True)
    window_start_time: Optional[time] = None
    window_end_time: Optional[time] = None
    expected_run_at: datetime = Field(index=True)
    calendar_event_id: Optional[int] = Field(default=None, index=True)
    site_booking_opens_at: Optional[datetime] = None
    discovered_class_id: Optional[int] = Field(default=None, foreign_key="discoveredclass.id")
    status: BookingStatus = Field(default=BookingStatus.PENDING, index=True)
    last_message: str = ""
    last_screenshot_path: str = ""
    capture_network: bool = Field(
        default=False,
        sa_column_kwargs={"server_default": "0"},
    )
    last_network_artifact_path: str = Field(
        default="",
        sa_column_kwargs={"server_default": ""},
    )
    notification_email_override: Optional[str] = None
    notification_sms_override: Optional[str] = None
    created_at: datetime = Field(default_factory=local_now, nullable=False)
    updated_at: datetime = Field(default_factory=local_now, nullable=False)


class BookingEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: Optional[int] = Field(default=None, foreign_key="bookingjob.id", index=True)
    discovered_class_id: Optional[int] = Field(default=None, foreign_key="discoveredclass.id")
    event_type: str = Field(index=True)
    status: BookingStatus
    message: str = ""
    screenshot_path: str = ""
    network_artifact_path: str = Field(
        default="",
        sa_column_kwargs={"server_default": ""},
    )
    site_booking_opens_at: Optional[datetime] = None
    observed_at: datetime = Field(default_factory=local_now, nullable=False, index=True)


class BookingApproval(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="appuser.id", index=True)
    job_id: int = Field(foreign_key="bookingjob.id", index=True, unique=True)
    token: str = Field(index=True, unique=True)
    status: ApprovalStatus = Field(default=ApprovalStatus.PENDING, index=True)
    request_due_at: datetime = Field(index=True)
    response_cutoff_at: datetime = Field(index=True)
    sent_at: Optional[datetime] = Field(default=None, index=True)
    response_at: Optional[datetime] = None
    responder_email: str = ""
    response_command: str = ""
    response_message_id: str = ""
    last_message_uid: int = Field(
        default=0,
        sa_column_kwargs={"server_default": "0"},
    )
    last_error: str = ""
    created_at: datetime = Field(default_factory=local_now, nullable=False)
    updated_at: datetime = Field(default_factory=local_now, nullable=False)


class DiscoveryEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: Optional[int] = Field(default=None, foreign_key="bookingjob.id", index=True)
    discovered_class_id: Optional[int] = Field(default=None, foreign_key="discoveredclass.id")
    event_type: str = Field(index=True)
    message: str = ""
    visible_week_start: Optional[date] = None
    visible_week_end: Optional[date] = None
    observed_at: datetime = Field(default_factory=local_now, nullable=False, index=True)


class SeatPreference(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="appuser.id", index=True)
    scope_key: str = Field(index=True, unique=True)
    layout_id: int = Field(index=True)
    category_key: str = Field(index=True)
    category_label: str = ""
    selection_mode: str = "strict"
    included_seats_json: str = "[]"
    excluded_seats_json: str = "[]"
    created_at: datetime = Field(default_factory=local_now, nullable=False)
    updated_at: datetime = Field(default_factory=local_now, nullable=False)


class WeeklyScheduleItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="appuser.id", index=True)
    scope_key: str = Field(index=True, unique=True)
    weekday: int = Field(index=True)
    start_time: Optional[time] = Field(default=None, index=True)
    title: str
    title_key: str = Field(index=True)
    category: str = ""
    instructor_name: str = ""
    booking_type_id: Optional[int] = None
    created_at: datetime = Field(default_factory=local_now, nullable=False)
    updated_at: datetime = Field(default_factory=local_now, nullable=False)


class WeeklyScheduleSkip(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="appuser.id", index=True)
    skip_key: str = Field(index=True, unique=True)
    scope_key: str = Field(index=True)
    week_start: date = Field(index=True)
    created_at: datetime = Field(default_factory=local_now, nullable=False)


class BookingPause(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="appuser.id", index=True)
    start_date: date = Field(index=True)
    resume_date: date = Field(index=True)
    created_at: datetime = Field(default_factory=local_now, nullable=False)
    updated_at: datetime = Field(default_factory=local_now, nullable=False)


class GymCredential(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="appuser.id", index=True)
    username: str = ""
    password: str = ""
    created_at: datetime = Field(default_factory=local_now, nullable=False)
    updated_at: datetime = Field(default_factory=local_now, nullable=False)
    last_login_attempted_at: Optional[datetime] = None
    last_login_succeeded_at: Optional[datetime] = None
    last_login_error: str = ""


class AppUser(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    email: str = Field(index=True, unique=True)
    role: UserRole = Field(default=UserRole.MEMBER, index=True)
    is_active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=local_now, nullable=False)
    updated_at: datetime = Field(default_factory=local_now, nullable=False)


class AppDelegationGrant(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    actor_user_id: int = Field(foreign_key="appuser.id", index=True)
    subject_user_id: int = Field(foreign_key="appuser.id", index=True)
    created_at: datetime = Field(default_factory=local_now, nullable=False)


class AppLoginCode(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="appuser.id", index=True)
    code_hash: str = Field(index=True)
    expires_at: datetime = Field(index=True)
    consumed_at: Optional[datetime] = Field(default=None, index=True)
    failed_attempts: int = Field(default=0)
    created_at: datetime = Field(default_factory=local_now, nullable=False, index=True)


class AppSession(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="appuser.id", index=True)
    active_user_id: int = Field(foreign_key="appuser.id", index=True)
    token_hash: str = Field(index=True, unique=True)
    expires_at: datetime = Field(index=True)
    revoked_at: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=local_now, nullable=False)
    last_seen_at: datetime = Field(default_factory=local_now, nullable=False)


class AppAuditEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    actor_user_id: int = Field(foreign_key="appuser.id", index=True)
    subject_user_id: int = Field(foreign_key="appuser.id", index=True)
    action: str = Field(index=True)
    resource: str = ""
    detail: str = ""
    observed_at: datetime = Field(default_factory=local_now, nullable=False, index=True)
