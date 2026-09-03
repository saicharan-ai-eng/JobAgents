"""SmartRecruiters Posting API adapter.

Public, unauthenticated, documented endpoints:
    GET https://api.smartrecruiters.com/v1/companies/{company_id}/postings?limit=100&offset=0
    GET https://api.smartrecruiters.com/v1/companies/{company_id}/postings/{posting_id}

Unlike Greenhouse/Ashby/Lever, the listing endpoint does NOT include the job
description, so this is a genuine two-stage fetch: Stage A pages through the
listing (offset/limit, `totalFound` drives the stop condition) for
id/title/location/date/URL only; Stage B opens the per-posting detail
endpoint only for identities the history diff marks unseen.
`company_id` comes from config/sources.json (`smartrecruiters_company_id`),
verified per-company before being added.
"""

from __future__ import annotations

import time
from typing import Any

from . import common

API_BASE = "https://api.smartrecruiters.com/v1/companies"
PAGE_LIMIT = 100
MAX_PAGES = 20


def _company_id(entry: dict[str, Any]) -> str:
    company_id = entry.get("smartrecruiters_company_id")
    if not company_id:
        raise common.AdapterError("Missing smartrecruiters_company_id in source config")
    return str(company_id)


def _location_str(posting: dict[str, Any]) -> str | None:
    location = posting.get("location")
    if not isinstance(location, dict):
        return None
    parts = [location.get("city"), location.get("region"), location.get("country")]
    label = ", ".join(str(p) for p in parts if p)
    if location.get("remote"):
        label = f"Remote - {label}" if label else "Remote"
    return label or None


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    company = entry["company"]
    company_id = _company_id(entry)
    inventory: list[dict[str, Any]] = []
    offset = 0
    for _page in range(MAX_PAGES):
        url = f"{API_BASE}/{company_id}/postings"
        data = common.get_json(session, url, timeout, params={"limit": PAGE_LIMIT, "offset": offset})
        content = data.get("content") if isinstance(data, dict) else None
        if not isinstance(content, list) or not content:
            break
        for posting in content:
            if not isinstance(posting, dict):
                continue
            title = str(common.first_nonempty(posting.get("name")) or "").strip()
            matched = common.keyword_matches(title, keywords)
            if not matched:
                continue
            posting_id = posting.get("id")
            ref = posting.get("ref")
            job_url = str(ref) if ref else f"https://jobs.smartrecruiters.com/{company_id}/{posting_id}"
            inventory.append(
                {
                    "company": company,
                    "job_title": title,
                    "job_id": str(posting_id) if posting_id is not None else None,
                    "location": _location_str(posting),
                    "posting_date": common.first_nonempty(posting.get("releasedDate"), posting.get("createdOn")),
                    "job_url": job_url,
                    "source_keyword": matched,
                }
            )
        total_found = data.get("totalFound") if isinstance(data, dict) else None
        offset += len(content)
        if isinstance(total_found, int) and offset >= total_found:
            break
        if len(content) < PAGE_LIMIT:
            break
    return inventory


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    company_id = _company_id(entry)
    job_id = item.get("job_id")
    url = f"{API_BASE}/{company_id}/postings/{job_id}"
    time.sleep(0.25)  # SmartRecruiters detail fetch is a real per-job request; pace it politely.
    data = common.get_json(session, url, timeout)
    job_ad = data.get("jobAd") if isinstance(data, dict) else None
    sections = (job_ad or {}).get("sections") if isinstance(job_ad, dict) else None
    description_parts: list[str] = []
    team = None
    if isinstance(sections, dict):
        for key in ("jobDescription", "qualifications", "additionalInformation"):
            section = sections.get(key)
            if isinstance(section, dict) and section.get("text"):
                description_parts.append(common.strip_html(section["text"]))
    department = data.get("department") if isinstance(data, dict) else None
    if isinstance(department, dict):
        team = common.first_nonempty(department.get("label"), department.get("name"))
    description = "\n\n".join(p for p in description_parts if p)
    return {
        "company": entry["company"],
        "job_title": item["job_title"],
        "job_id": item["job_id"],
        "location": item["location"],
        "posting_date": item["posting_date"],
        "experience_level_text": common.extract_experience(description),
        "job_url": item["job_url"],
        "team_department": team,
        "short_description": description[:500] if description else None,
        "full_description_text": description or None,
        "source_keyword": item["source_keyword"],
    }
