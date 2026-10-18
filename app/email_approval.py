from __future__ import annotations

from datetime import datetime, timedelta
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html import escape
import imaplib
import re
import secrets
from urllib.parse import urlencode

from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.db import get_session, init_db
from app.job_sources import MANUAL_SELECTION_RULE_NAME
from app.models import (
    ApprovalStatus,
    BookingApproval,
    BookingEvent,
    BookingJob,
    BookingStatus,
)
from app.notifications import parse_recipient_csv, send_email_message
from app.time_utils import local_now
from app.weekly_schedule import skip_weekly_schedule_occurrence


NEGATIVE_COMMANDS = {"no", "cancel", "reject", "skip", "stop"}
AFFIRMATIVE_COMMANDS = {"yes", "approve", "book", "go"}
TOKEN_PATTERN = re.compile(r"\[BookGym:([A-Z0-9]{8})\]", re.IGNORECASE)
RUNNABLE_STATUSES = {BookingStatus.PENDING, BookingStatus.NOT_YET_OPEN}


def _approval_request_due_at(job: BookingJob, settings: Settings) -> datetime:
    """Return the local send time on the same day as the booking attempt."""
    return datetime.combine(job.expected_run_at.date(), settings.email_approval_send_time)


def _normalized_command(value: str) -> str:
    return value.strip().casefold().rstrip(".!?")


def approval_command_from_message(message: Message) -> str | None:
    text = _message_text(message)
    for line in text.splitlines():
        candidate = _normalized_command(line)
        if not candidate:
            continue
        if candidate in NEGATIVE_COMMANDS or candidate in AFFIRMATIVE_COMMANDS:
            return candidate
        return None
    return None


