from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlmodel import select

from app.config import get_settings
from app.db import get_session, init_db
from app.models import GymCredential
from app.time_utils import local_now


@dataclass
class ResolvedGymCredentials:
    username: str
    password: str
    source: str


def auth_state_path() -> Path:
    settings = get_settings()
    return Path(settings.auth_state_path)


def auth_state_context_kwargs() -> dict[str, str]:
    path = auth_state_path()
    if not path.exists():
        return {}
    return {"storage_state": str(path)}


def ensure_auth_state_directory() -> Path:
    path = auth_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _current_credential_row(session, user_id: int | None = None) -> GymCredential | None:
    statement = select(GymCredential)
    if user_id is not None:
        statement = statement.where(GymCredential.user_id == user_id)
    return session.exec(statement.order_by(GymCredential.updated_at.desc())).first()


def resolve_gym_credentials(user_id: int | None = None) -> ResolvedGymCredentials | None:
    init_db()
    with get_session() as session:
        saved = _current_credential_row(session, user_id=user_id)
        if saved is not None and saved.username and saved.password:
            return ResolvedGymCredentials(
                username=saved.username,
                password=saved.password,
                source="app",
            )

    settings = get_settings()
    if settings.username and settings.password:
        return ResolvedGymCredentials(
            username=settings.username,
            password=settings.password,
            source="env",
        )
    return None


def save_gym_credentials(
    username: str,
    password: str,
    user_id: int | None = None,
) -> GymCredential:
    init_db()
    now = local_now()
    clean_username = username.strip()
    if not clean_username:
        raise ValueError("Gym username is required.")
    if not password.strip():
        raise ValueError("Gym password is required.")

    with get_session() as session:
        saved = _current_credential_row(session, user_id=user_id)
        if saved is None:
            saved = GymCredential(
                user_id=user_id,
                username=clean_username,
                password=password,
                created_at=now,
                updated_at=now,
            )
        else:
            if user_id is not None:
                saved.user_id = user_id
            saved.username = clean_username
            saved.password = password
            saved.updated_at = now
        saved.last_login_error = ""
        session.add(saved)
        session.commit()
        session.refresh(saved)
        return saved


def clear_gym_credentials(user_id: int | None = None) -> None:
    init_db()
    with get_session() as session:
        saved = _current_credential_row(session, user_id=user_id)
        if saved is None:
            return
        session.delete(saved)
        session.commit()


def record_login_result(success: bool, error_message: str = "") -> None:
    init_db()
    with get_session() as session:
        saved = _current_credential_row(session)
        if saved is None:
            return
        saved.last_login_attempted_at = local_now()
        if success:
            saved.last_login_succeeded_at = saved.last_login_attempted_at
            saved.last_login_error = ""
        else:
            saved.last_login_error = error_message
        session.add(saved)
        session.commit()


def _mask_username(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    if "@" in normalized:
        local_part, domain = normalized.split("@", 1)
        if len(local_part) <= 2:
            masked_local = local_part[:1] + "*"
        else:
            masked_local = f"{local_part[:2]}{'*' * max(2, len(local_part) - 2)}"
        return f"{masked_local}@{domain}"
    if len(normalized) <= 4:
        return normalized[:1] + "***"
    return f"{normalized[:2]}{'*' * max(2, len(normalized) - 4)}{normalized[-2:]}"


def gym_auth_status(user_id: int | None = None) -> dict[str, object]:
    init_db()
    with get_session() as session:
        saved = _current_credential_row(session, user_id=user_id)

    resolved = resolve_gym_credentials(user_id=user_id)
    state_path = auth_state_path()
    session_exists = state_path.exists()
    timezone = get_settings().timezone
    session_saved_at = (
        datetime.fromtimestamp(state_path.stat().st_mtime, tz=timezone).isoformat()
        if session_exists
        else None
    )

    return {
        "has_credentials": resolved is not None,
        "credential_source": resolved.source if resolved is not None else None,
        "display_username": _mask_username(resolved.username) if resolved is not None else "",
        "has_app_credentials": bool(saved and saved.username and saved.password),
        "has_env_credentials": bool(get_settings().username and get_settings().password),
        "session_cached": session_exists,
        "session_saved_at": session_saved_at,
        "last_login_attempted_at": (
            saved.last_login_attempted_at.isoformat()
            if saved is not None and saved.last_login_attempted_at is not None
            else None
        ),
        "last_login_succeeded_at": (
            saved.last_login_succeeded_at.isoformat()
            if saved is not None and saved.last_login_succeeded_at is not None
            else None
        ),
        "last_login_error": saved.last_login_error if saved is not None else "",
    }
