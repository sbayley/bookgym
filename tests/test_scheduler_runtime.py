from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.scheduler_runtime import scheduler_status_snapshot, update_scheduler_runtime
from app.time_utils import local_now


class SchedulerRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.status_path = Path(self.temp_dir.name) / "scheduler-status.json"
        self.path_patch = patch(
            "app.scheduler_runtime.scheduler_status_path",
            return_value=self.status_path,
        )
        self.path_patch.start()

    def tearDown(self) -> None:
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_snapshot_is_missing_without_heartbeat(self) -> None:
        snapshot = scheduler_status_snapshot()

        self.assertEqual("missing", snapshot["status"])
        self.assertFalse(snapshot["is_running"])

    def test_update_scheduler_runtime_records_running_heartbeat(self) -> None:
        update_scheduler_runtime(detail="Scheduler heartbeat is fresh.")

        snapshot = scheduler_status_snapshot()

        self.assertEqual("running", snapshot["status"])
        self.assertTrue(snapshot["is_running"])
        self.assertEqual("Scheduler heartbeat is fresh.", snapshot["detail"])

    def test_snapshot_becomes_stale_when_heartbeat_is_old(self) -> None:
        old_heartbeat = (local_now() - timedelta(seconds=120)).isoformat()
        self.status_path.write_text(
            json.dumps(
                {
                    "detail": "Old heartbeat",
                    "last_heartbeat_at": old_heartbeat,
                    "pid": 1234,
                }
            ),
            encoding="utf-8",
        )

        snapshot = scheduler_status_snapshot()

        self.assertEqual("stale", snapshot["status"])
        self.assertTrue(snapshot["is_stale"])
        self.assertEqual(1234, snapshot["pid"])


if __name__ == "__main__":
    unittest.main()
