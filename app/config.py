from datetime import time
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    env: str = Field(default="development", alias="BOOKGYM_ENV")
    preview_mode: bool = Field(default=False, alias="BOOKGYM_PREVIEW_MODE")
    timezone_name: str = Field(default="America/Los_Angeles", alias="BOOKGYM_TIMEZONE")
    database_url: str = Field(default="sqlite:///./bookgym.db", alias="BOOKGYM_DATABASE_URL")
    headless: bool = Field(default=False, alias="BOOKGYM_HEADLESS")

    app_auth_enabled: bool = Field(default=False, alias="BOOKGYM_APP_AUTH_ENABLED")
    app_auth_secret: str = Field(default="", alias="BOOKGYM_APP_AUTH_SECRET")
    app_session_days: int = Field(default=30, alias="BOOKGYM_APP_SESSION_DAYS")
    app_secure_cookie: bool = Field(default=False, alias="BOOKGYM_APP_SECURE_COOKIE")
    app_origin: str = Field(default="", alias="BOOKGYM_APP_ORIGIN")
    app_allowed_hosts: str = Field(default="*", alias="BOOKGYM_APP_ALLOWED_HOSTS")
    app_member_name: str = Field(default="Member", alias="BOOKGYM_APP_MEMBER_NAME")
    app_member_email: str = Field(default="", alias="BOOKGYM_APP_MEMBER_EMAIL")
    app_admin_name: str = Field(default="Administrator", alias="BOOKGYM_APP_ADMIN_NAME")
    app_admin_email: str = Field(default="", alias="BOOKGYM_APP_ADMIN_EMAIL")

    gym_name: str = Field(default="Example Gym", alias="BOOKGYM_GYM_NAME")
    base_url: str = Field(default="https://example.com", alias="BOOKGYM_BASE_URL")
    login_url: str = Field(default="https://example.com/login", alias="BOOKGYM_LOGIN_URL")
    fitness_center_id: int = Field(default=0, alias="BOOKGYM_FITNESS_CENTER_ID")
    department: str = Field(default="GroupExercise", alias="BOOKGYM_DEPARTMENT")
    calendar_url: str = Field(
        default="https://example.com/calendar",
        alias="BOOKGYM_CALENDAR_URL",
    )

    classes_open_days_ahead: int = Field(default=7, alias="BOOKGYM_CLASSES_OPEN_DAYS_AHEAD")
    classes_open_hour: int = Field(default=6, alias="BOOKGYM_CLASSES_OPEN_HOUR")
    classes_open_minute: int = Field(default=0, alias="BOOKGYM_CLASSES_OPEN_MINUTE")
    retry_after_open_seconds: int = Field(default=3, alias="BOOKGYM_RETRY_AFTER_OPEN_SECONDS")
    not_yet_open_fallback_minutes: int = Field(
        default=15,
        alias="BOOKGYM_NOT_YET_OPEN_FALLBACK_MINUTES",
    )

    username: str = Field(default="", alias="BOOKGYM_USERNAME")
    password: str = Field(default="", alias="BOOKGYM_PASSWORD")
    auth_state_path: str = Field(
        default="playwright/.auth/storage_state.json",
        alias="BOOKGYM_AUTH_STATE_PATH",
    )
    discovery_class_types: str = Field(
        default="Yoga,Pilates,BodyPump,BodyBalance",
        alias="BOOKGYM_DISCOVERY_CLASS_TYPES",
    )
    discovery_days_ahead: int = Field(default=28, alias="BOOKGYM_DISCOVERY_DAYS_AHEAD")
    discovery_refresh_minutes: int = Field(
        default=120,
        alias="BOOKGYM_DISCOVERY_REFRESH_MINUTES",
    )
    discovery_week_stale_days: int = Field(
        default=3,
        alias="BOOKGYM_DISCOVERY_WEEK_STALE_DAYS",
    )
    booking_worker_interval_seconds: int = Field(
        default=20,
        alias="BOOKGYM_BOOKING_WORKER_INTERVAL_SECONDS",
    )
    scheduler_heartbeat_seconds: int = Field(
        default=15,
        alias="BOOKGYM_SCHEDULER_HEARTBEAT_SECONDS",
    )
    scheduler_stale_seconds: int = Field(
        default=75,
        alias="BOOKGYM_SCHEDULER_STALE_SECONDS",
    )
    preferred_seat_numbers: str = Field(
        default="33,34,35,32,36,31,37,38,39,40",
        alias="BOOKGYM_PREFERRED_SEAT_NUMBERS",
    )

    notification_email: str = Field(default="", alias="BOOKGYM_NOTIFICATION_EMAIL")
    notification_sms: str = Field(default="", alias="BOOKGYM_NOTIFICATION_SMS")
    smtp_host: str = Field(default="", alias="BOOKGYM_SMTP_HOST")
    smtp_port: int = Field(default=587, alias="BOOKGYM_SMTP_PORT")
    smtp_username: str = Field(default="", alias="BOOKGYM_SMTP_USERNAME")
    smtp_password: str = Field(default="", alias="BOOKGYM_SMTP_PASSWORD")
    smtp_from_email: str = Field(default="", alias="BOOKGYM_SMTP_FROM_EMAIL")
    smtp_use_tls: bool = Field(default=True, alias="BOOKGYM_SMTP_USE_TLS")
    smtp_use_ssl: bool = Field(default=False, alias="BOOKGYM_SMTP_USE_SSL")
    email_approval_enabled: bool = Field(
        default=False,
        alias="BOOKGYM_EMAIL_APPROVAL_ENABLED",
    )
    email_approval_send_time: time = Field(
        default=time(9, 0),
        alias="BOOKGYM_EMAIL_APPROVAL_SEND_TIME",
    )
    email_approval_cutoff_minutes: int = Field(
        default=2,
        alias="BOOKGYM_EMAIL_APPROVAL_CUTOFF_MINUTES",
    )
    email_approval_poll_seconds: int = Field(
        default=30,
        alias="BOOKGYM_EMAIL_APPROVAL_POLL_SECONDS",
    )
    approval_inbox_email: str = Field(default="", alias="BOOKGYM_APPROVAL_INBOX_EMAIL")
    approval_allowed_senders: str = Field(
        default="",
        alias="BOOKGYM_APPROVAL_ALLOWED_SENDERS",
    )
    imap_host: str = Field(default="", alias="BOOKGYM_IMAP_HOST")
    imap_port: int = Field(default=993, alias="BOOKGYM_IMAP_PORT")
    imap_username: str = Field(default="", alias="BOOKGYM_IMAP_USERNAME")
    imap_password: str = Field(default="", alias="BOOKGYM_IMAP_PASSWORD")
    imap_mailbox: str = Field(default="INBOX", alias="BOOKGYM_IMAP_MAILBOX")
    twilio_account_sid: str = Field(default="", alias="BOOKGYM_TWILIO_ACCOUNT_SID")
    twilio_auth_token: str = Field(default="", alias="BOOKGYM_TWILIO_AUTH_TOKEN")
    twilio_from_number: str = Field(default="", alias="BOOKGYM_TWILIO_FROM_NUMBER")

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def preferred_seats(self) -> list[int]:
        values = []
        for chunk in self.preferred_seat_numbers.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            values.append(int(chunk))
        return values

    @property
    def discovery_terms(self) -> list[str]:
        values = []
        for chunk in self.discovery_class_types.split(","):
            chunk = chunk.strip()
            if chunk:
                values.append(chunk)
        return values

    @property
    def notification_sms_recipients(self) -> list[str]:
        values = []
        for chunk in self.notification_sms.split(","):
            chunk = chunk.strip()
            if chunk:
                values.append(chunk)
        return values

    @property
    def notification_email_recipients(self) -> list[str]:
        values = []
        for chunk in self.notification_email.split(","):
            chunk = chunk.strip()
            if chunk:
                values.append(chunk)
        return values

    @property
    def resolved_approval_inbox_email(self) -> str:
        return self.approval_inbox_email or self.smtp_from_email or self.smtp_username

    @property
    def resolved_imap_username(self) -> str:
        return self.imap_username or self.smtp_username

    @property
    def resolved_imap_password(self) -> str:
        return self.imap_password or self.smtp_password

    @property
    def approval_allowed_sender_addresses(self) -> list[str]:
        source = self.approval_allowed_senders.strip()
        if not source:
            return self.notification_email_recipients
        return [chunk.strip() for chunk in source.split(",") if chunk.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
