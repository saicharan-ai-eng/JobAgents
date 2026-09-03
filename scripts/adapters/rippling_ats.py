"""Rippling ATS adapter.

Public, unauthenticated, documented endpoint pattern:
    GET https://ats.rippling.com/api/v2/board/{board_slug}/jobs?page=N&pageSize=100
    GET https://ats.rippling.com/api/v2/board/{board_slug}/jobs/{job_id}   (detail)

Confirmed unauthenticated (cold curl, no cookies) during onboarding research
(2026-09-02) -- the detail endpoint additionally returns full `description`
HTML that the list endpoint omits. `board_slug` comes from
config/sources.json (`rippling_board_slug`, the path segment after
`ats.rippling.com/` in the board's public URL), verified per-company before
being added.
"""

from __future__ import annotations

from typing import Any

from . import common

API_BASE = "https://ats.rippling.com/api/v2/board"
PAGE_SIZE = 100
MAX_PAGES = 20


def _board_slug(entry: dict[str, Any]) -> str:
    slug = entry.get("rippling_board_slug")
    if not slug:
        raise common.AdapterError("Missing rippling_board_slug in source config")
    return str(slug)


def _location_str(job: dict[str, Any]) -> str | None:
    locations = job.get("locations")
    if isinstance(locations, list) and locations:
        names = [common.first_nonempty(loc.get("name")) for loc in locations if isinstance(loc, dict)]
        joined = " | ".join(str(n) for n in names if n)
        if joined:
            return joined
    return None


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    slug = _board_slug(entry)
    company = entry["company"]
    inventory: list[dict[str, Any]] = []

    for page in range(MAX_PAGES):
        url = f"{API_BASE}/{slug}/jobs"
        data = common.get_json(
            session,
            url,
            timeout,
            params={"searchQuery": "", "page": page, "pageSize": PAGE_SIZE},
        )
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            break

        for job in items:
            if not isinstance(job, dict):
                continue
            title = str(common.first_nonempty(job.get("name")) or "").strip()
            # Full description lives only on the detail endpoint -- Stage A
            # matches against title only, same fallback used elsewhere for
            # platforms whose list payload has no inline description.
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
                    "posting_date": None,
                    "job_url": str(common.first_nonempty(job.get("url")) or ""),
                    "source_keyword": matched,
                    "_platform_ref": {"board_slug": slug, "job_id": job_id},
                }
            )

        total_pages = data.get("totalPages") if isinstance(data, dict) else None
        if isinstance(total_pages, int) and page + 1 >= total_pages:
            break
        if len(items) < PAGE_SIZE:
            break

    return inventory


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    ref = item.get("_platform_ref") or {}
    slug = ref.get("board_slug")
    job_id = ref.get("job_id")
    if not slug or job_id is None:
        return None
    url = f"{API_BASE}/{slug}/jobs/{job_id}"
    data = common.get_json(session, url, timeout)
    description = common.strip_html(data.get("description"))
    posting_date = common.first_nonempty(data.get("createdOn"))
    return {
        "company": entry["company"],
        "job_title": item["job_title"],
        "job_id": item["job_id"],
        "location": item["location"],
        "posting_date": str(posting_date) if posting_date else item["posting_date"],
        "experience_level_text": common.extract_experience(description),
        "job_url": item["job_url"],
        "team_department": common.first_nonempty((data.get("department") or {}).get("name")),
        "short_description": description[:500] if description else None,
        "full_description_text": description or None,
        "source_keyword": item["source_keyword"],
    }
