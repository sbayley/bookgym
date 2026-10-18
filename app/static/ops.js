const OPS_AUTO_REFRESH_MS = 15_000;

const opsState = {
    schedulerStatus: null,
    jobs: [],
    emailApprovals: [],
    bookingEvents: [],
    discoveryEvents: [],
    networkCaptureLoadingJobIds: new Set(),
    networkCaptureErrors: new Map(),
    error: "",
};

document.addEventListener("DOMContentLoaded", () => {
    loadOpsState().catch((error) => {
        renderOpsError(error);
    });

    window.setInterval(() => {
        loadOpsState().catch((error) => {
            opsState.error = error.message;
            renderOps();
        });
    }, OPS_AUTO_REFRESH_MS);
});

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
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
    if (!parsed) return "unknown";
    const deltaSeconds = Math.max(0, Math.round((Date.now() - parsed.getTime()) / 1000));
    if (deltaSeconds < 60) return "just now";
    if (deltaSeconds < 3600) return `${Math.round(deltaSeconds / 60)} min ago`;
    if (deltaSeconds < 86400) return `${Math.round(deltaSeconds / 3600)} hr ago`;
    return `${Math.round(deltaSeconds / 86400)} day(s) ago`;
}

function statusPillKind(status) {
    if (!status) return "unknown";
    if (["running", "booked", "waitlisted"].includes(status)) return status;
    if (["stale", "missing", "failed"].includes(status)) return status;
    if (["pending", "not_yet_open", "dry_run"].includes(status)) return status;
    return "neutral";
}

function fetchJson(url, options) {
    return fetch(url, options).then(async (response) => {
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
    });
}

async function loadOpsState() {
    const [schedulerStatus, jobs, emailApprovals, bookingEvents, discoveryEvents] = await Promise.all([
        fetchJson("/scheduler-status"),
        fetchJson("/jobs"),
        fetchJson("/email-approvals"),
        fetchJson("/events"),
        fetchJson("/discovery-events"),
    ]);

    opsState.schedulerStatus = schedulerStatus;
    opsState.jobs = jobs;
    opsState.emailApprovals = emailApprovals;
    opsState.bookingEvents = bookingEvents;
    opsState.discoveryEvents = discoveryEvents;
    opsState.error = "";
    renderOps();
}

function renderOpsError(error) {
    opsState.error = error.message;
    renderOps();
}

function renderOps() {
    renderStatusCard();
    renderNextDueCard();
    renderSummaryCard();
    renderQueueCard();
    renderEventsCard("ops-booking-events", "Recent booking events", opsState.bookingEvents, "booking");
    renderEventsCard("ops-discovery-events", "Recent discovery events", opsState.discoveryEvents, "discovery");
}

function renderStatusCard() {
    const shell = document.getElementById("ops-status");
    if (!shell) return;

    const status = opsState.schedulerStatus;
    if (!status) {
        shell.innerHTML = `
            <h2>Worker status</h2>
            <p class="ops-error">${escapeHtml(opsState.error || "Status unavailable.")}</p>
        `;
        return;
    }

    const kind = statusPillKind(status.status);
    const errorBlock = status.last_error
        ? `<p class="ops-error">${escapeHtml(status.last_error)}</p>`
        : "";
    const commandBlock = kind === "running"
        ? ""
        : `<code class="ops-command">./.venv/bin/python scripts/run_bookgym_stack.py</code>`;

    shell.innerHTML = `
        <h2>Worker status</h2>
        <div class="ops-status-row">
            <span class="ops-pill ${escapeHtml(kind)}">${escapeHtml(status.status || "unknown")}</span>
            <span class="ops-metadata">PID ${escapeHtml(status.pid || "n/a")} on ${escapeHtml(status.hostname || "unknown host")}</span>
        </div>
        <p class="ops-copy">${escapeHtml(status.detail || "No scheduler detail was provided.")}</p>
        <p class="ops-copy">Last heartbeat ${escapeHtml(formatRelativeTime(status.last_heartbeat_at))}. Last discovery refresh ${escapeHtml(formatRelativeTime(status.last_discovery_refresh_finished_at))}.</p>
        <p class="ops-copy">Last booking poll ${escapeHtml(formatRelativeTime(status.last_booking_poll_at))}. Last booking run ${escapeHtml(formatRelativeTime(status.last_booking_run_finished_at))}.</p>
        ${errorBlock}
        ${commandBlock}
        ${opsState.error ? `<p class="ops-error">${escapeHtml(opsState.error)}</p>` : ""}
    `;
}

