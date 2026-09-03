"""Oracle Fusion Cloud HCM "Candidate Experience" (Oracle Recruiting Cloud)
adapter.

Public, unauthenticated, JSON REST endpoint pattern, confirmed live against
Dell's careers site on 2026-08-12 (see the `dell` entry in
state/source_baseline_status.json for the multi-day verification history
that first located these endpoints; this adapter turns that research into a
working two-stage fetch):

    GET {origin}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&expand=requisitionList.secondaryLocations
        &finder=findReqs;siteNumber={site},keyword="{kw}",limit={N},offset={M}
        -> {"items": [{"TotalJobsCount": N, "requisitionList": [...] }]}

    GET {origin}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
        ?expand=all&onlyData=true&finder=ById;Id="{jobId}",siteNumber={site}
        -> {"items": [{"ExternalDescriptionStr": "<html>", ...}]}

`enterpriseplatform.dell.com` is the REST API host; `jobs.dell.com` is
Dell's public-facing career site and its `/en/sites/{site}/job/{jobId}`
path (no title slug required -- confirmed by rendering it in a browser,
2026-08-12) resolves to the correct posting, so that public origin is used
for `job_url` rather than the raw API host.

`oracle_hcm_origin` / `oracle_hcm_site_number` / `oracle_hcm_public_origin`
come from config/sources.json, verified per-company against the live career
site before being added -- never guessed from the company name.

Known limitation: no live Dell posting observed while building/verifying
this adapter had a non-empty `secondaryLocations` array, so the exact
field name Oracle uses for a secondary location's display text is not
confirmed. `_secondary_location_text` extracts it defensively (tries the
plausible common field names, never fabricates) so a genuine multi-location
posting still gets *at least* its primary location -- if the field name
guess is wrong, coverage silently degrades to primary-location-only rather
than failing, which is the fail-closed-appropriate direction for this
project's US-location gate. Confirm the real field name against a live
multi-location posting before relying on secondary-location coverage.
"""

from __future__ import annotations

import warnings
from typing import Any

from . import common

DEFAULT_LIMIT = 20
# Safety ceiling only, not the real termination signal -- see the
# known-total-once / short-page / duplicate-page-stall handling in
# fetch_inventory below. Mirrors the Workday adapter's hardening (see
# adapters/workday.py) applied proactively here: the same offset/limit/total
# REST shape had an undetected per-page-total bug on a different vendor
# (NVIDIA's Workday CXS deployment, run 20260812T152540Z), so this adapter
# is built hardened from the start rather than retrofitted later.
MAX_PAGES = 300


def _cfg(entry: dict[str, Any]) -> tuple[str, str, str]:
    origin = entry.get("oracle_hcm_origin")
    site = entry.get("oracle_hcm_site_number")
    public_origin = entry.get("oracle_hcm_public_origin") or origin
    if not origin or not site:
        raise common.AdapterError("Missing oracle_hcm_origin/oracle_hcm_site_number in source config")
    return str(origin), str(site), str(public_origin)


def _build_direct_url(public_origin: str, site: str, job_id: str) -> str:
    return f"{public_origin.rstrip('/')}/en/sites/{site}/job/{job_id}"


