from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field as PydanticField
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import select

from app.db import get_session, init_db
from app.app_auth import (
    SESSION_COOKIE_NAME,
    current_auth_context,
    current_subject_user_id,
    record_action,
    request_login_code,
    reset_auth_context,
    resolve_session,
    revoke_session,
    serialize_auth_context,
    set_auth_context,
    set_managed_user,
    validate_auth_configuration,
    verify_login_code,
)
from app.booking_pauses import (
    booking_pause_for_date,
    clear_booking_pause,
    serialize_current_booking_pause,
    set_booking_pause,
)
from app.config import get_settings
from app.discovery import (
    _latest_week_refresh_attempt,
    _week_needs_view_refresh,
    probe_discovered_class_booking_status,
    refresh_discovery,
)
from app.email_approval import serialize_booking_approval
from app.gym_auth import clear_gym_credentials, gym_auth_status, save_gym_credentials
from app.gym_session import refresh_gym_session
from app.job_sources import DRY_RUN_SELECTION_RULE_NAME, MANUAL_SELECTION_RULE_NAME
from app.models import (
    AppAuditEvent,
    AppUser,
    BookingApproval,
    BookingEvent,
    BookingJob,
    BookingStatus,
    DiscoveredClass,
    DiscoveryEvent,
)
from app.notifications import parse_recipient_csv
from app.scheduler_runtime import scheduler_status_snapshot
from app.seat_preferences import reset_class_slot_seat_preferences, save_seat_preferences
from app.scheduler import get_next_due_job
from app.seat_layout import discovered_class_seat_metadata, fetch_discovered_class_seat_layout
from app.time_utils import local_now, local_today
from app.weekly_schedule import (
    add_class_to_weekly_schedule,
    clear_weekly_schedule_skip,
    remove_weekly_schedule_item,
    replace_weekly_schedule_from_week,
    resume_weekly_schedule_occurrence,
    serialize_weekly_schedule,
    skip_weekly_schedule_occurrence,
    stop_weekly_schedule,
    upsert_manual_selection_job,
)


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
VISIBLE_WEEK_REFRESH_MAX_AGE = timedelta(hours=1)

_visible_week_refresh_lock = Lock()
_visible_week_refreshes: set[date] = set()

app = FastAPI(title="BookGym")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class SeatPreferenceUpdatePayload(BaseModel):
    included_seats: list[str] = PydanticField(default_factory=list)
    excluded_seats: list[str] = PydanticField(default_factory=list)
    scope_kind: str = "class_slot"
    selection_mode: str = ""
    reset_to_inferred: bool = False


class GymCredentialUpdatePayload(BaseModel):
    username: str = ""
    password: str = ""


class JobNetworkCapturePayload(BaseModel):
    enabled: bool = False


class LoginEmailPayload(BaseModel):
    email: str = ""


class LoginCodePayload(BaseModel):
    email: str = ""
    code: str = ""


class ManagedUserPayload(BaseModel):
    user_id: int


class BookingPausePayload(BaseModel):
    start_date: date
    resume_date: date


def _static_asset_url(relative_path: str) -> str:
    asset_path = STATIC_DIR / relative_path
    version = int(asset_path.stat().st_mtime)
    return f"/static/{relative_path}?v={version}"


def render_template(template_name: str, replacements: dict[str, str]) -> HTMLResponse:
    html = (TEMPLATES_DIR / template_name).read_text(encoding="utf-8")
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def render_calendar_page() -> HTMLResponse:
    preview_mode = get_settings().preview_mode
    return render_template(
        "calendar.html",
        {
            "__SITE_CSS__": _static_asset_url("site.css"),
            "__ADMIN_CSS__": _static_asset_url("admin.css"),
            "__ADMIN_JS__": _static_asset_url("admin.js"),
            "__PREVIEW_BODY_ATTRIBUTE__": ' data-preview-mode="true"' if preview_mode else "",
            "__PREVIEW_BANNER__": (
                '<div class="preview-mode-banner" role="status">'
                '<strong>Local preview</strong>'
                '<span>Private database copy · live gym data allowed · real bookings disabled</span>'
                '</div>'
                if preview_mode
                else ""
            ),
        },
    )