function renderNextDueCard() {
    const shell = document.getElementById("ops-next-due");
    if (!shell) return;

    const nextDue = opsState.schedulerStatus?.next_due_job;
    if (!nextDue) {
        shell.innerHTML = `
            <h2>Next due job</h2>
            <p class="ops-empty">No pending booking or dry-run job is currently queued.</p>
        `;
        return;
    }

    const runType = nextDue.dry_run ? "Dry run" : "Live booking";
    shell.innerHTML = `
        <h2>Next due job</h2>
        <div class="ops-status-row">
            <span class="ops-pill ${escapeHtml(statusPillKind(nextDue.status))}">${escapeHtml(nextDue.status)}</span>
            <span class="ops-pill ${escapeHtml(statusPillKind(nextDue.dry_run ? "dry_run" : "neutral"))}">${escapeHtml(runType)}</span>
        </div>
        <p class="ops-copy"><strong>${escapeHtml(nextDue.target_match_text)}</strong> on ${escapeHtml(nextDue.class_date)}</p>
        <p class="ops-copy">Scheduled for ${escapeHtml(formatDateTime(nextDue.expected_run_at))}.</p>
        <p class="ops-metadata">Job #${escapeHtml(nextDue.id)}</p>
    `;
}

function renderSummaryCard() {
    const shell = document.getElementById("ops-summary");
    if (!shell) return;

    const pendingJobs = opsState.jobs.filter((job) => ["pending", "not_yet_open"].includes(job.status));
    const livePendingCount = pendingJobs.filter((job) => !job.dry_run).length;
    const dryRunCount = pendingJobs.filter((job) => job.dry_run).length;
    const failedCount = opsState.jobs.filter((job) => job.status === "failed").length;
    const bookedCount = opsState.jobs.filter((job) => job.status === "booked").length;

    shell.innerHTML = `
        <h2>Queue summary</h2>
        <div class="ops-summary-grid">
            <div class="ops-stat">
                <strong>${livePendingCount}</strong>
                <span>live jobs pending</span>
            </div>
            <div class="ops-stat">
                <strong>${dryRunCount}</strong>
                <span>dry runs pending</span>
            </div>
            <div class="ops-stat">
                <strong>${failedCount}</strong>
                <span>failed jobs</span>
            </div>
            <div class="ops-stat">
                <strong>${bookedCount}</strong>
                <span>jobs booked</span>
            </div>
        </div>
    `;
}

function networkCaptureCanChange(job) {
    return ["pending", "not_yet_open"].includes(job.status);
}

function renderNetworkCaptureControl(job) {
    const loading = opsState.networkCaptureLoadingJobIds.has(job.id);
    const error = opsState.networkCaptureErrors.get(job.id);
    const enabled = Boolean(job.capture_network);
    const canChange = networkCaptureCanChange(job);
    const artifactPath = job.last_network_artifact_path || "";

    if (!canChange) {
        return `
            <span class="ops-network-state"${artifactPath ? ` title="${escapeHtml(artifactPath)}"` : ""}>
                ${escapeHtml(artifactPath ? "Captured" : "Off")}
            </span>
        `;
    }

    return `
        <div class="ops-network-control">
            <button
                class="ops-network-toggle ${enabled ? "active" : ""}"
                type="button"
                data-network-capture-job-id="${escapeHtml(job.id)}"
                aria-label="${enabled ? "Turn network capture off" : "Capture network data on the next run"} for job ${escapeHtml(job.id)}"
                aria-pressed="${enabled ? "true" : "false"}"
                ${loading ? "disabled" : ""}
            >${escapeHtml(loading ? "Saving" : enabled ? "On" : "Off")}</button>
            ${error ? `<span class="ops-network-error">${escapeHtml(error)}</span>` : ""}
        </div>
    `;
}

