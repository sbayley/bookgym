from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import timedelta
from email.message import EmailMessage
import hashlib
import hmac
import secrets

from sqlmodel import select

from app.config import get_settings
from app.db import get_session, init_db
from app.models import (
    AppAuditEvent,
    AppDelegationGrant,
    AppLoginCode,
    AppSession,
    AppUser,
    UserRole,
)
from app.notifications import send_email_message
from app.time_utils import local_now


SESSION_COOKIE_NAME = "bookgym_session"
LOGIN_CODE_MINUTES = 10
LOGIN_CODE_RESEND_SECONDS = 60
MAX_LOGIN_ATTEMPTS = 5


@dataclass(frozen=True)
class AuthContext:
    actor: AppUser
    subject: AppUser
    session_id: int

    @property
    def is_admin(self) -> bool:
        return self.actor.role == UserRole.ADMIN

    @property
    def is_delegating(self) -> bool:
        return self.actor.id != self.subject.id


_auth_context: ContextVar[AuthContext | None] = ContextVar(
    "bookgym_auth_context",
    default=None,
)


def set_auth_context(context: AuthContext | None) -> Token:
    return _auth_context.set(context)


def reset_auth_context(token: Token) -> None:
    _auth_context.reset(token)


def current_auth_context() -> AuthContext | None:
    return _auth_context.get()


def current_subject_user_id() -> int | None:
    context = current_auth_context()
    return context.subject.id if context is not None else None


def current_actor_user_id() -> int | None:
    context = current_auth_context()
    return context.actor.id if context is not None else None


def validate_auth_configuration() -> str:
    settings = get_settings()
    public_origin = settings.app_origin.strip().rstrip("/")
    if public_origin:
        if not public_origin.startswith("https://"):
            return "BOOKGYM_APP_ORIGIN must use HTTPS."
        if not settings.app_auth_enabled:
            return "BookGym authentication must be enabled for a public portal."
        if not settings.app_secure_cookie:
            return "BOOKGYM_APP_SECURE_COOKIE must be true for a public portal."
    if not settings.app_auth_enabled:
        return ""
    if len(settings.app_auth_secret) < 32:
        return "BOOKGYM_APP_AUTH_SECRET must contain at least 32 characters."
    if not settings.app_member_email.strip():
        return "BOOKGYM_APP_MEMBER_EMAIL is required when app authentication is enabled."
    if not settings.app_admin_email.strip():
        return "BOOKGYM_APP_ADMIN_EMAIL is required when app authentication is enabled."
    if settings.app_member_email.strip().casefold() == settings.app_admin_email.strip().casefold():
        return "Member and admin email addresses must be different."
    return ""


def _digest(value: str) -> str:
    secret = get_settings().app_auth_secret.encode("utf-8")
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _code_digest(user_id: int, code: str) -> str:
    return _digest(f"login-code:{user_id}:{code}")


def _token_digest(token: str) -> str:
    return _digest(f"session:{token}")


def _generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _manageable_users_for_actor(session, actor: AppUser) -> list[AppUser]:
    if actor.role == UserRole.ADMIN:
        return list(
            session.exec(
                select(AppUser)
                .where(AppUser.is_active == True)  # noqa: E712
                .order_by(AppUser.name)
            ).all()
        )
    if actor.role != UserRole.DELEGATE:
        return []

    subject_ids = list(
        session.exec(
            select(AppDelegationGrant.subject_user_id)
            .where(AppDelegationGrant.actor_user_id == actor.id)
            .order_by(AppDelegationGrant.id)
        ).all()
    )
    if not subject_ids:
        return []
    subjects = list(
        session.exec(
            select(AppUser).where(
                AppUser.id.in_(subject_ids),
                AppUser.is_active == True,  # noqa: E712
            )
        ).all()
    )
    subjects_by_id = {int(subject.id): subject for subject in subjects}
    return [subjects_by_id[subject_id] for subject_id in subject_ids if subject_id in subjects_by_id]


