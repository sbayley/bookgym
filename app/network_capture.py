from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import BrowserContext


SENSITIVE_KEYS = {
    "access_token",
    "auth",
    "authorization",
    "code",
    "email",
    "id_token",
    "jwt",
    "password",
    "refresh_token",
    "session",
    "token",
    "username",
}

INTERESTING_PATTERN = re.compile(
    r"book|booking|reserve|reservation|capacity|spot|seat|layout|available|booked|unavailable|waitlist|class|schedule|calendar",
    re.IGNORECASE,
)

CAPTURE_NEXT_BOOKING_MARKER = Path("artifacts") / "capture-next-booking.json"
CAPTURE_NEXT_BOOKING_CLAIM_PREFIX = "capture-next-booking-job-"


def next_booking_capture_claim_path(job_id: int) -> Path:
    return CAPTURE_NEXT_BOOKING_MARKER.with_name(
        f"{CAPTURE_NEXT_BOOKING_CLAIM_PREFIX}{job_id}.json"
    )


def arm_next_booking_capture() -> Path:
    CAPTURE_NEXT_BOOKING_MARKER.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "armed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "next_live_booking",
    }
    CAPTURE_NEXT_BOOKING_MARKER.write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    return CAPTURE_NEXT_BOOKING_MARKER


def claim_next_booking_capture(job_id: int) -> bool:
    claim_path = next_booking_capture_claim_path(job_id)
    if claim_path.exists():
        return True
    try:
        CAPTURE_NEXT_BOOKING_MARKER.replace(claim_path)
    except FileNotFoundError:
        return False
    return True


def complete_next_booking_capture(job_id: int) -> None:
    next_booking_capture_claim_path(job_id).unlink(missing_ok=True)


def next_booking_capture_state() -> dict[str, object]:
    if CAPTURE_NEXT_BOOKING_MARKER.exists():
        return {
            "armed": True,
            "claimed_job_id": None,
            "path": str(CAPTURE_NEXT_BOOKING_MARKER),
        }

    claims = sorted(
        CAPTURE_NEXT_BOOKING_MARKER.parent.glob(
            f"{CAPTURE_NEXT_BOOKING_CLAIM_PREFIX}*.json"
        )
    )
    if not claims:
        return {"armed": False, "claimed_job_id": None, "path": ""}

    claim_path = claims[0]
    job_label = claim_path.stem.removeprefix(CAPTURE_NEXT_BOOKING_CLAIM_PREFIX)
    claimed_job_id = int(job_label) if job_label.isdigit() else None
    return {
        "armed": True,
        "claimed_job_id": claimed_job_id,
        "path": str(claim_path),
    }


def sanitize_url(url: str) -> str:
    parts = urlsplit(url)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in SENSITIVE_KEYS:
            query.append((key, "<redacted>"))
        else:
            query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def sanitize_text(value: str) -> str:
    sanitized = re.sub(
        r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1<redacted>",
        value,
    )
    sanitized = re.sub(
        r"(?i)(access_token|refresh_token|id_token|password|authorization|session|jwt)"
        r'(["\']?\s*[:=]\s*["\']?)[^,"\'&\s}]+',
        r"\1\2<redacted>",
        sanitized,
    )
    sanitized = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "<redacted-email>", sanitized)
    return sanitized


class BookingNetworkCapture:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self._tasks: list[asyncio.Task] = []
        self._attached = False

    def attach(self, context: BrowserContext) -> None:
        if self._attached:
            return
        self._attached = True
        context.on(
            "response",
            lambda response: self._tasks.append(asyncio.create_task(self._capture_response(response))),
        )

    async def _capture_response(self, response) -> None:
        request = response.request
        resource_type = request.resource_type
        if resource_type not in {"document", "fetch", "xhr"}:
            return

        request_post_data = sanitize_text(request.post_data or "")
        event = {
            "method": request.method,
            "resource_type": resource_type,
            "status": response.status,
            "url": sanitize_url(response.url),
            "request_post_data": request_post_data[:20_000],
            "response_content_type": response.headers.get("content-type", ""),
            "interesting": bool(
                INTERESTING_PATTERN.search(response.url)
                or INTERESTING_PATTERN.search(request_post_data)
            ),
            "body_preview": "",
            "body_error": "",
        }

        content_type = event["response_content_type"].lower()
        if "json" in content_type or "text" in content_type or "html" in content_type:
            try:
                body = sanitize_text(await response.text())
                if INTERESTING_PATTERN.search(body):
                    event["interesting"] = True
                if event["interesting"]:
                    event["body_preview"] = body[:20_000]
            except Exception as exc:
                event["body_error"] = str(exc)

        if event["interesting"] or request.method.upper() != "GET":
            self.events.append(event)

    async def save(
        self,
        *,
        job_id: int | None,
        class_name: str,
        class_date: str,
        expected_run_at: str,
        status: str,
        message: str,
    ) -> str:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        artifacts_dir = Path("artifacts")
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        job_label = f"job-{job_id}" if job_id is not None else "manual"
        path = artifacts_dir / f"booking-network-{job_label}-{class_date}-{stamp}.json"
        payload = {
            "target": {
                "job_id": job_id,
                "class_name": class_name,
                "class_date": class_date,
                "expected_run_at": expected_run_at,
            },
            "result": {
                "status": status,
                "message": sanitize_text(message),
            },
            "captured_response_count": len(self.events),
            "events": self.events,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return str(path)
