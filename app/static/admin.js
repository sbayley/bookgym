const AUTO_RELOAD_MS = 60_000;
const PROBE_STALE_MS = 24 * 60 * 60 * 1000;
const VISIBLE_WEEK_REFRESH_MS = 60 * 60 * 1000;
const CLASS_FILTER_STORAGE_KEY = "bookgym.admin.classFilters.v1";
const PREVIEW_MODE = document.body.dataset.previewMode === "true";

const CALENDAR_SCOPE_MODES = [
    { id: "all", label: "All" },
    { id: "mine", label: "Mine" },
];

const SEAT_PREFERENCE_MODES = [
    { id: "prefer", label: "Prefer", note: "Try first" },
    { id: "avoid", label: "Avoid", note: "Skip" },
];

const SEAT_BOOKING_POLICIES = [
    { id: "strict", label: "Only these", note: "No fallback" },
    { id: "preferences", label: "Prefer first", note: "Then any" },
    { id: "any", label: "Any seat", note: "Skip list" },
];

const WEEKDAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

const state = {
    classes: [],
    jobs: [],
    weeklySchedule: { active: false, items: [] },
    bookingPause: null,
    discoveryEvents: [],
    bookingEvents: [],
    authStatus: null,
    appUser: null,
    userMenuOpen: false,
    seatLayouts: new Map(),
    seatLayoutLoadingIds: new Set(),
    seatLayoutErrors: new Map(),
    seatPreferenceSavingIds: new Set(),
    seatPreferenceErrors: new Map(),
    seatPreferenceDrag: null,
    seatPreferenceMode: "prefer",
    seatEditorOpenClassId: null,
    selectedClassId: null,
    selectedWeekStartKey: null,
    detailScopeKey: "",
    weekActionLoading: "",
    weekClearConfirmKey: "",
    weekNotices: new Map(),
    monthDate: new Date(),
    typeFilters: new Set(),
    typeFilterMode: "all",
    calendarScopeMode: "all",
    searchQuery: "",
    probeLoadingIds: new Set(),
    selectionLoadingIds: new Set(),
    visibleWeekRefreshRequestedAt: new Map(),
    visibleWeekRefreshObserver: null,
    authBusy: false,
    authModalOpen: false,
    bookingPauseModalOpen: false,
    bookingPauseBusy: false,
    bookingPauseError: "",
    authNotice: "",
    authError: "",
    notices: new Map(),
    initializedMonth: false,
};

const fmtDate = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" });
const fmtMonth = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });
const fmtShortDate = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const fmtWeekdayDate = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" });
const fmtTime = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });

document.addEventListener("DOMContentLoaded", () => {
    loadClassFilterPreference();
    document.getElementById("prev-month")?.addEventListener("click", () => {
        movePeriod(-1);
    });
    document.getElementById("next-month")?.addEventListener("click", () => {
        movePeriod(1);
    });
    document.getElementById("search")?.addEventListener("input", (event) => {
        state.searchQuery = event.target.value.trim().toLowerCase();
        render();
    });
    document.getElementById("calendar-scope")?.addEventListener("click", (event) => {
        const button = event.target.closest("[data-scope-mode]");
        if (!button) return;
        const mode = button.dataset.scopeMode;
        if (!CALENDAR_SCOPE_MODES.some((entry) => entry.id === mode)) return;
        state.calendarScopeMode = mode;
        const selectedItem = state.classes.find((item) => item.id === state.selectedClassId);
        if (selectedItem && !itemMatchesScopeFilter(selectedItem)) {
            state.selectedClassId = null;
        }
        render();
    });
    document.getElementById("auth-modal-open")?.addEventListener("click", () => {
        state.authModalOpen = true;
        renderAuthControls();
    });
    document.querySelector("[data-booking-pause-open]")?.addEventListener("click", () => {
        openBookingPauseModal();
    });
    document.getElementById("user-menu-open")?.addEventListener("click", () => {
        state.userMenuOpen = !state.userMenuOpen;
        renderUserControls();
    });

    loadState().catch((error) => {
        renderLoadError(error);
    });

    window.setInterval(() => {
        loadState().catch(() => {
            // Keep the current UI steady if a background refresh fails.
        });
    }, AUTO_RELOAD_MS);

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && state.bookingPauseModalOpen) {
            state.bookingPauseModalOpen = false;
            state.bookingPauseError = "";
            renderBookingPauseControls();
            return;
        }
        if (event.key === "Escape" && state.authModalOpen) {
            state.authModalOpen = false;
            renderAuthControls();
            return;
        }
        if (event.key === "Escape" && state.seatEditorOpenClassId !== null) {
            closeSeatEditor();
            return;
        }
        if (event.key === "Escape" && state.selectedClassId !== null) {
            state.selectedClassId = null;
            render();
        }
    });
});

function renderLoadError(error) {
    const panel = document.getElementById("details");
    syncBodyModalState();
    state.seatEditorOpenClassId = null;
    panel.innerHTML = `
        <div class="detail-empty">
            <h2>Classes could not load</h2>
            <p>${escapeHtml(error.message)}</p>
        </div>
    `;
    renderAuthControls();
}

function syncBodyModalState(item = null) {
    document.body.classList.toggle(
        "modal-open",
        state.authModalOpen || state.bookingPauseModalOpen || isSeatEditorOpen(item),
    );
    document.body.classList.toggle("class-detail-open", Boolean(item));
}

function authSummaryLabel() {
    const authStatus = state.authStatus;
    if (!authStatus) return "Loading";
    if (authStatus.last_login_error) return "Needs attention";
    if (authStatus.session_cached) return "Connected";
    if (authStatus.has_credentials) return "Ready to reconnect";
    return "Needs setup";
}

function authSummaryClassName() {
    const authStatus = state.authStatus;
    if (!authStatus) return "pending";
    if (authStatus.last_login_error) return "warning";
    if (authStatus.session_cached) return "connected";
    if (authStatus.has_credentials) return "saved";
    return "empty";
}

function authSourceLabel(authStatus) {
    if (!authStatus?.credential_source) return "No saved credentials yet";
    return authStatus.credential_source === "app"
        ? "Stored in BookGym"
        : "Loaded from .env";
}

function authDetailMessage(authStatus) {
    if (!authStatus) return "Checking the saved gym session and login settings.";
    if (authStatus.last_login_error) return authStatus.last_login_error;
    if (authStatus.session_cached && authStatus.session_saved_at) {
        return `Saved gym session refreshed ${formatRelativeTime(authStatus.session_saved_at)}.`;
    }
    if (authStatus.has_credentials) {
        return "BookGym can try a fresh gym login automatically if the saved browser session expires.";
    }
    return "Save the gym username and password here so BookGym can recover automatically.";
}

function renderAuthControls() {
    const entryButton = document.getElementById("auth-modal-open");
    const entryValue = document.getElementById("auth-entry-value");
    const shell = document.getElementById("auth-modal-shell");

    if (entryButton) {
        entryButton.className = `auth-entry ${authSummaryClassName()}${state.authBusy ? " busy" : ""}`;
        entryButton.disabled = state.authBusy;
    }
    if (entryValue) {
        entryValue.textContent = authSummaryLabel();
    }

    syncBodyModalState(state.classes.find((entry) => entry.id === state.selectedClassId) || null);

    if (!shell) {
        return;
    }
    if (!state.authModalOpen) {
        shell.innerHTML = "";
        return;
    }

    const authStatus = state.authStatus;
    const noticeText = state.authError || state.authNotice;
    const noticeKind = state.authError ? "warning" : "success";
    shell.innerHTML = `
        <div class="auth-modal-overlay" id="auth-modal-overlay">
            <section class="auth-modal">
                <div class="auth-modal-head">
                    <div>
                        <p class="detail-topline">Gym session</p>
                        <h2>Keep BookGym signed in</h2>
                    </div>
                    <button class="auth-close" id="auth-modal-close" type="button" aria-label="Close gym login panel">×</button>
                </div>
                <div class="auth-summary ${escapeHtml(authSummaryClassName())}">
                    <div>
                        <span class="fact-label">Current status</span>
                        <strong>${escapeHtml(authSummaryLabel())}</strong>
                    </div>
                    <p>${escapeHtml(authDetailMessage(authStatus))}</p>
                </div>
                ${noticeText ? `<div class="notice-banner ${escapeHtml(noticeKind)}">${escapeHtml(noticeText)}</div>` : ""}
                <div class="auth-meta-grid">
                    <div class="meta-chip">
                        <span>Credential source</span>
                        <strong>${escapeHtml(authSourceLabel(authStatus))}</strong>
                    </div>
                    <div class="meta-chip">
                        <span>Session cache</span>
                        <strong>${escapeHtml(authStatus?.session_cached ? "Saved locally" : "Missing")}</strong>
                    </div>
                    <div class="meta-chip">
                        <span>Last refresh</span>
                        <strong>${escapeHtml(authStatus?.session_saved_at ? formatRelativeTime(authStatus.session_saved_at) : "Never")}</strong>
                    </div>
                    <div class="meta-chip">
                        <span>Gym account</span>
                        <strong>${escapeHtml(authStatus?.display_username || "Not shown yet")}</strong>
                    </div>
                </div>
                <form class="auth-form" id="auth-form">
                    <label class="auth-field">
                        <span>Gym username or email</span>
                        <input
                            id="gym-auth-username"
                            name="username"
                            type="text"
                            autocomplete="username"
                            placeholder="${escapeHtml(authStatus?.display_username || "name@example.com")}"
                        >
                    </label>
                    <label class="auth-field">
                        <span>Gym password</span>
                        <input
                            id="gym-auth-password"
                            name="password"
                            type="password"
                            autocomplete="current-password"
                            placeholder="${escapeHtml(authStatus?.has_credentials ? "Saved password will be replaced when you submit." : "Enter password")}"
                        >
                    </label>
                    <p class="auth-note">Credentials stay in BookGym's local database on this Mac and are only used when the gym session needs to be refreshed.</p>
                    <div class="auth-actions">
                        <button class="primary-link auth-button" type="submit" ${state.authBusy ? "disabled" : ""}>${escapeHtml(state.authBusy ? "Saving..." : "Save login")}</button>
                        <button class="subtle-link auth-button" id="auth-refresh" type="button" ${state.authBusy ? "disabled" : ""}>Refresh gym session</button>
                        <button class="subtle-link auth-button auth-danger" id="auth-clear" type="button" ${state.authBusy ? "disabled" : ""}>Clear saved login</button>
                    </div>
                </form>
            </section>
        </div>
    `;

    document.getElementById("auth-modal-close")?.addEventListener("click", () => {
        state.authModalOpen = false;
        renderAuthControls();
    });
    document.getElementById("auth-modal-overlay")?.addEventListener("click", (event) => {
        if (event.target?.id === "auth-modal-overlay") {
            state.authModalOpen = false;
            renderAuthControls();
        }
    });
    document.getElementById("auth-form")?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const username = form.querySelector("#gym-auth-username")?.value || "";
        const password = form.querySelector("#gym-auth-password")?.value || "";
        await saveAuthCredentials(username, password);
    });
    document.getElementById("auth-refresh")?.addEventListener("click", async () => {
        await refreshAuthSession();
    });
    document.getElementById("auth-clear")?.addEventListener("click", async () => {
        await clearAuthCredentials();
    });
}

async function saveAuthCredentials(username, password) {
    state.authBusy = true;
    state.authError = "";
    state.authNotice = "";
    renderAuthControls();
    try {
        const response = await fetchJson("/gym-auth/credentials", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password }),
        });
        state.authStatus = response;
        state.authNotice = response.message || "Gym credentials saved.";
    } catch (error) {
        state.authError = error.message;
    } finally {
        state.authBusy = false;
        renderAuthControls();
    }
}

async function refreshAuthSession() {
    state.authBusy = true;
    state.authError = "";
    state.authNotice = "";
    renderAuthControls();
    try {
        const response = await fetchJson("/gym-auth/refresh", { method: "POST" });
        state.authStatus = response;
        state.authNotice = response.message || "Gym session refreshed.";
    } catch (error) {
        state.authError = error.message;
    } finally {
        state.authBusy = false;
        renderAuthControls();
    }
}

async function clearAuthCredentials() {
    state.authBusy = true;
    state.authError = "";
    state.authNotice = "";
    renderAuthControls();
    try {
        const response = await fetchJson("/gym-auth/credentials", { method: "DELETE" });
        state.authStatus = response;
        state.authNotice = response.message || "Saved gym login cleared.";
    } catch (error) {
        state.authError = error.message;
    } finally {
        state.authBusy = false;
        renderAuthControls();
    }
}

function localDateInputValue(value = new Date()) {
    const year = value.getFullYear();
    const month = String(value.getMonth() + 1).padStart(2, "0");
    const day = String(value.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
}

function addDaysToDateKey(value, days) {
    const dateValue = parseLocalDate(value);
    dateValue.setDate(dateValue.getDate() + days);
    return localDateInputValue(dateValue);
}

function bookingPauseForDate(classDate) {
    const pause = state.bookingPause;
    const dateValue = String(classDate || "");
    if (!pause || !dateValue) return null;
    return pause.start_date <= dateValue && dateValue < pause.resume_date ? pause : null;
}

function bookingPauseForClass(item) {
    return bookingPauseForDate(item?.class_date);
}

function pauseDateLabel(value) {
    return fmtDate.format(parseLocalDate(value));
}

function bookingPauseIsActive(pause = state.bookingPause) {
    if (!pause) return false;
    const today = localDateInputValue();
    return pause.start_date <= today && today < pause.resume_date;
}

function openBookingPauseModal() {
    state.userMenuOpen = false;
    state.bookingPauseModalOpen = true;
    state.bookingPauseError = "";
    renderUserControls();
    renderBookingPauseControls();
}

function closeBookingPauseModal() {
    state.bookingPauseModalOpen = false;
    state.bookingPauseError = "";
    renderBookingPauseControls();
}

async function saveBookingPause(startDate, resumeDate) {
    state.bookingPauseBusy = true;
    state.bookingPauseError = "";
    renderBookingPauseControls();
    try {
        const payload = await fetchJson("/booking-pause", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ start_date: startDate, resume_date: resumeDate }),
        });
        state.bookingPause = payload.pause || null;
        state.bookingPauseModalOpen = false;
        await loadState();
    } catch (error) {
        state.bookingPauseError = error.message;
    } finally {
        state.bookingPauseBusy = false;
        renderBookingPauseControls();
    }
}

