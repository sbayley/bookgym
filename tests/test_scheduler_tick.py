from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.scheduler import _run_booking_worker_tick


class SchedulerTickTests(unittest.TestCase):
    def test_idle_tick_preserves_next_due_job_metadata(self) -> None:
        queued_job = SimpleNamespace(id=5, expected_run_at=datetime(2026, 7, 3, 20, 8, 34))

        with (
            patch("app.scheduler.local_now", side_effect=[
                datetime(2026, 7, 3, 19, 58, 1),
                datetime(2026, 7, 3, 19, 58, 2),
            ]),
            patch("app.scheduler.get_next_due_job", side_effect=[queued_job, queued_job]),
            patch("app.scheduler.book_next_available_class", return_value={
                "action": "idle",
                "message": "No due booking jobs were ready.",
            }),
            patch("app.scheduler.update_scheduler_runtime") as update_runtime_mock,
        ):
            _run_booking_worker_tick()

        self.assertEqual(2, update_runtime_mock.call_count)
        final_kwargs = update_runtime_mock.call_args_list[-1].kwargs
        self.assertEqual(5, final_kwargs["next_due_job_id"])
        self.assertEqual(queued_job.expected_run_at, final_kwargs["next_due_expected_run_at"])
        self.assertIn("Next queued job is 5.", final_kwargs["detail"])

    def test_ran_job_tick_updates_next_due_job_metadata(self) -> None:
        next_job = SimpleNamespace(id=2, expected_run_at=datetime(2026, 7, 4, 19, 0, 0))

        with (
            patch("app.scheduler.local_now", side_effect=[
                datetime(2026, 7, 3, 19, 55, 1),
                datetime(2026, 7, 3, 19, 55, 2),
            ]),
            patch("app.scheduler.get_next_due_job", side_effect=[SimpleNamespace(id=4, expected_run_at=datetime(2026, 7, 3, 19, 55, 27)), next_job]),
            patch("app.scheduler.book_next_available_class", return_value={
                "action": "ran_job",
                "job_id": 4,
            }),
            patch("app.scheduler.update_scheduler_runtime") as update_runtime_mock,
        ):
            _run_booking_worker_tick()

        self.assertEqual(2, update_runtime_mock.call_count)
        final_kwargs = update_runtime_mock.call_args_list[-1].kwargs
        self.assertEqual(2, final_kwargs["next_due_job_id"])
        self.assertEqual(next_job.expected_run_at, final_kwargs["next_due_expected_run_at"])
        self.assertIn("Next queued job is 2.", final_kwargs["detail"])


if __name__ == "__main__":
    unittest.main()
