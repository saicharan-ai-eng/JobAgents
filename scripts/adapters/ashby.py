"""Ashby Job Board (Posting API) adapter.

Public, unauthenticated, documented endpoint:
    GET https://api.ashbyhq.com/posting-api/job-board/{board_name}?includeCompensation=true

Returns every open posting for the board in one un-paginated request,
including `descriptionHtml` -- so, like Greenhouse, Stage B is field
extraction from the same payload Stage A already fetched, not a second
network call. `board_name` comes from config/sources.json
(`ashby_board_name`), verified per-company (Ashby board names do not always
match the company's obvious slug -- e.g. Mistral AI's board is `mistral.ai`,
not `mistral`) before being added.
"""

from __future__ import annotations

from typing import Any

from . import common

API_BASE = "https://api.ashbyhq.com/posting-api/job-board"


def _board_name(entry: dict[str, Any]) -> str:
    name = entry.get("ashby_board_name")
    if not name:
        raise common.AdapterError("Missing ashby_board_name in source config")
    return str(name)


def _location_str(job: dict[str, Any]) -> str | None:
    location = job.get("location")
    if isinstance(location, dict):
        return common.first_nonempty(location.get("locationName"), location.get("name"))
    return common.first_nonempty(location, job.get("locationName"))


def _team(job: dict[str, Any]) -> str | None:
    return common.first_nonempty(job.get("team"), job.get("department"))


def _fetch_catalog(entry: dict[str, Any], session, timeout: int) -> list[dict[str, Any]]:
    name = _board_name(entry)
    url = f"{API_BASE}/{name}"
    data = common.get_json(session, url, timeout, params={"includeCompensation": "true"})
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise common.AdapterError(f"Ashby board {name!r}: response missing 'jobs' array")
    return jobs


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    company = entry["company"]
    jobs = _fetch_catalog(entry, session, timeout)
    inventory: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(common.first_nonempty(job.get("title")) or "").strip()
        description = common.strip_html(job.get("descriptionHtml") or job.get("descriptionPlain"))
        matched = common.keyword_matches(f"{title}\n{description}", keywords)
        if not matched:
            continue
        job_id = job.get("id")
        job_url = common.first_nonempty(job.get("jobUrl"), job.get("applyUrl"))
        inventory.append(
            {
                "company": company,
                "job_title": title,
                "job_id": str(job_id) if job_id is not None else None,
                "location": _location_str(job),
                "posting_date": common.first_nonempty(job.get("publishedAt"), job.get("updatedAt")),
                "job_url": str(job_url or ""),
                "source_keyword": matched,
                "_platform_ref": job,
            }
        )
    return inventory


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    job = item.get("_platform_ref") or {}
    description = common.strip_html(job.get("descriptionHtml") or job.get("descriptionPlain"))
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
