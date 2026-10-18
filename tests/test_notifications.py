from __future__ import annotations

from datetime import date, time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.notifications import NotificationContext, send_notification


class NotificationsTests(unittest.TestCase):
    def test_send_notification_sends_email_via_smtp(self) -> None:
        settings = SimpleNamespace(
            notification_email="person1@example.com,person2@example.com",
            notification_email_recipients=["person1@example.com", "person2@example.com"],
            notification_sms="",
            notification_sms_recipients=[],
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="mailer@example.com",
            smtp_password="app-password",
            smtp_from_email="mailer@example.com",
            smtp_use_tls=True,
            smtp_use_ssl=False,
            twilio_account_sid="",
            twilio_auth_token="",
            twilio_from_number="",
        )
        fake_smtp = MagicMock()
        fake_smtp.__enter__.return_value = fake_smtp
        fake_smtp.__exit__.return_value = False

        with (
            patch("app.notifications.get_settings", return_value=settings),
            patch("app.notifications.smtplib.SMTP", return_value=fake_smtp) as smtp_mock,
            patch("builtins.print") as print_mock,
        ):
            send_notification(
                "BookGym: Yoga Level 1 booked for Sun Jul 05 at 8:15 AM",
                "Attempted to book 'Yoga Level 1' targeting seat 23. Success signal found.",
                context=NotificationContext(
                    class_name="Yoga Level 1",
                    status="booked",
                    class_date=date(2026, 7, 5),
                    class_time=time(8, 15),
                    seat_label="23",
                    is_dry_run=False,
                    screenshot_path="artifacts/booking-attempt-2026-07-05.png",
                ),
            )

        smtp_mock.assert_called_once_with("smtp.example.com", 587, timeout=15)
        fake_smtp.starttls.assert_called_once()
        fake_smtp.login.assert_called_once_with("mailer@example.com", "app-password")
        fake_smtp.send_message.assert_called_once()
        email_message = fake_smtp.send_message.call_args.args[0]
        self.assertEqual("BookGym: Yoga Level 1 booked for Sun Jul 05 at 8:15 AM", email_message["Subject"])
        self.assertEqual("mailer@example.com", email_message["From"])
        self.assertEqual("person1@example.com, person2@example.com", email_message["To"])
        plain_part, html_part = email_message.iter_parts()
        plain_body = plain_part.get_content()
        html_body = html_part.get_content()
        self.assertIn("Class booked", plain_body)
        self.assertIn("Class: Yoga Level 1", plain_body)
        self.assertIn("Seat: 23", plain_body)
        self.assertNotIn("Status:", plain_body)
        self.assertNotIn("BookGym: Yoga Level 1 booked for Sun Jul 05 at 8:15 AM", plain_body)
        self.assertNotIn("Mode:", plain_body)
        self.assertNotIn("Screenshot:", plain_body)
        self.assertIn("Yoga Level 1", html_body)
        self.assertIn("Class booked", html_body)
        self.assertIn("Seat", html_body)
        self.assertIn("23", html_body)
        self.assertNotIn("BookGym: Yoga Level 1 booked for Sun Jul 05 at 8:15 AM", html_body)
        self.assertNotIn("Mode", html_body)
        self.assertNotIn("Screenshot", html_body)
        print_mock.assert_called_with(
            "[notification -> email:person1@example.com, email:person2@example.com] "
            "BookGym: Yoga Level 1 booked for Sun Jul 05 at 8:15 AM\n"
            "Attempted to book 'Yoga Level 1' targeting seat 23. Success signal found."
        )

    def test_send_notification_uses_override_email_recipients(self) -> None:
        settings = SimpleNamespace(
            notification_email="default@example.com",
            notification_email_recipients=["default@example.com"],
            notification_sms="",
            notification_sms_recipients=[],
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="mailer@example.com",
            smtp_password="app-password",
            smtp_from_email="mailer@example.com",
            smtp_use_tls=True,
            smtp_use_ssl=False,
            twilio_account_sid="",
            twilio_auth_token="",
            twilio_from_number="",
        )
        fake_smtp = MagicMock()
        fake_smtp.__enter__.return_value = fake_smtp
        fake_smtp.__exit__.return_value = False

        with (
            patch("app.notifications.get_settings", return_value=settings),
            patch("app.notifications.smtplib.SMTP", return_value=fake_smtp),
            patch("builtins.print") as print_mock,
        ):
            send_notification(
                "BookGym: Dry run for Yoga Level 1 on Sun Jul 05 at 8:15 AM",
                "Dry run completed without clicking Done.",
                email_recipients=["override1@example.com", "override2@example.com"],
                sms_recipients=[],
            )

        email_message = fake_smtp.send_message.call_args.args[0]
        self.assertEqual("override1@example.com, override2@example.com", email_message["To"])
        print_mock.assert_called_with(
            "[notification -> email:override1@example.com, email:override2@example.com] "
            "BookGym: Dry run for Yoga Level 1 on Sun Jul 05 at 8:15 AM\n"
            "Dry run completed without clicking Done."
        )

    def test_send_notification_posts_sms_via_twilio(self) -> None:
        settings = SimpleNamespace(
            notification_email="",
            notification_email_recipients=[],
            notification_sms="+15551230001,+15551230002",
            notification_sms_recipients=["+15551230001", "+15551230002"],
            smtp_host="",
            smtp_port=587,
            smtp_username="",
            smtp_password="",
            smtp_from_email="",
            smtp_use_tls=True,
            smtp_use_ssl=False,
            twilio_account_sid="AC123",
            twilio_auth_token="secret-token",
            twilio_from_number="+15557654321",
        )
        fake_response = MagicMock()
        fake_response.__enter__.return_value = fake_response
        fake_response.__exit__.return_value = False

        with (
            patch("app.notifications.get_settings", return_value=settings),
            patch("app.notifications.urlopen", return_value=fake_response) as urlopen_mock,
            patch("builtins.print") as print_mock,
        ):
            send_notification("BookGym attempt: Yoga -> booked", "Booked seat 23")

        self.assertEqual(2, urlopen_mock.call_count)
        first_request = urlopen_mock.call_args_list[0].args[0]
        self.assertIn("/Accounts/AC123/Messages.json", first_request.full_url)
        self.assertIn("To=%2B15551230001", first_request.data.decode("utf-8"))
        self.assertIn("From=%2B15557654321", first_request.data.decode("utf-8"))
        self.assertIn("Booked+seat+23", first_request.data.decode("utf-8"))
        print_mock.assert_called_with(
            "[notification -> sms:+15551230001, sms:+15551230002] "
            "BookGym attempt: Yoga -> booked\nBooked seat 23"
        )

    def test_send_notification_falls_back_to_stdout_when_twilio_missing(self) -> None:
        settings = SimpleNamespace(
            notification_email="",
            notification_email_recipients=[],
            notification_sms="+15551230001",
            notification_sms_recipients=["+15551230001"],
            smtp_host="",
            smtp_port=587,
            smtp_username="",
            smtp_password="",
            smtp_from_email="",
            smtp_use_tls=True,
            smtp_use_ssl=False,
            twilio_account_sid="",
            twilio_auth_token="",
            twilio_from_number="",
        )

        with (
            patch("app.notifications.get_settings", return_value=settings),
            patch("app.notifications.urlopen") as urlopen_mock,
            patch("builtins.print") as print_mock,
        ):
            send_notification("BookGym attempt: Yoga -> failed", "Dry run only")

        urlopen_mock.assert_not_called()
        self.assertEqual(2, print_mock.call_count)
        warning_call = print_mock.call_args_list[0].args[0]
        self.assertIn("Twilio credentials are incomplete", warning_call)
        final_call = print_mock.call_args_list[1].args[0]
        self.assertIn("[notification -> sms:+15551230001]", final_call)

    def test_send_notification_falls_back_to_stdout_when_smtp_missing(self) -> None:
        settings = SimpleNamespace(
            notification_email="person@example.com",
            notification_email_recipients=["person@example.com"],
            notification_sms="",
            notification_sms_recipients=[],
            smtp_host="",
            smtp_port=587,
            smtp_username="",
            smtp_password="",
            smtp_from_email="",
            smtp_use_tls=True,
            smtp_use_ssl=False,
            twilio_account_sid="",
            twilio_auth_token="",
            twilio_from_number="",
        )

        with (
            patch("app.notifications.get_settings", return_value=settings),
            patch("app.notifications.smtplib.SMTP") as smtp_mock,
            patch("builtins.print") as print_mock,
        ):
            send_notification("BookGym attempt: Yoga -> failed", "SMTP not configured")

        smtp_mock.assert_not_called()
        self.assertEqual(2, print_mock.call_count)
        warning_call = print_mock.call_args_list[0].args[0]
        self.assertIn("SMTP settings are incomplete", warning_call)
        final_call = print_mock.call_args_list[1].args[0]
        self.assertIn("[notification -> email:person@example.com]", final_call)


if __name__ == "__main__":
    unittest.main()
