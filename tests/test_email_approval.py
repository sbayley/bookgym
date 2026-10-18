from __future__ import annotations

from datetime import date, datetime, time, timezone
from email.message import EmailMessage
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlmodel import create_engine, select

from app import db as app_db
from app.config import Settings
from app.email_approval import (
    approval_command_from_message,
    next_approval_request_due_at,
    process_approval_replies,
    send_due_approval_requests,
)
from app.job_sources import MANUAL_SELECTION_RULE_NAME
from app.models import (
    ApprovalStatus,
    BookingApproval,
    BookingJob,
    BookingStatus,
    DiscoveredClass,
    WeeklyScheduleItem,
    WeeklyScheduleSkip,
)
from app.time_utils import local_now
from app.weekly_schedule import weekly_schedule_scope_key


def approval_settings() -> Settings:
    return Settings(
        BOOKGYM_EMAIL_APPROVAL_ENABLED=True,
        BOOKGYM_EMAIL_APPROVAL_SEND_TIME="09:00",
        BOOKGYM_EMAIL_APPROVAL_CUTOFF_MINUTES=2,
        BOOKGYM_NOTIFICATION_EMAIL="person@example.com",
        BOOKGYM_SMTP_HOST="smtp.example.com",
        BOOKGYM_SMTP_FROM_EMAIL="bookgym@example.com",
        BOOKGYM_APPROVAL_INBOX_EMAIL="bookgym@example.com",
        BOOKGYM_APPROVAL_ALLOWED_SENDERS="person@example.com",
        BOOKGYM_IMAP_HOST="imap.example.com",
        BOOKGYM_IMAP_USERNAME="bookgym@example.com",
        BOOKGYM_IMAP_PASSWORD="secret",
    )


class TempDatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.original_engine = app_db.engine
        app_db.engine = create_engine(f"sqlite:///{self.db_path}", echo=False)
        app_db.init_db()

    def tearDown(self) -> None:
        app_db.engine = self.original_engine
        self.temp_dir.cleanup()

    def create_job(self, *, recurring: bool = False) -> int:
        now = datetime(2026, 7, 20, 19, 0)
        with app_db.get_session() as session:
            discovered = DiscoveredClass(
                source_key="event:123",
                calendar_event_id=123,
                title="Yoga Level 1",
                class_date=date(2026, 7, 24),
                start_time=time(10, 30),
                raw_summary="Yoga",
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(discovered)
            session.flush()
            job = BookingJob(
                rule_name=MANUAL_SELECTION_RULE_NAME,
                target_match_text=discovered.title,
                class_date=discovered.class_date,
                window_start_time=discovered.start_time,
                window_end_time=discovered.start_time,
                expected_run_at=datetime(2026, 7, 21, 19, 0),
                discovered_class_id=discovered.id,
                status=BookingStatus.PENDING,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            if recurring:
                session.add(
                    WeeklyScheduleItem(
                        scope_key=weekly_schedule_scope_key(
                            weekday=5,
                            start_time=discovered.start_time,
                            title=discovered.title,
                        ),
                        weekday=5,
                        start_time=discovered.start_time,
                        title=discovered.title,
                        title_key="yoga-level-1",
                        created_at=now,
                        updated_at=now,
                    )
                )
            session.commit()
            session.refresh(job)
            return job.id


class ApprovalCommandTests(unittest.TestCase):
    def test_command_must_be_exact_first_nonempty_line(self) -> None:
        message = EmailMessage()
        message.set_content("\n SKIP! \n\nOn Monday BookGym wrote: ...")
        self.assertEqual("skip", approval_command_from_message(message))

        message.set_content("Please skip this one\nSKIP")
        self.assertIsNone(approval_command_from_message(message))


class EmailApprovalTests(TempDatabaseTestCase):
    def test_due_request_is_sent_once_and_records_token(self) -> None:
        job_id = self.create_job()
        now = datetime(2026, 7, 21, 9, 0)
        settings = approval_settings()

        with (
            patch("app.email_approval.get_settings", return_value=settings),
            patch("app.email_approval.send_email_message") as send_mock,
        ):
            first = send_due_approval_requests(now)
            second = send_due_approval_requests(now)

        self.assertEqual(1, first["sent"])
        self.assertEqual(0, second["sent"])
        self.assertEqual(1, send_mock.call_count)
        sent_message = send_mock.call_args.args[0]
        self.assertIn("[BookGym:", sent_message["Subject"])
        self.assertEqual("bookgym@example.com", sent_message["Reply-To"])
        self.assertEqual(
            sent_message["Subject"].split(":", 1)[1].split("]", 1)[0],
            sent_message["X-BookGym-Approval-Request"],
        )
        with app_db.get_session() as session:
            approval = session.exec(
                select(BookingApproval).where(BookingApproval.job_id == job_id)
            ).one()
        self.assertEqual(ApprovalStatus.PENDING, approval.status)
        self.assertEqual(now, approval.sent_at)

    def test_negative_reply_skips_only_recurring_occurrence(self) -> None:
        job_id = self.create_job(recurring=True)
        now = datetime(2026, 7, 21, 9, 0)
        settings = approval_settings()
        with (
            patch("app.email_approval.get_settings", return_value=settings),
            patch("app.email_approval.send_email_message"),
        ):
            send_due_approval_requests(now)

        with app_db.get_session() as session:
            approval = session.exec(
                select(BookingApproval).where(BookingApproval.job_id == job_id)
            ).one()

        reply = EmailMessage()
        reply["Subject"] = f"Re: [BookGym:{approval.token}] Book Yoga Level 1?"
        reply["From"] = "Person <person@example.com>"
        reply["Date"] = datetime(2026, 7, 21, 18, 30, tzinfo=timezone.utc).strftime(
            "%a, %d %b %Y %H:%M:%S %z"
        )
        reply["Message-ID"] = "<reply-1@example.com>"
        reply.set_content("SKIP\n\nOn Monday BookGym wrote: ...")

        with (
            patch("app.email_approval.get_settings", return_value=settings),
            patch("app.email_approval._fetch_recent_messages", return_value=[(42, reply)]),
            patch("app.weekly_schedule.local_today", return_value=date(2026, 7, 20)),
        ):
            result = process_approval_replies(datetime(2026, 7, 21, 18, 45))

        self.assertEqual(1, result["rejected"])
        with app_db.get_session() as session:
            job = session.get(BookingJob, job_id)
            approval = session.exec(
                select(BookingApproval).where(BookingApproval.job_id == job_id)
            ).one()
            skips = list(session.exec(select(WeeklyScheduleSkip)).all())
        self.assertEqual(BookingStatus.CANCELED, job.status)
        self.assertEqual(ApprovalStatus.REJECTED, approval.status)
        self.assertEqual("skip", approval.response_command)
        self.assertEqual(1, len(skips))

    def test_unauthorized_reply_does_not_cancel(self) -> None:
        job_id = self.create_job()
        now = datetime(2026, 7, 21, 9, 0)
        settings = approval_settings()
        with (
            patch("app.email_approval.get_settings", return_value=settings),
            patch("app.email_approval.send_email_message"),
        ):
            send_due_approval_requests(now)
        with app_db.get_session() as session:
            approval = session.exec(
                select(BookingApproval).where(BookingApproval.job_id == job_id)
            ).one()

        reply = EmailMessage()
        reply["Subject"] = f"[BookGym:{approval.token}] Book Yoga Level 1?"
        reply["From"] = "stranger@example.com"
        reply.set_content("CANCEL")
        with (
            patch("app.email_approval.get_settings", return_value=settings),
            patch("app.email_approval._fetch_recent_messages", return_value=[(7, reply)]),
        ):
            result = process_approval_replies(datetime(2026, 7, 21, 18, 0))

        self.assertEqual(0, result["rejected"])
        with app_db.get_session() as session:
            job = session.get(BookingJob, job_id)
        self.assertEqual(BookingStatus.PENDING, job.status)

    def test_outgoing_request_copy_is_ignored_without_an_error(self) -> None:
        job_id = self.create_job()
        now = datetime(2026, 7, 21, 9, 0)
        settings = approval_settings()
        with (
            patch("app.email_approval.get_settings", return_value=settings),
            patch("app.email_approval.send_email_message"),
        ):
            send_due_approval_requests(now)
        with app_db.get_session() as session:
            approval = session.exec(
                select(BookingApproval).where(BookingApproval.job_id == job_id)
            ).one()

        request_copy = EmailMessage()
        request_copy["Subject"] = f"[BookGym:{approval.token}] Book Yoga Level 1?"
        request_copy["From"] = "bookgym@example.com"
        request_copy["X-BookGym-Approval-Request"] = approval.token
        request_copy.set_content("BookGym plans to book Yoga Level 1.")
        with (
            patch("app.email_approval.get_settings", return_value=settings),
            patch(
                "app.email_approval._fetch_recent_messages",
                return_value=[(8, request_copy)],
            ),
        ):
            result = process_approval_replies(datetime(2026, 7, 21, 12, 0))

        self.assertEqual(0, result["processed"])
        with app_db.get_session() as session:
            approval = session.exec(
                select(BookingApproval).where(BookingApproval.job_id == job_id)
            ).one()
        self.assertEqual("", approval.last_error)
        self.assertEqual(8, approval.last_message_uid)

    def test_approved_request_does_not_poll_gmail_again(self) -> None:
        job_id = self.create_job()
        sent_at = datetime(2026, 7, 21, 9, 0)
        settings = approval_settings()
        with (
            patch("app.email_approval.get_settings", return_value=settings),
            patch("app.email_approval.send_email_message"),
        ):
            send_due_approval_requests(sent_at)
        with app_db.get_session() as session:
            approval = session.exec(
                select(BookingApproval).where(BookingApproval.job_id == job_id)
            ).one()
            approval.status = ApprovalStatus.APPROVED
            approval.response_command = "book"
            session.add(approval)
            session.commit()

        with (
            patch("app.email_approval.get_settings", return_value=settings),
            patch("app.email_approval._fetch_recent_messages") as fetch_mock,
        ):
            result = process_approval_replies(datetime(2026, 7, 21, 12, 0))

        self.assertEqual(0, result["active_windows"])
        fetch_mock.assert_not_called()

    def test_closed_booking_window_does_not_poll_gmail(self) -> None:
        job_id = self.create_job()
        sent_at = datetime(2026, 7, 21, 9, 0)
        settings = approval_settings()
        with (
            patch("app.email_approval.get_settings", return_value=settings),
            patch("app.email_approval.send_email_message"),
        ):
            send_due_approval_requests(sent_at)

        with (
            patch("app.email_approval.get_settings", return_value=settings),
            patch("app.email_approval._fetch_recent_messages") as fetch_mock,
        ):
            result = process_approval_replies(datetime(2026, 7, 21, 19, 0, 1))

        self.assertEqual(0, result["active_windows"])
        fetch_mock.assert_not_called()
        with app_db.get_session() as session:
            approval = session.exec(
                select(BookingApproval).where(BookingApproval.job_id == job_id)
            ).one()
        self.assertEqual(ApprovalStatus.EXPIRED, approval.status)

    def test_canceled_booking_does_not_poll_gmail(self) -> None:
        job_id = self.create_job()
        sent_at = datetime(2026, 7, 21, 9, 0)
        settings = approval_settings()
        with (
            patch("app.email_approval.get_settings", return_value=settings),
            patch("app.email_approval.send_email_message"),
        ):
            send_due_approval_requests(sent_at)
        with app_db.get_session() as session:
            job = session.get(BookingJob, job_id)
            job.status = BookingStatus.CANCELED
            session.add(job)
            session.commit()

        with (
            patch("app.email_approval.get_settings", return_value=settings),
            patch("app.email_approval._fetch_recent_messages") as fetch_mock,
        ):
            result = process_approval_replies(datetime(2026, 7, 21, 12, 0))

        self.assertEqual(0, result["active_windows"])
        fetch_mock.assert_not_called()

    def test_next_request_due_is_morning_of_booking_day(self) -> None:
        self.create_job()
        settings = approval_settings()
        with patch("app.email_approval.get_settings", return_value=settings):
            due_at = next_approval_request_due_at(datetime(2026, 7, 19, 12, 0))
        self.assertEqual(datetime(2026, 7, 21, 9, 0), due_at)


if __name__ == "__main__":
    unittest.main()
