from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.scheduler import get_next_due_job
from app.email_approval import next_approval_request_due_at
from app.time_utils import local_now


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Keep macOS awake across BookGym's scheduled booking window.",
    )
    parser.add_argument(
        "--seconds",
        type=int,
        default=300,
        help="How long to hold wake assertions (default: 300 seconds).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Hold wake assertions even when no booking is due soon.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.seconds <= 0:
        raise SystemExit("--seconds must be greater than zero")

    next_job = get_next_due_job()
    next_approval_due_at = next_approval_request_due_at()
    now = local_now()
    seconds_until_due = (
        (next_job.expected_run_at - now).total_seconds()
        if next_job is not None
        else None
    )
    seconds_until_approval = (
        (next_approval_due_at - now).total_seconds()
        if next_approval_due_at is not None
        else None
    )
    has_nearby_action = any(
        seconds is not None and -args.seconds <= seconds <= args.seconds
        for seconds in [seconds_until_due, seconds_until_approval]
    )
    if not args.force and (
        not has_nearby_action
    ):
        details = []
        if next_job is not None:
            details.append(
                f"job {next_job.id} due at "
                f"{next_job.expected_run_at.isoformat(sep=' ', timespec='seconds')}"
            )
        if next_approval_due_at is not None:
            details.append(
                "approval email due at "
                f"{next_approval_due_at.isoformat(sep=' ', timespec='seconds')}"
            )
        next_detail = "; ".join(details) or "no queued booking or approval email"
        print(
            f"Wake-keeper exiting: no BookGym action is due within {args.seconds} seconds; "
            f"{next_detail}."
        )
        return 0

    started_at = datetime.now().astimezone()
    print(
        f"Wake-keeper started at {started_at.isoformat(timespec='seconds')} "
        f"for {args.seconds} seconds.",
        flush=True,
    )
    result = subprocess.run(
        [
            "/usr/bin/caffeinate",
            "-d",
            "-i",
            "-s",
            "-u",
            "-t",
            str(args.seconds),
        ],
        check=False,
    )
    finished_at = datetime.now().astimezone()
    print(
        f"Wake-keeper finished at {finished_at.isoformat(timespec='seconds')} "
        f"with exit code {result.returncode}.",
        flush=True,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
