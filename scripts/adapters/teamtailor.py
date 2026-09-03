"""Teamtailor public JSON Feed adapter.

Public, unauthenticated, documented endpoint (standard JSON Feed format,
https://jsonfeed.org/): every Teamtailor-hosted careers site -- including
ones on a custom domain -- publishes:

    GET {careers_domain}/jobs.json

Each `items[]` entry carries a `_jobposting` object following the
schema.org JobPosting vocabulary, including structured `jobLocation`
(address country/region/locality) -- unlike some other platforms this gives
genuinely structured location data rather than a free-text string, which
`_location_str` below normalizes into the same free-text `location` shape
the rest of the pipeline (and the US-location filter) expects. Returns every
open posting in one request with the full HTML description already inlined,
so Stage B is field extraction from the same payload Stage A already
fetched, not a second network call -- like Greenhouse/Ashby/Lever.

`teamtailor_domain` comes from config/sources.json (the full origin, e.g.
`https://careers.lindy.ai`), verified per-company before being added --
discovered by inspecting the live careers page (Teamtailor's CDN assets and
`jobs.json` feed are both a stable, documented fingerprint), never guessed
from the company name.
"""

from __future__ import annotations

from typing import Any

from . import common


def _domain(entry: dict[str, Any]) -> str:
    domain = entry.get("teamtailor_domain")
    if not domain:
        raise common.AdapterError("Missing teamtailor_domain in source config")
    return str(domain).rstrip("/")


def _location_str(jobposting: dict[str, Any]) -> str | None:
    locations = jobposting.get("jobLocation")
    if not isinstance(locations, list):
        return None
    parts: list[str] = []
    for place in locations:
        if not isinstance(place, dict):
            continue
        address = place.get("address") or {}
        if not isinstance(address, dict):
            continue
        locality = common.first_nonempty(address.get("addressLocality"))
        region = common.first_nonempty(address.get("addressRegion"))
        country = common.first_nonempty(address.get("addressCountry"))
        segment = ", ".join(str(p) for p in (locality, region, country) if p)
        if segment:
            parts.append(segment)
    return " | ".join(parts) if parts else None


def _fetch_feed(entry: dict[str, Any], session, timeout: int) -> list[dict[str, Any]]:
    domain = _domain(entry)
    url = f"{domain}/jobs.json"
    data = common.get_json(session, url, timeout)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise common.AdapterError(f"Teamtailor feed {url!r}: response missing 'items' array")
    return items


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    company = entry["company"]
    items = _fetch_feed(entry, session, timeout)
    inventory: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        jobposting = item.get("_jobposting") if isinstance(item.get("_jobposting"), dict) else {}
        title = str(common.first_nonempty(item.get("title"), jobposting.get("title")) or "").strip()
        description = common.strip_html(jobposting.get("description"))
        matched = common.keyword_matches(f"{title}\n{description}", keywords)
        if not matched:
            continue
        job_id = None
        identifier = jobposting.get("identifier")
        if isinstance(identifier, dict):
            job_id = identifier.get("value")
        job_id = job_id if job_id is not None else item.get("id")
        inventory.append(
            {
                "company": company,
                "job_title": title,
                "job_id": str(job_id) if job_id is not None else None,
                "location": _location_str(jobposting),
                "posting_date": common.first_nonempty(item.get("date_published"), jobposting.get("datePosted")),
                "job_url": str(common.first_nonempty(item.get("url")) or ""),
                "source_keyword": matched,
                "_platform_ref": item,
            }
        )
    return inventory


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    raw = item.get("_platform_ref") or {}
    jobposting = raw.get("_jobposting") if isinstance(raw.get("_jobposting"), dict) else {}
    description = common.strip_html(jobposting.get("description"))
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
