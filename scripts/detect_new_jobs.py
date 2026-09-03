#!/usr/bin/env python3
"""Compare a completed run with persistent history and produce ranked new-job alerts."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_SCHEMA_VERSION = 1
STATUS_START = "<!-- NEW-JOB-STATUS:START -->"
STATUS_END = "<!-- NEW-JOB-STATUS:END -->"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def stable_job_key(job: dict[str, Any]) -> str:
    company = normalize(job.get("company"))
    job_id = normalize(job.get("job_id"))
    if job_id:
        return f"{company}|id|{job_id}"
    return "|".join(
        [
            company,
            "fallback",
            normalize(job.get("job_title")),
            normalize(job.get("location")),
        ]
    )


def compact_job(job: dict[str, Any], first_seen_at: str, last_seen_at: str) -> dict[str, Any]:
    return {
        "company": job.get("company"),
        "job_id": job.get("job_id"),
        "job_title": job.get("job_title"),
        "location": job.get("location"),
        "posting_date": job.get("posting_date"),
        "job_url": job.get("job_url"),
        "level_classification": job.get("level_classification"),
        "experience_required": job.get("experience_required"),
        "relevance_keywords_matched": job.get("relevance_keywords_matched") or [],
        "fit_priority": job.get("fit_priority"),
        "fit_label": job.get("fit_label"),
        "fit_score": job.get("fit_score"),
        "fit_keywords_matched": job.get("fit_keywords_matched") or [],
        "discovery_type": job.get("discovery_type") or "new_posting",
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
    }


def escape_cell(value: Any) -> str:
    return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def render_job_table(jobs: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Priority Fit | Score | Company | Job Title | Level | Experience Required | Location | Fit Signals | Posted Date | Direct Apply Link |",
        "|---|---:|---|---|---|---|---|---|---|---|",
    ]
    for job in jobs:
        url = job.get("job_url")
        link = f"[Apply]({url})" if url else "—"
        lines.append(
            "| "
            + " | ".join(
                [
                    escape_cell(job.get("fit_label")),
                    escape_cell(job.get("fit_score")),
                    escape_cell(job.get("company")),
                    escape_cell(job.get("job_title")),
                    escape_cell(job.get("level_classification")),
                    escape_cell(job.get("experience_required")),
                    escape_cell(job.get("location")),
                    escape_cell(", ".join(job.get("fit_keywords_matched") or [])),
                    escape_cell(job.get("posting_date")),
                    link,
                ]
            )
            + " |"
        )
    return lines


def write_new_jobs_report(
    path: Path,
    *,
    new_jobs: list[dict[str, Any]],
    baseline_created: bool,
    current_count: int,
    timestamp: str,
) -> None:
    lines = ["# New Prioritized AI/ML Job Alert", "", f"Run timestamp: {timestamp}", ""]
    if baseline_created:
        lines.extend(
            [
                "## Baseline established",
                "",
                f"Stored {current_count} currently matching jobs as the starting baseline.",
                "No alert was sent because there was no earlier run to compare against.",
            ]
        )
    elif not new_jobs:
        lines.extend(["## No new matching jobs", "", "No previously unseen matching jobs were found in this run."])
    else:
        counts = Counter(int(job.get("fit_priority") or 4) for job in new_jobs)
        lines.extend(
            [
                f"## {len(new_jobs)} new matching job(s)",
                "",
                f"- Excellent inference/infrastructure fits: {counts.get(1, 0)}",
                f"- Strong GPU/systems adjacent fits: {counts.get(2, 0)}",
                f"- General AI/ML fits: {counts.get(3, 0)}",
                f"- Low-priority backups: {counts.get(4, 0)}",
                "",
            ]
        )
        lines.extend(render_job_table(new_jobs))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_main_report(
    report_path: Path,
    *,
    new_jobs: list[dict[str, Any]],
    baseline_created: bool,
    new_jobs_report_path: Path,
) -> None:
    if not report_path.exists():
        return
    existing = report_path.read_text(encoding="utf-8")
    status_lines = [STATUS_START, "", "## New-Job Monitor", ""]
    if baseline_created:
        status_lines.append("- Baseline created during this run; no notification was sent.")
    elif new_jobs:
        priority_counts = Counter(int(job.get("fit_priority") or 4) for job in new_jobs)
        status_lines.append(f"- **{len(new_jobs)} previously unseen matching job(s) found.**")
        status_lines.append(
            f"- Priority breakdown: P1={priority_counts.get(1, 0)}, P2={priority_counts.get(2, 0)}, "
            f"P3={priority_counts.get(3, 0)}, P4={priority_counts.get(4, 0)}."
        )
    else:
        status_lines.append("- No previously unseen matching jobs were found; no alert was sent.")
    status_lines.append(f"- Details: `{new_jobs_report_path.as_posix()}`")
    status_lines.extend(["", STATUS_END])
    status_block = "\n".join(status_lines)

    pattern = re.compile(re.escape(STATUS_START) + r".*?" + re.escape(STATUS_END), re.S)
    if pattern.search(existing):
        updated = pattern.sub(status_block, existing)
    else:
        updated = existing.rstrip() + "\n\n" + status_block + "\n"
    report_path.write_text(updated, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--state-file", default="state/seen_jobs.json")
    parser.add_argument("--config", default="config/notifications.json")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    state_file = Path(args.state_file)
    config_path = Path(args.config)
    deduped_path = run_dir / "deduplicated.json"
    if not deduped_path.exists():
        raise FileNotFoundError(f"Missing completed run output: {deduped_path}")

    jobs = load_json(deduped_path)
    if not isinstance(jobs, list) or not all(isinstance(job, dict) for job in jobs):
        raise ValueError("deduplicated.json must contain a JSON array of job objects")

    config: dict[str, Any] = {}
    if config_path.exists():
        loaded_config = load_json(config_path)
        if not isinstance(loaded_config, dict):
            raise ValueError(f"Notification config must be a JSON object: {config_path}")
        config = loaded_config

    timestamp = utc_now()
    state_exists = state_file.exists()
    if state_exists:
        state = load_json(state_file)
        if not isinstance(state, dict) or not isinstance(state.get("seen"), dict):
            raise ValueError(f"Refusing to overwrite invalid state file: {state_file}")
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported state schema in {state_file}")
    else:
        state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "created_at": timestamp,
            "updated_at": timestamp,
            "last_run_id": None,
            "seen": {},
        }

    seen: dict[str, dict[str, Any]] = state["seen"]
    first_run = not state_exists
    notify_on_first_run = bool(config.get("notify_on_first_run", False))
    baseline_created = first_run and not notify_on_first_run
    new_jobs: list[dict[str, Any]] = []

    # Compute the updated qualifying-job history in memory only -- it is not
    # written to state_file yet. See the note below the notification-output
    # writes for why this ordering matters.
    updated_seen: dict[str, dict[str, Any]] = dict(seen)
    for job in jobs:
        key = stable_job_key(job)
        previous = seen.get(key)
        # A job's discovery_type gates whether it may ever notify, independent
        # of whether it's absent from state/seen_jobs.json. Sources without
        # baseline-completeness tracking (or already-"complete" sources) never
        # set this field, so it defaults to "new_posting" -- unchanged
        # behavior for the normal case. "baseline_catchup" (a pre-existing
        # posting an incomplete source's baseline scan hadn't reached yet),
        # "baseline_existing", and "previously_processed" must never notify,
        # even the first time they're seen here.
        discovery_type = str(job.get("discovery_type") or "new_posting")
        if previous is None:
            if not baseline_created and discovery_type == "new_posting":
                new_job = dict(job)
                new_job["monitor_key"] = key
                new_job["first_seen_at"] = timestamp
                new_jobs.append(new_job)
            updated_seen[key] = compact_job(job, timestamp, timestamp)
        else:
            first_seen_at = str(previous.get("first_seen_at") or timestamp)
            updated_seen[key] = compact_job(job, first_seen_at, timestamp)

    new_jobs_path = run_dir / "new_jobs.json"
    new_jobs_report_path = run_dir / "new_jobs.md"
    atomic_write_json(new_jobs_path, new_jobs)
    write_new_jobs_report(
        new_jobs_report_path,
        new_jobs=new_jobs,
        baseline_created=baseline_created,
        current_count=len(jobs),
        timestamp=timestamp,
    )

    max_titles = max(1, int(config.get("max_titles_in_notification", 3)))
    title_samples = [
        f"[P{int(job.get('fit_priority') or 4)}] {job.get('company')}: {job.get('job_title')}"
        for job in new_jobs[:max_titles]
    ]
    extra_count = max(0, len(new_jobs) - len(title_samples))
    message_parts = title_samples[:]
    if extra_count:
        message_parts.append(f"and {extra_count} more")

    priority_counts = Counter(int(job.get("fit_priority") or 4) for job in new_jobs)
    should_notify = bool(new_jobs)
    high_priority_count = priority_counts.get(1, 0) + priority_counts.get(2, 0)
    if should_notify:
        title = f"{len(new_jobs)} new AI/ML job{'s' if len(new_jobs) != 1 else ''}"
        if high_priority_count:
            title += f" — {high_priority_count} high-priority"
    else:
        title = "No new AI/ML jobs"

    notification = {
        "should_notify": should_notify,
        "baseline_created": baseline_created,
        "new_job_count": len(new_jobs),
        "high_priority_new_job_count": high_priority_count,
        "new_jobs_by_priority": {str(priority): priority_counts.get(priority, 0) for priority in range(1, 5)},
        "current_matching_job_count": len(jobs),
        "run_id": run_dir.name,
        "created_at": timestamp,
        "title": title,
        "message": "; ".join(message_parts) if message_parts else (
            "Baseline created; future runs will alert only on unseen jobs."
            if baseline_created
            else "No previously unseen matching jobs were found."
        ),
        "new_jobs_json": str(new_jobs_path),
        "new_jobs_report": str(new_jobs_report_path),
        "priority_shortlist": str(run_dir / "priority_shortlist.md"),
        "desktop_notifications_enabled": bool(config.get("desktop_notifications", True)),
    }
    atomic_write_json(run_dir / "notification.json", notification)
    update_main_report(
        run_dir / "report.md",
        new_jobs=new_jobs,
        baseline_created=baseline_created,
        new_jobs_report_path=new_jobs_report_path,
    )

    # Commit the qualifying-job history only now, after every notification
    # output file above has been fully and atomically written. If the
    # process crashes anywhere before this point, state_file is untouched --
    # the next run re-reads the same old state, recomputes the identical
    # new_jobs, and the notification is safely retried rather than lost.
    # Once this commit succeeds, every job in `jobs` (new or not) is in
    # `seen`, so a later run can never alert on it again.
    state["updated_at"] = timestamp
    state["last_run_id"] = run_dir.name
    state["seen"] = updated_seen
    atomic_write_json(state_file, state)

    print(json.dumps(notification, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
