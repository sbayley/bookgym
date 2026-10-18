from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.main import STATIC_DIR, admin, home, ops, welcome


class PageRouteTests(unittest.TestCase):
    def test_preview_calendar_is_visibly_labeled(self) -> None:
        with patch("app.main.get_settings", return_value=SimpleNamespace(preview_mode=True)):
            html = home().body.decode("utf-8")

        self.assertIn('data-preview-mode="true"', html)
        self.assertIn("Local preview", html)
        self.assertIn("live gym data allowed", html)
        self.assertIn("real bookings disabled", html)

    def test_home_renders_booking_calendar(self) -> None:
        response = home()
        html = response.body.decode("utf-8")

        self.assertIn("calendar-grid", html)
        self.assertIn("Gym login", html)
        self.assertIn("calendar-scope", html)
        self.assertIn("Away dates", html)
        self.assertIn("booking-pause-modal-shell", html)
        self.assertIn("data-scope-mode=\"all\"", html)
        self.assertIn("data-scope-mode=\"mine\"", html)
        self.assertNotIn("status-toggle", html)
        self.assertNotIn("status-tray", html)
        self.assertNotIn("view-week", html)
        self.assertNotIn("view-month", html)
        self.assertNotIn("week-panel", html)
        self.assertNotIn("scheduler-strip", html)

    def test_login_supports_phone_autofill_and_remembers_verified_email(self) -> None:
        html = (STATIC_DIR.parent / "templates" / "login.html").read_text(encoding="utf-8")

        self.assertIn('name="email"', html)
        self.assertIn('autocomplete="username"', html)
        self.assertIn('name="one-time-code"', html)
        self.assertIn('autocomplete="one-time-code"', html)
        self.assertIn('window.localStorage.setItem(rememberedEmailKey, email)', html)
        self.assertIn('window.localStorage.getItem(rememberedEmailKey)', html)

    def test_admin_and_ops_render_operator_dashboard(self) -> None:
        admin_html = admin().body.decode("utf-8")
        ops_html = ops().body.decode("utf-8")

        self.assertIn("Operator dashboard", admin_html)
        self.assertIn("ops-status", admin_html)
        self.assertEqual(admin_html, ops_html)

    def test_network_capture_control_is_only_in_operator_script(self) -> None:
        calendar_script = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
        operator_script = (STATIC_DIR / "ops.js").read_text(encoding="utf-8")

        self.assertNotIn("network-capture-toggle", calendar_script)
        self.assertNotIn("/network-capture", calendar_script)
        self.assertIn("data-network-capture-job-id", operator_script)
        self.assertIn("/network-capture", operator_script)

    def test_calendar_refreshes_visible_weeks_in_the_background(self) -> None:
        calendar_script = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")

        self.assertIn("IntersectionObserver", calendar_script)
        self.assertIn("data-week-start", calendar_script)
        self.assertIn("/refresh", calendar_script)
        self.assertIn("VISIBLE_WEEK_REFRESH_MS = 60 * 60 * 1000", calendar_script)

    def test_calendar_supports_a_user_booking_pause_window(self) -> None:
        calendar_script = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
        calendar_styles = (STATIC_DIR / "admin.css").read_text(encoding="utf-8")

        self.assertIn('fetchJson("/booking-pause")', calendar_script)
        self.assertIn("Resume bookings on", calendar_script)
        self.assertIn("Existing gym reservations are not canceled", calendar_script)
        self.assertIn("bookingPauseForClass", calendar_script)
        self.assertIn("bookingPauseAffectsClass", calendar_script)
        self.assertIn("pausedJobForClass", calendar_script)
        self.assertIn(".class-chip.booking-paused", calendar_styles)
        self.assertIn(".day.booking-pause-day .day-number::after", calendar_styles)

    def test_mobile_calendar_uses_compact_cards_and_hides_empty_details(self) -> None:
        calendar_script = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
        calendar_styles = (STATIC_DIR / "admin.css").read_text(encoding="utf-8")

        self.assertIn("compactClassTitle", calendar_script)
        self.assertIn("class-title-mobile", calendar_script)
        self.assertIn(".class-title-mobile", calendar_styles)
        self.assertIn(".details-column.detail-prompt-mode", calendar_styles)
        self.assertIn("mobile-detail-close", calendar_script)
        self.assertIn(".mobile-detail-close", calendar_styles)
        self.assertIn("height: 100dvh", calendar_styles)
        self.assertIn("class-detail-open", calendar_script)
        self.assertIn("appUser.managed_users", calendar_script)
        self.assertIn("Use my account", calendar_script)
        self.assertIn(".details-column > .accordion-block", calendar_styles)
        self.assertIn("display: none", calendar_styles)

    def test_welcome_page_links_to_calendar_and_operator_view(self) -> None:
        html = welcome().body.decode("utf-8")

        self.assertIn('href="/"', html)
        self.assertIn('href="/admin"', html)


if __name__ == "__main__":
    unittest.main()
