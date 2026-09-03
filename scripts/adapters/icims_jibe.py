"""Modern iCIMS + Jibe JSON layer adapter.

Some iCIMS deployments are fronted by a newer Jibe-powered JSON API on a
separate marketing-branded host, distinct from the classic
`careers-<company>.icims.com` HTML-only deployment (see
`scripts/adapters/icims_classic.py`). Public, unauthenticated, documented
endpoint confirmed live during onboarding research (2026-09-02):
    GET {api_origin}/api/jobs?page=N&limit=M
Returns full structured JSON in one paginated call, including a complete
HTML `description` -- Stage B is field extraction from the same payload
Stage A already fetched, not a second network call. The direct posting page
lives on the classic iCIMS host and requires `?in_iframe=1` to render its
real content server-side (confirmed live; without it the page is an empty
JS-shell):
    {detail_origin}/jobs/{req_id}/job?in_iframe=1
`icims_jibe_api_origin` / `icims_jibe_detail_origin` come from
config/sources.json, verified per-company before being added.
"""

from __future__ import annotations

from typing import Any

from . import common

PAGE_SIZE = 50
MAX_PAGES = 20


def _cfg(entry: dict[str, Any]) -> tuple[str, str]:
    api_origin = entry.get("icims_jibe_api_origin")
    detail_origin = entry.get("icims_jibe_detail_origin")
    if not api_origin or not detail_origin:
        raise common.AdapterError("Missing icims_jibe_api_origin/icims_jibe_detail_origin in source config")
    return str(api_origin).rstrip("/"), str(detail_origin).rstrip("/")


def _location_str(job: dict[str, Any]) -> str | None:
    return common.first_nonempty(job.get("full_location"), job.get("short_location"), job.get("location_name"))


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    api_origin, detail_origin = _cfg(entry)
    company = entry["company"]
    inventory: list[dict[str, Any]] = []

    for page in range(1, MAX_PAGES + 1):
        data = common.get_json(session, f"{api_origin}/api/jobs", timeout, params={"page": page, "limit": PAGE_SIZE})
        wrapped = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(wrapped, list) or not wrapped:
            break

        for entry_wrapper in wrapped:
            job = entry_wrapper.get("data") if isinstance(entry_wrapper, dict) else None
            if not isinstance(job, dict):
                continue
            title = str(common.first_nonempty(job.get("title")) or "").strip()
            description = common.strip_html(job.get("description"))
            matched = common.keyword_matches(f"{title}\n{description}", keywords)
            if not matched:
                continue
            req_id = job.get("req_id") or job.get("slug")
            job_url = f"{detail_origin}/jobs/{req_id}/job?in_iframe=1" if req_id else ""
            inventory.append(
                {
                    "company": company,
                    "job_title": title,
                    "job_id": str(req_id) if req_id is not None else None,
                    "location": _location_str(job),
                    "posting_date": common.first_nonempty(job.get("posted_date"), job.get("create_date")),
                    "job_url": job_url,
                    "source_keyword": matched,
                    "_platform_ref": job,
                }
            )

        if len(wrapped) < PAGE_SIZE:
            break

    return inventory


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    job = item.get("_platform_ref") or {}
    description = common.strip_html(job.get("description"))
    return {
        "company": entry["company"],
        "job_title": item["job_title"],
        "job_id": item["job_id"],
        "location": item["location"],
        "posting_date": item["posting_date"],
        "experience_level_text": common.extract_experience(description),
        "job_url": item["job_url"],
        "team_department": common.first_nonempty(job.get("department"), job.get("category")),
        "short_description": description[:500] if description else None,
        "full_description_text": description or None,
        "source_keyword": item["source_keyword"],
    }
