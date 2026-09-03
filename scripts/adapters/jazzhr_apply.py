"""JazzHR (white-labeled as "ApplyToJob") adapter.

No JSON API exists for anonymous candidates (JazzHR's documented
resumatorapi.com REST API requires a per-company API key) -- but the public
listing page is plain, cookie-less, unauthenticated HTML with the job list
baked directly into the response (confirmed via cold curl during onboarding
research, 2026-09-02):
    GET https://{company_slug}.applytojob.com/apply
Each posting is a `<li class="list-group-item">` containing
`<h3><a href="...">Title</a></h3>` and a `<li>` with a map-marker icon
holding the location text. The direct job URL (absolute, already present in
the `href`) leads to a page whose `#job-description` element holds the full
text -- confirmed live. `company_slug` comes from config/sources.json
(`jazzhr_company_slug`), verified per-company before being added.
"""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup

from . import common


def _company_slug(entry: dict[str, Any]) -> str:
    slug = entry.get("jazzhr_company_slug")
    if not slug:
        raise common.AdapterError("Missing jazzhr_company_slug in source config")
    return str(slug)


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    slug = _company_slug(entry)
    company = entry["company"]
    url = f"https://{slug}.applytojob.com/apply"
    html_doc = common.get_html(session, url, timeout)
    soup = BeautifulSoup(html_doc, "html.parser")

    inventory: list[dict[str, Any]] = []
    for row in soup.select("li.list-group-item"):
        link = row.select_one("h3.list-group-item-heading a, h3 a")
        if link is None:
            continue
        title = link.get_text(strip=True)
        job_url = link.get("href") or ""
        matched = common.keyword_matches(title, keywords)
        if not matched:
            continue
        location_el = row.select_one("li i.fa-map-marker")
        location = location_el.parent.get_text(strip=True) if location_el else None
        job_id = job_url.rstrip("/").split("/apply/")[-1].split("/")[0] if "/apply/" in job_url else None
        inventory.append(
            {
                "company": company,
                "job_title": title,
                "job_id": job_id,
                "location": location,
                "posting_date": None,
                "job_url": job_url,
                "source_keyword": matched,
                "_platform_ref": {},
            }
        )
    return inventory


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    job_url = item.get("job_url")
    if not job_url:
        return None
    html_doc = common.get_html(session, job_url, timeout)
    description = common.extract_text(html_doc, "#job-description")
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