async function resumeBookingsNow() {
    state.bookingPauseBusy = true;
    state.bookingPauseError = "";
    renderBookingPauseControls();
    try {
        await fetchJson("/booking-pause", { method: "DELETE" });
        state.bookingPause = null;
        state.bookingPauseModalOpen = false;
        await loadState();
    } catch (error) {
        state.bookingPauseError = error.message;
    } finally {
        state.bookingPauseBusy = false;
        renderBookingPauseControls();
    }
}

function renderBookingPauseControls() {
    const banner = document.getElementById("booking-pause-banner");
    const shell = document.getElementById("booking-pause-modal-shell");
    const pause = state.bookingPause;

    if (banner) {
        banner.hidden = !pause;
        if (pause) {
            const active = bookingPauseIsActive(pause);
            banner.innerHTML = `
                <div>
                    <strong>${active ? "Bookings paused" : `Bookings pause ${escapeHtml(pauseDateLabel(pause.start_date))}`}</strong>
                    <span>Classes are skipped until bookings resume ${escapeHtml(pauseDateLabel(pause.resume_date))}. Existing reservations are unchanged.</span>
                </div>
                <div class="booking-pause-banner-actions">
                    <button data-booking-pause-open type="button">Change</button>
                    <button data-booking-pause-resume type="button">${active ? "Resume now" : "Cancel pause"}</button>
                </div>
            `;
            banner.querySelector("[data-booking-pause-open]")?.addEventListener("click", openBookingPauseModal);
            banner.querySelector("[data-booking-pause-resume]")?.addEventListener("click", resumeBookingsNow);
        } else {
            banner.innerHTML = "";
        }
    }

    syncBodyModalState(state.classes.find((entry) => entry.id === state.selectedClassId) || null);
    if (!shell) return;
    if (!state.bookingPauseModalOpen) {
        shell.innerHTML = "";
        return;
    }

    const today = localDateInputValue();
    const startDate = pause?.start_date || today;
    const resumeDate = pause?.resume_date || addDaysToDateKey(today, 7);
    shell.innerHTML = `
        <div class="auth-modal-overlay" id="booking-pause-modal-overlay">
            <section class="auth-modal booking-pause-modal" role="dialog" aria-modal="true" aria-labelledby="booking-pause-title">
                <div class="auth-modal-head">
                    <div>
                        <p class="detail-topline">Away dates</p>
                        <h2 id="booking-pause-title">${pause ? "Change booking pause" : "Pause bookings"}</h2>
                    </div>
                    <button class="auth-close" id="booking-pause-modal-close" type="button" aria-label="Close booking pause panel">×</button>
                </div>
                <p class="booking-pause-intro">BookGym will not book classes scheduled during this window. Existing gym reservations are not canceled.</p>
                ${state.bookingPauseError ? `<div class="notice-banner warning">${escapeHtml(state.bookingPauseError)}</div>` : ""}
                <form class="booking-pause-form" id="booking-pause-form">
                    <label class="auth-field">
                        <span>Pause starting</span>
                        <input name="start_date" type="date" value="${escapeHtml(startDate)}" required>
                    </label>
                    <label class="auth-field">
                        <span>Resume bookings on</span>
                        <input name="resume_date" type="date" value="${escapeHtml(resumeDate)}" min="${escapeHtml(addDaysToDateKey(today, 1))}" required>
                    </label>
                    <p class="auth-note">The resume date is the first class date BookGym can book again.</p>
                    <div class="auth-actions">
                        <button class="primary-link auth-button" type="submit" ${state.bookingPauseBusy ? "disabled" : ""}>${state.bookingPauseBusy ? "Saving..." : pause ? "Save changes" : "Pause bookings"}</button>
                        ${pause ? `<button class="subtle-link auth-button auth-danger" id="booking-pause-clear" type="button" ${state.bookingPauseBusy ? "disabled" : ""}>${bookingPauseIsActive(pause) ? "Resume now" : "Cancel scheduled pause"}</button>` : ""}
                    </div>
                </form>
            </section>
        </div>
    `;

    document.getElementById("booking-pause-modal-close")?.addEventListener("click", closeBookingPauseModal);
    document.getElementById("booking-pause-modal-overlay")?.addEventListener("click", (event) => {
        if (event.target?.id === "booking-pause-modal-overlay") closeBookingPauseModal();
    });
    document.getElementById("booking-pause-form")?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        await saveBookingPause(form.elements.start_date.value, form.elements.resume_date.value);
    });
    document.getElementById("booking-pause-clear")?.addEventListener("click", resumeBookingsNow);
}

function parseLocalDate(value) {
    const [year, month, day] = value.split("-").map(Number);
    return new Date(year, month - 1, day);
}

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function compareSeatLabels(left, right) {
    const leftValue = String(left || "").trim();
    const rightValue = String(right || "").trim();
    const leftNumber = Number.parseInt(leftValue, 10);
    const rightNumber = Number.parseInt(rightValue, 10);
    const leftIsNumber = /^[0-9]+$/.test(leftValue);
    const rightIsNumber = /^[0-9]+$/.test(rightValue);

    if (leftIsNumber && rightIsNumber && leftNumber !== rightNumber) {
        return leftNumber - rightNumber;
    }
    return leftValue.localeCompare(rightValue, undefined, { numeric: true, sensitivity: "base" });
}

function orderedSeatLabels(values) {
    const ordered = [];
    const seen = new Set();
    for (const value of values || []) {
        const label = String(value || "").trim();
        if (!label || seen.has(label)) {
            continue;
        }
        seen.add(label);
        ordered.push(label);
    }
    return ordered;
}

function sortSeatLabels(values) {
    return orderedSeatLabels(values)
        .sort(compareSeatLabels);
}

function classType(item) {
    const text = `${item.title || ""} ${item.category || ""}`.toLowerCase();
    if (text.includes("yoga")) return "Yoga";
    if (text.includes("bodypump")) return "BodyPump";
    if (text.includes("pilates")) return "Pilates";
    if (text.includes("zumba")) return "Zumba";
    return item.category || (item.title || "Other").split(" ")[0] || "Other";
}

function compactClassTitle(item) {
    const title = String(item.title || "Class").trim();
    return title.replace(/\bLevel\s+(\d+)\b/gi, "$1");
}

function availableTypes() {
    return Array.from(new Set(state.classes.map(classType))).sort();
}

function loadClassFilterPreference() {
    try {
        const raw = window.localStorage.getItem(CLASS_FILTER_STORAGE_KEY);
        if (!raw) {
            return;
        }
        const parsed = JSON.parse(raw);
        if (parsed?.mode === "custom" && Array.isArray(parsed.selectedTypes)) {
            state.typeFilterMode = "custom";
            state.typeFilters = new Set(parsed.selectedTypes.filter((value) => typeof value === "string"));
            return;
        }
        state.typeFilterMode = "all";
        state.typeFilters = new Set();
    } catch (_error) {
        state.typeFilterMode = "all";
        state.typeFilters = new Set();
    }
}

function saveClassFilterPreference(types = availableTypes()) {
    try {
        const mode = state.typeFilterMode === "custom" && state.typeFilters.size < types.length ? "custom" : "all";
        const payload = mode === "custom"
            ? { mode, selectedTypes: Array.from(state.typeFilters).sort() }
            : { mode: "all", selectedTypes: [] };
        window.localStorage.setItem(CLASS_FILTER_STORAGE_KEY, JSON.stringify(payload));
    } catch (_error) {
        // Ignore storage failures so the calendar still works in restricted browsers.
    }
}

function isMineClass(item) {
    const job = activeJobForClass(item.id);
    return (
        isWatchingJob(job) ||
        jobNeedsReview(job) ||
        isBooked(item) ||
        Boolean(weeklyScheduleItemForClass(item))
    );
}

function classTime(item) {
    if (!item.start_time) return "Time unavailable";
    return fmtTime.format(new Date(`2000-01-01T${item.start_time}`));
}

function activeJobForClass(classId) {
    return state.jobs.find((job) => (
        job.discovered_class_id === classId &&
        job.source === "manual_selection" &&
        jobStatus(job) !== "canceled"
    ));
}

function pausedJobForClass(classId) {
    return state.jobs.find((job) => (
        job.discovered_class_id === classId &&
        job.source === "manual_selection" &&
        jobStatus(job) === "canceled" &&
        String(job.last_message || "").startsWith("Bookings paused for classes from ")
    ));
}

function bookingPauseAffectsClass(item) {
    return Boolean(
        bookingPauseForClass(item) &&
        (weeklyScheduleItemForClass(item) || pausedJobForClass(item.id))
    );
}

function jobStatus(job) {
    return String(job?.status || "").toLowerCase();
}

function jobDisplayState(job) {
    if (!job) return null;

    const status = jobStatus(job);
    if (job.runnable || status === "pending" || status === "not_yet_open") {
        return {
            label: "Watching",
            kind: "selected",
            title: "Watching for booking",
            active: true,
        };
    }

    const terminalStates = {
        booked: {
            label: "Booked",
            kind: "booked",
            title: "Booked by BookGym",
        },
        waitlisted: {
            label: "Waitlisted",
            kind: "alert",
            title: "Waitlisted",
        },
        failed: {
            label: "Failed",
            kind: "alert",
            title: "Booking failed",
        },
        needs_confirmation: {
            label: "Needs confirmation",
            kind: "alert",
            title: "Needs confirmation",
        },
        dry_run: {
            label: "Dry run",
            kind: "subtle",
            title: "Dry run completed",
        },
    };

    return terminalStates[status] || {
        label: status ? status.replaceAll("_", " ") : "Not watching",
        kind: "subtle",
        title: status ? status.replaceAll("_", " ") : "Not watching",
    };
}

function isWatchingJob(job) {
    return Boolean(jobDisplayState(job)?.active);
}

function jobNeedsReview(job) {
    const display = jobDisplayState(job);
    return Boolean(job && display && !display.active && jobStatus(job) !== "booked");
}

function isBookedByBookGym(item) {
    return jobStatus(activeJobForClass(item.id)) === "booked";
}

function gymAvailability(item) {
    if (item.availability === "booked_at_gym" || item.is_booked_at_gym) return "Booked at gym";
    if (item.availability === "full") return "Full";
    if (item.availability === "available") return "Available";
    const text = String(item.raw_summary || "").toLowerCase();
    if (text.includes("booked")) return "Booked at gym";
    if (text.includes("book now (full)")) return "Full";
    if (text.includes("book now")) return "Available";
    return "Seen";
}

function availability(item) {
    if (isBookedByBookGym(item)) return "Booked by BookGym";
    return gymAvailability(item);
}

function isBookedAtGym(item) {
    return gymAvailability(item) === "Booked at gym";
}

function isBooked(item) {
    return isBookedByBookGym(item) || isBookedAtGym(item);
}

function bookedLabel(item) {
    if (isBookedByBookGym(item)) return "Booked by BookGym";
    if (isBookedAtGym(item)) return "Booked at gym";
    return "Booked";
}

function isFull(item) {
    return availability(item) === "Full";
}

function hasSeatMap(item) {
    return Boolean(item.has_seat_map && item.layout_id);
}

function spotText(item) {
    if (item.total_available_spots === null || item.total_available_spots === undefined) {
        return "";
    }
    if (item.total_spots === null || item.total_spots === undefined) {
        return `${item.total_available_spots} spots`;
    }
    return `${item.total_available_spots}/${item.total_spots} spots`;
}

function parseDateTime(value) {
    if (!value) return null;
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return null;
    return parsed;
}

function formatDateTime(value) {
    const parsed = parseDateTime(value);
    if (!parsed) return value || "Unknown";
    return parsed.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
        hour: "numeric",
        minute: "2-digit",
    });
}

function formatRelativeTime(value) {
    const parsed = parseDateTime(value);
    if (!parsed) return "never";
    const deltaMs = Date.now() - parsed.getTime();
    if (deltaMs < 60_000) return "just now";
    const deltaMinutes = Math.round(deltaMs / 60_000);
    if (deltaMinutes < 60) return `${deltaMinutes}m ago`;
    const deltaHours = Math.round(deltaMinutes / 60);
    if (deltaHours < 48) return `${deltaHours}h ago`;
    const deltaDays = Math.round(deltaHours / 24);
    return `${deltaDays}d ago`;
}

function bookingOpenValue(item) {
    if (item.site_booking_opens_at) return formatDateTime(item.site_booking_opens_at);
    if (item.expected_booking_opens_at) return formatDateTime(item.expected_booking_opens_at);
    return "Unknown";
}

function liveCheckStale(item) {
    if (!item.calendar_event_id) return false;
    const lastProbedAt = parseDateTime(item.last_probed_at);
    if (!lastProbedAt) return true;
    return (Date.now() - lastProbedAt.getTime()) >= PROBE_STALE_MS;
}

function hasFreshLiveSignal(item) {
    if (!item.calendar_event_id) return false;
    const lastProbedAt = parseDateTime(item.last_probed_at);
    if (!lastProbedAt) return false;
    return !liveCheckStale(item);
}

function bookingSignalLabel(item) {
    if (state.probeLoadingIds.has(item.id)) return "Refreshing now";
    if (hasFreshLiveSignal(item)) return "Site confirmed";
    return "Expected timing";
}

function bookingSignalSubtext(item) {
    if (state.probeLoadingIds.has(item.id)) {
        return "BookGym is checking the gym in the background.";
    }
    if (item.booking_probe_status === "bookable_now") {
        return "The latest gym check says this class can be booked right now.";
    }
    if (item.booking_probe_status === "not_yet_open" && item.last_probed_at) {
        return `Last refresh: ${formatRelativeTime(item.last_probed_at)}.`;
    }
    if (item.booking_probe_status === "blocked" && item.booking_probe_message) {
        return item.booking_probe_message;
    }
    if (!item.calendar_event_id) {
        return "Using the normal booking window for now.";
    }
    if (!item.last_probed_at) {
        return "Open this class and BookGym will fetch a live signal automatically.";
    }
    if (liveCheckStale(item)) {
        return `Last refresh: ${formatRelativeTime(item.last_probed_at)}. Opening this class refreshes it automatically.`;
    }
    return `Last refresh: ${formatRelativeTime(item.last_probed_at)}.`;
}

function bookingSignalPillKind(item) {
    if (state.probeLoadingIds.has(item.id)) return "alert";
    if (item.booking_probe_status === "bookable_now") return "selected";
    if (item.booking_probe_status === "blocked") return "full";
    if (item.booking_probe_status === "not_yet_open" && hasFreshLiveSignal(item)) return "alert";
    return "subtle";
}