def _secondary_location_text(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    value = common.first_nonempty(entry.get("Name"), entry.get("LocationName"), entry.get("City"))
    return str(value).strip() if value else None


def _combined_location(primary: Any, secondary_list: Any) -> str | None:
    parts: list[str] = []
    if primary:
        parts.append(str(primary).strip())
    if isinstance(secondary_list, list):
        for entry in secondary_list:
            text = _secondary_location_text(entry)
            if text:
                parts.append(text)
    parts = [p for p in parts if p]
    return "; ".join(parts) if parts else None


def _first_item(data: dict[str, Any]) -> dict[str, Any]:
    items = data.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return {}


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    origin, site, public_origin = _cfg(entry)
    company = entry["company"]
    endpoint = f"{origin.rstrip('/')}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    inventory_by_key: dict[str, dict[str, Any]] = {}

    for keyword in keywords:
        offset = 0
        known_total: int | None = None
        keyword_keys: set[str] = set()
        for _page in range(MAX_PAGES):
            finder = f'findReqs;siteNumber={site},keyword="{keyword}",limit={DEFAULT_LIMIT},offset={offset}'
            params = {"onlyData": "true", "expand": "requisitionList.secondaryLocations", "finder": finder}
            data = common.get_json(session, endpoint, timeout, params=params)
            item0 = _first_item(data)
            requisitions = item0.get("requisitionList")
            if not isinstance(requisitions, list) or not requisitions:
                break

            new_keys = 0
            for req in requisitions:
                if not isinstance(req, dict):
                    continue
                job_id = common.first_nonempty(req.get("Id"))
                title = common.first_nonempty(req.get("Title"))
                if not job_id or not title:
                    continue
                key = str(job_id)
                location = _combined_location(req.get("PrimaryLocation"), req.get("secondaryLocations"))
                posting_date = common.first_nonempty(req.get("PostedDate"))
                matched = common.keyword_matches(str(title), keywords) or keyword
                if key not in keyword_keys:
                    new_keys += 1
                    keyword_keys.add(key)
                inventory_by_key[key] = {
                    "company": company,
                    "job_title": str(title).strip(),
                    "job_id": key,
                    "location": location,
                    "posting_date": str(posting_date).strip() if posting_date is not None else None,
                    "job_url": _build_direct_url(public_origin, site, key),
                    "source_keyword": matched,
                    "_platform_ref": {"job_id": key},
                }

            # Same total-per-page caution as the Workday adapter: capture
            # TotalJobsCount once, from the first page only, and rely on it
            # (plus short-page/duplicate-page signals) for the rest of this
            # keyword's pagination.
            if known_total is None:
                candidate_total = item0.get("TotalJobsCount")
                if isinstance(candidate_total, int):
                    known_total = candidate_total

            page_size = len(requisitions)
            offset += page_size

            if new_keys == 0:
                warnings.warn(
                    f"oracle_hcm adapter: {company} {keyword!r} offset {offset - page_size}: page returned "
                    f"{page_size} item(s) but zero new job identities -- stopping (duplicate/stalled page)",
                    RuntimeWarning,
                    stacklevel=2,
                )
                break
            if isinstance(known_total, int) and offset >= known_total:
                break
            if page_size < DEFAULT_LIMIT:
                break
        else:
            warnings.warn(
                f"oracle_hcm adapter: {company} {keyword!r} reached the {MAX_PAGES}-page safety ceiling "
                f"(offset={offset}, known_total={known_total}) without a confirmed end-of-results signal",
                RuntimeWarning,
                stacklevel=2,
            )

    return list(inventory_by_key.values())


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    origin, site, _public_origin = _cfg(entry)
    ref = item.get("_platform_ref") or {}
    job_id = common.first_nonempty(ref.get("job_id"), item.get("job_id"))
    if not job_id:
        return None
    endpoint = f"{origin.rstrip('/')}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
    finder = f'ById;Id="{job_id}",siteNumber={site}'
    data = common.get_json(session, endpoint, timeout, params={"expand": "all", "onlyData": "true", "finder": finder})
    detail = _first_item(data)

    combined_html = "\n".join(
        str(x)
        for x in [
            detail.get("ExternalDescriptionStr"),
            detail.get("ExternalResponsibilitiesStr"),
            detail.get("ExternalQualificationsStr"),
        ]
        if x
    )
    description = common.strip_html(combined_html)

    team = common.first_nonempty(detail.get("JobFunction"), detail.get("Category"), detail.get("Department"))
    location = _combined_location(detail.get("PrimaryLocation"), detail.get("otherWorkLocations")) or item.get("location")
    posting_date = common.first_nonempty(detail.get("PostedDate")) or item.get("posting_date")
    title = common.first_nonempty(item.get("job_title"), detail.get("Title"))

    return {
        "company": entry["company"],
        "job_title": str(title).strip() if title else item.get("job_title"),
        "job_id": str(job_id),
        "location": str(location).strip() if location is not None else None,
        "posting_date": str(posting_date).strip() if posting_date is not None else None,
        "experience_level_text": common.extract_experience(description),
        "job_url": item.get("job_url"),
        "team_department": str(team).strip() if team is not None else None,
        "short_description": description[:500] if description else None,
        "full_description_text": description or None,
        "source_keyword": item.get("source_keyword"),
    }
