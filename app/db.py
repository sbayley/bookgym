from contextlib import contextmanager

from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings


settings = get_settings()
engine = create_engine(settings.database_url, echo=False)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _ensure_discovered_class_columns()
    _ensure_booking_job_columns()
    _ensure_booking_event_columns()
    _ensure_booking_approval_columns()
    _ensure_seat_preference_columns()
    _ensure_user_ownership_columns()
    _seed_configured_users()
    _drop_legacy_tables()


def _ensure_discovered_class_columns() -> None:
    required_columns = {
        "calendar_event_id": "INTEGER",
        "layout_id": "INTEGER",
        "event_type": "TEXT NOT NULL DEFAULT ''",
        "booking_type_id": "INTEGER",
        "resource_id": "INTEGER",
        "can_show_layout": "INTEGER",
        "seat_title": "TEXT NOT NULL DEFAULT ''",
        "total_spots": "INTEGER",
        "total_available_spots": "INTEGER",
        "total_booked": "INTEGER",
        "is_full": "INTEGER",
        "can_book_event": "INTEGER",
        "gym_booking_status": "TEXT NOT NULL DEFAULT ''",
        "is_booked_at_gym": "INTEGER",
        "gym_booking_spot_labels": "TEXT NOT NULL DEFAULT ''",
        "gym_booking_actions": "TEXT NOT NULL DEFAULT ''",
        "deep_link_url": "TEXT NOT NULL DEFAULT ''",
        "expected_booking_opens_at": "TEXT",
        "site_booking_opens_at": "TEXT",
        "booking_probe_status": "TEXT NOT NULL DEFAULT ''",
        "booking_probe_message": "TEXT NOT NULL DEFAULT ''",
        "last_probed_at": "TEXT",
        "api_payload": "TEXT NOT NULL DEFAULT ''",
    }

    with engine.begin() as connection:
        table_exists = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='discoveredclass'"
        ).first()
        if not table_exists:
            return

        existing_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info('discoveredclass')").fetchall()
        }
        for column_name, column_sql in required_columns.items():
            if column_name in existing_columns:
                continue
            connection.exec_driver_sql(
                f"ALTER TABLE discoveredclass ADD COLUMN {column_name} {column_sql}"
            )

        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_discoveredclass_calendar_event_id "
            "ON discoveredclass (calendar_event_id)"
        )

        open_time = f"{settings.classes_open_hour:02d}:{settings.classes_open_minute:02d}:00"
        connection.exec_driver_sql(
            "UPDATE discoveredclass "
            "SET expected_booking_opens_at = datetime(class_date || ' ' || ?, ?) "
            "WHERE expected_booking_opens_at IS NULL",
            (open_time, f"-{settings.classes_open_days_ahead} days"),
        )


def _ensure_booking_job_columns() -> None:
    required_columns = {
        "calendar_event_id": "INTEGER",
        "capture_network": "INTEGER NOT NULL DEFAULT 0",
        "last_network_artifact_path": "TEXT NOT NULL DEFAULT ''",
        "notification_email_override": "TEXT",
        "notification_sms_override": "TEXT",
    }

    with engine.begin() as connection:
        table_exists = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bookingjob'"
        ).first()
        if not table_exists:
            return

        existing_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info('bookingjob')").fetchall()
        }
        for column_name, column_sql in required_columns.items():
            if column_name in existing_columns:
                continue
            connection.exec_driver_sql(
                f"ALTER TABLE bookingjob ADD COLUMN {column_name} {column_sql}"
            )

        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_bookingjob_calendar_event_id "
            "ON bookingjob (calendar_event_id)"
        )
        connection.exec_driver_sql(
            "UPDATE bookingjob "
            "SET calendar_event_id = ("
            "    SELECT discoveredclass.calendar_event_id "
            "    FROM discoveredclass "
            "    WHERE discoveredclass.id = bookingjob.discovered_class_id"
            ") "
            "WHERE calendar_event_id IS NULL AND discovered_class_id IS NOT NULL"
        )