function detailContextMessage(item, job) {
    if (isBookedAtGym(item)) {
        return "The gym already shows you on this class, so BookGym does not need to step in.";
    }
    if (isFull(item)) {
        return "The class looks full right now. If the gym opens a spot later, you can still select it.";
    }
    const pause = bookingPauseForClass(item);
    if (pause) {
        if (bookingPauseAffectsClass(item)) {
            return `This planned class is paused. Bookings resume ${pauseDateLabel(pause.resume_date)}.`;
        }
        return `Bookings are paused, so this class cannot be watched until ${pauseDateLabel(pause.resume_date)}.`;
    }
    return "";
}

function upcomingBookingAttemptMessage(item, job) {
    if (!job || !job.runnable || isBookedAtGym(item)) {
        return "";
    }
    const expectedRunAt = parseDateTime(job.expected_run_at);
    if (!expectedRunAt || expectedRunAt.getTime() <= Date.now()) {
        return "";
    }
    return `Booking attempt scheduled for ${formatDateTime(job.expected_run_at)}.`;
}

function scheduleSummaryRows(item, rowClass = "raw-summary-row") {
    const lines = String(item.raw_summary || "")
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
    const labels = ["Time", "Length", "Class", "Instructor", "Category", "Action"];
    if (!lines.length) {
        return '<p class="empty-inline">No raw schedule summary is stored for this class yet.</p>';
    }
    return lines.map((line, index) => `
        <div class="${escapeHtml(rowClass)}">
            <span>${labels[index] || `Line ${index + 1}`}</span>
            <strong>${escapeHtml(line)}</strong>
        </div>
    `).join("");
}

function activityTitle(event) {
    if (event.source_kind === "booking") {
        return event.status === "booked" ? "Booking succeeded" : "Booking activity";
    }
    if (event.event_type === "matched_class") return "Seen during discovery";
    if (event.event_type.startsWith("booking_probe_")) return "Live booking check";
    return event.event_type.replaceAll("_", " ");
}

function activityForClass(classId) {
    const discovery = state.discoveryEvents
        .filter((event) => event.discovered_class_id === classId)
        .map((event) => ({ ...event, source_kind: "discovery" }));
    const booking = state.bookingEvents
        .filter((event) => event.discovered_class_id === classId)
        .map((event) => ({ ...event, source_kind: "booking" }));
    return [...discovery, ...booking]
        .sort((a, b) => String(b.observed_at).localeCompare(String(a.observed_at)))
        .slice(0, 8);
}

function selectionDisabledReason(item, job) {
    if (job) return "";
    if (isBooked(item)) return bookedLabel(item);
    if (bookingPauseForClass(item)) return "Bookings paused";
    if (isFull(item)) return "Full right now";
    return "";
}

function itemMatchesSearch(item) {
    if (!state.searchQuery) return true;
    const haystack = `${item.title || ""} ${item.instructor_name || ""} ${item.category || ""}`.toLowerCase();
    return haystack.includes(state.searchQuery);
}

function itemMatchesTypeFilter(item) {
    return Boolean(state.searchQuery) || state.typeFilters.has(classType(item));
}

function itemMatchesScopeFilter(item) {
    if (state.calendarScopeMode !== "mine") {
        return true;
    }
    return isMineClass(item);
}

function visibleClasses() {
    return state.classes.filter((item) => (
        itemMatchesTypeFilter(item) &&
        itemMatchesSearch(item) &&
        itemMatchesScopeFilter(item)
    ));
}

function searchMatchedTypes() {
    if (!state.searchQuery) {
        return new Set();
    }

    return new Set(state.classes
        .filter((item) => itemMatchesSearch(item) && itemMatchesScopeFilter(item))
        .map(classType));
}

async function fetchJson(url, options) {
    const response = await fetch(url, options);
    if (response.status === 401 && !window.location.pathname.startsWith("/login")) {
        const next = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.assign(`/login?next=${next}`);
        throw new Error("Sign in required");
    }
    if (!response.ok) {
        let detail = response.statusText;
        try {
            const body = await response.json();
            detail = body.detail || detail;
        } catch (_error) {
            // Keep the standard status text if the response body is not JSON.
        }
        throw new Error(`${response.status} ${detail}`);
    }
    return response.json();
}

async function loadState() {
    const [classes, jobs, weeklySchedule, bookingPause, discoveryEvents, bookingEvents, authStatus, appUser] = await Promise.all([
        fetchJson("/discovered-classes"),
        fetchJson("/jobs"),
        fetchJson("/weekly-schedule"),
        fetchJson("/booking-pause"),
        fetchJson("/discovery-events"),
        fetchJson("/events"),
        fetchJson("/gym-auth/status"),
        fetchJson("/auth/me"),
    ]);

    state.classes = classes;
    state.jobs = jobs;
    state.weeklySchedule = weeklySchedule || { active: false, items: [] };
    state.bookingPause = bookingPause?.pause || null;
    state.discoveryEvents = discoveryEvents;
    state.bookingEvents = bookingEvents;
    state.authStatus = authStatus;
    state.appUser = appUser;

    for (const classId of Array.from(state.seatLayouts.keys())) {
        if (!state.classes.some((item) => item.id === classId)) {
            state.seatLayouts.delete(classId);
            state.seatLayoutErrors.delete(classId);
            state.seatPreferenceErrors.delete(classId);
            state.seatPreferenceSavingIds.delete(classId);
        }
    }

    if (!state.initializedMonth) {
        const today = new Date();
        state.monthDate = new Date(today.getFullYear(), today.getMonth(), 1);
        state.selectedWeekStartKey = weekStartKeyForDate(today);
        state.initializedMonth = true;
    }

    if (state.selectedClassId && !state.classes.some((item) => item.id === state.selectedClassId)) {
        state.selectedClassId = null;
    }

    syncTypeFilters();
    ensureSelectedWeek();
    render();

    const selectedItem = state.classes.find((item) => item.id === state.selectedClassId);
    if (selectedItem && hasSeatMap(selectedItem)) {
        void maybeLoadSeatLayout(selectedItem);
    }
}

function syncTypeFilters({ savePreference = true } = {}) {
    const types = availableTypes();
    if (state.typeFilterMode === "all" || !state.typeFilters.size) {
        state.typeFilterMode = "all";
        state.typeFilters = new Set(types);
    }

    for (const existing of Array.from(state.typeFilters)) {
        if (!types.includes(existing)) {
            state.typeFilters.delete(existing);
        }
    }

    if (!state.typeFilters.size) {
        state.typeFilterMode = "all";
        types.forEach((type) => state.typeFilters.add(type));
    }

    const filters = document.getElementById("filters");
    if (!filters) {
        if (savePreference) {
            saveClassFilterPreference(types);
        }
        return;
    }
    filters.innerHTML = "";

    const matchedTypes = searchMatchedTypes();

    types.forEach((type) => {
        const selected = state.typeFilters.has(type);
        const searchMatched = matchedTypes.has(type);
        const button = document.createElement("button");
        button.type = "button";
        button.className = [
            "chip-button",
            selected ? "active" : "",
            searchMatched && !selected ? "search-match" : "",
        ].filter(Boolean).join(" ");
        button.textContent = type;
        button.setAttribute("aria-pressed", String(selected));
        button.title = searchMatched && !selected
            ? `${type} matches the current search`
            : `Toggle ${type}`;
        button.addEventListener("click", () => {
            if (state.typeFilters.has(type) && state.typeFilters.size > 1) {
                state.typeFilters.delete(type);
            } else {
                state.typeFilters.add(type);
            }
            state.typeFilterMode = state.typeFilters.size === types.length ? "all" : "custom";
            syncTypeFilters();
            render();
        });
        filters.append(button);
    });

    if (savePreference) {
        saveClassFilterPreference(types);
    }
}

function renderScopeControl() {
    const control = document.getElementById("calendar-scope");
    if (!control) {
        return;
    }

    control.querySelectorAll("[data-scope-mode]").forEach((button) => {
        const active = button.dataset.scopeMode === state.calendarScopeMode;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", String(active));
    });
}

function monthDays(date) {
    const year = date.getFullYear();
    const month = date.getMonth();
    const first = new Date(year, month, 1);
    const last = new Date(year, month + 1, 0);
    const today = new Date();
    const viewingCurrentMonth = today.getFullYear() === year && today.getMonth() === month;
    const start = viewingCurrentMonth ? startOfWeek(today) : startOfWeek(first);
    const end = addDays(startOfWeek(last), 6);
    const days = [];

    for (let day = start; day <= end; day = addDays(day, 1)) {
        days.push(day);
    }

    return days;
}


function addDays(value, days) {
    return new Date(value.getFullYear(), value.getMonth(), value.getDate() + days);
}

function startOfWeek(value) {
    return addDays(value, -value.getDay());
}

function dateKey(value) {
    const month = String(value.getMonth() + 1).padStart(2, "0");
    const day = String(value.getDate()).padStart(2, "0");
    return `${value.getFullYear()}-${month}-${day}`;
}

function weekStartKeyForDate(value) {
    return dateKey(startOfWeek(value));
}

function selectedWeekStartDate() {
    if (state.selectedWeekStartKey) {
        return parseLocalDate(state.selectedWeekStartKey);
    }
    return startOfWeek(new Date());
}

function weekRangeLabel(weekStart) {
    const weekEnd = addDays(weekStart, 6);
    const sameMonth = weekStart.getFullYear() === weekEnd.getFullYear()
        && weekStart.getMonth() === weekEnd.getMonth();
    if (sameMonth) {
        return `${fmtShortDate.format(weekStart)} - ${weekEnd.getDate()}, ${weekEnd.getFullYear()}`;
    }
    return `${fmtShortDate.format(weekStart)} - ${fmtDate.format(weekEnd)}`;
}

function pluralize(count, singular, plural = `${singular}s`) {
    return `${count} ${count === 1 ? singular : plural}`;
}

function sortClassesBySchedule(items) {
    return [...items].sort((a, b) => (
        String(a.class_date).localeCompare(String(b.class_date))
        || String(a.start_time || "").localeCompare(String(b.start_time || ""))
        || String(a.title || "").localeCompare(String(b.title || ""))
    ));
}

function weekClasses(weekStart, visibleOnly = true) {
    const weekKey = dateKey(weekStart);
    const source = visibleOnly ? visibleClasses() : state.classes;
    return sortClassesBySchedule(source.filter((item) => (
        weekStartKeyForDate(parseLocalDate(item.class_date)) === weekKey
    )));
}

function weekScheduleClasses(weekStart, visibleOnly = true) {
    return weekClasses(weekStart, visibleOnly).filter((item) => (
        activeJobForClass(item.id) || isBookedAtGym(item) || weeklyScheduleItemForClass(item)
    ));
}

function watchedClassesForWeek(weekStart, visibleOnly = true) {
    return weekClasses(weekStart, visibleOnly).filter((item) => isWatchingJob(activeJobForClass(item.id)));
}

function reviewClassesForWeek(weekStart, visibleOnly = true) {
    return weekClasses(weekStart, visibleOnly).filter((item) => jobNeedsReview(activeJobForClass(item.id)));
}

function continuableClassesForWeek(weekStart, visibleOnly = true) {
    return weekClasses(weekStart, visibleOnly).filter((item) => {
        const job = activeJobForClass(item.id);
        return isWatchingJob(job) || isBookedByBookGym(item);
    });
}

function clearableWatchedClassesForWeek(weekStart, visibleOnly = true) {
    return weekClasses(weekStart, visibleOnly).filter((item) => {
        const job = activeJobForClass(item.id);
        return job && !isBookedByBookGym(item);
    });
}