def _message_text(message: Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() != "text/plain":
                continue
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            try:
                return part.get_content()
            except (LookupError, UnicodeDecodeError):
                payload = part.get_payload(decode=True) or b""
                return payload.decode("utf-8", errors="replace")
        return ""
    try:
        return message.get_content()
    except (LookupError, UnicodeDecodeError):
        payload = message.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def _token_from_subject(subject: str) -> str | None:
    match = TOKEN_PATTERN.search(subject)
    return match.group(1).upper() if match else None


def _approval_recipients(job: BookingJob, settings: Settings) -> list[str]:
    if job.notification_email_override is not None:
        return parse_recipient_csv(job.notification_email_override)
    return settings.notification_email_recipients


def _allowed_senders(job: BookingJob, settings: Settings) -> set[str]:
    configured = settings.approval_allowed_sender_addresses
    recipients = _approval_recipients(job, settings)
    return {address.casefold() for address in (configured or recipients)}


def _new_token(session: Session) -> str:
    while True:
        token = secrets.token_hex(4).upper()
        existing = session.exec(
            select(BookingApproval).where(BookingApproval.token == token)
        ).first()
        if existing is None:
            return token


def _ensure_approval_records(
    session: Session,
    *,
    now: datetime,
    settings: Settings,
) -> list[BookingApproval]:
    jobs = list(
        session.exec(
            select(BookingJob).where(
                BookingJob.rule_name == MANUAL_SELECTION_RULE_NAME,
                BookingJob.status.in_(list(RUNNABLE_STATUSES)),
                BookingJob.expected_run_at > now,
            )
        ).all()
    )
    approvals = []
    for job in jobs:
        approval = session.exec(
            select(BookingApproval).where(BookingApproval.job_id == job.id)
        ).first()
        request_due_at = _approval_request_due_at(job, settings)
        response_cutoff_at = job.expected_run_at - timedelta(
            minutes=settings.email_approval_cutoff_minutes
        )
        if approval is None:
            approval = BookingApproval(
                user_id=job.user_id,
                job_id=job.id,
                token=_new_token(session),
                request_due_at=request_due_at,
                response_cutoff_at=response_cutoff_at,
                created_at=now,
                updated_at=now,
            )
        elif approval.sent_at is None:
            approval.user_id = job.user_id
            approval.request_due_at = request_due_at
            approval.response_cutoff_at = response_cutoff_at
            approval.updated_at = now
        session.add(approval)
        approvals.append(approval)
    session.flush()
    return approvals


def next_approval_request_due_at(now: datetime | None = None) -> datetime | None:
    settings = get_settings()
    if not settings.email_approval_enabled:
        return None
    now = now or local_now()
    init_db()
    with get_session() as session:
        jobs = list(
            session.exec(
                select(BookingJob).where(
                    BookingJob.rule_name == MANUAL_SELECTION_RULE_NAME,
                    BookingJob.status.in_(list(RUNNABLE_STATUSES)),
                    BookingJob.expected_run_at > now,
                ).order_by(BookingJob.expected_run_at)
            ).all()
        )
        for job in jobs:
            approval = session.exec(
                select(BookingApproval).where(BookingApproval.job_id == job.id)
            ).first()
            if approval is not None and approval.sent_at is not None:
                continue
            return _approval_request_due_at(job, settings)
    return None


def _format_class_time(job: BookingJob) -> str:
    class_time = job.window_start_time.strftime("%-I:%M %p") if job.window_start_time else "time TBD"
    return f"{job.class_date.strftime('%A, %B %-d')} at {class_time}"


def _approval_message(job: BookingJob, approval: BookingApproval, settings: Settings) -> EmailMessage:
    recipients = _approval_recipients(job, settings)
    inbox = settings.resolved_approval_inbox_email
    subject = f"[BookGym:{approval.token}] Book {job.target_match_text}?"
    when_label = _format_class_time(job)
    cutoff_label = approval.response_cutoff_at.strftime("%A at %-I:%M %p")
    skip_url = "mailto:" + inbox + "?" + urlencode({"subject": subject, "body": "SKIP"})
    approve_url = "mailto:" + inbox + "?" + urlencode({"subject": subject, "body": "BOOK"})

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from_email
    message["To"] = ", ".join(recipients)
    message["Reply-To"] = inbox
    message["X-BookGym-Approval-Request"] = approval.token
    message.set_content(
        "\n".join(
            [
                f"BookGym plans to book {job.target_match_text} on {when_label}.",
                "",
                "No response is needed to keep the booking scheduled.",
                f"To cancel this occurrence, reply before {cutoff_label} with SKIP on the first line.",
                "Accepted cancel commands: NO, CANCEL, REJECT, SKIP, or STOP.",
                "Accepted affirmative commands: YES, APPROVE, BOOK, or GO.",
                "",
                f"Skip: {skip_url}",
                f"Keep: {approve_url}",
            ]
        )
    )
    message.add_alternative(
        f"""
<html><body style="margin:0;padding:24px;background:#f6f2e8;font-family:Arial,sans-serif;color:#20312b;">
  <div style="max-width:620px;margin:0 auto;background:#fffaf2;border:1px solid rgba(32,49,43,.12);border-radius:24px;padding:28px;">
    <div style="color:#1f6a52;font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;">BookGym</div>
    <h1 style="margin:14px 0 8px;font-size:28px;">Book {escape(job.target_match_text)}?</h1>
    <p style="font-size:16px;line-height:1.55;">{escape(when_label)}</p>
    <p style="line-height:1.55;">No response is needed to keep this booking scheduled. To cancel only this occurrence, reply before <strong>{escape(cutoff_label)}</strong>.</p>
    <div style="margin:24px 0;">
      <a href="{escape(approve_url, quote=True)}" style="display:inline-block;margin:0 10px 10px 0;padding:12px 18px;border-radius:999px;background:#1f6a52;color:white;text-decoration:none;font-weight:700;">Book it</a>
      <a href="{escape(skip_url, quote=True)}" style="display:inline-block;padding:12px 18px;border-radius:999px;background:#ffe6df;color:#8f4717;text-decoration:none;font-weight:700;">Skip this week</a>
    </div>
    <p style="font-size:13px;color:#6f7d76;">You can also reply with NO, CANCEL, REJECT, SKIP, or STOP as the first line.</p>
  </div>
</body></html>
""".strip(),
        subtype="html",
    )
    return message


def send_due_approval_requests(now: datetime | None = None) -> dict[str, object]:
    settings = get_settings()
    if not settings.email_approval_enabled:
        return {"action": "disabled", "sent": 0}
    now = now or local_now()
    init_db()
    sent = 0
    errors = []
    with get_session() as session:
        approvals = _ensure_approval_records(session, now=now, settings=settings)
        session.commit()
        for approval in approvals:
            if approval.sent_at is not None:
                continue
            if now >= approval.response_cutoff_at:
                approval.status = ApprovalStatus.EXPIRED
                approval.last_error = "Approval request was not sent before the veto cutoff"
                approval.updated_at = now
                session.add(approval)
                continue
            if now < approval.request_due_at:
                continue
            job = session.get(BookingJob, approval.job_id)
            if job is None or job.status not in RUNNABLE_STATUSES:
                continue
            recipients = _approval_recipients(job, settings)
            if not recipients or not settings.resolved_approval_inbox_email:
                approval.last_error = "Approval email recipients or inbox address are missing"
                approval.updated_at = now
                session.add(approval)
                errors.append(approval.last_error)
                continue
            try:
                send_email_message(_approval_message(job, approval, settings))
            except Exception as exc:
                approval.last_error = str(exc)
                approval.updated_at = now
                session.add(approval)
                errors.append(str(exc))
                continue
            approval.sent_at = now
            approval.last_error = ""
            approval.updated_at = now
            session.add(approval)
            sent += 1
        session.commit()
    return {"action": "checked", "sent": sent, "errors": errors}


def _message_received_at(message: Message, settings: Settings, fallback: datetime) -> datetime:
    raw_date = message.get("Date")
    if not raw_date:
        return fallback
    try:
        parsed = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError):
        return fallback
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(settings.timezone).replace(tzinfo=None)