def render_login_page() -> HTMLResponse:
    return render_template(
        "login.html",
        {
            "__SITE_CSS__": _static_asset_url("site.css"),
            "__LOGIN_CSS__": _static_asset_url("login.css"),
        },
    )


def render_ops_page() -> HTMLResponse:
    return render_template(
        "ops.html",
        {
            "__SITE_CSS__": _static_asset_url("site.css"),
            "__OPS_CSS__": _static_asset_url("ops.css"),
            "__OPS_JS__": _static_asset_url("ops.js"),
        },
    )


def render_welcome_page() -> HTMLResponse:
    return render_template(
        "home.html",
        {
            "__SITE_CSS__": _static_asset_url("site.css"),
        },
    )


def serialize_job(job: BookingJob) -> dict:
    is_manual_selection = job.rule_name == MANUAL_SELECTION_RULE_NAME
    is_dry_run = job.rule_name == DRY_RUN_SELECTION_RULE_NAME
    return {
        "id": job.id,
        "user_id": job.user_id,
        "source": (
            "manual_selection"
            if is_manual_selection
            else "manual_dry_run"
            if is_dry_run
            else "unknown"
        ),
        "runnable": (
            (is_manual_selection or is_dry_run)
            and job.status in [BookingStatus.PENDING, BookingStatus.NOT_YET_OPEN]
        ),
        "dry_run": is_dry_run,
        "target_match_text": job.target_match_text,
        "class_date": job.class_date.isoformat(),
        "window_start_time": (
            job.window_start_time.isoformat(timespec="seconds")
            if job.window_start_time is not None
            else None
        ),
        "window_end_time": (
            job.window_end_time.isoformat(timespec="seconds")
            if job.window_end_time is not None
            else None
        ),
        "expected_run_at": job.expected_run_at.isoformat(),
        "calendar_event_id": job.calendar_event_id,
        "site_booking_opens_at": (
            job.site_booking_opens_at.isoformat()
            if job.site_booking_opens_at is not None
            else None
        ),
        "discovered_class_id": job.discovered_class_id,
        "status": job.status.value,
        "last_message": job.last_message,
        "last_screenshot_path": job.last_screenshot_path,
        "capture_network": job.capture_network,
        "last_network_artifact_path": job.last_network_artifact_path,
        "notification_email_override": (
            parse_recipient_csv(job.notification_email_override)
            if job.notification_email_override is not None
            else None
        ),
        "notification_sms_override": (
            parse_recipient_csv(job.notification_sms_override)
            if job.notification_sms_override is not None
            else None
        ),
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


def serialize_next_due_job(job: BookingJob | None) -> dict | None:
    if job is None:
        return None
    is_dry_run = job.rule_name == DRY_RUN_SELECTION_RULE_NAME
    return {
        "id": job.id,
        "source": "manual_dry_run" if is_dry_run else "manual_selection",
        "dry_run": is_dry_run,
        "class_date": job.class_date.isoformat(),
        "expected_run_at": job.expected_run_at.isoformat(),
        "status": job.status.value,
        "target_match_text": job.target_match_text,
        "notification_email_override": (
            parse_recipient_csv(job.notification_email_override)
            if job.notification_email_override is not None
            else None
        ),
    }


def serialize_discovered_class(discovered_class: DiscoveredClass) -> dict:
    seat_metadata = discovered_class_seat_metadata(discovered_class)
    return {
        "id": discovered_class.id,
        "source_key": discovered_class.source_key,
        "title": discovered_class.title,
        "class_date": discovered_class.class_date.isoformat(),
        "start_time": (
            discovered_class.start_time.isoformat(timespec="seconds")
            if discovered_class.start_time is not None
            else None
        ),
        "instructor_name": discovered_class.instructor_name,
        "category": discovered_class.category,
        "duration_minutes": discovered_class.duration_minutes,
        "calendar_event_id": discovered_class.calendar_event_id,
        "layout_id": seat_metadata["layout_id"],
        "event_type": discovered_class.event_type,
        "booking_type_id": discovered_class.booking_type_id,
        "resource_id": discovered_class.resource_id,
        "can_show_layout": seat_metadata["can_show_layout"],
        "seat_title": seat_metadata["seat_title"],
        "has_seat_map": seat_metadata["has_seat_map"],
        "total_spots": discovered_class.total_spots,
        "total_available_spots": discovered_class.total_available_spots,
        "total_booked": discovered_class.total_booked,
        "is_full": discovered_class.is_full,
        "can_book_event": discovered_class.can_book_event,
        "gym_booking_status": discovered_class.gym_booking_status,
        "is_booked_at_gym": discovered_class.is_booked_at_gym,
        "gym_booking_spot_labels": discovered_class.gym_booking_spot_labels,
        "gym_booking_actions": discovered_class.gym_booking_actions,
        "deep_link_url": discovered_class.deep_link_url,
        "expected_booking_opens_at": (
            discovered_class.expected_booking_opens_at.isoformat()
            if discovered_class.expected_booking_opens_at is not None
            else None
        ),
        "site_booking_opens_at": (
            discovered_class.site_booking_opens_at.isoformat()
            if discovered_class.site_booking_opens_at is not None
            else None
        ),
        "booking_probe_status": discovered_class.booking_probe_status,
        "booking_probe_message": discovered_class.booking_probe_message,
        "last_probed_at": (
            discovered_class.last_probed_at.isoformat()
            if discovered_class.last_probed_at is not None
            else None
        ),
        "raw_summary": discovered_class.raw_summary,
        "availability": (
            "booked_at_gym"
            if discovered_class.is_booked_at_gym
            else "full"
            if discovered_class.is_full or "book now (full)" in discovered_class.raw_summary.lower()
            else "available"
            if discovered_class.can_book_event is not False
            else "seen"
        ),
        "first_seen_at": discovered_class.first_seen_at.isoformat(),
        "last_seen_at": discovered_class.last_seen_at.isoformat(),
    }


def serialize_event(event: BookingEvent) -> dict:
    return {
        "id": event.id,
        "job_id": event.job_id,
        "discovered_class_id": event.discovered_class_id,
        "event_type": event.event_type,
        "status": event.status.value,
        "message": event.message,
        "screenshot_path": event.screenshot_path,
        "network_artifact_path": event.network_artifact_path,
        "site_booking_opens_at": (
            event.site_booking_opens_at.isoformat()
            if event.site_booking_opens_at is not None
            else None
        ),
        "observed_at": event.observed_at.isoformat(),
    }


def serialize_discovery_event(event: DiscoveryEvent) -> dict:
    return {
        "id": event.id,
        "job_id": event.job_id,
        "discovered_class_id": event.discovered_class_id,
        "event_type": event.event_type,
        "message": event.message,
        "visible_week_start": event.visible_week_start.isoformat() if event.visible_week_start else None,
        "visible_week_end": event.visible_week_end.isoformat() if event.visible_week_end else None,
        "observed_at": event.observed_at.isoformat(),
    }


@app.on_event("startup")
def on_startup() -> None:
    init_db()


_PUBLIC_PATHS = {"/login", "/auth/request-code", "/auth/verify-code", "/health"}
_PAGE_PATHS = {"/", "/calendar", "/welcome", "/admin", "/ops"}
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _host_is_allowed(hostname: str | None, configured_hosts: str) -> bool:
    allowed_hosts = {
        host.strip().casefold()
        for host in configured_hosts.split(",")
        if host.strip()
    }
    if not allowed_hosts or "*" in allowed_hosts:
        return True
    return hostname is not None and hostname.casefold() in allowed_hosts


def _origin_is_allowed(origin: str | None, configured_origin: str) -> bool:
    expected_origin = configured_origin.strip().rstrip("/")
    if not expected_origin:
        return True
    return bool(origin) and origin.strip().rstrip("/").casefold() == expected_origin.casefold()


def _https_redirect_url(
    scheme: str,
    hostname: str | None,
    path: str,
    query: str,
    configured_origin: str,
) -> str | None:
    public_origin = configured_origin.strip().rstrip("/")
    public_hostname = urlsplit(public_origin).hostname
    if (
        not public_origin
        or not public_hostname
        or scheme.casefold() == "https"
        or hostname is None
        or hostname.casefold() != public_hostname.casefold()
    ):
        return None
    destination = f"{public_origin}{path}"
    return f"{destination}?{query}" if query else destination


def _admin_only_request(path: str, method: str) -> bool:
    if path in {"/admin", "/ops", "/scheduler-status", "/email-approvals", "/app-audit-events", "/next-due"}:
        return True
    if path.startswith("/gym-auth/") and not (path == "/gym-auth/status" and method == "GET"):
        return True
    if path.endswith("/network-capture"):
        return True
    return False


@app.middleware("http")
async def app_auth_middleware(request: Request, call_next):
    settings = get_settings()
    if not _host_is_allowed(request.url.hostname, settings.app_allowed_hosts):
        return JSONResponse(status_code=400, content={"detail": "Host is not allowed."})

    configuration_error = validate_auth_configuration()
    if configuration_error:
        return JSONResponse(status_code=503, content={"detail": configuration_error})

    redirect_url = _https_redirect_url(
        request.url.scheme,
        request.url.hostname,
        request.url.path,
        request.url.query,
        settings.app_origin,
    )
    if redirect_url:
        return RedirectResponse(url=redirect_url, status_code=308)

    if not settings.app_auth_enabled:
        return await call_next(request)

    path = request.url.path
    if path.startswith("/static/") or path in _PUBLIC_PATHS:
        return await call_next(request)

    context = resolve_session(request.cookies.get(SESSION_COOKIE_NAME))
    if context is None:
        if path in _PAGE_PATHS:
            next_path = path if path != "/" else "/calendar"
            return RedirectResponse(url=f"/login?next={next_path}", status_code=303)
        return JSONResponse(status_code=401, content={"detail": "Sign in to BookGym."})
    if _admin_only_request(path, request.method) and not context.is_admin:
        if path in _PAGE_PATHS:
            return RedirectResponse(url="/calendar", status_code=303)
        return JSONResponse(status_code=403, content={"detail": "Administrator access required."})
    if request.method in _MUTATING_METHODS and not _origin_is_allowed(
        request.headers.get("origin"),
        settings.app_origin,
    ):
        return JSONResponse(status_code=403, content={"detail": "Request origin is not allowed."})

    context_token = set_auth_context(context)
    try:
        response = await call_next(request)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and response.status_code < 400:
            if not path.startswith("/auth/"):
                record_action(
                    context,
                    action=f"{request.method.lower()}_request",
                    resource=path,
                )
        return response
    finally:
        reset_auth_context(context_token)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )
    if not request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    if not get_settings().app_auth_enabled:
        return RedirectResponse(url="/calendar", status_code=303)
    return render_login_page()