function normalizedScheduleText(value) {
    return String(value || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

function scheduleTimeKey(value) {
    return value ? String(value).slice(0, 5) : "any";
}

function scheduleScopeKeyForClass(item) {
    const weekday = parseLocalDate(item.class_date).getDay();
    return `${weekday}:${scheduleTimeKey(item.start_time)}:${normalizedScheduleText(item.title)}`;
}

function weeklyScheduleItems() {
    return state.weeklySchedule?.items || [];
}

function weeklyScheduleSkips() {
    return state.weeklySchedule?.skips || [];
}

function weeklyScheduleItemForClass(item) {
    const scopeKey = scheduleScopeKeyForClass(item);
    return weeklyScheduleItems().find((entry) => entry.scope_key === scopeKey) || null;
}

function weeklyScheduleSkipForClass(item) {
    const scopeKey = scheduleScopeKeyForClass(item);
    const weekStart = weekStartKeyForDate(parseLocalDate(item.class_date));
    return weeklyScheduleSkips().find((entry) => (
        entry.scope_key === scopeKey && entry.week_start === weekStart
    )) || null;
}

function weeklyScheduleItemSummary(item) {
    const weekday = WEEKDAY_LABELS[item.weekday] || "Weekly";
    const time = item.start_time ? item.start_time.slice(0, 5) : "any time";
    return `${weekday} ${time}`;
}

function weeklyScheduleItemsMissingFromWeek(weekStart) {
    const currentWeekScopeKeys = new Set(
        weekClasses(weekStart, false).map(scheduleScopeKeyForClass)
    );
    return weeklyScheduleItems().filter((item) => !currentWeekScopeKeys.has(item.scope_key));
}

function displayedWeekKeys() {
    return new Set(monthDays(state.monthDate).map((day) => weekStartKeyForDate(day)));
}

function defaultWeekStartKey(displayedKeys = null) {
    const todayKey = weekStartKeyForDate(new Date());
    if (!displayedKeys || displayedKeys.has(todayKey)) {
        return todayKey;
    }

    const visibleItem = visibleClasses().find((item) => (
        displayedKeys.has(weekStartKeyForDate(parseLocalDate(item.class_date)))
    ));
    if (visibleItem) {
        return weekStartKeyForDate(parseLocalDate(visibleItem.class_date));
    }

    const anyItem = state.classes.find((item) => (
        displayedKeys.has(weekStartKeyForDate(parseLocalDate(item.class_date)))
    ));
    if (anyItem) {
        return weekStartKeyForDate(parseLocalDate(anyItem.class_date));
    }

    return weekStartKeyForDate(state.monthDate);
}

function ensureSelectedWeek({ requireDisplayed = false } = {}) {
    const selectedItem = state.classes.find((entry) => entry.id === state.selectedClassId);
    if (selectedItem) {
        state.selectedWeekStartKey = weekStartKeyForDate(parseLocalDate(selectedItem.class_date));
        return;
    }

    if (state.selectedWeekStartKey && (!requireDisplayed || displayedWeekKeys().has(state.selectedWeekStartKey))) {
        return;
    }

    state.selectedWeekStartKey = defaultWeekStartKey(requireDisplayed ? displayedWeekKeys() : null);
}

function renderPeriodControls() {
    const label = document.getElementById("month-label");
    const previous = document.getElementById("prev-month");
    const next = document.getElementById("next-month");
    if (label) {
        label.textContent = fmtMonth.format(state.monthDate);
    }
    if (previous) {
        previous.title = "Previous month";
    }
    if (next) {
        next.title = "Next month";
    }
}

function movePeriod(delta) {
    moveMonth(delta);
}

function moveMonth(delta) {
    state.monthDate = new Date(state.monthDate.getFullYear(), state.monthDate.getMonth() + delta, 1);
    state.selectedClassId = null;
    state.seatEditorOpenClassId = null;
    state.weekClearConfirmKey = "";
    state.selectedWeekStartKey = defaultWeekStartKey(displayedWeekKeys());
    render();
}

function selectWeek(day) {
    state.selectedWeekStartKey = weekStartKeyForDate(day);
    state.selectedClassId = null;
    state.seatEditorOpenClassId = null;
    state.weekClearConfirmKey = "";
    render();
}

async function selectClass(item) {
    state.selectedClassId = item.id;
    state.selectedWeekStartKey = weekStartKeyForDate(parseLocalDate(item.class_date));
    state.weekClearConfirmKey = "";
    render();
    await maybeAutoProbe(item.id);
    const refreshedItem = state.classes.find((entry) => entry.id === item.id) || item;
    void maybeLoadSeatLayout(refreshedItem);
}

function syncDetailPanelScroll(panel, scopeKey) {
    if (state.detailScopeKey === scopeKey) {
        return;
    }
    state.detailScopeKey = scopeKey;
    panel.scrollTop = 0;
}

function renderCalendar() {
    ensureSelectedWeek({ requireDisplayed: true });
    renderPeriodControls();
    renderMonthCalendar();
}


function renderMonthCalendar() {
    const grid = document.getElementById("calendar-grid");
    if (!grid) {
        return;
    }
    ensureSelectedWeek({ requireDisplayed: true });
    grid.innerHTML = "";

    const currentMonth = state.monthDate.getMonth();
    const selectedWeekKey = state.selectedWeekStartKey;
    const byDate = new Map();
    visibleClasses().forEach((item) => {
        const list = byDate.get(item.class_date) || [];
        list.push(item);
        byDate.set(item.class_date, list);
    });

    monthDays(state.monthDate).forEach((day) => {
        const cell = document.createElement("div");
        const weekKey = weekStartKeyForDate(day);
        const dayKey = dateKey(day);
        cell.className = [
            "day",
            day.getMonth() === currentMonth ? "" : "outside",
            bookingPauseForDate(dayKey) ? "booking-pause-day" : "",
            weekKey === selectedWeekKey ? "selected-week" : "",
            day.getDay() === 0 ? "week-start" : "",
            day.getDay() === 6 ? "week-end" : "",
        ].filter(Boolean).join(" ");
        cell.dataset.date = dayKey;
        cell.dataset.weekStart = weekKey;
        cell.addEventListener("click", () => {
            selectWeek(day);
        });

        const header = document.createElement("div");
        header.className = "day-number";
        header.textContent = String(day.getDate());
        cell.append(header);

        const items = (byDate.get(dayKey) || [])
            .sort((a, b) => String(a.start_time).localeCompare(String(b.start_time)));

        items.forEach((item) => {
            const job = activeJobForClass(item.id);
            const booked = isBooked(item);
            const paused = bookingPauseAffectsClass(item);
            const jobState = jobDisplayState(job);
            const selected = isWatchingJob(job);
            const review = jobNeedsReview(job);
            const recurring = Boolean(weeklyScheduleItemForClass(item));
            const recurringSkipped = recurring && Boolean(weeklyScheduleSkipForClass(item));
            const signalClass = state.probeLoadingIds.has(item.id)
                ? "signal-checking"
                : hasFreshLiveSignal(item)
                    ? "signal-fresh"
                    : "signal-expected";
            const tile = document.createElement("button");
            tile.type = "button";
            tile.className = [
                "class-chip",
                selected ? "selected" : "",
                review ? "needs-review" : "",
                booked ? "booked-at-gym" : "",
                paused ? "booking-paused" : "",
                isFull(item) ? "full" : "",
                recurring ? "recurring" : "",
                recurringSkipped ? "recurring-skipped" : "",
                state.selectedClassId === item.id ? "active" : "",
                signalClass,
            ].filter(Boolean).join(" ");
            tile.setAttribute(
                "aria-label",
                [
                    classTime(item),
                    spotText(item),
                    item.title,
                    item.instructor_name,
                    booked ? bookedLabel(item) : paused ? "Paused" : jobState ? jobState.label : isFull(item) ? "Full" : recurringSkipped ? "Skipped" : recurring ? "Recurring" : "Open",
                ].filter(Boolean).join(", "),
            );
            tile.innerHTML = `
                <div class="class-time-row">
                    <span class="class-time">${classTime(item)}</span>
                    <span class="class-spot">${spotText(item) || classType(item)}</span>
                </div>
                <span class="class-title class-title-desktop">${escapeHtml(item.title)}</span>
                <span class="class-title class-title-mobile">${escapeHtml(compactClassTitle(item))}</span>
                <span class="class-meta">${escapeHtml(item.instructor_name || "Instructor not listed")} • ${escapeHtml(item.category || classType(item))}</span>
                <div class="class-flag-row">
                    <span class="mini-pill ${booked ? "booked" : paused ? "subtle" : jobState ? jobState.kind : isFull(item) ? "full" : recurringSkipped ? "subtle" : recurring ? "selected" : "subtle"}">${escapeHtml(booked ? bookedLabel(item) : paused ? "Paused" : jobState ? jobState.label : isFull(item) ? "Full" : recurringSkipped ? "Skipped" : recurring ? "Recurring" : "Open")}</span>
                </div>
            `;
            tile.addEventListener("click", async (event) => {
                event.stopPropagation();
                await selectClass(item);
            });
            cell.append(tile);
        });

        grid.append(cell);
    });

    observeVisibleCalendarWeeks();
}

function weekIsEntirelyPast(weekKey) {
    const weekEnd = addDays(parseLocalDate(weekKey), 6);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    return weekEnd < today;
}

async function refreshVisibleWeek(weekKey) {
    const requestedAt = state.visibleWeekRefreshRequestedAt.get(weekKey) || 0;
    if (Date.now() - requestedAt < VISIBLE_WEEK_REFRESH_MS || weekIsEntirelyPast(weekKey)) {
        return;
    }

    state.visibleWeekRefreshRequestedAt.set(weekKey, Date.now());
    try {
        const result = await fetchJson(`/weeks/${weekKey}/refresh`, { method: "POST" });
        if (result.status === "refreshed") {
            await loadState();
        }
    } catch (_error) {
        // The cached calendar remains usable; a later view can retry after the cooldown.
    }
}

function observeVisibleCalendarWeeks() {
    state.visibleWeekRefreshObserver?.disconnect();
    state.visibleWeekRefreshObserver = null;

    if (!("IntersectionObserver" in window)) {
        const currentWeek = weekStartKeyForDate(new Date());
        void refreshVisibleWeek(currentWeek);
        return;
    }

    const observer = new IntersectionObserver((entries) => {
        const visibleWeekKeys = new Set();
        entries.forEach((entry) => {
            if (entry.isIntersecting && entry.target.dataset.weekStart) {
                visibleWeekKeys.add(entry.target.dataset.weekStart);
            }
        });
        visibleWeekKeys.forEach((weekKey) => {
            void refreshVisibleWeek(weekKey);
        });
    }, { threshold: 0.15 });

    state.visibleWeekRefreshObserver = observer;
    document.querySelectorAll("#calendar-grid .day[data-week-start]").forEach((cell) => {
        observer.observe(cell);
    });
}

async function maybeAutoProbe(classId) {
    const item = state.classes.find((entry) => entry.id === classId);
    if (!item || !item.calendar_event_id || !liveCheckStale(item) || state.probeLoadingIds.has(classId)) {
        return;
    }

    state.probeLoadingIds.add(classId);
    state.notices.set(classId, { kind: "info", message: "Refreshing the gym timing in the background..." });
    renderDetails();

    try {
        await fetchJson(`/discovered-classes/${classId}/probe-booking`, { method: "POST" });
        await loadState();
        state.selectedClassId = classId;
        state.notices.set(classId, { kind: "success", message: "Gym timing refreshed." });
        renderDetails();
    } catch (error) {
        state.notices.set(classId, { kind: "error", message: `Could not refresh the gym timing: ${error.message}` });
        renderDetails();
    } finally {
        state.probeLoadingIds.delete(classId);
        renderDetails();
    }
}

async function maybeLoadSeatLayout(item, force = false) {
    if (!item || !hasSeatMap(item)) {
        return;
    }
    if (state.seatLayoutLoadingIds.has(item.id)) {
        return;
    }
    if (!force && state.seatLayouts.has(item.id)) {
        return;
    }

    state.seatLayoutLoadingIds.add(item.id);
    state.seatLayoutErrors.delete(item.id);
    renderDetails();

    try {
        const payload = await fetchJson(`/discovered-classes/${item.id}/seat-layout`);
        state.seatLayouts.set(item.id, payload);
        state.seatLayoutErrors.delete(item.id);
    } catch (error) {
        state.seatLayoutErrors.set(item.id, error.message);
    } finally {
        state.seatLayoutLoadingIds.delete(item.id);
        renderDetails();
    }
}


async function toggleBooking(item, job) {
    const classId = item.id;
    state.selectionLoadingIds.add(classId);
    const wasWatching = isWatchingJob(job);
    renderDetails();

    try {
        if (job) {
            await fetchJson(`/jobs/${job.id}/deselect`, { method: "POST" });
            state.notices.set(classId, {
                kind: "success",
                message: wasWatching
                    ? "BookGym is no longer watching this class."
                    : "BookGym cleared the retained booking status for this class.",
            });
        } else {
            await fetchJson(`/discovered-classes/${classId}/select`, { method: "POST" });
            state.notices.set(classId, {
                kind: "success",
                message: PREVIEW_MODE
                    ? "Preview selection saved only in the private database copy."
                    : "BookGym will watch this class for you.",
            });
        }
        await loadState();
        state.selectedClassId = classId;
        renderDetails();
    } catch (error) {
        state.notices.set(classId, { kind: "error", message: error.message });
        renderDetails();
    } finally {
        state.selectionLoadingIds.delete(classId);
        renderDetails();
    }
}

function renderUsefulDetails(item) {
    const rows = [
        { label: "Spots", value: spotText(item) || "Spot count unavailable" },
        { label: "Site booking status", value: item.gym_booking_status || "Nothing special listed" },
        { label: "Seat or spot info", value: item.gym_booking_spot_labels || "No seat note from the site" },
        { label: "Site actions", value: item.gym_booking_actions || "No explicit site actions listed" },
        { label: "Seat map", value: hasSeatMap(item) ? `Layout ${item.layout_id}` : "Not exposed for this class" },
        { label: "Seat label", value: item.seat_title || "Spot" },
        { label: "First seen", value: formatDateTime(item.first_seen_at) },
        { label: "Last refreshed", value: formatDateTime(item.last_seen_at) },
    ];
    return rows.map((row) => `
        <div class="meta-row">
            <span>${escapeHtml(row.label)}</span>
            <strong>${escapeHtml(row.value)}</strong>
        </div>
    `).join("");
}

function renderTechnicalDetails(item) {
    const rows = [
        { label: "Event ID", value: item.calendar_event_id || "unknown" },
        { label: "Layout ID", value: item.layout_id || "unknown" },
        { label: "Booking type ID", value: item.booking_type_id || "unknown" },
        { label: "Resource ID", value: item.resource_id || "unknown" },
        { label: "Seat map exposed", value: item.can_show_layout === null || item.can_show_layout === undefined ? "unknown" : String(item.can_show_layout) },
        { label: "Can book event", value: item.can_book_event === null || item.can_book_event === undefined ? "unknown" : String(item.can_book_event) },
        { label: "Source key", value: item.source_key || "unknown" },
    ];
    return rows.map((row) => `
        <div class="meta-row">
            <span>${escapeHtml(row.label)}</span>
            <strong>${escapeHtml(row.value)}</strong>
        </div>
    `).join("");
}

function renderBookingDetails(item, upcomingAttemptMessage) {
    const rows = [
        { label: "Booking opens", value: bookingOpenValue(item) },
        { label: "Gym check", value: bookingSignalLabel(item) },
        { label: "Latest signal", value: bookingSignalSubtext(item) },
    ];
    if (upcomingAttemptMessage) {
        rows.push({ label: "Next attempt", value: upcomingAttemptMessage });
    }

    return rows.map((row) => `
        <div class="meta-row">
            <span>${escapeHtml(row.label)}</span>
            <strong>${escapeHtml(row.value)}</strong>
        </div>
    `).join("");
}

function upcomingBookingAttemptBrief(job) {
    const expectedRunAt = parseDateTime(job?.expected_run_at);
    if (!expectedRunAt || expectedRunAt.getTime() <= Date.now()) {
        return "";
    }
    return `Attempts ${expectedRunAt.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
    })}`;
}

function bookingPlanState(item, job, disabledReason, loading) {
    if (isBooked(item)) {
        const bookedByBookGym = isBookedByBookGym(item);
        return {
            title: bookedByBookGym ? "Booked by BookGym" : "Already booked at gym",
            pillLabel: "Booked",
            pillKind: "booked",
            actionLabel: "",
            actionKind: "disabled",
            actionDisabled: true,
            copy: bookedByBookGym
                ? "BookGym recorded this booking."
                : detailContextMessage(item, job),
        };
    }
    if (job) {
        const jobState = jobDisplayState(job);
        const watching = isWatchingJob(job);
        return {
            title: jobState.title,
            pillLabel: jobState.label,
            pillKind: jobState.kind,
            actionLabel: loading ? "Saving..." : watching ? "Stop watching" : "Clear status",
            actionKind: "secondary",
            actionDisabled: loading,
            copy: watching ? upcomingBookingAttemptBrief(job) : job.last_message,
        };
    }
    if (disabledReason) {
        return {
            title: disabledReason,
            pillLabel: disabledReason,
            pillKind: isFull(item) ? "full" : "subtle",
            actionLabel: "",
            actionKind: "disabled",
            actionDisabled: true,
            copy: detailContextMessage(item, job),
        };
    }
    return {
        title: "Ready to watch",
        pillLabel: "Available",
        pillKind: "subtle",
        actionLabel: loading ? "Saving..." : "Watch",
        actionKind: "primary",
        actionDisabled: loading,
        copy: "No booking job yet.",
    };
}

function recurringScheduleText(item) {
    const weekday = WEEKDAY_LABELS[parseLocalDate(item.class_date).getDay()] || "Weekly";
    return `${weekday} at ${classTime(item)}`;
}

function renderPlanAction(plan) {
    if (!plan.actionLabel) {
        return "";
    }
    return `<button
        class="plan-action ${escapeHtml(plan.actionKind)}"
        id="selection-toggle"
        type="button"
        ${plan.actionDisabled ? "disabled" : ""}
    >${escapeHtml(plan.actionLabel)}</button>`;
}


function renderPlanSection(item, job, disabledReason, loading) {
    const plan = bookingPlanState(item, job, disabledReason, loading);
    const scheduleItem = weeklyScheduleItemForClass(item);
    const recurringLoading = state.weekActionLoading === `recurring:${item.id}`;
    const skipLoading = state.weekActionLoading === `skip:${item.id}`;
    const recurringActive = Boolean(scheduleItem);
    const recurringSkipped = recurringActive && Boolean(weeklyScheduleSkipForClass(item));
    const recurringStatusLabel = recurringLoading ? "Saving" : recurringActive ? "On" : "Off";
    return `
        <section class="detail-section plan-panel">
            <p class="plan-kicker">Plan</p>
            <div class="plan-row">
                <div class="plan-row-copy">
                    <span>Booking</span>
                    <strong>${escapeHtml(plan.title)}</strong>
                    ${plan.copy ? `<p>${escapeHtml(plan.copy)}</p>` : ""}
                </div>
                <div class="plan-row-actions">
                    <span class="status-pill ${escapeHtml(plan.pillKind)}">${escapeHtml(plan.pillLabel)}</span>
                    ${renderPlanAction(plan)}
                </div>
            </div>
            <div class="plan-row">
                <div class="plan-row-copy">
                    <span>Recurring</span>
                    <strong>${recurringActive ? "Weekly" : "Off"}</strong>
                    <p>${escapeHtml(recurringScheduleText(item))}${recurringSkipped ? " · skipped this week" : ""}</p>
                </div>
                <div class="plan-row-actions">
                    ${recurringActive ? `<button
                        class="plan-action secondary recurring-skip-toggle ${recurringSkipped ? "active" : ""}"
                        id="recurring-skip-toggle"
                        type="button"
                        ${recurringLoading || skipLoading ? "disabled" : ""}
                    >${skipLoading ? "Saving..." : recurringSkipped ? "Resume this week" : "Skip this week"}</button>` : ""}
                    <button
                        class="plan-action recurring-toggle ${recurringActive ? "active" : ""}"
                        id="recurring-toggle"
                        type="button"
                        aria-label="${recurringActive ? "Turn recurring off" : "Turn recurring on"}"
                        aria-pressed="${recurringActive ? "true" : "false"}"
                        ${recurringLoading || skipLoading ? "disabled" : ""}
                    >${escapeHtml(recurringStatusLabel)}</button>
                </div>
            </div>
        </section>
    `;
}

function renderSeatSummary(layout) {
    const summary = layout.summary || {};
    const preferences = layout.seat_preferences || {};
    const includedSeats = orderedSeatLabels(preferences.included_seats || []);
    const excludedSeats = sortSeatLabels(preferences.excluded_seats || []);
    const pills = [
        { label: `${summary.available || 0} open`, kind: "subtle" },
        { label: `${summary.booked || 0} booked`, kind: "occupied" },
        { label: `${summary.unavailable || 0} unavailable`, kind: "full" },
        { label: `${summary.non_bookable || 0} fixed`, kind: "subtle" },
    ];
    if (includedSeats.length) {
        pills.push({ label: `${includedSeats.length} included`, kind: "included" });
    }
    if (excludedSeats.length) {
        pills.push({ label: `${excludedSeats.length} excluded`, kind: "excluded" });
    }
    if ((preferences.selection_mode || "preferences") === "any") {
        pills.push({ label: "any seat", kind: "included" });
    }
    if ((preferences.selection_mode || "preferences") === "strict") {
        pills.push({ label: "only these", kind: "included" });
    }
    return `
        <div class="status-row seat-status-row">
            ${pills.map((pill) => `<span class="status-pill ${escapeHtml(pill.kind)}">${escapeHtml(pill.label)}</span>`).join("")}
        </div>
    `;
}

function seatPreferenceScopeCopy(item, layout) {
    const source = seatPreferenceSource(layout);
    const scope = layout?.class_slot_preference_scope || layout?.preference_scope || {};
    if (source.source_type === "explicit") {
        return `Saved for ${scope.category_label || item.title}.`;
    }
    return `Inferred from ${source.label || "default seats"}.`;
}

function seatPreferencesForLayout(layout) {
    const preferences = layout?.seat_preferences || {};
    const includedSeats = orderedSeatLabels(preferences.included_seats || []);
    const excludedSeats = sortSeatLabels(preferences.excluded_seats || []);
    return {
        includedSeats,
        excludedSeats,
        selectionMode: seatPreferenceSelectionMode(
            includedSeats,
            excludedSeats,
            preferences.selection_mode || "",
        ),
    };
}

function seatPreferenceSource(layout) {
    return layout?.seat_preference_source || {};
}

function seatPreferenceState(layout, cell) {
    if (!layout || cell.kind !== "available") {
        return "";
    }
    const { includedSeats, excludedSeats } = seatPreferencesForLayout(layout);
    if (includedSeats.includes(cell.title)) {
        return "included";
    }
    if (excludedSeats.includes(cell.title)) {
        return "excluded";
    }
    return "";
}

function classSlotScopeKey(layout) {
    return String(layout?.class_slot_preference_scope?.scope_key || "");
}

function seatPreferenceSelectionMode(includedSeats, excludedSeats, explicitMode = "") {
    if (explicitMode === "any") {
        return "any";
    }
    if (explicitMode === "strict" && includedSeats.length) {
        return "strict";
    }
    if (explicitMode === "preferences" && (includedSeats.length || excludedSeats.length)) {
        return "preferences";
    }
    if (includedSeats.length) {
        return "strict";
    }
    return excludedSeats.length ? "preferences" : "any";
}

function seatSelectionModeForListUpdate(currentMode, includedSeats, excludedSeats) {
    if (currentMode === "preferences" && (includedSeats.length || excludedSeats.length)) {
        return "preferences";
    }
    if (currentMode === "strict" && includedSeats.length) {
        return "strict";
    }
    if (includedSeats.length) {
        return "strict";
    }
    return excludedSeats.length ? "preferences" : "any";
}

function seatPreferenceSourceCandidates(layout) {
    return layout?.seat_preference_sources || [];
}

function findSeatPreferenceSource(layout, sourceKey) {
    return seatPreferenceSourceCandidates(layout)
        .find((source) => String(source.source_key || "") === String(sourceKey || ""));
}

function mergeSeatPreferencePayload(layout, payload) {
    if (!layout || !payload) {
        return layout;
    }
    return {
        ...layout,
        preference_scope: payload.preference_scope || layout.preference_scope,
        seat_preferences: payload.seat_preferences || layout.seat_preferences,
        seat_preference_source: payload.seat_preference_source || layout.seat_preference_source,
        seat_preference_sources: payload.seat_preference_sources || layout.seat_preference_sources,
        class_slot_preference_scope: payload.class_slot_preference_scope || layout.class_slot_preference_scope,
        class_slot_seat_preferences: payload.class_slot_seat_preferences || layout.class_slot_seat_preferences,
        room_category_preference_scope: payload.room_category_preference_scope || layout.room_category_preference_scope,
        room_category_seat_preferences: payload.room_category_seat_preferences || layout.room_category_seat_preferences,
    };
}

function updateSeatPreferenceLayouts(scopeKey, payload) {
    const targetScopeKey = String(scopeKey || payload?.class_slot_preference_scope?.scope_key || "");
    for (const [classId, layout] of state.seatLayouts.entries()) {
        if (targetScopeKey && classSlotScopeKey(layout) !== targetScopeKey) {
            continue;
        }
        state.seatLayouts.set(classId, mergeSeatPreferencePayload(layout, payload));
    }
}

function buildOptimisticSeatPreferencePayload(layout, includedSeats, excludedSeats, selectionMode) {
    const scope = layout?.class_slot_preference_scope || layout?.preference_scope || {};
    let normalizedIncluded = orderedSeatLabels(includedSeats);
    let normalizedExcluded = sortSeatLabels(excludedSeats);
    const normalizedMode = seatPreferenceSelectionMode(
        normalizedIncluded,
        normalizedExcluded,
        selectionMode,
    );
    if (normalizedMode === "any") {
        normalizedIncluded = [];
        normalizedExcluded = [];
    }
    const preferences = {
        included_seats: normalizedIncluded,
        excluded_seats: normalizedExcluded,
        selection_mode: normalizedMode,
        has_saved_preferences: true,
    };
    return {
        preference_scope: scope,
        seat_preferences: preferences,
        seat_preference_source: {
            source_key: scope.scope_key || "",
            source_type: "explicit",
            source_kind: "class_slot",
            scope_key: scope.scope_key || "",
            label: "Saved for this class slot",
            exact_match: true,
        },
        seat_preference_sources: layout?.seat_preference_sources || [],
        class_slot_preference_scope: scope,
        class_slot_seat_preferences: preferences,
        room_category_preference_scope: layout?.room_category_preference_scope,
        room_category_seat_preferences: layout?.room_category_seat_preferences,
    };
}

function snapshotSeatPreferenceLayouts(scopeKey) {
    const snapshots = new Map();
    const targetScopeKey = String(scopeKey || "");

    for (const [classId, layout] of state.seatLayouts.entries()) {
        if (targetScopeKey && classSlotScopeKey(layout) !== targetScopeKey) {
            continue;
        }
        snapshots.set(classId, layout);
    }

    return snapshots;
}

function restoreSeatPreferenceLayouts(snapshots) {
    for (const [classId, snapshot] of snapshots.entries()) {
        state.seatLayouts.set(classId, snapshot);
    }
}

async function saveSeatPreferences(item, includedSeats, excludedSeats, selectionMode = "") {
    const layout = state.seatLayouts.get(item.id);
    if (!layout) {
        return;
    }

    let normalizedIncluded = orderedSeatLabels(includedSeats);
    let normalizedExcluded = sortSeatLabels(excludedSeats);
    const normalizedMode = seatPreferenceSelectionMode(
        normalizedIncluded,
        normalizedExcluded,
        selectionMode,
    );
    if (normalizedMode === "any") {
        normalizedIncluded = [];
        normalizedExcluded = [];
    }
    const scopeKey = classSlotScopeKey(layout);
    const snapshots = snapshotSeatPreferenceLayouts(scopeKey);

    updateSeatPreferenceLayouts(
        scopeKey,
        buildOptimisticSeatPreferencePayload(
            layout,
            normalizedIncluded,
            normalizedExcluded,
            normalizedMode,
        ),
    );
    state.seatPreferenceSavingIds.add(item.id);
    state.seatPreferenceErrors.delete(item.id);
    renderDetails();

    try {
        const payload = await fetchJson(`/discovered-classes/${item.id}/seat-preferences`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                included_seats: normalizedIncluded,
                excluded_seats: normalizedExcluded,
                scope_kind: "class_slot",
                selection_mode: normalizedMode,
            }),
        });
        updateSeatPreferenceLayouts(classSlotScopeKey({ class_slot_preference_scope: payload.class_slot_preference_scope }), payload);
        state.seatPreferenceErrors.delete(item.id);
    } catch (error) {
        restoreSeatPreferenceLayouts(snapshots);
        state.seatPreferenceErrors.set(item.id, error.message);
    } finally {
        state.seatPreferenceSavingIds.delete(item.id);
        renderDetails();
    }
}