def _cancel_job_for_rejection(job_id: int, responder: str, command: str, now: datetime) -> bool:
    with get_session() as session:
        job = session.get(BookingJob, job_id)
        if job is None or job.status not in RUNNABLE_STATUSES:
            return False
        discovered_class_id = job.discovered_class_id

    recurring_skipped = False
    if discovered_class_id is not None:
        try:
            skip_weekly_schedule_occurrence(discovered_class_id)
            recurring_skipped = True
        except ValueError:
            recurring_skipped = False

    with get_session() as session:
        job = session.get(BookingJob, job_id)
        if job is None:
            return False
        if not recurring_skipped and job.status in RUNNABLE_STATUSES:
            job.status = BookingStatus.CANCELED
            job.capture_network = False
            job.last_message = f"Canceled by email reply from {responder}: {command.upper()}"
            job.updated_at = now
            session.add(job)
        if job.status != BookingStatus.CANCELED:
            return False
        session.add(
            BookingEvent(
                job_id=job.id,
                discovered_class_id=job.discovered_class_id,
                event_type="email_approval_rejected",
                status=BookingStatus.CANCELED,
                message=f"Email veto from {responder}: {command.upper()}",
                observed_at=now,
            )
        )
        session.commit()
    return True


def _imap_is_configured(settings: Settings) -> bool:
    return bool(
        settings.imap_host
        and settings.imap_port
        and settings.resolved_imap_username
        and settings.resolved_imap_password
    )


