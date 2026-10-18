from __future__ import annotations

from pathlib import Path
import os
import subprocess


PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "com.bookgym.scheduler.plist"


def main() -> int:
    domain = f"gui/{os.getuid()}"
    label = "com.bookgym.scheduler"
    subprocess.run(["launchctl", "bootout", domain, str(PLIST_PATH)], check=False)
    subprocess.run(["launchctl", "disable", f"{domain}/{label}"], check=False)
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        print(f"Removed {PLIST_PATH}")
    else:
        print(f"No plist found at {PLIST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
