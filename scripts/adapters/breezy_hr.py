"""Breezy HR adapter.

Public, unauthenticated, documented endpoint:
    GET https://{company_slug}.breezy.hr/json

Returns every open posting for the board in one un-paginated JSON array.
Confirmed unauthenticated (cold curl, no cookies, `Access-Control-Allow-Origin: *`)
during onboarding research (2026-09-02). The list payload does not include
the full description -- only Stage B (the posting's own `url`) has it, in an
HTML page whose `.position-description` element holds the real text
(confirmed live against cyber-advisors.breezy.hr). `company_slug` comes from
config/sources.json (`breezy_hr_company_slug`), verified per-company before
being added.
"""

from __future__ import annotations

from typing import Any

from . import common


def _company_slug(entry: dict[str, Any]) -> str:
    slug = entry.get("breezy_hr_company_slug")
    if not slug:
        raise common.AdapterError("Missing breezy_hr_company_slug in source config")
    return str(slug)


def _location_str(job: dict[str, Any]) -> str | None:
    location = job.get("location")
    if not isinstance(location, dict):
        return None
    parts = [
        common.first_nonempty(location.get("city")),
        common.first_nonempty((location.get("state") or {}).get("name")) if isinstance(location.get("state"), dict) else None,
        common.first_nonempty((location.get("country") or {}).get("name")) if isinstance(location.get("country"), dict) else None,
    ]
    joined = ", ".join(str(p) for p in parts if p)
    if joined:
        return joined
    return common.first_nonempty(location.get("name"))


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    slug = _company_slug(entry)
    company = entry["company"]
    url = f"https://{slug}.breezy.hr/json"
    jobs = common.get_json(session, url, timeout)
    if not isinstance(jobs, list):
        raise common.AdapterError(f"Breezy HR board {slug!r}: response was not a JSON array")

    inventory: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(common.first_nonempty(job.get("name")) or "").strip()
        # Full description is not present in the list payload -- match
        # against title only at Stage A, same fallback already used by the
        # Workday adapter for platforms without an inline description.
        matched = common.keyword_matches(title, keywords)
        if not matched:
            continue
        job_id = job.get("id")
        inventory.append(
            {
                "company": company,
                "job_title": title,
                "job_id": str(job_id) if job_id is not None else None,
                "location": _location_str(job),
                "posting_date": common.first_nonempty(job.get("published_date")),
                "job_url": str(common.first_nonempty(job.get("url")) or ""),
                "source_keyword": matched,
                "_platform_ref": job,
            }
        )
    return inventory


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    job_url = item.get("job_url")
    if not job_url:
        return None
    html_doc = common.get_html(session, job_url, timeout)
    description = common.extract_text(html_doc, ".position-description")
    job = item.get("_platform_ref") or {}
    return {
        "company": entry["company"],
        "job_title": item["job_title"],
        "job_id": item["job_id"],
        "location": item["location"],
        "posting_date": item["posting_date"],
        "experience_level_text": common.extract_experience(description),
        "job_url": job_url,
        "team_department": common.first_nonempty(job.get("department")),
        "short_description": description[:500] if description else None,
        "full_description_text": description or None,
        "source_keyword": item["source_keyword"],
    }
