from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.network_capture import (
    arm_next_booking_capture,
    claim_next_booking_capture,
    complete_next_booking_capture,
    next_booking_capture_claim_path,
    next_booking_capture_state,
)


class NextBookingNetworkCaptureTests(unittest.TestCase):
    def test_marker_is_claimed_and_completed_by_one_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "capture-next-booking.json"
            with patch("app.network_capture.CAPTURE_NEXT_BOOKING_MARKER", marker):
                self.assertEqual(marker, arm_next_booking_capture())
                self.assertEqual(
                    {"armed": True, "claimed_job_id": None, "path": str(marker)},
                    next_booking_capture_state(),
                )

                self.assertTrue(claim_next_booking_capture(13))
                self.assertFalse(marker.exists())
                self.assertTrue(next_booking_capture_claim_path(13).exists())
                self.assertEqual(13, next_booking_capture_state()["claimed_job_id"])
                self.assertFalse(claim_next_booking_capture(14))

                complete_next_booking_capture(13)
                self.assertEqual(
                    {"armed": False, "claimed_job_id": None, "path": ""},
                    next_booking_capture_state(),
                )


if __name__ == "__main__":
    unittest.main()
