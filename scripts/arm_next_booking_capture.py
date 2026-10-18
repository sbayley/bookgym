from __future__ import annotations

from pathlib import Path
import json
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.network_capture import arm_next_booking_capture, next_booking_capture_state


def main() -> int:
    path = arm_next_booking_capture()
    print(f"Armed one-shot network capture for the next live booking: {path}")
    print(json.dumps(next_booking_capture_state(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
