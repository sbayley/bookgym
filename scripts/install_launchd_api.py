from __future__ import annotations

from pathlib import Path
import os
import socket
import subprocess
import sys
import time
from xml.sax.saxutils import escape


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = PROJECT_ROOT / "launchd" / "com.bookgym.api.plist.template"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "com.bookgym.api.plist"
LOG_DIR = PROJECT_ROOT / "artifacts" / "launchd"
LABEL = "com.bookgym.api"
HOST = "127.0.0.1"
PORT = 8000


def preferred_python_executable() -> Path:
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return venv_python
    return Path(sys.executable)


def build_plist() -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "__PYTHON__": escape(str(preferred_python_executable())),
        "__WORKDIR__": escape(str(PROJECT_ROOT.resolve())),
        "__STDOUT__": escape(str((LOG_DIR / "api.stdout.log").resolve())),
        "__STDERR__": escape(str((LOG_DIR / "api.stderr.log").resolve())),
    }

    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def port_is_open(host: str = HOST, port: int = PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


def wait_for_port_to_close(timeout_seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not port_is_open():
            return True
        time.sleep(0.25)
    return not port_is_open()


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(PLIST_PATH)], check=False)

    if not wait_for_port_to_close():
        print(
            f"{HOST}:{PORT} is already in use. Stop the existing uvicorn terminal first, "
            "then rerun this installer."
        )
        return 1

    PLIST_PATH.write_text(build_plist(), encoding="utf-8")
    print(f"Wrote {PLIST_PATH}")
    print(f"Using Python executable {preferred_python_executable()}")

    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST_PATH)], check=True)
    subprocess.run(["launchctl", "enable", f"{domain}/{LABEL}"], check=False)
    subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"], check=True)
    print(f"Installed and started launchd agent {LABEL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