def _ensure_booking_event_columns() -> None:
    required_columns = {
        "network_artifact_path": "TEXT NOT NULL DEFAULT ''",
    }

    with engine.begin() as connection:
        table_exists = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bookingevent'"
        ).first()
        if not table_exists:
            return

        existing_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info('bookingevent')").fetchall()
        }
        for column_name, column_sql in required_columns.items():
            if column_name in existing_columns:
                continue
            connection.exec_driver_sql(
                f"ALTER TABLE bookingevent ADD COLUMN {column_name} {column_sql}"
            )


def _ensure_booking_approval_columns() -> None:
    required_columns = {
        "last_message_uid": "INTEGER NOT NULL DEFAULT 0",
        "last_error": "TEXT NOT NULL DEFAULT ''",
    }

    with engine.begin() as connection:
        table_exists = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bookingapproval'"
        ).first()
        if not table_exists:
            return

        existing_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info('bookingapproval')").fetchall()
        }
        for column_name, column_sql in required_columns.items():
            if column_name in existing_columns:
                continue
            connection.exec_driver_sql(
                f"ALTER TABLE bookingapproval ADD COLUMN {column_name} {column_sql}"
            )


def _ensure_seat_preference_columns() -> None:
    required_columns = {
        "selection_mode": "TEXT NOT NULL DEFAULT 'strict'",
    }

    with engine.begin() as connection:
        table_exists = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='seatpreference'"
        ).first()
        if not table_exists:
            return

        existing_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info('seatpreference')").fetchall()
        }
        for column_name, column_sql in required_columns.items():
            if column_name in existing_columns:
                continue
            connection.exec_driver_sql(
                f"ALTER TABLE seatpreference ADD COLUMN {column_name} {column_sql}"
            )


def _ensure_user_ownership_columns() -> None:
    owned_tables = [
        "bookingjob",
        "bookingapproval",
        "seatpreference",
        "weeklyscheduleitem",
        "weeklyscheduleskip",
        "gymcredential",
    ]
    with engine.begin() as connection:
        for table_name in owned_tables:
            table_exists = connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).first()
            if not table_exists:
                continue
            existing_columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    f"PRAGMA table_info('{table_name}')"
                ).fetchall()
            }
            if "user_id" not in existing_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table_name} ADD COLUMN user_id INTEGER REFERENCES appuser(id)"
                )
            connection.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS ix_{table_name}_user_id ON {table_name} (user_id)"
            )


def _seed_configured_users() -> None:
    member_email = settings.app_member_email.strip().lower()
    admin_email = settings.app_admin_email.strip().lower()
    configured = [
        (member_email, settings.app_member_name.strip() or "Member", "MEMBER"),
        (admin_email, settings.app_admin_name.strip() or "Admin", "ADMIN"),
    ]
    now = __import__("app.time_utils", fromlist=["local_now"]).local_now().isoformat(sep=" ")

    with engine.begin() as connection:
        for email, name, role in configured:
            if not email:
                continue
            existing = connection.exec_driver_sql(
                "SELECT id FROM appuser WHERE lower(email) = ?",
                (email,),
            ).first()
            if existing:
                connection.exec_driver_sql(
                    "UPDATE appuser SET name = ?, role = ?, is_active = 1, updated_at = ? WHERE id = ?",
                    (name, role, now, existing[0]),
                )
            else:
                connection.exec_driver_sql(
                    "INSERT INTO appuser (name, email, role, is_active, created_at, updated_at) "
                    "VALUES (?, ?, ?, 1, ?, ?)",
                    (name, email, role, now, now),
                )

        if not member_email:
            return
        member = connection.exec_driver_sql(
            "SELECT id FROM appuser WHERE lower(email) = ?",
            (member_email,),
        ).first()
        if member is None:
            return
        member_id = member[0]
        for table_name in [
            "bookingjob",
            "bookingapproval",
            "seatpreference",
            "weeklyscheduleitem",
            "weeklyscheduleskip",
            "bookingpause",
            "gymcredential",
        ]:
            connection.exec_driver_sql(
                f"UPDATE {table_name} SET user_id = ? WHERE user_id IS NULL",
                (member_id,),
            )


def _drop_legacy_tables() -> None:
    legacy_tables = ["bookingattempt", "classpreference"]

    with engine.begin() as connection:
        for table_name in legacy_tables:
            connection.exec_driver_sql(f"DROP TABLE IF EXISTS {table_name}")


@contextmanager
def get_session() -> Session:
    with Session(engine) as session:
        yield session
