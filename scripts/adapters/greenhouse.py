"""Greenhouse Job Board API adapter.

Public, unauthenticated, documented endpoint:
    GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true

Returns every open posting for the board in one request, including the full
HTML description -- so unlike Workday there is no separate per-job detail
call: Stage B is field extraction from the same payload Stage A already
fetched (same pattern already used for the AWS source, see CLAUDE.md /
state/source_baseline_status.json "aws" coverage notes), not an additional
network request. `board_token` comes from config/sources.json
(`greenhouse_board_token`), verified per-company against the live board
before being added -- never guessed from the company name.
"""

from __future__ import annotations

from typing import Any

from . import common

API_BASE = "https://boards-api.greenhouse.io/v1/boards"


def _board_token(entry: dict[str, Any]) -> str:
    token = entry.get("greenhouse_board_token")
    if not token:
        raise common.AdapterError("Missing greenhouse_board_token in source config")
    return str(token)


def _location_str(job: dict[str, Any]) -> str | None:
    location = job.get("location")
    if isinstance(location, dict):
        return common.first_nonempty(location.get("name"))
    return common.first_nonempty(location)


def _team(job: dict[str, Any]) -> str | None:
    departments = job.get("departments")
    if isinstance(departments, list) and departments:
        first = departments[0]
        if isinstance(first, dict):
            return common.first_nonempty(first.get("name"))
    return None


def _fetch_catalog(entry: dict[str, Any], session, timeout: int) -> list[dict[str, Any]]:
    token = _board_token(entry)
    url = f"{API_BASE}/{token}/jobs"
    data = common.get_json(session, url, timeout, params={"content": "true"})
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise common.AdapterError(f"Greenhouse board {token!r}: response missing 'jobs' array")
    return jobs


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    company = entry["company"]
    jobs = _fetch_catalog(entry, session, timeout)
    inventory: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(common.first_nonempty(job.get("title")) or "").strip()
        description = common.strip_html(job.get("content"))
        matched = common.keyword_matches(f"{title}\n{description}", keywords)
        if not matched:
            continue
        job_id = job.get("id")
        inventory.append(
            {
                "company": company,
                "job_title": title,
                "job_id": str(job_id) if job_id is not None else None,
                "location": _location_str(job),
                "posting_date": common.first_nonempty(job.get("first_published"), job.get("updated_at")),
                "job_url": str(common.first_nonempty(job.get("absolute_url")) or ""),
                "source_keyword": matched,
                "_platform_ref": job,
            }
        )
    return inventory


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    job = item.get("_platform_ref") or {}
    description = common.strip_html(job.get("content"))
    return {
        "company": entry["company"],
        "job_title": item["job_title"],
        "job_id": item["job_id"],
        "location": item["location"],
        "posting_date": item["posting_date"],
        "experience_level_text": common.extract_experience(description),
        "job_url": item["job_url"],
        "team_department": _team(job),
        "short_description": description[:500] if description else None,
        "full_description_text": description or None,
        "source_keyword": item["source_keyword"],
    }
