"""AgileATS adapter.

Public, unauthenticated, documented endpoint:
    POST {origin}/graphql
        body: {"query": "query { Jobs { id title status location city state
                zip country published_date description_publishable } }"}

Confirmed unauthenticated (cold curl POST, no headers/cookies/token at all)
during onboarding research (2026-09-02) -- the query already returns full
`description_publishable` HTML, so Stage B is field extraction from the same
payload Stage A already fetched, not a second network call. The site itself
is a pure client-rendered Angular SPA with no per-job URL routing (clicking a
job opens an in-page modal; the address bar never changes), so the direct
posting URL was recovered by intercepting the app's own "copy link" button
(hooking `document.execCommand('copy')` and reading the text it was about to
copy) rather than guessed -- confirmed as a genuine, fresh-navigable deep
link:
    {origin}/jobs/details/{id}
`agile_ats_origin` comes from config/sources.json, verified per-company
before being added.
"""

from __future__ import annotations

from typing import Any

from . import common

GRAPHQL_QUERY = (
    "query { Jobs { id title status location city state zip country "
    "published_date description_publishable } }"
)


def _origin(entry: dict[str, Any]) -> str:
    origin = entry.get("agile_ats_origin")
    if not origin:
        raise common.AdapterError("Missing agile_ats_origin in source config")
    return str(origin).rstrip("/")


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    origin = _origin(entry)
    company = entry["company"]
    url = f"{origin}/graphql"
    data = common.post_json(session, url, timeout, json={"query": GRAPHQL_QUERY}, headers={"Content-Type": "application/json"})
    jobs = ((data or {}).get("data") or {}).get("Jobs")
    if not isinstance(jobs, list):
        raise common.AdapterError(f"AgileATS {company!r}: response missing 'data.Jobs' array")

    inventory: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if str(job.get("status") or "").strip().lower() not in ("open", ""):
            continue
        title = str(common.first_nonempty(job.get("title")) or "").strip()
        description = common.strip_html(job.get("description_publishable"))
        matched = common.keyword_matches(f"{title}\n{description}", keywords)
        if not matched:
            continue
        job_id = job.get("id")
        location_parts = [job.get("city"), job.get("state"), job.get("country")]
        location = ", ".join(str(p) for p in location_parts if p) or common.first_nonempty(job.get("location"))
        inventory.append(
            {
                "company": company,
                "job_title": title,
                "job_id": str(job_id) if job_id is not None else None,
                "location": location,
                "posting_date": common.first_nonempty(job.get("published_date")),
                "job_url": f"{origin}/jobs/details/{job_id}" if job_id is not None else "",
                "source_keyword": matched,
                "_platform_ref": job,
            }
        )
    return inventory


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    job = item.get("_platform_ref") or {}
    description = common.strip_html(job.get("description_publishable"))
    return {
        "company": entry["company"],
        "job_title": item["job_title"],
        "job_id": item["job_id"],
        "location": item["location"],
        "posting_date": item["posting_date"],
        "experience_level_text": common.extract_experience(description),
        "job_url": item["job_url"],
        "team_department": None,
        "short_description": description[:500] if description else None,
        "full_description_text": description or None,
        "source_keyword": item["source_keyword"],
    }