@app.post("/auth/request-code")
def request_code_route(payload: LoginEmailPayload) -> dict[str, str]:
    if not get_settings().app_auth_enabled:
        raise HTTPException(status_code=404, detail="App authentication is disabled")
    try:
        request_login_code(payload.email)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"message": "If that email is invited, a sign-in code is on its way."}


@app.post("/auth/verify-code")
def verify_code_route(payload: LoginCodePayload) -> JSONResponse:
    if not get_settings().app_auth_enabled:
        raise HTTPException(status_code=404, detail="App authentication is disabled")
    verified = verify_login_code(payload.email, payload.code)
    if verified is None:
        raise HTTPException(status_code=400, detail="That code is invalid or has expired.")
    raw_token, context = verified
    response = JSONResponse(content=serialize_auth_context(context))
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        max_age=get_settings().app_session_days * 24 * 60 * 60,
        httponly=True,
        secure=get_settings().app_secure_cookie,
        samesite="lax",
        path="/",
    )
    return response


@app.get("/auth/me")
def auth_me_route() -> dict[str, object]:
    if not get_settings().app_auth_enabled:
        return {"enabled": False}
    context = current_auth_context()
    if context is None:
        raise HTTPException(status_code=401, detail="Sign in to BookGym.")
    return {"enabled": True, **serialize_auth_context(context)}


