"""Jobvite adapter.

No JSON API exists for anonymous candidates, but the public board listing
page is plain, cookie-less, unauthenticated, server-rendered HTML (confirmed
via cold curl during onboarding research, 2026-09-03):
    GET https://jobs.jobvite.com/{company_slug}
Each posting is a `<table class="jv-job-list">` row: `<td class="jv-job-list-name">
<a href="/{company_slug}/job/{id}">Title</a></td>` plus a sibling
`<td class="jv-job-list-location">`. The direct job page
(`https://jobs.jobvite.com{href}`) is also plain unauthenticated HTML whose
`.jv-job-detail-description` element holds the full text -- confirmed live.
`company_slug` comes from config/sources.json (`jobvite_company_slug`),
verified per-company before being added.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import common

ORIGIN = "https://jobs.jobvite.com"


def _company_slug(entry: dict[str, Any]) -> str:
    slug = entry.get("jobvite_company_slug")
    if not slug:
        raise common.AdapterError("Missing jobvite_company_slug in source config")
    return str(slug)


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    slug = _company_slug(entry)
    company = entry["company"]
    html_doc = common.get_html(session, f"{ORIGIN}/{slug}", timeout)
    soup = BeautifulSoup(html_doc, "html.parser")

    inventory: list[dict[str, Any]] = []
    for name_cell in soup.select("td.jv-job-list-name"):
        link = name_cell.select_one("a[href]")
        if link is None:
            continue
        title = link.get_text(strip=True)
        matched = common.keyword_matches(title, keywords)
        if not matched:
            continue
        href = link.get("href") or ""
        job_url = urljoin(ORIGIN, href)
        job_id = href.rstrip("/").rsplit("/", 1)[-1] or None
        location_cell = name_cell.find_next_sibling("td", class_="jv-job-list-location")
        location = location_cell.get_text(" ", strip=True) if location_cell else None
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
    description = common.extract_text(html_doc, ".jv-job-detail-description")
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