def _fetch_recent_messages(settings: Settings, since: datetime) -> list[tuple[int, EmailMessage]]:
    messages = []
    with imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=15) as client:
        client.login(settings.resolved_imap_username, settings.resolved_imap_password)
        status, _ = client.select(settings.imap_mailbox, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not select IMAP mailbox {settings.imap_mailbox!r}")
        status, data = client.uid("search", None, "SINCE", since.strftime("%d-%b-%Y"))
        if status != "OK":
            raise RuntimeError("IMAP search failed")
        for raw_uid in (data[0] or b"").split():
            uid = int(raw_uid)
            status, fetched = client.uid("fetch", raw_uid, "(RFC822)")
            if status != "OK" or not fetched:
                continue
            payload = next(
                (part[1] for part in fetched if isinstance(part, tuple) and len(part) > 1),
                None,
            )
            if not payload:
                continue
            parsed = BytesParser(policy=policy.default).parsebytes(payload)
            messages.append((uid, parsed))
    return messages


def process_approval_replies(now: datetime | None = None) -> dict[str, object]:
    settings = get_settings()
    if not settings.email_approval_enabled:
        return {"action": "disabled", "processed": 0, "rejected": 0}
    if not _imap_is_configured(settings):
        return {
            "action": "not_configured",
            "processed": 0,
            "rejected": 0,
            "error": "IMAP settings are incomplete",
        }
    now = now or local_now()
    init_db()
    with get_session() as session:
        pending_approvals = list(
            session.exec(
                select(BookingApproval).where(
                    BookingApproval.sent_at.is_not(None),
                    BookingApproval.status == ApprovalStatus.PENDING,
                )
            ).all()
        )
        approvals = []
        for approval in pending_approvals:
            job = session.get(BookingJob, approval.job_id)
            if job is None or job.status not in RUNNABLE_STATUSES:
                approval.status = ApprovalStatus.EXPIRED
                approval.last_error = "Approval window closed because the booking is no longer runnable"
                approval.updated_at = now
                session.add(approval)
                continue
            if now > job.expected_run_at:
                approval.status = ApprovalStatus.EXPIRED
                approval.last_error = "Approval window closed when the booking time passed"
                approval.updated_at = now
                session.add(approval)
                continue
            approvals.append(approval)
        session.commit()
        if not approvals:
            return {
                "action": "checked",
                "active_windows": 0,
                "processed": 0,
                "rejected": 0,
            }
        since = min(approval.sent_at for approval in approvals if approval.sent_at) - timedelta(days=1)

    try:
        messages = _fetch_recent_messages(settings, since)
    except Exception as exc:
        print(f"Email approval inbox poll failed: {exc}")
        return {"action": "error", "processed": 0, "rejected": 0, "error": str(exc)}

    processed = 0
    rejected = 0
    approval_by_token = {approval.token.upper(): approval.id for approval in approvals}
    for uid, message in messages:
        token = _token_from_subject(str(message.get("Subject") or ""))
        approval_id = approval_by_token.get(token or "")
        if approval_id is None:
            continue
        should_cancel = False
        cancel_details: tuple[int, str, str] | None = None
        with get_session() as session:
            approval = session.get(BookingApproval, approval_id)
            if (
                approval is None
                or approval.status != ApprovalStatus.PENDING
                or uid <= approval.last_message_uid
            ):
                continue
            approval.last_message_uid = uid
            approval.updated_at = now
            if str(message.get("X-BookGym-Approval-Request") or "").upper() == approval.token.upper():
                session.add(approval)
                session.commit()
                continue
            job = session.get(BookingJob, approval.job_id)
            if job is None or job.status not in RUNNABLE_STATUSES:
                approval.status = ApprovalStatus.EXPIRED
                approval.last_error = "Approval window closed because the booking is no longer runnable"
                session.add(approval)
                session.commit()
                continue
            sender = parseaddr(str(message.get("From") or ""))[1].casefold()
            command = approval_command_from_message(message)
            received_at = _message_received_at(message, settings, now)
            if sender not in _allowed_senders(job, settings):
                approval.last_error = f"Ignored reply from unauthorized sender {sender or '(missing)'}"
            elif command is None:
                approval.last_error = "Ignored reply without an exact approval command on the first line"
            elif received_at > approval.response_cutoff_at:
                approval.last_error = "Ignored reply received after the veto cutoff"
            else:
                approval.response_at = received_at
                approval.responder_email = sender
                approval.response_command = command
                approval.response_message_id = str(message.get("Message-ID") or "")
                approval.last_error = ""
                processed += 1
                if command in NEGATIVE_COMMANDS:
                    approval.status = ApprovalStatus.REJECTED
                    should_cancel = True
                    cancel_details = (job.id, sender, command)
                else:
                    approval.status = ApprovalStatus.APPROVED
            session.add(approval)
            session.commit()
        if should_cancel and cancel_details is not None:
            if _cancel_job_for_rejection(*cancel_details, now=now):
                rejected += 1

    with get_session() as session:
        expired = list(
            session.exec(
                select(BookingApproval).where(
                    BookingApproval.status == ApprovalStatus.PENDING,
                    BookingApproval.response_cutoff_at < now,
                )
            ).all()
        )
        for approval in expired:
            approval.status = ApprovalStatus.EXPIRED
            approval.updated_at = now
            session.add(approval)
        session.commit()
    return {
        "action": "checked",
        "active_windows": len(approvals),
        "processed": processed,
        "rejected": rejected,
    }


def serialize_booking_approval(approval: BookingApproval) -> dict[str, object]:
    return {
        "id": approval.id,
        "job_id": approval.job_id,
        "token": approval.token,
        "status": approval.status.value,
        "request_due_at": approval.request_due_at.isoformat(),
        "response_cutoff_at": approval.response_cutoff_at.isoformat(),
        "sent_at": approval.sent_at.isoformat() if approval.sent_at else None,
        "response_at": approval.response_at.isoformat() if approval.response_at else None,
        "responder_email": approval.responder_email,
        "response_command": approval.response_command,
        "last_error": approval.last_error,
    }
