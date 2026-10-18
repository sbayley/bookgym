from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import (
    install_launchd_preview,
    install_launchd_scheduler,
    install_launchd_wakekeeper,
    run_bookgym_stack,
)


class LaunchdScriptTests(unittest.TestCase):
    def test_preview_plist_runs_private_preview_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            template_path = project_root / "com.bookgym.preview.plist.template"
            template_path.write_text(
                "__SCRIPT__\n__WORKDIR__\n__STDOUT__\n__STDERR__\n",
                encoding="utf-8",
            )

            with (
                patch.object(install_launchd_preview, "PROJECT_ROOT", project_root),
                patch.object(install_launchd_preview, "TEMPLATE_PATH", template_path),
                patch.object(install_launchd_preview, "LOG_DIR", project_root / "logs"),
            ):
                plist = install_launchd_preview.build_plist()

        self.assertIn("run_mobile_preview.sh", plist)
        self.assertIn(str(project_root), plist)

    def test_install_plist_prefers_project_venv_python(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            template_path = project_root / "com.bookgym.scheduler.plist.template"
            template_path.write_text(
                "__PYTHON__\n__SCRIPT__\n__WORKDIR__\n__STDOUT__\n__STDERR__\n",
                encoding="utf-8",
            )

            venv_python = project_root / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True, exist_ok=True)
            venv_python.write_text("", encoding="utf-8")

            log_dir = project_root / "logs"

            with (
                patch.object(install_launchd_scheduler, "PROJECT_ROOT", project_root),
                patch.object(install_launchd_scheduler, "TEMPLATE_PATH", template_path),
                patch.object(install_launchd_scheduler, "LOG_DIR", log_dir),
                patch("scripts.install_launchd_scheduler.sys.executable", "/usr/bin/python3"),
            ):
                plist = install_launchd_scheduler.build_plist()

        self.assertIn(str(venv_python), plist)
        self.assertNotIn("/usr/bin/python3", plist)

    def test_stack_launcher_prefers_project_venv_python(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            venv_python = project_root / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True, exist_ok=True)
            venv_python.write_text("", encoding="utf-8")

            with (
                patch.object(run_bookgym_stack, "PROJECT_ROOT", project_root),
                patch("scripts.run_bookgym_stack.sys.executable", "/usr/bin/python3"),
            ):
                selected = run_bookgym_stack.preferred_python_executable()

        self.assertEqual(str(venv_python), selected)

    def test_wakekeeper_plist_runs_daily_before_booking_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            template_path = project_root / "com.bookgym.wakekeeper.plist.template"
            template_path.write_text(
                "__PYTHON__\n__SCRIPT__\n__WORKDIR__\n__STDOUT__\n__STDERR__\n"
                "<key>Hour</key><integer>18</integer>\n"
                "<key>Minute</key><integer>58</integer>\n",
                encoding="utf-8",
            )

            venv_python = project_root / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True, exist_ok=True)
            venv_python.write_text("", encoding="utf-8")

            with (
                patch.object(install_launchd_wakekeeper, "PROJECT_ROOT", project_root),
                patch.object(install_launchd_wakekeeper, "TEMPLATE_PATH", template_path),
                patch.object(install_launchd_wakekeeper, "LOG_DIR", project_root / "logs"),
            ):
                plist = install_launchd_wakekeeper.build_plist()

        self.assertIn(str(venv_python), plist)
        self.assertIn("run_wakekeeper.py", plist)
        self.assertIn("<integer>18</integer>", plist)
        self.assertIn("<integer>58</integer>", plist)


if __name__ == "__main__":
    unittest.main()
