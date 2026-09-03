"""Eightfold AI "PCSX" (Position Career Site Experience) adapter.

Public, unauthenticated, documented endpoint pattern confirmed live during
onboarding research (2026-09-03):
    GET {origin}/api/pcsx/search?domain={domain}&query=&location=&start=N&num=M   (Stage A: listing)
    GET {origin}/api/apply/v2/jobs/{id}?domain={domain}                          (Stage B: detail)

Both confirmed unauthenticated (cold curl, no cookies). Note: Eightfold's
older `/api/apply/v2/jobs` *search* endpoint is deprecated/gated on some
tenants (returned "Not authorized for PCSX") -- `/api/pcsx/search` is the
correct Stage-A listing call for a natively-hosted PCSX site -- but the
*single-job* detail route on the older `/api/apply/v2/jobs/{id}` path still
works and is what carries the full `job_description` HTML text. `origin` /
`domain` come from config/sources.json (`eightfold_origin`,
`eightfold_domain`), verified per-company before being added.
"""

from __future__ import annotations

from typing import Any

from . import common

PAGE_SIZE = 50
MAX_PAGES = 20


def _cfg(entry: dict[str, Any]) -> tuple[str, str]:
    origin = entry.get("eightfold_origin")
    domain = entry.get("eightfold_domain")
    if not origin or not domain:
        raise common.AdapterError("Missing eightfold_origin/eightfold_domain in source config")
    return str(origin).rstrip("/"), str(domain)


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    origin, domain = _cfg(entry)
    company = entry["company"]
    inventory: list[dict[str, Any]] = []

    for page in range(MAX_PAGES):
        data = common.get_json(
            session,
            f"{origin}/api/pcsx/search",
            timeout,
            params={"domain": domain, "query": "", "location": "", "start": page * PAGE_SIZE, "num": PAGE_SIZE},
        )
        positions = ((data or {}).get("data") or {}).get("positions")
        if not isinstance(positions, list) or not positions:
            break

        for position in positions:
            if not isinstance(position, dict):
                continue
            title = str(common.first_nonempty(position.get("name")) or "").strip()
            matched = common.keyword_matches(title, keywords)
            if not matched:
                continue
            job_id = position.get("id")
            locations = position.get("standardizedLocations") or position.get("locations")
            location = " | ".join(str(loc) for loc in locations) if isinstance(locations, list) else None
            position_url = position.get("positionUrl") or ""
            job_url = f"{origin}{position_url}" if position_url.startswith("/") else str(position_url)
            inventory.append(
                {
                    "company": company,
                    "job_title": title,
                    "job_id": str(job_id) if job_id is not None else None,
                    "location": location,
                    "posting_date": common.first_nonempty(position.get("postedTs")),
                    "job_url": job_url,
                    "source_keyword": matched,
                    "_platform_ref": {"job_id": job_id},
                }
            )

        if len(positions) < PAGE_SIZE:
            break

    return inventory


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    origin, domain = _cfg(entry)
    ref = item.get("_platform_ref") or {}
    job_id = ref.get("job_id")
    if job_id is None:
        return None
    data = common.get_json(session, f"{origin}/api/apply/v2/jobs/{job_id}", timeout, params={"domain": domain})
    description = common.strip_html(data.get("job_description"))
    return {
        "company": entry["company"],
        "job_title": item["job_title"],
        "job_id": item["job_id"],
        "location": item["location"] or common.first_nonempty(data.get("location")),
        "posting_date": item["posting_date"],
        "experience_level_text": common.extract_experience(description),
        "job_url": item["job_url"],
        "team_department": common.first_nonempty(data.get("department"), data.get("business_unit")),
        "short_description": description[:500] if description else None,
        "full_description_text": description or None,
        "source_keyword": item["source_keyword"],
    }