async function toggleNetworkCapture(jobId) {
    const job = opsState.jobs.find((candidate) => candidate.id === jobId);
    if (!job || !networkCaptureCanChange(job) || opsState.networkCaptureLoadingJobIds.has(jobId)) return;

    const enabled = !job.capture_network;
    opsState.networkCaptureLoadingJobIds.add(jobId);
    opsState.networkCaptureErrors.delete(jobId);
    renderQueueCard();

    try {
        const updated = await fetchJson(`/jobs/${jobId}/network-capture`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled }),
        });
        opsState.jobs = opsState.jobs.map((existing) => (existing.id === updated.id ? updated : existing));
    } catch (error) {
        opsState.networkCaptureErrors.set(jobId, error.message || "Could not update network capture.");
    } finally {
        opsState.networkCaptureLoadingJobIds.delete(jobId);
        renderQueueCard();
    }
}

function renderQueueCard() {
    const shell = document.getElementById("ops-queue");
    if (!shell) return;

    const queue = [...opsState.jobs]
        .filter(networkCaptureCanChange)
        .sort((left, right) => String(left.expected_run_at).localeCompare(String(right.expected_run_at)))
        .slice(0, 10);

    if (!queue.length) {
        shell.innerHTML = `
            <h2>Job queue</h2>
            <p class="ops-empty">No active jobs are queued right now.</p>
        `;
        return;
    }

    const approvalsByJob = new Map(opsState.emailApprovals.map((approval) => [approval.job_id, approval]));
    const rows = queue.map((job) => {
        const approval = approvalsByJob.get(job.id);
        const approvalLabel = job.dry_run
            ? "—"
            : approval?.sent_at
                ? approval.status
                : approval
                    ? `Email due ${formatDateTime(approval.request_due_at)}`
                    : "Pending setup";
        return `
        <tr>
            <td>
                <strong>${escapeHtml(job.target_match_text)}</strong>
                <div class="ops-list-meta">${escapeHtml(job.class_date)}${job.window_start_time ? ` at ${escapeHtml(job.window_start_time.slice(0, 5))}` : ""}</div>
            </td>
            <td>${escapeHtml(formatDateTime(job.expected_run_at))}</td>
            <td><span class="ops-pill ${escapeHtml(statusPillKind(job.status))}">${escapeHtml(job.status)}</span></td>
            <td>${escapeHtml(job.dry_run ? "Dry run" : "Live")}</td>
            <td>${escapeHtml(approvalLabel)}</td>
            <td>${renderNetworkCaptureControl(job)}</td>
        </tr>
    `;
    }).join("");

    shell.innerHTML = `
        <h2>Job queue</h2>
        <table class="ops-table">
            <thead>
                <tr>
                    <th>Class</th>
                    <th>Expected run</th>
                    <th>Status</th>
                    <th>Mode</th>
                    <th>Email veto</th>
                    <th>Network capture</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>
    `;

    shell.querySelectorAll("[data-network-capture-job-id]").forEach((button) => {
        button.addEventListener("click", () => {
            const jobId = Number(button.dataset.networkCaptureJobId);
            if (Number.isInteger(jobId)) {
                toggleNetworkCapture(jobId);
            }
        });
    });
}

function renderEventsCard(containerId, title, events, kind) {
    const shell = document.getElementById(containerId);
    if (!shell) return;

    const rows = [...events]
        .sort((left, right) => String(right.observed_at).localeCompare(String(left.observed_at)))
        .slice(0, 6);

    if (!rows.length) {
        shell.innerHTML = `
            <h2>${escapeHtml(title)}</h2>
            <p class="ops-empty">No ${escapeHtml(kind)} activity has been recorded yet.</p>
        `;
        return;
    }

    const items = rows.map((event) => `
        <div class="ops-list-item">
            <div class="ops-list-head">
                <div>
                    <div class="ops-list-title">${escapeHtml(event.event_type.replaceAll("_", " "))}</div>
                    <div class="ops-list-meta">${escapeHtml(formatDateTime(event.observed_at))}</div>
                </div>
                ${event.status ? `<span class="ops-pill ${escapeHtml(statusPillKind(event.status))}">${escapeHtml(event.status)}</span>` : ""}
            </div>
            <p class="ops-copy">${escapeHtml(event.message || "No message recorded.")}</p>
        </div>
    `).join("");

    shell.innerHTML = `
        <h2>${escapeHtml(title)}</h2>
        <div class="ops-list">${items}</div>
    `;
}
