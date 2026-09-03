"""SAP SuccessFactors Career Site Builder (CSB) adapter.

No JSON API exists -- this SF product line is purely server-rendered HTML
(confirmed: even an `Accept: application/json` / `X-Requested-With:
XMLHttpRequest` request still returns `text/html`; SAP's OData/Recruiting
API is a separate, authenticated back-office product, not exposed to
anonymous candidates). But the keyword-scoped search page is plain,
cookie-less, unauthenticated HTML with the real job list baked directly into
the response (confirmed via cold curl during onboarding research,
2026-09-02):
    GET {search_origin}/search/?q={keyword}&startrow={N}
25 results per page (`tr.data-row`, `a.jobTitle-link` for title+href,
`td.colLocation span.jobLocation` for location). Stage B fetches the same
per-job HTML page and extracts visible text generically (no company-specific
detail selector was reverse-engineered -- unnecessary, since the classifier
only needs the real qualifying words in `full_description_text`).
`successfactors_search_origin` comes from config/sources.json, verified
per-company before being added.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import common

PAGE_SIZE = 25
MAX_PAGES_PER_KEYWORD = 8


def _origin(entry: dict[str, Any]) -> str:
    origin = entry.get("successfactors_search_origin")
    if not origin:
        raise common.AdapterError("Missing successfactors_search_origin in source config")
    return str(origin).rstrip("/")


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    origin = _origin(entry)
    company = entry["company"]
    inventory_by_url: dict[str, dict[str, Any]] = {}

    for keyword in keywords:
        for page in range(MAX_PAGES_PER_KEYWORD):
            html_doc = common.get_html(
                session,
                f"{origin}/search/",
                timeout,
                params={"q": keyword, "startrow": page * PAGE_SIZE},
            )
            soup = BeautifulSoup(html_doc, "html.parser")
            rows = soup.select("tr.data-row")
            if not rows:
                break

            new_rows = 0
            for row in rows:
                link = row.select_one("a.jobTitle-link")
                if link is None:
                    continue
                title = link.get_text(strip=True)
                href = link.get("href") or ""
                job_url = urljoin(origin + "/", href.lstrip("/"))
                if job_url in inventory_by_url:
                    continue
                new_rows += 1
                # SAP's own search relevance is loose/tokenized (a search for
                # "AI infrastructure" matched titles like "Architect II" with
                # no visible AI/ML connection at all) -- unlike Workday's
                # search, it cannot be trusted as a de-facto keyword filter.
                # Re-verify client-side against the title before accepting,
                # same discipline as every non-server-search adapter.
                matched = common.keyword_matches(title, keywords)
                if not matched:
                    continue
                loc_el = row.select_one("td.colLocation span.jobLocation")
                location = loc_el.get_text(" ", strip=True) if loc_el else None
                inventory_by_url[job_url] = {
                    "company": company,
                    "job_title": title,
                    "job_id": href.rstrip("/").rsplit("/", 1)[-1] or None,
                    "location": location,
                    "posting_date": None,
                    "job_url": job_url,
                    "source_keyword": matched,
                    "_platform_ref": {},
                }

            if new_rows == 0 or len(rows) < PAGE_SIZE:
                break

    return list(inventory_by_url.values())


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    job_url = item.get("job_url")
    if not job_url:
        return None
    html_doc = common.get_html(session, job_url, timeout)
    description = common.extract_text(html_doc)
    return {
        "company": entry["company"],
        "job_title": item["job_title"],
        "job_id": item["job_id"],
        "location": item["location"],
        "posting_date": item["posting_date"],
        "experience_level_text": common.extract_experience(description),
        "job_url": job_url,
        "team_department": None,
        "short_description": description[:500] if description else None,
        "full_description_text": description or None,
        "source_keyword": item["source_keyword"],
    }
