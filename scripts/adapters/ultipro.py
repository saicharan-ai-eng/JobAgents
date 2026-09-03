"""UltiPro / UKG Recruiting adapter.

Public, unauthenticated, documented endpoint pattern:
    POST {origin}/{company_code}/JobBoard/{board_id}/JobBoardView/LoadSearchResults
        body: {"opportunitySearch": {"Top": N, "Skip": offset, "QueryString": "", "Filters": []}}

Confirmed unauthenticated (cold curl POST, no cookies) during onboarding
research (2026-09-02). The search response's own items carry only a
`BriefDescription`, no direct URL and no full description. The direct
candidate-facing posting URL was confirmed live:
    {origin}/{company_code}/JobBoard/{board_id}/OpportunityDetail?opportunityId={Id}
This detail page is plain server-rendered HTML (no JS execution needed) that
embeds a `new US.Opportunity.CandidateOpportunityDetail({...});` JSON blob
containing the full `Description` field -- Stage B regex-extracts that blob
rather than needing a second documented API call. `ultipro_company_code` /
`ultipro_board_id` / `ultipro_origin` come from config/sources.json, verified
per-company before being added.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import common

DEFAULT_ORIGIN = "https://recruiting.ultipro.com"
PAGE_SIZE = 50
MAX_PAGES = 20

_DETAIL_BLOB = re.compile(r"new US\.Opportunity\.CandidateOpportunityDetail\((\{.*?\})\);", re.S)


def _cfg(entry: dict[str, Any]) -> tuple[str, str, str]:
    company_code = entry.get("ultipro_company_code")
    board_id = entry.get("ultipro_board_id")
    origin = entry.get("ultipro_origin") or DEFAULT_ORIGIN
    if not company_code or not board_id:
        raise common.AdapterError("Missing ultipro_company_code/ultipro_board_id in source config")
    return str(origin), str(company_code), str(board_id)


def _location_str(opportunity: dict[str, Any]) -> str | None:
    locations = opportunity.get("Locations")
    if not isinstance(locations, list):
        return None
    names = [common.first_nonempty(loc.get("LocalizedName")) for loc in locations if isinstance(loc, dict)]
    joined = " | ".join(str(n) for n in names if n)
    return joined or None


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    origin, company_code, board_id = _cfg(entry)
    company = entry["company"]
    url = f"{origin.rstrip('/')}/{company_code}/JobBoard/{board_id}/JobBoardView/LoadSearchResults"
    inventory: list[dict[str, Any]] = []

    for page in range(MAX_PAGES):
        body = {"opportunitySearch": {"Top": PAGE_SIZE, "Skip": page * PAGE_SIZE, "QueryString": "", "Filters": []}}
        data = common.post_json(
            session,
            url,
            timeout,
            json=body,
            headers={"Content-Type": "application/json; charset=UTF-8", "X-Requested-With": "XMLHttpRequest"},
        )
        opportunities = data.get("opportunities") if isinstance(data, dict) else None
        if not isinstance(opportunities, list) or not opportunities:
            break

        for opp in opportunities:
            if not isinstance(opp, dict):
                continue
            title = str(common.first_nonempty(opp.get("Title")) or "").strip()
            brief = common.strip_html(opp.get("BriefDescription"))
            matched = common.keyword_matches(f"{title}\n{brief}", keywords)
            if not matched:
                continue
            opp_id = opp.get("Id")
            job_url = f"{origin.rstrip('/')}/{company_code}/JobBoard/{board_id}/OpportunityDetail?opportunityId={opp_id}"
            inventory.append(
                {
                    "company": company,
                    "job_title": title,
                    "job_id": str(common.first_nonempty(opp.get("RequisitionNumber"), opp_id)),
                    "location": _location_str(opp),
                    "posting_date": common.first_nonempty(opp.get("PostedDate")),
                    "job_url": job_url,
                    "source_keyword": matched,
                    "_platform_ref": {"detail_url": job_url, "team": opp.get("JobCategoryName")},
                }
            )

        if len(opportunities) < PAGE_SIZE:
            break

    return inventory


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    ref = item.get("_platform_ref") or {}
    detail_url = ref.get("detail_url")
    if not detail_url:
        return None
    html_doc = common.get_html(session, detail_url, timeout)
    match = _DETAIL_BLOB.search(html_doc)
    description = ""
    if match:
        try:
            blob = json.loads(match.group(1))
            description = common.strip_html(blob.get("Description"))
        except ValueError:
            description = ""
    if not description:
        description = common.extract_text(html_doc, ".opportunity-description")
    return {
        "company": entry["company"],
        "job_title": item["job_title"],
        "job_id": item["job_id"],
        "location": item["location"],
        "posting_date": item["posting_date"],
        "experience_level_text": common.extract_experience(description),
        "job_url": item["job_url"],
        "team_department": ref.get("team"),
        "short_description": description[:500] if description else None,
        "full_description_text": description or None,
        "source_keyword": item["source_keyword"],
    }