@app.post("/auth/logout")
def logout_route() -> JSONResponse:
    context = current_auth_context()
    if context is not None:
        revoke_session(context.session_id)
    response = JSONResponse(content={"message": "Signed out."})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@app.post("/auth/manage-user")
def manage_user_route(payload: ManagedUserPayload) -> dict[str, object]:
    context = current_auth_context()
    if context is None or not context.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required.")
    try:
        updated = set_managed_user(
            session_id=context.session_id,
            actor_user_id=int(context.actor.id),
            subject_user_id=payload.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"enabled": True, **serialize_auth_context(updated)}


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return render_calendar_page()


@app.get("/calendar", response_class=HTMLResponse)
def calendar() -> HTMLResponse:
    return render_calendar_page()


@app.get("/welcome", response_class=HTMLResponse)
def welcome() -> HTMLResponse:
    return render_welcome_page()


@app.get("/admin", response_class=HTMLResponse)
def admin() -> HTMLResponse:
    return render_ops_page()


@app.get("/ops", response_class=HTMLResponse)
def ops() -> HTMLResponse:
    return render_ops_page()


@app.get("/health")
def healthcheck() -> dict:
    if get_settings().preview_mode:
        return {
            "status": "preview",
            "preview_mode": True,
            "scheduler": {"status": "disabled", "is_running": False, "is_stale": False},
        }
    scheduler = scheduler_status_snapshot()
    overall_status = "ok" if scheduler["status"] == "running" else "degraded"
    if get_settings().app_auth_enabled:
        return {
            "status": overall_status,
            "scheduler": {
                "status": scheduler["status"],
                "is_running": scheduler["is_running"],
                "is_stale": scheduler["is_stale"],
            },
        }
    return {
        "status": overall_status,
        "scheduler": scheduler,
        "next_due_job": serialize_next_due_job(get_next_due_job()),
    }


