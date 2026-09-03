"""ADP Workforce Now (Career Center) adapter.

Public, unauthenticated, documented endpoint pattern:
    GET {origin}/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions
        ?cid={cid}&ccId={ccId}&lang=en_US&locale=en_US&$top=100         (Stage A: listing)
    GET {origin}/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions/{itemID}
        ?cid={cid}&ccId={ccId}&lang=en_US&locale=en_US                  (Stage B: detail)

Both confirmed unauthenticated (cold curl, no cookies) during onboarding
research (2026-09-02). The listing endpoint's own items never carry a direct
candidate-facing URL or full description; the detail endpoint adds
`requisitionDescription` (full HTML), and the direct posting URL was
reverse-engineered by watching the candidate-facing SPA update its own
address bar when opening a job -- confirmed as a genuine, fresh-navigable
deep link:
    {origin}/mascsr/default/mdf/recruitment/recruitment.html
        ?cid={cid}&ccId={ccId}&lang=en_US&jobId={ExternalJobID}
`ExternalJobID` is a distinct field from the internal `itemID` (which the
detail-fetch API needs) -- both come from the same requisition record.
`adp_cid` / `adp_ccid` / `adp_origin` come from config/sources.json, verified
per-company before being added.
"""

from __future__ import annotations

from typing import Any

from . import common

DEFAULT_ORIGIN = "https://workforcenow.adp.com"
PAGE_SIZE = 100


def _cfg(entry: dict[str, Any]) -> tuple[str, str, str]:
    cid = entry.get("adp_cid")
    ccid = entry.get("adp_ccid")
    origin = entry.get("adp_origin") or DEFAULT_ORIGIN
    if not cid or not ccid:
        raise common.AdapterError("Missing adp_cid/adp_ccid in source config")
    return str(origin), str(cid), str(ccid)


def _external_job_id(requisition: dict[str, Any]) -> str | None:
    string_fields = ((requisition.get("customFieldGroup") or {}).get("stringFields")) or []
    for field in string_fields:
        if not isinstance(field, dict):
            continue
        code = (field.get("nameCode") or {}).get("codeValue")
        if code == "ExternalJobID":
            return common.first_nonempty(field.get("stringValue"))
    return None


def _location_str(requisition: dict[str, Any]) -> str | None:
    locations = requisition.get("requisitionLocations")
    if not isinstance(locations, list):
        return None
    names: list[str] = []
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        name = (loc.get("nameCode") or {}).get("shortName")
        if name:
            names.append(str(name).strip())
    joined = " | ".join(names)
    return joined or None


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    origin, cid, ccid = _cfg(entry)
    company = entry["company"]
    url = f"{origin.rstrip('/')}/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
    data = common.get_json(
        session,
        url,
        timeout,
        params={"cid": cid, "ccId": ccid, "lang": "en_US", "locale": "en_US", "$top": PAGE_SIZE},
    )
    requisitions = data.get("jobRequisitions") if isinstance(data, dict) else None
    if not isinstance(requisitions, list):
        raise common.AdapterError(f"ADP Workforce Now {company!r}: response missing 'jobRequisitions' array")

    inventory: list[dict[str, Any]] = []
    for requisition in requisitions:
        if not isinstance(requisition, dict):
            continue
        title = str(common.first_nonempty(requisition.get("requisitionTitle")) or "").strip()
        # Full description is Stage-B-only -- match against title here,
        # same fallback used elsewhere for list-only-has-title platforms.
        matched = common.keyword_matches(title, keywords)
        if not matched:
            continue
        item_id = requisition.get("itemID")
        external_job_id = _external_job_id(requisition)
        job_url = (
            f"{origin.rstrip('/')}/mascsr/default/mdf/recruitment/recruitment.html"
            f"?cid={cid}&ccId={ccid}&lang=en_US&jobId={external_job_id}"
            if external_job_id
            else ""
        )
        inventory.append(
            {
                "company": company,
                "job_title": title,
                "job_id": str(item_id) if item_id is not None else None,
                "location": _location_str(requisition),
                "posting_date": common.first_nonempty(requisition.get("postDate")),
                "job_url": job_url,
                "source_keyword": matched,
                "_platform_ref": {"item_id": item_id},
            }
        )
    return inventory


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    origin, cid, ccid = _cfg(entry)
    ref = item.get("_platform_ref") or {}
    item_id = ref.get("item_id")
    if not item_id or not item.get("job_url"):
        return None
    url = f"{origin.rstrip('/')}/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions/{item_id}"
    data = common.get_json(session, url, timeout, params={"cid": cid, "ccId": ccid, "lang": "en_US", "locale": "en_US"})
    description = common.strip_html(data.get("requisitionDescription"))
    return {
        "company": entry["company"],
        "job_title": item["job_title"],
        "job_id": item["job_id"],
        "location": item["location"] or _location_str(data),
        "posting_date": item["posting_date"],
        "experience_level_text": common.extract_experience(description),
        "job_url": item["job_url"],
        "team_department": None,
        "short_description": description[:500] if description else None,
        "full_description_text": description or None,
        "source_keyword": item["source_keyword"],
    }
