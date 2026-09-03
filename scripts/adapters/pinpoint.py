"""Pinpoint (pinpointhq.com) adapter.

Public, unauthenticated, documented endpoint:
    GET https://{company_slug}.pinpointhq.com/{locale}/postings.json

Returns every open posting in one un-paginated JSON array, including a full
HTML `description` -- Stage B is field extraction from the same payload
Stage A already fetched, not a second network call. Confirmed unauthenticated
(cold curl, no cookies) during onboarding research (2026-09-02); note the
response's `Content-Type` header is `text/html` despite the body being valid
JSON, so this reads it as text and parses explicitly rather than relying on
`requests`' content-type-sniffing `.json()`. Pinpoint's structured
`location` fields are frequently empty on real postings (US-ness has to be
read from free-text in the title/description instead, which the project's
authoritative US-location filter already re-applies at Stage-B regardless).
`company_slug` comes from config/sources.json (`pinpoint_company_slug`),
verified per-company before being added.
"""

from __future__ import annotations

import json
from typing import Any

from . import common


def _company_slug(entry: dict[str, Any]) -> str:
    slug = entry.get("pinpoint_company_slug")
    if not slug:
        raise common.AdapterError("Missing pinpoint_company_slug in source config")
    return str(slug)


def _location_str(job: dict[str, Any]) -> str | None:
    location = job.get("location")
    if isinstance(location, dict):
        parts = [common.first_nonempty(location.get("city")), common.first_nonempty(location.get("province"))]
        joined = ", ".join(str(p) for p in parts if p)
        if joined:
            return joined
        name = common.first_nonempty(location.get("name"))
        if name:
            return str(name)
    return common.first_nonempty(job.get("workplace_type_text"))


def _fetch_catalog(entry: dict[str, Any], session, timeout: int) -> list[dict[str, Any]]:
    slug = _company_slug(entry)
    locale = entry.get("pinpoint_locale") or "en"
    url = f"https://{slug}.pinpointhq.com/{locale}/postings.json"
    try:
        response = session.get(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise common.AdapterError(f"GET {url} failed: {type(exc).__name__}: {exc}") from exc
    if response.status_code in (401, 403):
        raise common.AdapterError(f"GET {url} returned {response.status_code} (blocked/authentication required)")
    if response.status_code >= 400:
        raise common.AdapterError(f"GET {url} returned HTTP {response.status_code}")
    try:
        data = json.loads(response.text)
    except ValueError as exc:  # noqa: BLE001
        raise common.AdapterError(f"GET {url} did not return valid JSON: {exc}") from exc
    postings = data.get("data") if isinstance(data, dict) else None
    if not isinstance(postings, list):
        raise common.AdapterError(f"Pinpoint board {slug!r}: response missing 'data' array")
    return postings


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    company = entry["company"]
    jobs = _fetch_catalog(entry, session, timeout)
    inventory: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(common.first_nonempty(job.get("title")) or "").strip()
        description = common.strip_html(job.get("description"))
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
                "posting_date": None,
                "job_url": str(common.first_nonempty(job.get("url")) or ""),
                "source_keyword": matched,
                "_platform_ref": job,
            }
        )
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
        "team_department": common.first_nonempty(job.get("employment_type_text")),
        "short_description": description[:500] if description else None,
        "full_description_text": description or None,
        "source_keyword": item["source_keyword"],
    }
