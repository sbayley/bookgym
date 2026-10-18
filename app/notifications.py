from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, time
from html import escape
import json
import smtplib
import ssl
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import Settings, get_settings


@dataclass
class NotificationContext:
    class_name: str = ""
    status: str = ""
    class_date: date | None = None
    class_time: time | None = None
    seat_label: str = ""
    is_dry_run: bool = False
    screenshot_path: str = ""


def parse_recipient_csv(value: str | None) -> list[str]:
    if value is None:
        return []
    values = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if chunk:
            values.append(chunk)
    return values


def format_recipient_csv(recipients: list[str]) -> str:
    normalized = []
    seen = set()
    for recipient in recipients:
        trimmed = recipient.strip()
        if not trimmed:
            continue
        key = trimmed.casefold()
        if key in seen:
            continue
        normalized.append(trimmed)
        seen.add(key)
    return ",".join(normalized)


def _notification_destinations(
    *,
    email_recipients: list[str],
    sms_recipients: list[str],
) -> list[str]:
    destinations = []
    for recipient in email_recipients:
        destinations.append(f"email:{recipient}")
    for recipient in sms_recipients:
        destinations.append(f"sms:{recipient}")
    return destinations


def _smtp_is_configured(settings: Settings) -> bool:
    return bool(settings.smtp_host and settings.smtp_from_email and settings.smtp_port)


def _smtp_has_partial_credentials(settings: Settings) -> bool:
    return bool(settings.smtp_username) != bool(settings.smtp_password)


def _twilio_is_configured(settings: Settings) -> bool:
    return bool(
        settings.twilio_account_sid
        and settings.twilio_auth_token
        and settings.twilio_from_number
    )


def _format_sms_body(subject: str, body: str) -> str:
    message = f"{subject}\n{body}".strip()
    return message[:1600]


def _twilio_auth_header(settings: Settings) -> str:
    raw = f"{settings.twilio_account_sid}:{settings.twilio_auth_token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _send_twilio_sms(settings: Settings, recipient: str, message: str) -> None:
    payload = urlencode(
        {
            "To": recipient,
            "From": settings.twilio_from_number,
            "Body": message,
        }
    ).encode("utf-8")
    request = Request(
        url=(
            "https://api.twilio.com/2010-04-01/Accounts/"
            f"{settings.twilio_account_sid}/Messages.json"
        ),
        data=payload,
        headers={
            "Authorization": _twilio_auth_header(settings),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            response.read()
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
            detail = parsed.get("message") or body
        except json.JSONDecodeError:
            detail = body
        raise RuntimeError(f"Twilio SMS failed for {recipient}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Twilio SMS failed for {recipient}: {exc.reason}") from exc


def _format_date(value: date | None) -> str:
    if value is None:
        return ""
    return value.strftime("%a %b %d, %Y")


def _format_time(value: time | None) -> str:
    if value is None:
        return ""
    return value.strftime("%I:%M %p").lstrip("0")


def _notification_title(context: NotificationContext) -> str:
    titles = {
        "booked": "Class booked",
        "waitlisted": "Joined waitlist",
        "failed": "Booking failed",
        "dry_run": "Dry run complete",
        "not_yet_open": "Not open yet",
        "needs_confirmation": "Confirmation needed",
        "pending": "Booking pending",
        "canceled": "Booking canceled",
    }
    return titles.get(context.status, "Booking update")


def _build_email_text(subject: str, body: str, context: NotificationContext | None) -> str:
    if context is None:
        return body

    lines = [_notification_title(context), ""]
    if context.class_name:
        lines.append(f"Class: {context.class_name}")
    if context.class_date:
        lines.append(f"Date: {_format_date(context.class_date)}")
    if context.class_time:
        lines.append(f"Time: {_format_time(context.class_time)}")
    if context.seat_label:
        lines.append(f"Seat: {context.seat_label}")
    lines.extend(["", "Details:", body])
    return "\n".join(lines)


def _status_accent_colors(status: str) -> tuple[str, str]:
    palette = {
        "booked": ("#dceee7", "#144d3b"),
        "waitlisted": ("#e7f1ec", "#1f6a52"),
        "dry_run": ("#f3ede4", "#5f5648"),
        "failed": ("#ffe6df", "#8f4717"),
        "not_yet_open": ("#fff2df", "#8a5a00"),
        "needs_confirmation": ("#fff2df", "#8a5a00"),
    }
    return palette.get(status, ("#eef2f1", "#20312b"))


def _build_email_html(subject: str, body: str, context: NotificationContext | None) -> str:
    if context is None:
        return (
            "<html><body style=\"font-family:Arial,sans-serif;color:#20312b;\">"
            f"<h2>{escape(subject)}</h2>"
            f"<p style=\"white-space:pre-wrap;line-height:1.5;\">{escape(body)}</p>"
            "</body></html>"
        )

    accent_background, accent_text = _status_accent_colors(context.status)
    title = _notification_title(context)
    rows = []
    if context.class_name:
        rows.append(("Class", context.class_name))
    if context.class_date:
        rows.append(("Date", _format_date(context.class_date)))
    if context.class_time:
        rows.append(("Time", _format_time(context.class_time)))
    if context.seat_label:
        rows.append(("Seat", context.seat_label))
    row_html = "".join(
        (
            "<tr>"
            f"<td style=\"padding:8px 0;color:#6f7d76;font-size:13px;vertical-align:top;\">{escape(label)}</td>"
            f"<td style=\"padding:8px 0 8px 18px;color:#20312b;font-size:14px;font-weight:600;\">{escape(value)}</td>"
            "</tr>"
        )
        for label, value in rows
    )
    return f"""
<html>
  <body style="margin:0;padding:24px;background:#f6f2e8;font-family:Arial,sans-serif;color:#20312b;">
    <div style="max-width:620px;margin:0 auto;background:#fffaf2;border:1px solid rgba(32,49,43,0.12);border-radius:24px;padding:28px;box-shadow:0 18px 42px rgba(34,52,45,0.08);">
      <div style="color:#1f6a52;font-size:12px;font-weight:700;letter-spacing:0.14em;text-transform:uppercase;">BookGym</div>
      <div style="width:72px;height:6px;margin:12px 0 14px;border-radius:999px;background:{accent_background};"></div>
      <h1 style="margin:0 0 10px;font-size:28px;line-height:1.08;color:{accent_text};">{escape(title)}</h1>
      <table style="width:100%;border-collapse:collapse;margin-top:18px;">{row_html}</table>
      <div style="margin-top:22px;padding:18px;border-radius:18px;background:#ffffff;border:1px solid rgba(32,49,43,0.10);">
        <div style="font-size:12px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#6f7d76;margin-bottom:10px;">Details</div>
        <div style="white-space:pre-wrap;line-height:1.6;font-size:14px;">{escape(body)}</div>
      </div>
    </div>
  </body>
</html>
""".strip()


def _build_email_message(
    settings: Settings,
    recipients: list[str],
    subject: str,
    body: str,
    context: NotificationContext | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from_email
    message["To"] = ", ".join(recipients)
    message.set_content(_build_email_text(subject=subject, body=body, context=context))
    message.add_alternative(_build_email_html(subject=subject, body=body, context=context), subtype="html")
    return message


def _send_email(
    settings: Settings,
    recipients: list[str],
    subject: str,
    body: str,
    context: NotificationContext | None = None,
) -> None:
    message = _build_email_message(
        settings=settings,
        recipients=recipients,
        subject=subject,
        body=body,
        context=context,
    )
    _send_email_message(settings, message)


def _send_email_message(settings: Settings, message: EmailMessage) -> None:
    timeout_seconds = 15
    if settings.smtp_use_ssl:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout_seconds) as smtp:
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
        return

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout_seconds) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)


