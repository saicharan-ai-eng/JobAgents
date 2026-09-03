"""Workday CXS (Candidate Experience Site) adapter.

Public, unauthenticated, documented endpoint pattern:
    POST {origin}/wday/cxs/{tenant}/{site}/jobs           (Stage A: search/listing)
    GET  {origin}/wday/cxs/{tenant}/{site}{externalPath}   (Stage B: per-posting detail)

Same underlying protocol as `scripts/workday_fetch.py` (used standalone by
the original Dell/HPE/Red Hat sources) -- this module re-implements it behind
the shared adapter interface (`fetch_inventory` / `fetch_detail`) so newly
onboarded Workday-backed companies go through the generic `run_source.py`
driver instead of a bespoke script. `workday_fetch.py` itself is left
untouched: it is production-proven for the original three sources and
changing its call signature was unnecessary risk.

`workday_origin` / `workday_tenant` / `workday_site` come from
config/sources.json, verified per-company against the live career site
before being added -- never guessed from the company name.
"""

from __future__ import annotations

import warnings
from typing import Any
from urllib.parse import urljoin

from . import common

DEFAULT_LIMIT = 20
# Safety ceiling per keyword, not the real termination signal (see the
# known_total / short-page / duplicate-page-stall checks below) -- 300 pages
# at the default limit of 20 is 6000 records, comfortably above any
# realistic single-keyword match count. Raised from 50 (which silently
# truncated a broad keyword's results at 1000 records with no warning at
# all -- the same bug class that undercounted NVIDIA's inventory in
# workday_fetch.py before its fix).
MAX_PAGES = 300


def _cfg(entry: dict[str, Any]) -> tuple[str, str, str]:
    origin = entry.get("workday_origin")
    tenant = entry.get("workday_tenant")
    site = entry.get("workday_site")
    if not origin or not tenant or not site:
        raise common.AdapterError("Missing workday_origin/workday_tenant/workday_site in source config")
    return str(origin), str(tenant), str(site)


