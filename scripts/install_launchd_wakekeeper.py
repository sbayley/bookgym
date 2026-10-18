from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
from xml.sax.saxutils import escape


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = PROJECT_ROOT / "launchd" / "com.bookgym.wakekeeper.plist.template"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "com.bookgym.wakekeeper.plist"
LOG_DIR = PROJECT_ROOT / "artifacts" / "launchd"


def preferred_python_executable() -> Path:
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return venv_python
    return Path(sys.executable)


def build_plist() -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "__PYTHON__": escape(str(preferred_python_executable())),
        "__SCRIPT__": escape(str((PROJECT_ROOT / "scripts" / "run_wakekeeper.py").resolve())),
        "__WORKDIR__": escape(str(PROJECT_ROOT.resolve())),
        "__STDOUT__": escape(str((LOG_DIR / "wakekeeper.stdout.log").resolve())),
        "__STDERR__": escape(str((LOG_DIR / "wakekeeper.stderr.log").resolve())),
    }

    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(build_plist(), encoding="utf-8")
    print(f"Wrote {PLIST_PATH}")
    print(f"Using Python executable {preferred_python_executable()}")

    domain = f"gui/{os.getuid()}"
    label = "com.bookgym.wakekeeper"
    subprocess.run(["launchctl", "bootout", domain, str(PLIST_PATH)], check=False)
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST_PATH)], check=True)
    subprocess.run(["launchctl", "enable", f"{domain}/{label}"], check=False)
    print(f"Installed launchd agent {label} for 6:58 PM daily")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