def send_email_message(message: EmailMessage) -> None:
    """Send a prebuilt email and surface delivery errors to the caller."""
    settings = get_settings()
    if not _smtp_is_configured(settings):
        raise RuntimeError("SMTP settings are incomplete")
    if _smtp_has_partial_credentials(settings):
        raise RuntimeError("SMTP username/password are only partially configured")
    _send_email_message(settings, message)


def send_notification(
    subject: str,
    body: str,
    context: NotificationContext | None = None,
    email_recipients: list[str] | None = None,
    sms_recipients: list[str] | None = None,
) -> None:
    settings = get_settings()
    resolved_email_recipients = settings.notification_email_recipients if email_recipients is None else email_recipients
    resolved_sms_recipients = settings.notification_sms_recipients if sms_recipients is None else sms_recipients
    sms_body = _format_sms_body(subject=subject, body=body)
    destinations = _notification_destinations(
        email_recipients=resolved_email_recipients,
        sms_recipients=resolved_sms_recipients,
    )
    routed_destinations = []

    if resolved_email_recipients and not _smtp_is_configured(settings):
        print(
            "[notification warning] Email recipients are set, "
            "but SMTP settings are incomplete. Falling back to stdout."
        )
    elif _smtp_has_partial_credentials(settings):
        print(
            "[notification warning] SMTP username/password are only partially configured. "
            "Falling back to stdout for email."
        )
    elif resolved_email_recipients:
        try:
            _send_email(
                settings=settings,
                recipients=resolved_email_recipients,
                subject=subject,
                body=body,
                context=context,
            )
            routed_destinations.extend(
                f"email:{recipient}" for recipient in resolved_email_recipients
            )
        except Exception as exc:
            print(f"[notification warning] Email delivery failed: {exc}")

    if resolved_sms_recipients and not _twilio_is_configured(settings):
        print(
            "[notification warning] SMS recipients are set, "
            "but Twilio credentials are incomplete. Falling back to stdout."
        )

    if _twilio_is_configured(settings):
        for recipient in resolved_sms_recipients:
            try:
                _send_twilio_sms(settings=settings, recipient=recipient, message=sms_body)
            except Exception as exc:
                print(f"[notification warning] SMS delivery failed for {recipient}: {exc}")
                continue
            routed_destinations.append(f"sms:{recipient}")

    destination = ", ".join(routed_destinations or destinations) if (routed_destinations or destinations) else "stdout"
    print(f"[notification -> {destination}] {subject}\n{body}")
