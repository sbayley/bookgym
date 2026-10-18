from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import socket
from typing import Any

from app.config import get_settings
from app.time_utils import local_now


def scheduler_status_path() -> Path:
    return Path("artifacts") / "scheduler-status.json"


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _read_payload() -> dict[str, Any]:
    path = scheduler_status_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_payload(payload: dict[str, Any]) -> None:
    path = scheduler_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def update_scheduler_runtime(
    *,
    detail: str,
    last_booking_poll_at: datetime | None = None,
    last_booking_run_started_at: datetime | None = None,
    last_booking_run_finished_at: datetime | None = None,
    last_discovery_refresh_started_at: datetime | None = None,
    last_discovery_refresh_finished_at: datetime | None = None,
    last_error: str | None = None,
    next_due_job_id: int | None = None,
    next_due_expected_run_at: datetime | None = None,
) -> None:
    now = local_now()
    payload = _read_payload()
    if not payload.get("started_at"):
        payload["started_at"] = now.isoformat()

    payload.update(
        {
            "service": "scheduler",
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "detail": detail,
            "last_heartbeat_at": now.isoformat(),
            "last_error": last_error or "",
            "next_due_job_id": next_due_job_id,
            "next_due_expected_run_at": (
                next_due_expected_run_at.isoformat() if next_due_expected_run_at is not None else None
            ),
        }
    )

    if last_booking_poll_at is not None:
        payload["last_booking_poll_at"] = last_booking_poll_at.isoformat()
    if last_booking_run_started_at is not None:
        payload["last_booking_run_started_at"] = last_booking_run_started_at.isoformat()
    if last_booking_run_finished_at is not None:
        payload["last_booking_run_finished_at"] = last_booking_run_finished_at.isoformat()
    if last_discovery_refresh_started_at is not None:
        payload["last_discovery_refresh_started_at"] = last_discovery_refresh_started_at.isoformat()
    if last_discovery_refresh_finished_at is not None:
        payload["last_discovery_refresh_finished_at"] = last_discovery_refresh_finished_at.isoformat()

    _write_payload(payload)


def scheduler_status_snapshot() -> dict[str, Any]:
    settings = get_settings()
    now = local_now()
    payload = _read_payload()
    last_heartbeat_at = _parse_datetime(payload.get("last_heartbeat_at"))

    if last_heartbeat_at is None:
        return {
            "status": "missing",
            "detail": "No scheduler heartbeat has been recorded yet.",
            "is_running": False,
            "is_stale": True,
            "last_heartbeat_at": None,
            "seconds_since_heartbeat": None,
            "stale_after_seconds": settings.scheduler_stale_seconds,
            "pid": payload.get("pid"),
            "hostname": payload.get("hostname") or "",
            "last_booking_poll_at": payload.get("last_booking_poll_at"),
            "last_booking_run_started_at": payload.get("last_booking_run_started_at"),
            "last_booking_run_finished_at": payload.get("last_booking_run_finished_at"),
            "last_discovery_refresh_started_at": payload.get("last_discovery_refresh_started_at"),
            "last_discovery_refresh_finished_at": payload.get("last_discovery_refresh_finished_at"),
            "last_error": payload.get("last_error") or "",
            "next_due_job_id": payload.get("next_due_job_id"),
            "next_due_expected_run_at": payload.get("next_due_expected_run_at"),
        }

    seconds_since_heartbeat = max(0, int((now - last_heartbeat_at).total_seconds()))
    is_stale = seconds_since_heartbeat > settings.scheduler_stale_seconds
    detail = str(payload.get("detail") or "").strip()
    if is_stale:
        detail = detail or "Scheduler heartbeat is stale."
    elif not detail:
        detail = "Scheduler heartbeat is fresh."

    return {
        "status": "stale" if is_stale else "running",
        "detail": detail,
        "is_running": not is_stale,
        "is_stale": is_stale,
        "last_heartbeat_at": last_heartbeat_at.isoformat(),
        "seconds_since_heartbeat": seconds_since_heartbeat,
        "stale_after_seconds": settings.scheduler_stale_seconds,
        "pid": payload.get("pid"),
        "hostname": payload.get("hostname") or "",
        "last_booking_poll_at": payload.get("last_booking_poll_at"),
        "last_booking_run_started_at": payload.get("last_booking_run_started_at"),
        "last_booking_run_finished_at": payload.get("last_booking_run_finished_at"),
        "last_discovery_refresh_started_at": payload.get("last_discovery_refresh_started_at"),
        "last_discovery_refresh_finished_at": payload.get("last_discovery_refresh_finished_at"),
        "last_error": payload.get("last_error") or "",
        "next_due_job_id": payload.get("next_due_job_id"),
        "next_due_expected_run_at": payload.get("next_due_expected_run_at"),
    }