async function resetSeatPreferencesToInferred(item) {
    const layout = state.seatLayouts.get(item.id);
    if (!layout || state.seatPreferenceSavingIds.has(item.id)) {
        return;
    }

    const scopeKey = classSlotScopeKey(layout);
    if (!scopeKey) {
        return;
    }
    const snapshots = snapshotSeatPreferenceLayouts(scopeKey);

    state.seatPreferenceSavingIds.add(item.id);
    state.seatPreferenceErrors.delete(item.id);
    renderDetails();

    try {
        const payload = await fetchJson(`/discovered-classes/${item.id}/seat-preferences`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                included_seats: [],
                excluded_seats: [],
                reset_to_inferred: true,
            }),
        });
        updateSeatPreferenceLayouts(classSlotScopeKey({ class_slot_preference_scope: payload.class_slot_preference_scope }), payload);
        state.seatPreferenceErrors.delete(item.id);
    } catch (error) {
        restoreSeatPreferenceLayouts(snapshots);
        state.seatPreferenceErrors.set(item.id, error.message);
    } finally {
        state.seatPreferenceSavingIds.delete(item.id);
        renderDetails();
    }
}

async function copySeatPreferenceSource(item, sourceKey) {
    const layout = state.seatLayouts.get(item.id);
    const source = findSeatPreferenceSource(layout, sourceKey);
    if (!source) {
        return;
    }
    await saveSeatPreferences(
        item,
        source.included_seats || [],
        source.excluded_seats || [],
        source.selection_mode || "preferences",
    );
}

async function updateSeatBookingPolicy(item, selectionMode) {
    if (state.seatPreferenceSavingIds.has(item.id)) {
        return;
    }

    const layout = state.seatLayouts.get(item.id);
    if (!layout) {
        return;
    }

    const { includedSeats, excludedSeats } = seatPreferencesForLayout(layout);
    if (selectionMode === "strict" && !includedSeats.length) {
        state.seatPreferenceErrors.set(item.id, "Choose at least one preferred seat before using Only these.");
        renderDetails();
        return;
    }

    await saveSeatPreferences(item, includedSeats, excludedSeats, selectionMode);
}

