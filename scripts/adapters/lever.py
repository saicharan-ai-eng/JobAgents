"""Lever Postings API adapter.

Public, unauthenticated, documented endpoint:
    GET https://api.lever.co/v0/postings/{company_slug}?mode=json

Returns every open posting in one un-paginated array, including
`description`/`descriptionPlain` -- Stage B is field extraction from the
same payload Stage A already fetched, not a second network call.
`company_slug` comes from config/sources.json (`lever_company_slug`),
verified per-company before being added.
"""

from __future__ import annotations

from typing import Any

from . import common

API_BASE = "https://api.lever.co/v0/postings"


def _company_slug(entry: dict[str, Any]) -> str:
    slug = entry.get("lever_company_slug")
    if not slug:
        raise common.AdapterError("Missing lever_company_slug in source config")
    return str(slug)


def _location_str(job: dict[str, Any]) -> str | None:
    categories = job.get("categories") or {}
    if isinstance(categories, dict):
        loc = common.first_nonempty(categories.get("location"), categories.get("allLocations"))
        if isinstance(loc, list):
            return " | ".join(str(x) for x in loc if x)
        return loc
    return None


def _team(job: dict[str, Any]) -> str | None:
    categories = job.get("categories") or {}
    if isinstance(categories, dict):
        return common.first_nonempty(categories.get("team"), categories.get("department"))
    return None


def _fetch_catalog(entry: dict[str, Any], session, timeout: int) -> list[dict[str, Any]]:
    slug = _company_slug(entry)
    url = f"{API_BASE}/{slug}"
    data = common.get_json(session, url, timeout, params={"mode": "json"})
    if not isinstance(data, list):
        raise common.AdapterError(f"Lever company {slug!r}: response was not a JSON array")
    return data


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    company = entry["company"]
    jobs = _fetch_catalog(entry, session, timeout)
    inventory: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(common.first_nonempty(job.get("text")) or "").strip()
        description = common.strip_html(job.get("description") or job.get("descriptionPlain"))
        matched = common.keyword_matches(f"{title}\n{description}", keywords)
        if not matched:
            continue
        job_id = job.get("id")
        job_url = common.first_nonempty(job.get("hostedUrl"), job.get("applyUrl"))
        created = job.get("createdAt")
        posting_date = None
        if isinstance(created, (int, float)):
            from datetime import datetime, timezone

            posting_date = datetime.fromtimestamp(created / 1000, tz=timezone.utc).date().isoformat()
        inventory.append(
            {
                "company": company,
                "job_title": title,
                "job_id": str(job_id) if job_id is not None else None,
                "location": _location_str(job),
                "posting_date": posting_date,
                "job_url": str(job_url or ""),
                "source_keyword": matched,
                "_platform_ref": job,
            }
        )
    return inventory


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    job = item.get("_platform_ref") or {}
    description = common.strip_html(job.get("description") or job.get("descriptionPlain"))
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