def upsert_app_user(name: str, email: str, role: UserRole) -> AppUser:
    init_db()
    normalized_email = email.strip().casefold()
    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("A display name is required")
    if not normalized_email or "@" not in normalized_email:
        raise ValueError("A valid email address is required")

    now = local_now()
    with get_session() as session:
        user = session.exec(select(AppUser).where(AppUser.email == normalized_email)).first()
        if user is None:
            user = AppUser(
                name=normalized_name,
                email=normalized_email,
                role=role,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        else:
            user.name = normalized_name
            user.role = role
            user.is_active = True
            user.updated_at = now
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def replace_user_delegations(actor_user_id: int, subject_emails: list[str]) -> list[AppUser]:
    init_db()
    normalized_emails = list(dict.fromkeys(email.strip().casefold() for email in subject_emails if email.strip()))
    if not normalized_emails:
        raise ValueError("At least one managed user email is required")

    with get_session() as session:
        actor = session.get(AppUser, actor_user_id)
        if actor is None or not actor.is_active:
            raise ValueError("Delegate user not found")

        subjects = []
        for email in normalized_emails:
            subject = session.exec(
                select(AppUser).where(
                    AppUser.email == email,
                    AppUser.is_active == True,  # noqa: E712
                )
            ).first()
            if subject is None:
                raise ValueError(f"Managed user {email} was not found")
            if subject.id == actor.id:
                raise ValueError("A user cannot delegate to themselves")
            subjects.append(subject)

        for existing in session.exec(
            select(AppDelegationGrant).where(AppDelegationGrant.actor_user_id == actor_user_id)
        ).all():
            session.delete(existing)
        session.flush()

        for subject in subjects:
            session.add(
                AppDelegationGrant(
                    actor_user_id=actor_user_id,
                    subject_user_id=int(subject.id),
                    created_at=local_now(),
                )
            )
        session.commit()
        for subject in subjects:
            session.refresh(subject)
        return subjects


def _send_login_code(user: AppUser, code: str) -> None:
    message = EmailMessage()
    message["Subject"] = f"{code} is your BookGym sign-in code"
    message["From"] = get_settings().smtp_from_email
    message["To"] = user.email
    message.set_content(
        f"Hi {user.name},\n\n"
        f"Your BookGym sign-in code is {code}.\n\n"
        f"It expires in {LOGIN_CODE_MINUTES} minutes. If you did not request it, you can ignore this email."
    )
    message.add_alternative(
        "<html><body style=\"margin:0;padding:32px;background:#f6f2e8;font-family:Arial,sans-serif;color:#20312b\">"
        "<div style=\"max-width:520px;margin:auto;background:#fffaf2;border:1px solid #dfe5df;border-radius:24px;padding:30px\">"
        "<div style=\"color:#1f6a52;font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase\">BookGym</div>"
        f"<h1 style=\"font-size:26px;margin:16px 0 8px\">Hi {user.name}</h1>"
        "<p style=\"line-height:1.6\">Use this code to sign in:</p>"
        f"<div style=\"font-size:38px;font-weight:800;letter-spacing:.18em;color:#1f6a52;margin:24px 0\">{code}</div>"
        f"<p style=\"color:#6f7d76;line-height:1.6\">The code expires in {LOGIN_CODE_MINUTES} minutes.</p>"
        "</div></body></html>",
        subtype="html",
    )
    send_email_message(message)


def request_login_code(email: str) -> None:
    init_db()
    normalized_email = email.strip().casefold()
    now = local_now()
    with get_session() as session:
        user = session.exec(
            select(AppUser).where(
                AppUser.email == normalized_email,
                AppUser.is_active == True,  # noqa: E712
            )
        ).first()
        if user is None:
            return

        latest = session.exec(
            select(AppLoginCode)
            .where(
                AppLoginCode.user_id == user.id,
                AppLoginCode.consumed_at.is_(None),
            )
            .order_by(AppLoginCode.created_at.desc())
        ).first()
        if latest is not None and (now - latest.created_at).total_seconds() < LOGIN_CODE_RESEND_SECONDS:
            return

        for previous in session.exec(
            select(AppLoginCode).where(
                AppLoginCode.user_id == user.id,
                AppLoginCode.consumed_at.is_(None),
            )
        ).all():
            previous.consumed_at = now
            session.add(previous)

        code = _generate_code()
        login_code = AppLoginCode(
            user_id=int(user.id),
            code_hash=_code_digest(int(user.id), code),
            expires_at=now + timedelta(minutes=LOGIN_CODE_MINUTES),
            created_at=now,
        )
        session.add(login_code)
        session.commit()
        session.refresh(user)

    _send_login_code(user, code)


def verify_login_code(email: str, code: str) -> tuple[str, AuthContext] | None:
    init_db()
    normalized_email = email.strip().casefold()
    normalized_code = "".join(character for character in code if character.isdigit())
    now = local_now()
    with get_session() as session:
        user = session.exec(
            select(AppUser).where(
                AppUser.email == normalized_email,
                AppUser.is_active == True,  # noqa: E712
            )
        ).first()
        if user is None or len(normalized_code) != 6:
            return None

        login_code = session.exec(
            select(AppLoginCode)
            .where(
                AppLoginCode.user_id == user.id,
                AppLoginCode.consumed_at.is_(None),
                AppLoginCode.expires_at >= now,
            )
            .order_by(AppLoginCode.created_at.desc())
        ).first()
        if login_code is None:
            return None
        if not hmac.compare_digest(login_code.code_hash, _code_digest(int(user.id), normalized_code)):
            login_code.failed_attempts += 1
            if login_code.failed_attempts >= MAX_LOGIN_ATTEMPTS:
                login_code.consumed_at = now
            session.add(login_code)
            session.commit()
            return None

        login_code.consumed_at = now
        active_user = user
        if user.role == UserRole.ADMIN:
            member = session.exec(
                select(AppUser)
                .where(
                    AppUser.role == UserRole.MEMBER,
                    AppUser.is_active == True,  # noqa: E712
                )
                .order_by(AppUser.name)
            ).first()
            if member is not None:
                active_user = member
        elif user.role == UserRole.DELEGATE:
            manageable_users = _manageable_users_for_actor(session, user)
            if manageable_users:
                active_user = manageable_users[0]

        raw_token = secrets.token_urlsafe(32)
        app_session = AppSession(
            user_id=int(user.id),
            active_user_id=int(active_user.id),
            token_hash=_token_digest(raw_token),
            expires_at=now + timedelta(days=get_settings().app_session_days),
            created_at=now,
            last_seen_at=now,
        )
        session.add(login_code)
        session.add(app_session)
        session.flush()
        session.add(
            AppAuditEvent(
                actor_user_id=int(user.id),
                subject_user_id=int(active_user.id),
                action="signed_in",
                resource="session",
                observed_at=now,
            )
        )
        session.commit()
        session.refresh(user)
        if active_user.id != user.id:
            session.refresh(active_user)
        session.refresh(app_session)
        return raw_token, AuthContext(
            actor=user,
            subject=active_user,
            session_id=int(app_session.id),
        )


def resolve_session(raw_token: str | None) -> AuthContext | None:
    if not raw_token:
        return None
    now = local_now()
    with get_session() as session:
        app_session = session.exec(
            select(AppSession).where(
                AppSession.token_hash == _token_digest(raw_token),
                AppSession.revoked_at.is_(None),
                AppSession.expires_at >= now,
            )
        ).first()
        if app_session is None:
            return None
        actor = session.get(AppUser, app_session.user_id)
        subject = session.get(AppUser, app_session.active_user_id)
        if actor is None or subject is None or not actor.is_active or not subject.is_active:
            return None
        app_session.last_seen_at = now
        session.add(app_session)
        session.commit()
        session.refresh(actor)
        if subject.id != actor.id:
            session.refresh(subject)
        return AuthContext(actor=actor, subject=subject, session_id=int(app_session.id))


def revoke_session(session_id: int) -> None:
    with get_session() as session:
        app_session = session.get(AppSession, session_id)
        if app_session is not None:
            app_session.revoked_at = local_now()
            session.add(app_session)
            session.commit()


def set_managed_user(session_id: int, actor_user_id: int, subject_user_id: int) -> AuthContext:
    now = local_now()
    with get_session() as session:
        actor = session.get(AppUser, actor_user_id)
        subject = session.get(AppUser, subject_user_id)
        app_session = session.get(AppSession, session_id)
        if (
            actor is None
            or actor.role not in {UserRole.ADMIN, UserRole.DELEGATE}
            or not actor.is_active
            or subject is None
            or not subject.is_active
            or app_session is None
            or app_session.user_id != actor_user_id
        ):
            raise ValueError("Managed user not found")
        if actor.role == UserRole.DELEGATE:
            grant = session.exec(
                select(AppDelegationGrant).where(
                    AppDelegationGrant.actor_user_id == actor_user_id,
                    AppDelegationGrant.subject_user_id == subject_user_id,
                )
            ).first()
            if grant is None:
                raise ValueError("Managed user not found")
        app_session.active_user_id = subject_user_id
        app_session.last_seen_at = now
        session.add(app_session)
        session.add(
            AppAuditEvent(
                actor_user_id=actor_user_id,
                subject_user_id=subject_user_id,
                action="managed_user_selected",
                resource="session",
                observed_at=now,
            )
        )
        session.commit()
        session.refresh(actor)
        session.refresh(subject)
        return AuthContext(actor=actor, subject=subject, session_id=session_id)


def serialize_auth_context(context: AuthContext) -> dict[str, object]:
    with get_session() as session:
        members = []
        managed_users = []
        if context.is_admin:
            members = list(
                session.exec(
                    select(AppUser)
                    .where(
                        AppUser.role == UserRole.MEMBER,
                        AppUser.is_active == True,  # noqa: E712
                    )
                    .order_by(AppUser.name)
                ).all()
            )
        if context.actor.role in {UserRole.ADMIN, UserRole.DELEGATE}:
            actor = session.get(AppUser, context.actor.id)
            if actor is not None:
                managed_users = _manageable_users_for_actor(session, actor)
    return {
        "actor": {
            "id": context.actor.id,
            "name": context.actor.name,
            "email": context.actor.email,
            "role": context.actor.role.value,
        },
        "active_user": {
            "id": context.subject.id,
            "name": context.subject.name,
            "email": context.subject.email,
            "role": context.subject.role.value,
        },
        "is_admin": context.is_admin,
        "can_manage_users": bool(managed_users),
        "is_delegating": context.is_delegating,
        "members": [
            {"id": member.id, "name": member.name, "email": member.email}
            for member in members
        ],
        "managed_users": [
            {"id": user.id, "name": user.name, "email": user.email, "role": user.role.value}
            for user in managed_users
        ],
    }


def record_action(context: AuthContext, action: str, resource: str, detail: str = "") -> None:
    with get_session() as session:
        session.add(
            AppAuditEvent(
                actor_user_id=int(context.actor.id),
                subject_user_id=int(context.subject.id),
                action=action,
                resource=resource,
                detail=detail[:1000],
                observed_at=local_now(),
            )
        )
        session.commit()