def _build_direct_url(origin: str, site: str, external_path: str) -> str:
    if not external_path:
        return ""
    if external_path.startswith("http://") or external_path.startswith("https://"):
        return external_path
    if external_path.startswith("/job/"):
        return f"{origin.rstrip('/')}/{site}{external_path}"
    return urljoin(f"{origin.rstrip('/')}/{site}/", external_path.lstrip("/"))


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    origin, tenant, site = _cfg(entry)
    company = entry["company"]
    endpoint = f"{origin.rstrip('/')}/wday/cxs/{tenant}/{site}/jobs"
    inventory_by_key: dict[str, dict[str, Any]] = {}

    for keyword in keywords:
        offset = 0
        known_total: int | None = None
        keyword_keys: set[str] = set()
        for _page in range(MAX_PAGES):
            payload = {"appliedFacets": {}, "limit": DEFAULT_LIMIT, "offset": offset, "searchText": keyword}
            try:
                response = session.post(endpoint, json=payload, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                raise common.AdapterError(f"POST {endpoint} ({keyword!r}) failed: {type(exc).__name__}: {exc}") from exc
            if response.status_code in (401, 403):
                raise common.AdapterError(f"POST {endpoint} returned {response.status_code} (blocked/authentication required)")
            if response.status_code == 429:
                raise common.AdapterError(f"POST {endpoint} returned 429 (rate limited)")
            if response.status_code >= 400:
                raise common.AdapterError(f"POST {endpoint} returned HTTP {response.status_code}")
            try:
                data = response.json()
            except ValueError as exc:  # noqa: BLE001
                raise common.AdapterError(f"POST {endpoint} did not return valid JSON: {exc}") from exc

            items = data.get("jobPostings") or data.get("jobPostingsList") or []
            if not isinstance(items, list) or not items:
                break

            new_keys = 0
            for summary in items:
                if not isinstance(summary, dict):
                    continue
                external_path = str(common.first_nonempty(summary.get("externalPath")) or "")
                title = common.first_nonempty(summary.get("title"))
                job_id = common.first_nonempty(
                    summary.get("bulletFields", [None])[0]
                    if isinstance(summary.get("bulletFields"), list) and summary.get("bulletFields")
                    else None
                )
                if not title and not external_path:
                    continue
                location = common.first_nonempty(summary.get("locationsText"), summary.get("location"))
                posting_date = common.first_nonempty(summary.get("postedOn"))
                matched = common.keyword_matches(str(title or ""), keywords) or keyword
                key = str(job_id) if job_id is not None else external_path
                if not key:
                    continue
                if key not in keyword_keys:
                    new_keys += 1
                    keyword_keys.add(key)
                inventory_by_key[key] = {
                    "company": company,
                    "job_title": str(title or "").strip(),
                    "job_id": str(job_id).strip() if job_id is not None else None,
                    "location": str(location).strip() if location is not None else None,
                    "posting_date": str(posting_date).strip() if posting_date is not None else None,
                    "job_url": _build_direct_url(origin, site, external_path),
                    "source_keyword": matched,
                    "_platform_ref": {"external_path": external_path},
                }

            # Some Workday tenants (observed on NVIDIA's CXS deployment) only
            # report an accurate `total` on the first page of a given search
            # and return 0 on subsequent pages of the same query. Capture it
            # once, from the first page only, and rely on it (plus the
            # short-page/duplicate-page signals) for the rest of this
            # keyword's pagination -- trusting a fresh `total` every page
            # would cause a spurious early break after just one page.
            if known_total is None:
                candidate_total = data.get("total")
                if isinstance(candidate_total, int):
                    known_total = candidate_total

            page_size = len(items)
            offset += page_size

            if new_keys == 0:
                # The server returned a non-empty page but every identity on
                # it was already collected this keyword -- it is ignoring
                # `offset` (or repeating a page), not returning fresh
                # results. Stop rather than loop to MAX_PAGES re-fetching
                # the same page.
                warnings.warn(
                    f"workday adapter: {company} {keyword!r} offset {offset - page_size}: page returned "
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
                f"workday adapter: {company} {keyword!r} reached the {MAX_PAGES}-page safety ceiling "
                f"(offset={offset}, known_total={known_total}) without a confirmed end-of-results signal -- "
                "results for this keyword may be incomplete",
                RuntimeWarning,
                stacklevel=2,
            )

    return list(inventory_by_key.values())


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    origin, tenant, site = _cfg(entry)
    ref = item.get("_platform_ref") or {}
    external_path = ref.get("external_path") or ""
    if not external_path:
        return None
    detail_url = f"{origin.rstrip('/')}/wday/cxs/{tenant}/{site}{external_path}"
    data = common.get_json(session, detail_url, timeout)
    posting_info = data.get("jobPostingInfo") if isinstance(data.get("jobPostingInfo"), dict) else {}
    description_html = common.first_nonempty(posting_info.get("jobDescription"), data.get("jobDescription"))
    description = common.strip_html(description_html)
    team = common.first_nonempty(
        posting_info.get("jobFamily"), posting_info.get("jobFamilyGroup"),
        data.get("jobFamily"), data.get("jobFamilyGroup"),
    )
    location = common.first_nonempty(posting_info.get("location"), data.get("location")) or item["location"]
    posting_date = common.first_nonempty(posting_info.get("postedOn"), data.get("postedOn")) or item["posting_date"]
    return {
        "company": entry["company"],
        "job_title": item["job_title"],
        "job_id": item["job_id"],
        "location": str(location).strip() if location is not None else None,
        "posting_date": str(posting_date).strip() if posting_date is not None else None,
        "experience_level_text": common.extract_experience(description),
        "job_url": item["job_url"],
        "team_department": str(team).strip() if team is not None else None,
        "short_description": description[:500] if description else None,
        "full_description_text": description or None,
        "source_keyword": item["source_keyword"],
    }
