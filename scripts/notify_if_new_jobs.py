#!/usr/bin/env python3
"""Deliver a best-effort local desktop alert only when notification.json requests it."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_delivery(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--config", default="config/notifications.json")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    notification_path = run_dir / "notification.json"
    output_path = run_dir / "notification_delivery.json"
    notification = json.loads(notification_path.read_text(encoding="utf-8"))

    result: dict[str, Any] = {
        "attempted_at": utc_now(),
        "should_notify": bool(notification.get("should_notify")),
        "status": None,
        "method": None,
        "reason": None,
    }

    if not result["should_notify"]:
        result.update(status="skipped", reason="No new jobs")
        write_delivery(output_path, result)
        print(json.dumps(result, indent=2))
        return 0

    if not bool(notification.get("desktop_notifications_enabled", True)):
        result.update(status="skipped", reason="Desktop notifications disabled in config/notifications.json")
        write_delivery(output_path, result)
        print(json.dumps(result, indent=2))
        return 0

    title = str(notification.get("title") or "New AI/ML jobs found")
    message = str(notification.get("message") or "Open new_jobs.md for details")
    system = platform.system().lower()

    try:
        if system == "windows":
            powershell = shutil.which("powershell") or shutil.which("pwsh")
            script = Path(__file__).with_name("windows_desktop_notification.ps1")
            if not powershell or not script.exists():
                raise RuntimeError("PowerShell notification helper is unavailable")
            config = {}
            config_path = Path(args.config)
            if config_path.exists():
                config = json.loads(config_path.read_text(encoding="utf-8"))
            duration = max(1, int(config.get("notification_duration_seconds", 8)))
            subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-Title",
                    title,
                    "-Message",
                    message,
                    "-DurationSeconds",
                    str(duration),
                ],
                check=True,
                timeout=duration + 15,
            )
            result.update(status="delivered", method="windows-notify-icon")
        elif system == "darwin":
            osascript = shutil.which("osascript")
            if not osascript:
                raise RuntimeError("osascript is unavailable")
            escaped_title = title.replace('"', '\\"')
            escaped_message = message.replace('"', '\\"')
            subprocess.run(
                [osascript, "-e", f'display notification "{escaped_message}" with title "{escaped_title}"'],
                check=True,
                timeout=15,
            )
            result.update(status="delivered", method="macos-notification-center")
        else:
            notify_send = shutil.which("notify-send")
            if not notify_send:
                raise RuntimeError("notify-send is unavailable in this environment")
            subprocess.run([notify_send, title, message], check=True, timeout=15)
            result.update(status="delivered", method="notify-send")
    except Exception as exc:  # Notification failure must not invalidate the job search.
        result.update(status="unavailable", method=system or "unknown", reason=f"{type(exc).__name__}: {exc}")

    write_delivery(output_path, result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