async function applySeatPreferenceMode(item, seatLabel) {
    if (!seatLabel || state.seatPreferenceSavingIds.has(item.id)) {
        return;
    }

    const layout = state.seatLayouts.get(item.id);
    if (!layout) {
        return;
    }

    const { includedSeats, excludedSeats, selectionMode } = seatPreferencesForLayout(layout);
    const isIncluded = includedSeats.includes(seatLabel);
    const isExcluded = excludedSeats.includes(seatLabel);

    if (state.seatPreferenceMode === "prefer") {
        if (isIncluded && !isExcluded) {
            return;
        }
        const nextIncluded = [...includedSeats.filter((value) => value !== seatLabel), seatLabel];
        const nextExcluded = excludedSeats.filter((value) => value !== seatLabel);
        await saveSeatPreferences(
            item,
            nextIncluded,
            nextExcluded,
            seatSelectionModeForListUpdate(selectionMode, nextIncluded, nextExcluded),
        );
        return;
    }

    if (state.seatPreferenceMode === "avoid") {
        if (isExcluded && !isIncluded) {
            return;
        }
        const nextIncluded = includedSeats.filter((value) => value !== seatLabel);
        const nextExcluded = [...excludedSeats.filter((value) => value !== seatLabel), seatLabel];
        await saveSeatPreferences(
            item,
            nextIncluded,
            nextExcluded,
            seatSelectionModeForListUpdate(selectionMode, nextIncluded, nextExcluded),
        );
        return;
    }
}

async function removeSeatPreference(item, seatLabel, listKind) {
    if (!seatLabel || state.seatPreferenceSavingIds.has(item.id)) {
        return;
    }

    const layout = state.seatLayouts.get(item.id);
    if (!layout) {
        return;
    }

    const { includedSeats, excludedSeats, selectionMode } = seatPreferencesForLayout(layout);
    if (listKind === "included") {
        const nextIncluded = includedSeats.filter((value) => value !== seatLabel);
        await saveSeatPreferences(
            item,
            nextIncluded,
            excludedSeats,
            seatSelectionModeForListUpdate(selectionMode, nextIncluded, excludedSeats),
        );
        return;
    }
    const nextExcluded = excludedSeats.filter((value) => value !== seatLabel);
    await saveSeatPreferences(
        item,
        includedSeats,
        nextExcluded,
        seatSelectionModeForListUpdate(selectionMode, includedSeats, nextExcluded),
    );
}

async function moveIncludedSeat(item, seatLabel, direction) {
    if (!seatLabel || state.seatPreferenceSavingIds.has(item.id)) {
        return;
    }

    const layout = state.seatLayouts.get(item.id);
    if (!layout) {
        return;
    }

    const { includedSeats, excludedSeats } = seatPreferencesForLayout(layout);
    const currentIndex = includedSeats.indexOf(seatLabel);
    if (currentIndex === -1) {
        return;
    }

    const targetIndex = direction === "up"
        ? currentIndex - 1
        : direction === "down"
            ? currentIndex + 1
            : currentIndex;

    if (targetIndex < 0 || targetIndex >= includedSeats.length || targetIndex === currentIndex) {
        return;
    }

    await moveIncludedSeatToIndex(item, seatLabel, targetIndex);
}

async function moveIncludedSeatToIndex(item, seatLabel, targetIndex) {
    if (!seatLabel || state.seatPreferenceSavingIds.has(item.id)) {
        return;
    }

    const layout = state.seatLayouts.get(item.id);
    if (!layout) {
        return;
    }

    const { includedSeats, excludedSeats, selectionMode } = seatPreferencesForLayout(layout);
    if (!includedSeats.includes(seatLabel)) {
        return;
    }

    const reorderedSeats = includedSeats.filter((value) => value !== seatLabel);
    const clampedIndex = Math.max(0, Math.min(targetIndex, reorderedSeats.length));
    reorderedSeats.splice(clampedIndex, 0, seatLabel);

    if (reorderedSeats.length === includedSeats.length
        && reorderedSeats.every((value, index) => value === includedSeats[index])) {
        return;
    }

    await saveSeatPreferences(
        item,
        reorderedSeats,
        excludedSeats,
        seatSelectionModeForListUpdate(selectionMode, reorderedSeats, excludedSeats),
    );
}

async function moveIncludedSeatToTarget(item, seatLabel, targetSeatLabel, placement) {
    if (!seatLabel || !targetSeatLabel || seatLabel === targetSeatLabel) {
        return;
    }

    const layout = state.seatLayouts.get(item.id);
    if (!layout) {
        return;
    }

    const { includedSeats } = seatPreferencesForLayout(layout);
    if (!includedSeats.includes(seatLabel)) {
        return;
    }

    const reorderedSeats = includedSeats.filter((value) => value !== seatLabel);
    const targetIndex = reorderedSeats.indexOf(targetSeatLabel);
    if (targetIndex === -1) {
        return;
    }

    await moveIncludedSeatToIndex(
        item,
        seatLabel,
        placement === "after" ? targetIndex + 1 : targetIndex,
    );
}

function clearSeatPreferenceDragFeedback() {
    document.querySelectorAll(".seat-pref-order-card").forEach((card) => {
        card.classList.remove("dragging", "drop-before", "drop-after");
    });
    document.querySelectorAll(".seat-pref-order-strip").forEach((strip) => {
        strip.classList.remove("drop-end");
    });
    if (state.seatPreferenceDrag) {
        const draggingCard = Array.from(document.querySelectorAll(".seat-pref-order-card"))
            .find((card) => card.getAttribute("data-seat-order-label") === state.seatPreferenceDrag.seatLabel);
        draggingCard?.classList.add("dragging");
    }
}

function resolveSeatPreferenceDragLabel(itemId, event) {
    if (state.seatPreferenceDrag && state.seatPreferenceDrag.itemId === itemId) {
        return state.seatPreferenceDrag.seatLabel;
    }
    return event.dataTransfer?.getData("text/plain") || "";
}

function seatPreferenceDropPlacement(card, event) {
    const rect = card.getBoundingClientRect();
    return event.clientX < rect.left + (rect.width / 2) ? "before" : "after";
}

function seatCellLabel(cell) {
    if (cell.display_title) return cell.display_title;
    return "";
}

function seatCellActionTitle(cell, preferenceState) {
    if (state.seatPreferenceMode === "prefer") {
        return preferenceState === "included"
            ? `${cell.title} is already preferred`
            : `Prefer ${cell.title}`;
    }
    if (state.seatPreferenceMode === "avoid") {
        return preferenceState === "excluded"
            ? `${cell.title} is already avoided`
            : `Avoid ${cell.title}`;
    }
    return `Seat ${cell.title}`;
}

function renderSeatCell(cell, layout, saving) {
    const label = seatCellLabel(cell);
    const preferenceState = seatPreferenceState(layout, cell);
    const classes = [
        "seat-cell",
        cell.kind,
        preferenceState,
    ].filter(Boolean).join(" ");
    const fallbackLabel = cell.kind === "door"
        ? "D"
        : cell.kind === "trainer"
            ? "T"
            : "\u00A0";
    const body = `
        ${preferenceState === "excluded" ? '<span class="seat-cell-block" aria-hidden="true"></span>' : ""}
        <span class="seat-cell-label">${escapeHtml(label || fallbackLabel)}</span>
    `;

    if (cell.kind === "available") {
        return `
            <button
                type="button"
                class="${escapeHtml(classes)}"
                data-seat-apply="${escapeHtml(cell.title)}"
                title="${escapeHtml(seatCellActionTitle(cell, preferenceState))}"
                ${saving ? "disabled" : ""}
            >${body}</button>
        `;
    }

    return `
        <div class="${escapeHtml(classes)}" title="${escapeHtml(label || cell.kind)}">
            ${body}
        </div>
    `;
}

function renderSeatPreferenceModeControl(saving) {
    return `
        <div class="seat-mode-control" role="group" aria-label="Seat edit mode">
            ${SEAT_PREFERENCE_MODES.map((mode) => `
                <button
                    type="button"
                    class="seat-mode-button ${state.seatPreferenceMode === mode.id ? "active" : ""}"
                    data-seat-mode="${escapeHtml(mode.id)}"
                    aria-pressed="${state.seatPreferenceMode === mode.id ? "true" : "false"}"
                    ${saving ? "disabled" : ""}
                >
                    <span class="seat-mode-dot ${escapeHtml(mode.id)}" aria-hidden="true"></span>
                    <span class="seat-mode-copy">
                        <span class="seat-mode-label">${escapeHtml(mode.label)}</span>
                        <span class="seat-mode-note">${escapeHtml(mode.note)}</span>
                    </span>
                </button>
            `).join("")}
        </div>
    `;
}

function renderSeatBookingPolicyControl(layout, saving) {
    const preferences = seatPreferencesForLayout(layout);
    const currentMode = preferences.selectionMode || "preferences";
    return `
        <div class="seat-policy-card">
            <span class="seat-pref-label">Booking policy</span>
            <div class="seat-policy-control" role="group" aria-label="Seat booking policy">
                ${SEAT_BOOKING_POLICIES.map((policy) => {
                    const disabled = saving || (policy.id === "strict" && !preferences.includedSeats.length);
                    return `
                        <button
                            type="button"
                            class="seat-mode-button ${currentMode === policy.id ? "active" : ""}"
                            data-seat-policy="${escapeHtml(policy.id)}"
                            aria-pressed="${currentMode === policy.id ? "true" : "false"}"
                            ${disabled ? "disabled" : ""}
                        >
                            <span class="seat-mode-dot ${escapeHtml(policy.id)}" aria-hidden="true"></span>
                            <span class="seat-mode-copy">
                                <span class="seat-mode-label">${escapeHtml(policy.label)}</span>
                                <span class="seat-mode-note">${escapeHtml(policy.note)}</span>
                            </span>
                        </button>
                    `;
                }).join("")}
            </div>
        </div>
    `;
}

function renderSeatPreferenceSourceControl(layout, saving) {
    const source = seatPreferenceSource(layout);
    const sources = seatPreferenceSourceCandidates(layout);
    const isExplicit = source.source_type === "explicit";
    const currentSourceKey = String(source.source_key || "");
    return `
        <div class="seat-source-card ${isExplicit ? "explicit" : "inferred"}">
            <div class="seat-source-copy">
                <span class="seat-pref-label">Plan source</span>
                <strong>${escapeHtml(isExplicit ? "Saved for this class slot" : source.label || "Default seat list")}</strong>
            </div>
            ${!isExplicit && sources.length > 1 ? `
                <select
                    class="seat-source-select"
                    id="seat-preference-source-select"
                    ${saving ? "disabled" : ""}
                >
                    ${sources.map((candidate) => `
                        <option
                            value="${escapeHtml(candidate.source_key || "")}"
                            ${String(candidate.source_key || "") === currentSourceKey ? "selected" : ""}
                        >${escapeHtml(candidate.label || "Default seat list")}</option>
                    `).join("")}
                </select>
            ` : ""}
            ${isExplicit ? `
                <button
                    type="button"
                    class="seat-layout-action ghost"
                    id="seat-preferences-reset-inferred"
                    ${saving ? "disabled" : ""}
                >Reset to inferred</button>
            ` : ""}
        </div>
    `;
}

function renderSeatLegend() {
    const entries = [
        { kind: "available", label: "Available" },
        { kind: "included", label: "Included" },
        { kind: "excluded", label: "Excluded" },
        { kind: "booked", label: "Booked" },
        { kind: "unavailable", label: "Unavailable" },
        { kind: "door", label: "Door" },
        { kind: "trainer", label: "Trainer" },
    ];
    return `
        <div class="seat-legend">
            ${entries.map((entry) => `
                <div class="seat-legend-item">
                    <span class="seat-legend-swatch ${escapeHtml(entry.kind)}"></span>
                    <span>${escapeHtml(entry.label)}</span>
                </div>
            `).join("")}
        </div>
    `;
}

function renderSeatPreferenceRow(label, kind, seatLabels, saving, emptyText = "None yet") {
    if (kind === "excluded" && !seatLabels.length) {
        return "";
    }

    if (!seatLabels.length) {
        return `
            <div class="seat-pref-row">
                <div class="seat-pref-row-heading">
                    <span class="seat-pref-label">${escapeHtml(label)}</span>
                </div>
                <span class="seat-pref-empty">${escapeHtml(emptyText)}</span>
            </div>
        `;
    }

    if (kind === "included") {
        return `
            <div class="seat-pref-row ordered">
                <div class="seat-pref-row-heading">
                    <span class="seat-pref-label">${escapeHtml(label)}</span>
                </div>
                <div class="seat-pref-order-shell">
                    <div class="seat-pref-order-strip" data-seat-order-strip="included">
                    ${seatLabels.map((seatLabel, index) => `
                        <div
                            class="seat-pref-order-card included"
                            data-seat-order-label="${escapeHtml(seatLabel)}"
                            draggable="${saving ? "false" : "true"}"
                            title="${escapeHtml(`Seat ${seatLabel}, priority ${index + 1}`)}"
                        >
                            <span class="seat-pref-card-label">${escapeHtml(seatLabel)}</span>
                            <span class="seat-pref-drag-handle" aria-hidden="true">::</span>
                            <div class="seat-pref-card-actions">
                                <button
                                    type="button"
                                    class="seat-pref-action reorder"
                                    data-seat-move-label="${escapeHtml(seatLabel)}"
                                    data-seat-move-direction="left"
                                    ${saving || index === 0 ? "disabled" : ""}
                                    title="${escapeHtml(`Move ${seatLabel} earlier`)}"
                                >←</button>
                                <button
                                    type="button"
                                    class="seat-pref-action reorder"
                                    data-seat-move-label="${escapeHtml(seatLabel)}"
                                    data-seat-move-direction="right"
                                    ${saving || index === seatLabels.length - 1 ? "disabled" : ""}
                                    title="${escapeHtml(`Move ${seatLabel} later`)}"
                                >→</button>
                                <button
                                    type="button"
                                    class="seat-pref-action remove"
                                    data-seat-remove-kind="included"
                                    data-seat-remove-label="${escapeHtml(seatLabel)}"
                                    ${saving ? "disabled" : ""}
                                    title="${escapeHtml(`Remove ${seatLabel}`)}"
                                >×</button>
                            </div>
                        </div>
                    `).join("")}
                    </div>
                </div>
            </div>
        `;
    }

    return `
        <div class="seat-pref-row">
            <div class="seat-pref-row-heading">
                <span class="seat-pref-label">${escapeHtml(label)}</span>
            </div>
            <div class="seat-pref-chip-row">
                ${seatLabels.map((seatLabel) => `
                    <button
                        type="button"
                        class="seat-pref-chip ${escapeHtml(kind)}"
                        data-seat-remove-kind="${escapeHtml(kind)}"
                        data-seat-remove-label="${escapeHtml(seatLabel)}"
                        ${saving ? "disabled" : ""}
                        title="${escapeHtml(`Remove ${seatLabel}`)}"
                    >${escapeHtml(seatLabel)}</button>
                `).join("")}
            </div>
        </div>
    `;
}

