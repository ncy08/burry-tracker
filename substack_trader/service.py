"""launchd service management with StartCalendarInterval scheduling.

Install, uninstall, and status management with two deliberate design
choices:

- Replaces `KeepAlive` + `RunAtLoad` with `StartCalendarInterval` (15
  entries: 8am, 1pm, 6pm ET on weekdays Mon-Fri).
- This is a SCHEDULED service, not always-on. KeepAlive would
  auto-restart the daemon between fires; RunAtLoad would fire it at
  install time. Both are inappropriate for a 3x-per-weekday cadence.

TZ env var ensures Hour values fire on America/New_York regardless of
system timezone.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

SERVICE_NAME = "com.burry-tracker.substack-trader"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_NAME}.plist"
LOG_DIR = Path.home() / ".local" / "log"
LOG_PATH = LOG_DIR / "substack-trader-stdout.log"
ERR_PATH = LOG_DIR / "substack-trader-stderr.log"

SCHEDULE_HOURS = (8, 13, 18)  # 8am, 1pm, 6pm
SCHEDULE_WEEKDAYS = (1, 2, 3, 4, 5)  # Mon-Fri (launchd: 0=Sun, 6=Sat)


def get_python_path() -> str:
    """Get the path to the current Python interpreter."""
    return sys.executable


def get_working_dir() -> Path:
    """Get the repository root (parent of this package)."""
    return Path(__file__).parent.parent


def _calendar_interval_block() -> str:
    """Build the StartCalendarInterval array (3 hours x 5 weekdays = 15 entries)."""
    entries = []
    for hour in SCHEDULE_HOURS:
        for weekday in SCHEDULE_WEEKDAYS:
            entries.append(
                f"        <dict><key>Hour</key><integer>{hour}</integer>"
                f"<key>Minute</key><integer>0</integer>"
                f"<key>Weekday</key><integer>{weekday}</integer></dict>"
            )
    return "\n".join(entries)


def generate_plist() -> str:
    """Generate the launchd plist content."""
    python_path = get_python_path()
    working_dir = get_working_dir()
    intervals = _calendar_interval_block()

    return dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
        "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{SERVICE_NAME}</string>
            <key>ProgramArguments</key>
            <array>
                <string>{python_path}</string>
                <string>-m</string>
                <string>substack_trader</string>
                <string>run</string>
            </array>
            <key>WorkingDirectory</key>
            <string>{working_dir}</string>
            <key>StartCalendarInterval</key>
            <array>
{intervals}
            </array>
            <key>StandardOutPath</key>
            <string>{LOG_PATH}</string>
            <key>StandardErrorPath</key>
            <string>{ERR_PATH}</string>
            <key>EnvironmentVariables</key>
            <dict>
                <key>PATH</key>
                <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
                <key>HOME</key>
                <string>{Path.home()}</string>
                <key>TZ</key>
                <string>America/New_York</string>
            </dict>
        </dict>
        </plist>
    """)


def cmd_install_service() -> int:
    """Install the launchd service."""
    plist_content = generate_plist()
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(plist_content)
    print(f"Created: {PLIST_PATH}")

    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{SERVICE_NAME}"],
            capture_output=True,
        )
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
            capture_output=True,
            text=True,
        )

    if result.returncode == 0:
        print(f"Service installed: {SERVICE_NAME}")
        print("Schedule: 8am / 1pm / 6pm ET, Mon-Fri")
        print(f"Logs:    {LOG_PATH}")
        print(f"Errors:  {ERR_PATH}")
        print(f"\nTo verify: launchctl list | grep {SERVICE_NAME}")
        return 0
    print(f"Failed to start service: {result.stderr}", file=sys.stderr)
    return 1


def cmd_uninstall_service() -> int:
    """Uninstall the launchd service."""
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{SERVICE_NAME}"],
        capture_output=True,
        text=True,
    )
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        print(f"Removed: {PLIST_PATH}")
    print(f"Service uninstalled: {SERVICE_NAME}")
    return 0


def cmd_service_status() -> int:
    """Check the service status."""
    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{SERVICE_NAME}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"Service: {SERVICE_NAME}")
        print("Status: loaded")
        print("Schedule: 8am / 1pm / 6pm ET, Mon-Fri")
        for line in result.stdout.splitlines():
            if "pid" in line.lower() or "next fire" in line.lower():
                print(f"  {line.strip()}")
        print(f"\nLogs:    {LOG_PATH}")
        print(f"Errors:  {ERR_PATH}")
    else:
        print(f"Service: {SERVICE_NAME}")
        print("Status: not loaded")
        if PLIST_PATH.exists():
            print("  (plist exists but service is not loaded)")
        else:
            print("  (not installed)")
    return 0
