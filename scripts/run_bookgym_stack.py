from __future__ import annotations

from pathlib import Path
import os
import signal
import subprocess
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def preferred_python_executable() -> str:
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def _spawn(name: str, command: list[str]) -> subprocess.Popen:
    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        env=os.environ.copy(),
    )
    print(f"Started {name} (pid={process.pid}): {' '.join(command)}")
    return process


def _stop_process(name: str, process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    print(f"Stopped {name} (pid={process.pid})")


def main() -> int:
    stop_requested = False
    python_executable = preferred_python_executable()

    def _handle_signal(_signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    processes = {
        "api": _spawn(
            "api",
            [python_executable, "-m", "uvicorn", "app.main:app", "--reload"],
        ),
        "scheduler": _spawn(
            "scheduler",
            [python_executable, str(PROJECT_ROOT / "scripts" / "run_scheduler.py")],
        ),
    }

    exit_code = 0
    try:
        while not stop_requested:
            for name, process in processes.items():
                return_code = process.poll()
                if return_code is None:
                    continue
                print(f"{name} exited with code {return_code}; shutting down the BookGym stack.")
                exit_code = return_code or exit_code
                stop_requested = True
                break
            time.sleep(1)
    finally:
        for name, process in reversed(list(processes.items())):
            _stop_process(name, process)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