function renderSeatPreferenceSummaryGroup(label, kind, seatLabels) {
    if (!seatLabels.length) {
        return "";
    }
    return `
        <div class="seat-pref-summary-group">
            <span class="seat-pref-label">${escapeHtml(label)}</span>
            <div class="seat-pref-summary-list">
                ${seatLabels.map((seatLabel) => `
                    <span class="seat-pref-summary-chip ${escapeHtml(kind)}">${escapeHtml(seatLabel)}</span>
                `).join("")}
            </div>
        </div>
    `;
}

function renderCompactSeatMap(layout) {
    if (!layout) {
        return "";
    }
    return `
        <div class="seat-grid-shell compact">
            <div class="seat-grid compact" style="grid-template-columns: repeat(${layout.columns || 1}, minmax(0, 1fr));">
                ${(layout.cells || []).map((cell) => {
                    const label = seatCellLabel(cell);
                    const preferenceState = seatPreferenceState(layout, cell);
                    const classes = [
                        "seat-cell",
                        "compact",
                        cell.kind,
                        preferenceState,
                    ].filter(Boolean).join(" ");
                    const fallbackLabel = cell.kind === "door"
                        ? "D"
                        : cell.kind === "trainer"
                            ? "T"
                            : "\u00A0";
                    return `
                        <div class="${escapeHtml(classes)}" title="${escapeHtml(cell.title || label || cell.kind)}">
                            <span class="seat-cell-label">${escapeHtml(label || fallbackLabel)}</span>
                        </div>
                    `;
                }).join("")}
            </div>
        </div>
    `;
}

function isSeatEditorOpen(item) {
    return Boolean(item && state.seatEditorOpenClassId === item.id);
}

async function openSeatEditor(item) {
    if (!item || !hasSeatMap(item)) {
        return;
    }
    state.seatEditorOpenClassId = item.id;
    renderDetails();
    await maybeLoadSeatLayout(item);
}

function closeSeatEditor() {
    if (state.seatEditorOpenClassId === null) {
        return;
    }
    state.seatEditorOpenClassId = null;
    state.seatPreferenceDrag = null;
    clearSeatPreferenceDragFeedback();
    renderDetails();
}

function renderSeatEditorBody(item, layout, loading, error, preferenceError, saving) {
    if (loading && !layout) {
        return '<p class="empty-inline">Loading the live seat map...</p>';
    }
    if (error && !layout) {
        return `<div class="notice-banner error">${escapeHtml(error)}</div>`;
    }
    if (!layout) {
        return '<p class="empty-inline">Load the live seat map to set room-and-category seat preferences here.</p>';
    }

    const preferences = seatPreferencesForLayout(layout);
    const source = seatPreferenceSource(layout);
    const isExplicit = source.source_type === "explicit";
    const isAnySeatPlan = preferences.selectionMode === "any";
    const scopeCopy = seatPreferenceScopeCopy(item, layout);
    const summary = layout.summary || {};
    const saveStateLabel = saving ? "Saving" : isExplicit ? "Saved" : "Inferred";
    return `
        ${error ? `<div class="notice-banner error">${escapeHtml(error)}</div>` : ""}
        ${preferenceError ? `<div class="notice-banner error">${escapeHtml(preferenceError)}</div>` : ""}
        <div class="seat-studio">
            <div class="seat-studio-main">
                <div class="seat-map-toolbar">
                    <div>
                        <span class="seat-map-kicker">Room map</span>
                        <strong class="seat-map-stat">${escapeHtml(String(summary.available || 0))} open spots</strong>
                    </div>
                    ${renderSeatPreferenceModeControl(saving)}
                </div>
                <div class="seat-grid-shell">
                    <div class="seat-grid" style="grid-template-columns: repeat(${layout.columns || 1}, minmax(0, 1fr));">
                        ${(layout.cells || []).map((cell) => renderSeatCell(cell, layout, saving)).join("")}
                    </div>
                </div>
                ${renderSeatLegend()}
            </div>
            <aside class="seat-studio-side">
                <div class="seat-plan-card">
                    <div class="seat-plan-head">
                        <div>
                            <span class="seat-map-kicker">Booking plan</span>
                            <strong class="seat-plan-title">Try order</strong>
                        </div>
                        <span class="${saving ? "seat-pref-saving" : "seat-pref-saved"}">${escapeHtml(saveStateLabel)}</span>
                    </div>
                    <div class="seat-pref-panel">
                        ${renderSeatPreferenceSourceControl(layout, saving)}
                        ${renderSeatBookingPolicyControl(layout, saving)}
                        ${renderSeatPreferenceRow("Preferred", "included", preferences.includedSeats, saving, isAnySeatPlan ? "Any available seat" : "None yet")}
                        ${renderSeatPreferenceRow("Avoid", "excluded", preferences.excludedSeats, saving)}
                    </div>
                    ${(preferences.includedSeats.length || preferences.excludedSeats.length) ? `
                        <button
                            type="button"
                            class="seat-layout-action clear"
                            id="seat-preferences-clear-all"
                            ${saving ? "disabled" : ""}
                        >Clear all</button>
                    ` : ""}
                </div>
                <div class="seat-scope-note">
                    <span class="seat-map-kicker">Scope</span>
                    <p>${escapeHtml(scopeCopy)}</p>
                </div>
            </aside>
        </div>
    `;
}

function renderSeatEditorModal(item, layout, loading, error, preferenceError, saving) {
    if (!isSeatEditorOpen(item)) {
        return "";
    }

    const actionLabel = layout ? "Refresh live map" : "Load live map";
    return `
        <div class="seat-editor-backdrop" id="seat-editor-overlay">
            <section class="seat-editor-dialog" role="dialog" aria-modal="true" aria-labelledby="seat-editor-title">
                <div class="seat-editor-header">
                    <div>
                        <p class="detail-topline seat-editor-topline">${escapeHtml(fmtDate.format(parseLocalDate(item.class_date)))} • ${escapeHtml(classTime(item))}</p>
                        <h3 class="seat-editor-title" id="seat-editor-title">Seat studio</h3>
                        <p class="section-copy seat-map-copy">Live ${escapeHtml((item.seat_title || "Spot").toLowerCase())} layout for ${escapeHtml(item.title)}.</p>
                    </div>
                    <div class="seat-editor-actions">
                        <button
                            type="button"
                            class="seat-layout-action"
                            id="seat-layout-refresh"
                            ${loading ? "disabled" : ""}
                        >${escapeHtml(loading ? "Loading..." : actionLabel)}</button>
                        <button
                            type="button"
                            class="seat-layout-action ghost"
                            id="seat-editor-close"
                        >Close</button>
                    </div>
                </div>
                <div class="seat-editor-body">
                    ${renderSeatEditorBody(item, layout, loading, error, preferenceError, saving)}
                </div>
            </section>
        </div>
    `;
}

function renderSeatLayoutSection(item) {
    if (!hasSeatMap(item)) {
        return "";
    }

    const layout = state.seatLayouts.get(item.id);
    const loading = state.seatLayoutLoadingIds.has(item.id);
    const error = state.seatLayoutErrors.get(item.id);
    const preferenceError = state.seatPreferenceErrors.get(item.id);
    const saving = state.seatPreferenceSavingIds.has(item.id);
    const openLabel = loading && !layout
        ? "Loading..."
        : layout
            ? "Edit seats"
            : "Load seat map";

    let body = "";
    if (loading && !layout) {
        body = '<p class="empty-inline">Loading the live seat map...</p>';
    } else if (error && !layout) {
        body = `
            <div class="notice-banner error">${escapeHtml(error)}</div>
            <p class="empty-inline">Open the editor to try loading the live map again.</p>
        `;
    } else if (layout) {
        body = `
            ${preferenceError ? `<div class="notice-banner error">${escapeHtml(preferenceError)}</div>` : ""}
            ${renderSeatSummary(layout)}
            <p class="section-copy seat-summary-copy">${escapeHtml(seatPreferenceScopeCopy(item, layout))}</p>
            ${saving ? '<p class="seat-pref-saving">Saving seat preferences...</p>' : ""}
            ${renderCompactSeatMap(layout)}
        `;
    } else {
        body = '<p class="empty-inline">Open the live seat editor to load the room map and set room-and-category seat preferences.</p>';
    }

    return `
        <section class="detail-section seat-layout-panel seat-layout-summary-panel">
            <div class="section-heading">
                <div>
                    <h3>Seat preferences</h3>
                </div>
                <button
                    type="button"
                    class="seat-layout-action primary"
                    id="seat-editor-open"
                    ${loading && !layout ? "disabled" : ""}
                >${escapeHtml(openLabel)}</button>
            </div>
            ${body}
        </section>
        ${renderSeatEditorModal(item, layout, loading, error, preferenceError, saving)}
    `;
}

function renderActivity(item) {
    const history = activityForClass(item.id);
    if (!history.length) {
        return '<p class="empty-inline">No recent activity has been recorded for this class yet.</p>';
    }
    return `
        <div class="activity-list">
            ${history.map((event) => `
                <article class="activity-item">
                    <div class="activity-head">
                        <span class="activity-title">${escapeHtml(activityTitle(event))}</span>
                        <span class="activity-time">${escapeHtml(formatDateTime(event.observed_at))}</span>
                    </div>
                    <p class="activity-message">${escapeHtml(event.message || "No message recorded.")}</p>
                </article>
            `).join("")}
        </div>
    `;
}

function weeklyScheduleNotice(payload, fallbackMessage) {
    const itemCount = payload?.schedule?.items?.length || weeklyScheduleItems().length;
    const materialized = payload?.materialized || {};
    const createdCount = materialized.created_count || 0;
    const alreadyCount = materialized.already_count || 0;
    const skippedCount = materialized.skipped_count || 0;
    const missingCount = materialized.missing_count || 0;
    const sentences = [fallbackMessage];
    if (createdCount) {
        sentences.push(`${pluralize(createdCount, "upcoming class", "upcoming classes")} selected.`);
    }
    if (alreadyCount) {
        sentences.push(`${pluralize(alreadyCount, "class was", "classes were")} already being watched.`);
    }
    if (skippedCount) {
        sentences.push(`${pluralize(skippedCount, "class was", "classes were")} already booked or full.`);
    }
    if (missingCount) {
        sentences.push(`${pluralize(missingCount, "weekly class is", "weekly classes are")} not on the discovered calendar.`);
    }

    return {
        kind: createdCount || itemCount ? "success" : "info",
        message: sentences.join(" "),
    };
}

async function addClassToWeeklySchedule(classId) {
    if (state.weekActionLoading) {
        return;
    }

    state.weekActionLoading = `recurring:${classId}`;
    renderDetails();

    try {
        const payload = await fetchJson(`/discovered-classes/${classId}/weekly-schedule`, { method: "POST" });
        await loadState();
        state.selectedClassId = classId;
        state.notices.set(classId, weeklyScheduleNotice(payload, "Recurring is on."));
        render();
    } catch (error) {
        state.notices.set(classId, {
            kind: "error",
            message: `Could not update recurring: ${error.message}`,
        });
        renderDetails();
    } finally {
        state.weekActionLoading = "";
        renderDetails();
    }
}

async function removeWeeklyScheduleItem(itemId, classId = null) {
    const noticeClassId = classId || state.selectedClassId;
    if (state.weekActionLoading) {
        return;
    }

    state.weekActionLoading = noticeClassId ? `recurring:${noticeClassId}` : "schedule";
    renderDetails();

    try {
        await fetchJson(`/weekly-schedule/items/${itemId}`, { method: "DELETE" });
        await loadState();
        if (noticeClassId) {
            state.selectedClassId = noticeClassId;
            state.notices.set(noticeClassId, {
                kind: "success",
                message: "Recurring is off.",
            });
        }
        render();
    } catch (error) {
        if (noticeClassId) {
            state.notices.set(noticeClassId, {
                kind: "error",
                message: `Could not update recurring: ${error.message}`,
            });
        }
        renderDetails();
    } finally {
        state.weekActionLoading = "";
        renderDetails();
    }
}

async function toggleWeeklyScheduleSkip(item, skipped) {
    if (state.weekActionLoading) {
        return;
    }

    state.weekActionLoading = `skip:${item.id}`;
    renderDetails();

    try {
        await fetchJson(`/discovered-classes/${item.id}/weekly-schedule/skip`, {
            method: skipped ? "DELETE" : "POST",
        });
        await loadState();
        state.selectedClassId = item.id;
        state.notices.set(item.id, {
            kind: "success",
            message: skipped
                ? "This recurring class is active again for this week."
                : "Skipped this week. Future weeks remain recurring.",
        });
        render();
    } catch (error) {
        state.notices.set(item.id, {
            kind: "error",
            message: `Could not update this week: ${error.message}`,
        });
        renderDetails();
    } finally {
        state.weekActionLoading = "";
        renderDetails();
    }
}


function renderWeekDetailPrompt() {
    const panel = document.getElementById("details");
    if (!panel) {
        return;
    }
    state.seatEditorOpenClassId = null;
    panel.classList.add("detail-prompt-mode");
    syncBodyModalState();
    panel.innerHTML = `
        <div class="detail-empty detail-prompt">
            <p class="detail-topline">Class details</p>
            <h2>Select a class</h2>
            <p>Booking state, recurring rules, and seat preferences appear here.</p>
        </div>
    `;
    syncDetailPanelScroll(panel, "prompt:calendar");
}