@app.get("/scheduler-status")
def scheduler_status() -> dict:
    return {
        **scheduler_status_snapshot(),
        "next_due_job": serialize_next_due_job(get_next_due_job()),
    }


@app.get("/gym-auth/status")
def gym_auth_status_route() -> dict[str, object]:
    return gym_auth_status(user_id=current_subject_user_id())


@app.post("/gym-auth/credentials")
def save_gym_credentials_route(payload: GymCredentialUpdatePayload) -> dict[str, object]:
    try:
        save_gym_credentials(
            username=payload.username,
            password=payload.password,
            user_id=current_subject_user_id(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **gym_auth_status(user_id=current_subject_user_id()),
        "message": "Gym credentials saved locally in BookGym.",
    }


@app.delete("/gym-auth/credentials")
def clear_gym_credentials_route() -> dict[str, object]:
    clear_gym_credentials(user_id=current_subject_user_id())
    return {
        **gym_auth_status(user_id=current_subject_user_id()),
        "message": "Saved BookGym gym credentials were cleared.",
    }


@app.post("/gym-auth/refresh")
def refresh_gym_auth_route() -> dict[str, object]:
    try:
        refreshed = refresh_gym_session(headless=True)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gym login refresh failed: {exc}") from exc
    return {
        **refreshed,
        "message": "Gym session refreshed.",
    }


@app.get("/jobs")
def list_jobs() -> list[dict]:
    with get_session() as session:
        statement = select(BookingJob)
        user_id = current_subject_user_id()
        if user_id is not None:
            statement = statement.where(BookingJob.user_id == user_id)
        jobs = list(session.exec(statement.order_by(BookingJob.expected_run_at)).all())
    return [serialize_job(job) for job in jobs]


@app.get("/email-approvals")
def list_email_approvals() -> list[dict[str, object]]:
    with get_session() as session:
        approvals = list(
            session.exec(
                select(BookingApproval).order_by(BookingApproval.request_due_at)
            ).all()
        )
    return [serialize_booking_approval(approval) for approval in approvals]


@app.get("/app-audit-events")
def list_app_audit_events() -> list[dict[str, object]]:
    with get_session() as session:
        events = list(
            session.exec(
                select(AppAuditEvent).order_by(AppAuditEvent.observed_at.desc()).limit(250)
            ).all()
        )
        user_ids = {event.actor_user_id for event in events} | {
            event.subject_user_id for event in events
        }
        users = {
            int(user.id): user
            for user in session.exec(select(AppUser).where(AppUser.id.in_(user_ids))).all()
        } if user_ids else {}
        return [
            {
                "id": event.id,
                "actor_user_id": event.actor_user_id,
                "actor_name": users[event.actor_user_id].name if event.actor_user_id in users else "",
                "subject_user_id": event.subject_user_id,
                "subject_name": users[event.subject_user_id].name if event.subject_user_id in users else "",
                "action": event.action,
                "resource": event.resource,
                "detail": event.detail,
                "observed_at": event.observed_at.isoformat(),
            }
            for event in events
        ]


@app.get("/weekly-schedule")
def weekly_schedule() -> dict:
    return serialize_weekly_schedule(user_id=current_subject_user_id())


@app.get("/booking-pause")
def booking_pause() -> dict:
    return serialize_current_booking_pause(user_id=current_subject_user_id())


@app.put("/booking-pause")
def update_booking_pause(payload: BookingPausePayload) -> dict:
    try:
        return set_booking_pause(
            start_date=payload.start_date,
            resume_date=payload.resume_date,
            user_id=current_subject_user_id(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/booking-pause")
def delete_booking_pause() -> dict:
    return clear_booking_pause(user_id=current_subject_user_id())


@app.get("/events")
def list_events() -> list[dict]:
    with get_session() as session:
        statement = select(BookingEvent)
        user_id = current_subject_user_id()
        if user_id is not None:
            job_ids = list(
                session.exec(select(BookingJob.id).where(BookingJob.user_id == user_id)).all()
            )
            statement = statement.where(BookingEvent.job_id.in_(job_ids))
        events = list(session.exec(statement.order_by(BookingEvent.observed_at)).all())
    return [serialize_event(event) for event in events]


@app.get("/discovery-events")
def list_discovery_events() -> list[dict]:
    with get_session() as session:
        events = list(session.exec(select(DiscoveryEvent).order_by(DiscoveryEvent.observed_at)).all())
    return [serialize_discovery_event(event) for event in events]


@app.get("/discovered-classes")
def list_discovered_classes() -> list[dict]:
    with get_session() as session:
        discovered_classes = list(
            session.exec(
                select(DiscoveredClass).order_by(DiscoveredClass.class_date, DiscoveredClass.start_time)
            ).all()
        )
    return [serialize_discovered_class(discovered_class) for discovered_class in discovered_classes]


@app.post("/weeks/{week_start}/refresh")
def refresh_visible_week(week_start: date) -> dict:
    week_end = week_start + timedelta(days=6)
    if week_start.weekday() != 6:
        raise HTTPException(status_code=400, detail="week_start must be a Sunday")
    if week_end < local_today():
        return {
            "status": "past",
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
        }

    with _visible_week_refresh_lock:
        if week_start in _visible_week_refreshes:
            return {
                "status": "refreshing",
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
            }

        refresh_cutoff = local_now() - VISIBLE_WEEK_REFRESH_MAX_AGE
        with get_session() as session:
            if not _week_needs_view_refresh(
                session=session,
                range_start=week_start,
                range_end=week_end,
                refreshed_before=refresh_cutoff,
            ):
                latest_attempt = _latest_week_refresh_attempt(
                    session=session,
                    range_start=week_start,
                    range_end=week_end,
                )
                return {
                    "status": "fresh",
                    "week_start": week_start.isoformat(),
                    "week_end": week_end.isoformat(),
                    "last_attempted_at": (
                        latest_attempt.observed_at.isoformat() if latest_attempt is not None else None
                    ),
                }

            requested_at = local_now()
            session.add(
                DiscoveryEvent(
                    event_type="view_refresh_requested",
                    message=(
                        f"Visible calendar week requested a refresh for "
                        f"{week_start.isoformat()} through {week_end.isoformat()}."
                    ),
                    visible_week_start=week_start,
                    visible_week_end=week_end,
                    observed_at=requested_at,
                )
            )
            session.commit()
        _visible_week_refreshes.add(week_start)

    try:
        summary = refresh_discovery(
            date_from=week_start,
            date_to=week_end,
            stale_only=False,
        )
    except Exception as exc:
        with get_session() as session:
            session.add(
                DiscoveryEvent(
                    event_type="view_refresh_failed",
                    message=f"Visible calendar week refresh failed: {exc}",
                    visible_week_start=week_start,
                    visible_week_end=week_end,
                    observed_at=local_now(),
                )
            )
            session.commit()
        raise HTTPException(status_code=500, detail=f"Calendar week refresh failed: {exc}") from exc
    finally:
        with _visible_week_refresh_lock:
            _visible_week_refreshes.discard(week_start)

    return {
        "status": "refreshed",
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "scanned_ranges": summary.scanned_ranges,
        "matched_classes": summary.matched_classes,
    }


@app.get("/discovered-classes/{discovered_class_id}/seat-layout")
def get_discovered_class_seat_layout(discovered_class_id: int) -> dict:
    try:
        return fetch_discovered_class_seat_layout(
            discovered_class_id,
            headless=True,
            user_id=current_subject_user_id(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Seat layout fetch failed: {exc}") from exc


@app.post("/discovered-classes/{discovered_class_id}/seat-preferences")
def update_discovered_class_seat_preferences(
    discovered_class_id: int,
    payload: SeatPreferenceUpdatePayload,
) -> dict:
    init_db()
    with get_session() as session:
        discovered_class = session.get(DiscoveredClass, discovered_class_id)
        if discovered_class is None:
            raise HTTPException(status_code=404, detail="Discovered class not found")
        try:
            if payload.reset_to_inferred:
                response = reset_class_slot_seat_preferences(
                    session=session,
                    discovered_class=discovered_class,
                    user_id=current_subject_user_id(),
                )
            else:
                response = save_seat_preferences(
                    session=session,
                    discovered_class=discovered_class,
                    included_seats=payload.included_seats,
                    excluded_seats=payload.excluded_seats,
                    scope_kind=payload.scope_kind,
                    selection_mode=payload.selection_mode,
                    user_id=current_subject_user_id(),
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session.commit()
        return response


@app.post("/discovered-classes/{discovered_class_id}/probe-booking")
def probe_discovered_class(discovered_class_id: int) -> dict:
    try:
        discovered_class = probe_discovered_class_booking_status(discovered_class_id, headless=True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Booking probe failed: {exc}") from exc
    return serialize_discovered_class(discovered_class)


@app.post("/discovered-classes/{discovered_class_id}/select")
def select_discovered_class(discovered_class_id: int) -> dict:
    init_db()
    now = local_now()
    with get_session() as session:
        discovered_class = session.get(DiscoveredClass, discovered_class_id)
        if discovered_class is None:
            raise HTTPException(status_code=404, detail="Discovered class not found")
        if discovered_class.is_booked_at_gym:
            raise HTTPException(status_code=400, detail="Discovered class already appears booked at the gym")
        if discovered_class.is_full or "book now (full)" in discovered_class.raw_summary.lower():
            raise HTTPException(status_code=400, detail="Discovered class appears full right now")

        user_id = current_subject_user_id()
        pause = booking_pause_for_date(
            session,
            discovered_class.class_date,
            user_id=user_id,
        )
        if pause is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Bookings are paused for this class date. "
                    f"Bookings resume on {pause.resume_date.isoformat()}."
                ),
            )
        clear_weekly_schedule_skip(session, discovered_class, user_id=user_id)
        job, _created = upsert_manual_selection_job(
            session,
            discovered_class,
            now=now,
            last_message="Selected from admin calendar",
            user_id=user_id,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return serialize_job(job)


@app.post("/discovered-classes/{discovered_class_id}/weekly-schedule")
def add_discovered_class_to_weekly_schedule(discovered_class_id: int) -> dict:
    try:
        return add_class_to_weekly_schedule(discovered_class_id, user_id=current_subject_user_id())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/discovered-classes/{discovered_class_id}/weekly-schedule/skip")
def skip_discovered_class_week(discovered_class_id: int) -> dict:
    try:
        return skip_weekly_schedule_occurrence(discovered_class_id, user_id=current_subject_user_id())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/discovered-classes/{discovered_class_id}/weekly-schedule/skip")
def resume_discovered_class_week(discovered_class_id: int) -> dict:
    try:
        return resume_weekly_schedule_occurrence(discovered_class_id, user_id=current_subject_user_id())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/weekly-schedule/items/{item_id}")
def delete_weekly_schedule_item(item_id: int) -> dict:
    try:
        return remove_weekly_schedule_item(item_id, user_id=current_subject_user_id())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/weekly-schedule")
def delete_weekly_schedule() -> dict:
    return stop_weekly_schedule(user_id=current_subject_user_id())


@app.post("/weeks/{week_start}/weekly-schedule")
def use_week_as_weekly_schedule(week_start: date) -> dict:
    try:
        return replace_weekly_schedule_from_week(week_start, user_id=current_subject_user_id())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/jobs/{job_id}/deselect")
def deselect_job(job_id: int) -> dict:
    init_db()
    with get_session() as session:
        job = session.get(BookingJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        user_id = current_subject_user_id()
        if user_id is not None and job.user_id != user_id:
            raise HTTPException(status_code=404, detail="Job not found")

        job.status = BookingStatus.CANCELED
        job.last_message = "Deselected from admin calendar"
        job.updated_at = local_now()
        session.add(job)
        session.commit()
        session.refresh(job)
        return serialize_job(job)


@app.post("/jobs/{job_id}/network-capture")
def update_job_network_capture(job_id: int, payload: JobNetworkCapturePayload) -> dict:
    init_db()
    with get_session() as session:
        job = session.get(BookingJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status not in [BookingStatus.PENDING, BookingStatus.NOT_YET_OPEN]:
            raise HTTPException(status_code=400, detail="Network capture can only be changed for pending jobs")

        job.capture_network = payload.enabled
        job.updated_at = local_now()
        session.add(job)
        session.commit()
        session.refresh(job)
        return serialize_job(job)


@app.post("/weeks/{week_start}/clear-watched")
def clear_week_watched_classes(week_start: date) -> dict:
    init_db()
    week_end = week_start + timedelta(days=6)
    clearable_statuses = [
        BookingStatus.PENDING,
        BookingStatus.NOT_YET_OPEN,
        BookingStatus.FAILED,
        BookingStatus.NEEDS_CONFIRMATION,
        BookingStatus.WAITLISTED,
    ]
    now = local_now()

    with get_session() as session:
        statement = (
                select(BookingJob)
                .where(
                    BookingJob.rule_name == MANUAL_SELECTION_RULE_NAME,
                    BookingJob.class_date >= week_start,
                    BookingJob.class_date <= week_end,
                    BookingJob.status.in_(clearable_statuses),
                )
                .order_by(BookingJob.class_date, BookingJob.window_start_time)
        )
        user_id = current_subject_user_id()
        if user_id is not None:
            statement = statement.where(BookingJob.user_id == user_id)
        jobs = list(session.exec(statement).all())
        for job in jobs:
            job.status = BookingStatus.CANCELED
            job.last_message = f"Cleared from week view for {week_start.isoformat()}"
            job.updated_at = now
            session.add(job)
        session.commit()
        for job in jobs:
            session.refresh(job)

        return {
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "cleared_count": len(jobs),
            "jobs": [serialize_job(job) for job in jobs],
        }


@app.get("/next-due")
def next_due() -> dict:
    job = get_next_due_job()
    if job is None:
        return {"next_due": None}
    return {"next_due": serialize_job(job)}
