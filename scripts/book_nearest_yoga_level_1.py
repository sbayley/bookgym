from __future__ import annotations

import asyncio
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.jobs.book_class import _run_manual_booking_attempt
from app.time_utils import local_today


if __name__ == "__main__":
    asyncio.run(
        _run_manual_booking_attempt(
            class_name="Yoga Level 1",
            class_date=local_today(),
        )
    )