function renderDetails() {
    const panel = document.getElementById("details");
    const item = state.classes.find((entry) => entry.id === state.selectedClassId);
    if (!item) {
        renderWeekDetailPrompt();
        return;
    }

    panel.classList.remove("detail-prompt-mode");

    if (state.seatEditorOpenClassId !== null && state.seatEditorOpenClassId !== item.id) {
        state.seatEditorOpenClassId = null;
    }

    syncBodyModalState(item);

    const job = activeJobForClass(item.id);
    const notice = state.notices.get(item.id);
    const selectionDisabledReasonText = selectionDisabledReason(item, job);
    const selectionLoading = state.selectionLoadingIds.has(item.id);
    const heroFactPills = [];
    if (item.category || classType(item)) {
        heroFactPills.push({ label: item.category || classType(item), kind: "subtle" });
    }
    if (item.duration_minutes) {
        heroFactPills.push({ label: `${item.duration_minutes} min`, kind: "subtle" });
    }
    if (spotText(item)) {
        heroFactPills.push({ label: spotText(item), kind: "subtle" });
    }
    const upcomingAttemptMessage = upcomingBookingAttemptMessage(item, job);

    panel.innerHTML = `
        <button
            class="mobile-detail-close"
            id="mobile-detail-close"
            type="button"
            aria-label="Close class details"
        >&times;</button>
        <section class="detail-hero">
            <button
                class="detail-breadcrumb"
                id="detail-week-back"
                type="button"
            >&larr; Calendar</button>
            <p class="detail-topline">${escapeHtml(fmtDate.format(parseLocalDate(item.class_date)))} • ${escapeHtml(classTime(item))}</p>
            <div class="detail-header">
                <div class="detail-copy">
                    <h2 class="detail-title">${escapeHtml(item.title)}</h2>
                    <p class="detail-subtitle">${escapeHtml(item.instructor_name || "Instructor not listed")}</p>
                </div>
            </div>
            ${heroFactPills.length ? `
                <div class="detail-fact-row">
                    ${heroFactPills.map((pill) => `<span class="status-pill ${escapeHtml(pill.kind)}">${escapeHtml(pill.label)}</span>`).join("")}
                </div>
            ` : ""}
            ${notice ? `<div class="notice-banner ${escapeHtml(notice.kind)}">${escapeHtml(notice.message)}</div>` : ""}
        </section>

        ${renderPlanSection(item, job, selectionDisabledReasonText, selectionLoading)}

        ${renderSeatLayoutSection(item)}

        <details class="accordion-block">
            <summary>Useful details</summary>
            <div class="accordion-body">
                <div class="meta-grid">
                    ${renderUsefulDetails(item)}
                </div>
            </div>
        </details>

        <details class="accordion-block">
            <summary>Recent activity</summary>
            <div class="accordion-body">
                ${renderActivity(item)}
            </div>
        </details>

        <details class="accordion-block">
            <summary>Class details</summary>
            <div class="accordion-body">
                <div class="meta-grid">
                    ${renderBookingDetails(item, upcomingAttemptMessage)}
                    ${item.deep_link_url ? `
                        <div class="meta-row">
                            <span>Gym page</span>
                            <strong><a class="detail-link" href="${escapeHtml(item.deep_link_url)}" target="_blank" rel="noreferrer">Open gym page</a></strong>
                        </div>
                    ` : ""}
                    ${scheduleSummaryRows(item, "meta-row")}
                </div>
            </div>
        </details>

        <details class="accordion-block">
            <summary>Technical details</summary>
            <div class="accordion-body">
                <div class="meta-grid">
                    ${renderTechnicalDetails(item)}
                </div>
            </div>
        </details>
    `;

    syncDetailPanelScroll(panel, `class:${item.id}`);

    const closeClassDetails = () => {
        state.selectedClassId = null;
        state.seatEditorOpenClassId = null;
        render();
    };
    document.getElementById("detail-week-back")?.addEventListener("click", closeClassDetails);
    document.getElementById("mobile-detail-close")?.addEventListener("click", closeClassDetails);

    const toggleButton = document.getElementById("selection-toggle");
    if (toggleButton && !toggleButton.disabled) {
        toggleButton.addEventListener("click", async () => {
            await toggleBooking(item, job);
        });
    }

    const recurringToggle = document.getElementById("recurring-toggle");
    if (recurringToggle && !recurringToggle.disabled) {
        recurringToggle.addEventListener("click", async () => {
            const scheduleItem = weeklyScheduleItemForClass(item);
            if (scheduleItem) {
                await removeWeeklyScheduleItem(scheduleItem.id, item.id);
                return;
            }
            await addClassToWeeklySchedule(item.id);
        });
    }

    const recurringSkipToggle = document.getElementById("recurring-skip-toggle");
    if (recurringSkipToggle && !recurringSkipToggle.disabled) {
        recurringSkipToggle.addEventListener("click", async () => {
            await toggleWeeklyScheduleSkip(item, Boolean(weeklyScheduleSkipForClass(item)));
        });
    }

    const seatLayoutRefresh = document.getElementById("seat-layout-refresh");
    if (seatLayoutRefresh && hasSeatMap(item)) {
        seatLayoutRefresh.addEventListener("click", async () => {
            await maybeLoadSeatLayout(item, true);
        });
    }

    const seatEditorOpenButton = document.getElementById("seat-editor-open");
    if (seatEditorOpenButton && hasSeatMap(item)) {
        seatEditorOpenButton.addEventListener("click", async () => {
            await openSeatEditor(item);
        });
    }

    const seatEditorCloseButton = document.getElementById("seat-editor-close");
    if (seatEditorCloseButton) {
        seatEditorCloseButton.addEventListener("click", () => {
            closeSeatEditor();
        });
    }

    const seatEditorOverlay = document.getElementById("seat-editor-overlay");
    if (seatEditorOverlay) {
        seatEditorOverlay.addEventListener("click", (event) => {
            if (event.target === seatEditorOverlay) {
                closeSeatEditor();
            }
        });
    }

    const clearAllButton = document.getElementById("seat-preferences-clear-all");
    if (clearAllButton) {
        clearAllButton.addEventListener("click", async () => {
            await saveSeatPreferences(item, [], []);
        });
    }

    const resetInferredButton = document.getElementById("seat-preferences-reset-inferred");
    if (resetInferredButton) {
        resetInferredButton.addEventListener("click", async () => {
            await resetSeatPreferencesToInferred(item);
        });
    }

    const sourceSelect = document.getElementById("seat-preference-source-select");
    if (sourceSelect) {
        sourceSelect.addEventListener("change", async () => {
            await copySeatPreferenceSource(item, sourceSelect.value);
        });
    }

    document.querySelectorAll("[data-seat-policy]").forEach((button) => {
        button.addEventListener("click", async () => {
            const policy = button.getAttribute("data-seat-policy") || "preferences";
            if (SEAT_BOOKING_POLICIES.some((entry) => entry.id === policy)) {
                await updateSeatBookingPolicy(item, policy);
            }
        });
    });

    document.querySelectorAll("[data-seat-mode]").forEach((button) => {
        button.addEventListener("click", async () => {
            const mode = button.getAttribute("data-seat-mode") || "prefer";
            if (SEAT_PREFERENCE_MODES.some((entry) => entry.id === mode)) {
                state.seatPreferenceMode = mode;
                renderDetails();
            }
        });
    });

    document.querySelectorAll("[data-seat-apply]").forEach((button) => {
        button.addEventListener("click", async () => {
            const seatLabel = button.getAttribute("data-seat-apply") || "";
            await applySeatPreferenceMode(item, seatLabel);
        });
    });

    document.querySelectorAll("[data-seat-move-label]").forEach((button) => {
        button.addEventListener("click", async () => {
            const seatLabel = button.getAttribute("data-seat-move-label") || "";
            const direction = button.getAttribute("data-seat-move-direction") || "";
            await moveIncludedSeat(
                item,
                seatLabel,
                direction === "left" ? "up" : direction === "right" ? "down" : direction,
            );
        });
    });

    document.querySelectorAll("[data-seat-remove-kind]").forEach((button) => {
        button.addEventListener("click", async () => {
            const listKind = button.getAttribute("data-seat-remove-kind") || "";
            const seatLabel = button.getAttribute("data-seat-remove-label") || "";
            await removeSeatPreference(item, seatLabel, listKind);
        });
    });

    document.querySelectorAll("[data-seat-order-label]").forEach((card) => {
        card.addEventListener("dragstart", (event) => {
            const seatLabel = card.getAttribute("data-seat-order-label") || "";
            if (!seatLabel || state.seatPreferenceSavingIds.has(item.id)) {
                event.preventDefault();
                return;
            }
            state.seatPreferenceDrag = { itemId: item.id, seatLabel };
            clearSeatPreferenceDragFeedback();
            card.classList.add("dragging");
            if (event.dataTransfer) {
                event.dataTransfer.effectAllowed = "move";
                try {
                    event.dataTransfer.setData("text/plain", seatLabel);
                } catch {
                    // Some runtimes still dispatch drag events without a writable payload.
                }
            }
        });

        card.addEventListener("dragover", (event) => {
            const targetLabel = card.getAttribute("data-seat-order-label") || "";
            const draggedLabel = resolveSeatPreferenceDragLabel(item.id, event);
            if (!targetLabel || !draggedLabel || draggedLabel === targetLabel) {
                return;
            }
            event.preventDefault();
            clearSeatPreferenceDragFeedback();
            card.classList.add(seatPreferenceDropPlacement(card, event) === "after"
                ? "drop-after"
                : "drop-before");
        });

        card.addEventListener("dragleave", (event) => {
            if (card.contains(event.relatedTarget)) {
                return;
            }
            card.classList.remove("drop-before", "drop-after");
        });

        card.addEventListener("drop", async (event) => {
            const targetLabel = card.getAttribute("data-seat-order-label") || "";
            const draggedLabel = resolveSeatPreferenceDragLabel(item.id, event);
            if (!targetLabel || !draggedLabel || draggedLabel === targetLabel) {
                clearSeatPreferenceDragFeedback();
                return;
            }
            event.preventDefault();
            const placement = seatPreferenceDropPlacement(card, event);
            clearSeatPreferenceDragFeedback();
            await moveIncludedSeatToTarget(item, draggedLabel, targetLabel, placement);
        });

        card.addEventListener("dragend", () => {
            state.seatPreferenceDrag = null;
            clearSeatPreferenceDragFeedback();
        });
    });

    const seatOrderStrip = document.querySelector("[data-seat-order-strip]");
    if (seatOrderStrip) {
        seatOrderStrip.addEventListener("dragover", (event) => {
            if (event.target.closest("[data-seat-order-label]")) {
                return;
            }
            const draggedLabel = resolveSeatPreferenceDragLabel(item.id, event);
            if (!draggedLabel) {
                return;
            }
            event.preventDefault();
            clearSeatPreferenceDragFeedback();
            seatOrderStrip.classList.add("drop-end");
        });

        seatOrderStrip.addEventListener("dragleave", (event) => {
            if (seatOrderStrip.contains(event.relatedTarget)) {
                return;
            }
            seatOrderStrip.classList.remove("drop-end");
        });

        seatOrderStrip.addEventListener("drop", async (event) => {
            if (event.target.closest("[data-seat-order-label]")) {
                return;
            }
            const draggedLabel = resolveSeatPreferenceDragLabel(item.id, event);
            if (!draggedLabel) {
                clearSeatPreferenceDragFeedback();
                return;
            }
            event.preventDefault();
            clearSeatPreferenceDragFeedback();
            await moveIncludedSeatToIndex(item, draggedLabel, Number.MAX_SAFE_INTEGER);
        });
    }
}

function render() {
    syncTypeFilters({ savePreference: false });
    renderScopeControl();
    renderCalendar();
    renderDetails();
    renderAuthControls();
    renderBookingPauseControls();
    renderUserControls();
}

function renderUserControls() {
    const appUser = state.appUser;
    const entry = document.getElementById("user-menu-open");
    const banner = document.getElementById("managed-user-banner");
    const shell = document.getElementById("user-menu-shell");
    const gymEntry = document.getElementById("auth-modal-open");

    if (!appUser?.enabled) {
        if (entry) entry.hidden = true;
        if (banner) banner.hidden = true;
        if (shell) shell.innerHTML = "";
        if (gymEntry) gymEntry.hidden = false;
        return;
    }

    const actor = appUser.actor || {};
    const activeUser = appUser.active_user || actor;
    if (entry) {
        entry.hidden = false;
        entry.textContent = appUser.is_delegating ? activeUser.name : actor.name;
    }
    if (gymEntry) gymEntry.hidden = !appUser.is_admin;

    if (banner) {
        banner.hidden = !appUser.is_delegating;
        banner.innerHTML = appUser.is_delegating
            ? `Managing BookGym for ${escapeHtml(activeUser.name)} <a href="/admin">Exit to operator view</a>`
            : "";
    }

    if (!shell) return;
    if (!state.userMenuOpen) {
        shell.innerHTML = "";
        return;
    }

    const managedUsers = appUser.managed_users || appUser.members || [];
    const managedUserButtons = appUser.can_manage_users
        ? managedUsers
            .filter((user) => Number(user.id) !== Number(activeUser.id))
            .map((user) => `
            <button type="button" data-managed-user-id="${Number(user.id)}">
                ${Number(user.id) === Number(actor.id) ? "Use my account" : `Manage ${escapeHtml(user.name)}`}
            </button>
        `).join("")
        : "";
    shell.innerHTML = `
        <div class="user-menu-overlay" id="user-menu-overlay">
            <section class="user-menu" aria-label="BookGym account">
                <p class="detail-topline">Signed in as</p>
                <p class="user-menu-name">${escapeHtml(actor.name || "BookGym user")}</p>
                <p class="user-menu-email">${escapeHtml(actor.email || "")}</p>
                ${appUser.is_delegating ? `<p class="user-menu-context">Managing the calendar for ${escapeHtml(activeUser.name)}</p>` : ""}
                <div class="user-menu-actions">
                    ${managedUserButtons}
                    <button type="button" data-booking-pause-open>${state.bookingPause ? "Change away dates" : "Pause bookings"}</button>
                    ${appUser.is_admin ? '<a href="/admin">Open operator view</a>' : ""}
                    <button id="bookgym-logout" type="button">Sign out</button>
                </div>
            </section>
        </div>
    `;
    document.getElementById("user-menu-overlay")?.addEventListener("click", (event) => {
        if (event.target?.id === "user-menu-overlay") {
            state.userMenuOpen = false;
            renderUserControls();
        }
    });
    shell.querySelector("[data-booking-pause-open]")?.addEventListener("click", openBookingPauseModal);
    document.querySelectorAll("[data-managed-user-id]").forEach((button) => {
        button.addEventListener("click", async () => {
            state.appUser = await fetchJson("/auth/manage-user", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ user_id: Number(button.dataset.managedUserId) }),
            });
            state.userMenuOpen = false;
            await loadState();
        });
    });
    document.getElementById("bookgym-logout")?.addEventListener("click", async () => {
        await fetchJson("/auth/logout", { method: "POST" });
        window.location.assign("/login");
    });
}
