from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlmodel import create_engine

from app import db as app_db
from app.gym_auth import clear_gym_credentials, gym_auth_status, resolve_gym_credentials, save_gym_credentials
from app.main import (
    GymCredentialUpdatePayload,
    clear_gym_credentials_route,
    gym_auth_status_route,
    refresh_gym_auth_route,
    save_gym_credentials_route,
)


class GymAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.auth_state_path = Path(self.temp_dir.name) / "auth.json"
        self.original_engine = app_db.engine
        app_db.engine = create_engine(f"sqlite:///{self.db_path}", echo=False)
        app_db.init_db()
        self.settings_patch = patch(
            "app.gym_auth.get_settings",
            return_value=SimpleNamespace(
                username="",
                password="",
                auth_state_path=str(self.auth_state_path),
                timezone=ZoneInfo("America/Los_Angeles"),
            ),
        )
        self.settings_patch.start()

    def tearDown(self) -> None:
        self.settings_patch.stop()
        app_db.engine = self.original_engine
        self.temp_dir.cleanup()

    def test_save_and_clear_gym_credentials_updates_status(self) -> None:
        save_gym_credentials(username="member@example.com", password="secret")

        resolved = resolve_gym_credentials()
        status = gym_auth_status()

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual("member@example.com", resolved.username)
        self.assertEqual("app", resolved.source)
        self.assertTrue(status["has_credentials"])
        self.assertTrue(status["has_app_credentials"])
        self.assertEqual("me****@example.com", status["display_username"])

        clear_gym_credentials()

        cleared_status = gym_auth_status()
        self.assertFalse(cleared_status["has_credentials"])
        self.assertFalse(cleared_status["has_app_credentials"])


class GymAuthRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.original_engine = app_db.engine
        app_db.engine = create_engine(f"sqlite:///{self.db_path}", echo=False)
        app_db.init_db()

    def tearDown(self) -> None:
        app_db.engine = self.original_engine
        self.temp_dir.cleanup()

    def test_gym_credential_routes_round_trip(self) -> None:
        fake_settings = SimpleNamespace(
            username="",
            password="",
            auth_state_path=str(Path(self.temp_dir.name) / "auth.json"),
            timezone=ZoneInfo("America/Los_Angeles"),
        )

        with patch("app.gym_auth.get_settings", return_value=fake_settings):
            save_response = save_gym_credentials_route(
                GymCredentialUpdatePayload(username="member@example.com", password="secret")
            )
            self.assertTrue(save_response["has_app_credentials"])

            status_response = gym_auth_status_route()
            self.assertEqual("app", status_response["credential_source"])

            clear_response = clear_gym_credentials_route()
            self.assertFalse(clear_response["has_credentials"])

    def test_refresh_route_returns_refreshed_status(self) -> None:
        with patch(
            "app.main.refresh_gym_session",
            return_value={
                "has_credentials": True,
                "credential_source": "app",
                "display_username": "me****@example.com",
                "has_app_credentials": True,
                "has_env_credentials": False,
                "session_cached": True,
                "session_saved_at": None,
                "last_login_attempted_at": None,
                "last_login_succeeded_at": None,
                "last_login_error": "",
            },
        ):
            response = refresh_gym_auth_route()

        self.assertEqual("Gym session refreshed.", response["message"])
        self.assertTrue(response["session_cached"])


if __name__ == "__main__":
    unittest.main()
