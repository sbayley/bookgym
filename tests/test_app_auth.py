from __future__ import annotations

from datetime import date, time, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlmodel import Session, create_engine, select

from app import app_auth
from app import db as app_db
from app.config import Settings
from app.main import _host_is_allowed, _https_redirect_url, _origin_is_allowed
from app.models import AppAuditEvent, AppUser, BookingJob, BookingStatus, DiscoveredClass, UserRole
from app.time_utils import local_now
from app.weekly_schedule import upsert_manual_selection_job


class AppAuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.original_engine = app_db.engine
        self.original_settings = app_db.settings
        self.settings = Settings(
            _env_file=None,
            BOOKGYM_APP_AUTH_ENABLED=True,
            BOOKGYM_APP_AUTH_SECRET="test-secret-that-is-deliberately-longer-than-32-characters",
            BOOKGYM_APP_MEMBER_NAME="Member",
            BOOKGYM_APP_MEMBER_EMAIL="member@example.com",
            BOOKGYM_APP_ADMIN_NAME="Administrator",
            BOOKGYM_APP_ADMIN_EMAIL="admin@example.com",
            BOOKGYM_APP_SESSION_DAYS=30,
            BOOKGYM_SMTP_HOST="smtp.example.com",
            BOOKGYM_SMTP_FROM_EMAIL="bookgym@example.com",
        )
        app_db.engine = create_engine(f"sqlite:///{self.db_path}", echo=False)
        app_db.settings = self.settings
        app_db.init_db()

    def tearDown(self) -> None:
        app_db.engine = self.original_engine
        app_db.settings = self.original_settings
        self.temp_dir.cleanup()

    def test_configured_users_are_seeded_and_existing_jobs_are_assigned_to_member(self) -> None:
        now = local_now()
        with Session(app_db.engine) as session:
            users = list(session.exec(select(AppUser).order_by(AppUser.role)).all())
            member = next(user for user in users if user.role == UserRole.MEMBER)
            member_id = int(member.id)
            discovered_class = DiscoveredClass(
                source_key="auth-owner-class",
                title="Yoga Level 1",
                class_date=date(2026, 7, 20),
                start_time=time(9, 0),
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(discovered_class)
            session.flush()
            job = BookingJob(
                user_id=None,
                rule_name="Manual selection",
                target_match_text="Yoga Level 1",
                class_date=discovered_class.class_date,
                expected_run_at=now + timedelta(hours=1),
                discovered_class_id=discovered_class.id,
                status=BookingStatus.PENDING,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.commit()
            job_id = int(job.id)

        app_db.init_db()

        with Session(app_db.engine) as session:
            users = list(session.exec(select(AppUser)).all())
            job = session.get(BookingJob, job_id)

        self.assertEqual(2, len(users))
        self.assertEqual(member_id, job.user_id)

    def test_admin_code_login_defaults_to_managing_member_and_creates_audit_event(self) -> None:
        with patch("app.app_auth.get_settings", return_value=self.settings), patch(
            "app.app_auth._generate_code", return_value="123456"
        ), patch("app.app_auth._send_login_code") as send_login_code:
            app_auth.request_login_code("admin@example.com")
            verified = app_auth.verify_login_code("admin@example.com", "123456")

            self.assertIsNotNone(verified)
            raw_token, context = verified
            resolved = app_auth.resolve_session(raw_token)

        send_login_code.assert_called_once()
        self.assertEqual("Administrator", context.actor.name)
        self.assertEqual("Member", context.subject.name)
        self.assertTrue(context.is_admin)
        self.assertTrue(context.is_delegating)
        self.assertIsNotNone(resolved)
        self.assertEqual("Member", resolved.subject.name)

        with Session(app_db.engine) as session:
            actions = [event.action for event in session.exec(select(AppAuditEvent)).all()]
        self.assertIn("signed_in", actions)

    def test_jobs_can_be_owned_independently_by_member_and_admin(self) -> None:
        now = local_now()
        with Session(app_db.engine) as session:
            users = list(session.exec(select(AppUser)).all())
            member = next(user for user in users if user.role == UserRole.MEMBER)
            admin = next(user for user in users if user.role == UserRole.ADMIN)
            discovered_class = DiscoveredClass(
                source_key="shared-class",
                title="BodyBalance",
                class_date=date(2026, 7, 21),
                start_time=time(10, 30),
                expected_booking_opens_at=now + timedelta(hours=1),
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(discovered_class)
            session.flush()
            member_job, _ = upsert_manual_selection_job(
                session,
                discovered_class,
                now=now,
                user_id=int(member.id),
            )
            admin_job, _ = upsert_manual_selection_job(
                session,
                discovered_class,
                now=now,
                user_id=int(admin.id),
            )
            session.flush()
            member_job_id = int(member_job.id)
            admin_job_id = int(admin_job.id)
            member_user_id = int(member.id)
            admin_user_id = int(admin.id)
            session.commit()

        self.assertNotEqual(member_job_id, admin_job_id)
        with Session(app_db.engine) as session:
            saved_member_job = session.get(BookingJob, member_job_id)
            saved_admin_job = session.get(BookingJob, admin_job_id)
        self.assertEqual(member_user_id, saved_member_job.user_id)
        self.assertEqual(admin_user_id, saved_admin_job.user_id)

    def test_delegate_can_manage_only_explicitly_granted_users(self) -> None:
        with patch("app.app_auth.get_settings", return_value=self.settings), patch(
            "app.app_auth._generate_code", return_value="654321"
        ), patch("app.app_auth._send_login_code"):
            jamie = app_auth.upsert_app_user(
                name="Jamie",
                email="jamie@example.com",
                role=UserRole.DELEGATE,
            )
            managed_users = app_auth.replace_user_delegations(
                int(jamie.id),
                ["member@example.com", "admin@example.com"],
            )
            app_auth.request_login_code("jamie@example.com")
            verified = app_auth.verify_login_code("jamie@example.com", "654321")

        self.assertEqual(["Member", "Administrator"], [user.name for user in managed_users])
        self.assertIsNotNone(verified)
        _raw_token, context = verified
        self.assertEqual("Jamie", context.actor.name)
        self.assertEqual("Member", context.subject.name)
        self.assertFalse(context.is_admin)
        self.assertTrue(context.is_delegating)

        serialized = app_auth.serialize_auth_context(context)
        self.assertTrue(serialized["can_manage_users"])
        self.assertEqual(
            ["Member", "Administrator"],
            [user["name"] for user in serialized["managed_users"]],
        )

        sean = next(user for user in managed_users if user.name == "Administrator")
        sean_context = app_auth.set_managed_user(
            session_id=context.session_id,
            actor_user_id=int(jamie.id),
            subject_user_id=int(sean.id),
        )
        self.assertEqual("Administrator", sean_context.subject.name)

        with self.assertRaisesRegex(ValueError, "Managed user not found"):
            app_auth.set_managed_user(
                session_id=context.session_id,
                actor_user_id=int(jamie.id),
                subject_user_id=int(jamie.id),
            )

    def test_public_portal_configuration_fails_closed_without_https_security(self) -> None:
        insecure_settings = Settings(
            _env_file=None,
            BOOKGYM_APP_AUTH_ENABLED=True,
            BOOKGYM_APP_AUTH_SECRET="test-secret-that-is-deliberately-longer-than-32-characters",
            BOOKGYM_APP_MEMBER_EMAIL="member@example.com",
            BOOKGYM_APP_ADMIN_EMAIL="admin@example.com",
            BOOKGYM_APP_ORIGIN="https://bookgym.seanbayley.com",
            BOOKGYM_APP_SECURE_COOKIE=False,
        )
        with patch("app.app_auth.get_settings", return_value=insecure_settings):
            self.assertEqual(
                "BOOKGYM_APP_SECURE_COOKIE must be true for a public portal.",
                app_auth.validate_auth_configuration(),
            )

    def test_public_host_and_origin_are_restricted(self) -> None:
        allowed_hosts = "bookgym.seanbayley.com,localhost,127.0.0.1"
        self.assertTrue(_host_is_allowed("bookgym.seanbayley.com", allowed_hosts))
        self.assertTrue(_host_is_allowed("127.0.0.1", allowed_hosts))
        self.assertFalse(_host_is_allowed("seanbayley.com", allowed_hosts))
        self.assertTrue(
            _origin_is_allowed(
                "https://bookgym.seanbayley.com",
                "https://bookgym.seanbayley.com",
            )
        )
        self.assertFalse(
            _origin_is_allowed(
                "https://seanbayley.com",
                "https://bookgym.seanbayley.com",
            )
        )
        self.assertFalse(_origin_is_allowed(None, "https://bookgym.seanbayley.com"))

    def test_public_http_requests_redirect_to_https(self) -> None:
        self.assertEqual(
            "https://bookgym.seanbayley.com/calendar?week=2026-07-19",
            _https_redirect_url(
                "http",
                "bookgym.seanbayley.com",
                "/calendar",
                "week=2026-07-19",
                "https://bookgym.seanbayley.com",
            ),
        )
        self.assertIsNone(
            _https_redirect_url(
                "http",
                "127.0.0.1",
                "/health",
                "",
                "https://bookgym.seanbayley.com",
            )
        )
        self.assertIsNone(
            _https_redirect_url(
                "https",
                "bookgym.seanbayley.com",
                "/calendar",
                "",
                "https://bookgym.seanbayley.com",
            )
        )


if __name__ == "__main__":
    unittest.main()
