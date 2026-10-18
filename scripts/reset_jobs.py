from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import get_session, init_db
from app.models import BookingJob, BookingStatus
from app.time_utils import local_now


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "job_ids",
        nargs="*",
        type=int,
        help="Specific job IDs to reset.",
    )
    parser.add_argument(
        "--failed",
        action="store_true",
        help="Reset all failed jobs.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    init_db()

    if not args.job_ids and not args.failed:
        print("Pass one or more job IDs, or use --failed.")
        return

    now = local_now()
    reset_count = 0

    with get_session() as session:
        jobs: list[BookingJob] = []
        for job_id in args.job_ids:
            job = session.get(BookingJob, job_id)
            if job is None:
                print(f"Job {job_id} not found.")
                continue
            jobs.append(job)

        if args.failed:
            failed_jobs = session.query(BookingJob).filter(
                BookingJob.status == BookingStatus.FAILED
            ).all()
            jobs.extend(failed_jobs)

        seen_ids: set[int] = set()
        for job in jobs:
            if job.id is None or job.id in seen_ids:
                continue
            seen_ids.add(job.id)

            job.status = BookingStatus.PENDING
            job.last_message = "Reset to pending by scripts/reset_jobs.py"
            job.last_screenshot_path = ""
            job.updated_at = now
            session.add(job)
            reset_count += 1
            print(f"Reset job {job.id} to pending.")

        session.commit()

    print(f"Reset {reset_count} job(s).")


if __name__ == "__main__":
    main()
