"""Comeet adapter.

No conventional REST endpoint was found reachable from a bare company slug
(guessed `careers-api/2.0/company/{slug}/positions` variants all 400/404),
but the public hosted job-board page itself is plain, cookie-less,
unauthenticated, server-rendered HTML that embeds the full position catalog
directly as a JS variable assignment (confirmed via cold curl during
onboarding research, 2026-09-03):
    GET https://www.comeet.com/jobs/{company_slug}/{group_uid}
        ...  POSITIONS_DATA = [ {...}, {...}, ... ];  ...
Each position object already includes structured `location`
(city/state/country), a direct `url_comeet_hosted_page`, and a
`custom_fields.details[]` array whose `Description`/`Requirements`/etc.
entries hold the full HTML text -- Stage B is field extraction from the same
payload Stage A already fetched, not a second network call.
`comeet_company_slug` / `comeet_group_uid` come from config/sources.json
(the two path segments in the board's public URL, e.g.
`comeet.com/jobs/vastdata/43.001`), verified per-company before being added.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import common

_POSITIONS_BLOB = re.compile(r"POSITIONS_DATA\s*=\s*(\[.*?\]);", re.S)


def _cfg(entry: dict[str, Any]) -> tuple[str, str]:
    slug = entry.get("comeet_company_slug")
    group_uid = entry.get("comeet_group_uid")
    if not slug or not group_uid:
        raise common.AdapterError("Missing comeet_company_slug/comeet_group_uid in source config")
    return str(slug), str(group_uid)


def _location_str(position: dict[str, Any]) -> str | None:
    location = position.get("location")
    if isinstance(location, dict):
        return common.first_nonempty(location.get("name"))
    return None


def _description(position: dict[str, Any]) -> str:
    details = ((position.get("custom_fields") or {}).get("details")) or []
    parts = [common.strip_html(d.get("value")) for d in details if isinstance(d, dict) and d.get("value")]
    return "\n\n".join(p for p in parts if p)


def _fetch_positions(entry: dict[str, Any], session, timeout: int) -> list[dict[str, Any]]:
    slug, group_uid = _cfg(entry)
    url = f"https://www.comeet.com/jobs/{slug}/{group_uid}"
    # Comeet's server does strict content negotiation and returns HTTP 406
    # for the shared session's default `Accept: application/json` header
    # (confirmed live during onboarding, 2026-09-03) -- override it for this
    # HTML page.
    html_doc = common.get_html(session, url, timeout, headers={"Accept": "text/html,*/*"})
    match = _POSITIONS_BLOB.search(html_doc)
    if not match:
        raise common.AdapterError(f"Comeet board {slug!r}/{group_uid!r}: POSITIONS_DATA blob not found in page")
    try:
        positions = json.loads(match.group(1))
    except ValueError as exc:
        raise common.AdapterError(f"Comeet board {slug!r}: POSITIONS_DATA was not valid JSON: {exc}") from exc
    if not isinstance(positions, list):
        raise common.AdapterError(f"Comeet board {slug!r}: POSITIONS_DATA was not a JSON array")
    return positions


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    company = entry["company"]
    positions = _fetch_positions(entry, session, timeout)

    inventory: list[dict[str, Any]] = []
    for position in positions:
        if not isinstance(position, dict):
            continue
        title = str(common.first_nonempty(position.get("name")) or "").strip()
        description = _description(position)
        matched = common.keyword_matches(f"{title}\n{description}", keywords)
        if not matched:
            continue
        job_url = str(common.first_nonempty(position.get("url_comeet_hosted_page"), position.get("url_active_page")) or "")
        inventory.append(
            {
                "company": company,
                "job_title": title,
                "job_id": common.first_nonempty(position.get("uid")),
                "location": _location_str(position),
                "posting_date": common.first_nonempty(position.get("time_updated")),
                "job_url": job_url,
                "source_keyword": matched,
                "_platform_ref": position,
            }
        )
    return inventory


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    position = item.get("_platform_ref") or {}
    description = _description(position)
    return {
        "company": entry["company"],
        "job_title": item["job_title"],
        "job_id": item["job_id"],
        "location": item["location"],
        "posting_date": item["posting_date"],
        "experience_level_text": common.extract_experience(description),
        "job_url": item["job_url"],
        "team_department": common.first_nonempty(position.get("department")),
        "short_description": description[:500] if description else None,
        "full_description_text": description or None,
        "source_keyword": item["source_keyword"],
    }
