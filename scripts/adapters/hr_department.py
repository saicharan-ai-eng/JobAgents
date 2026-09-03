"""HR Department (hrdepartment.com / Monster "MUA") adapter.

No JSON API exists -- job data is baked directly into plain server-rendered
HTML (confirmed via cold curl during onboarding research, 2026-09-02):
    GET {origin}/hr/ats/JobSearch/viewAll/jobSearchPaginationExternal_pageSize:100/jobSearchPaginationExternal_page:{N}
Each row is a `<tr><td><a href="/hr/ats/Posting/view/{id}"><span>Title</span>
</a></td><td>Location</td><td>Date</td>...</tr>` -- a single pageSize:100
request captures the whole catalog for these small company boards. Stage B
fetches the same per-job detail page (`/hr/ats/Posting/view/{id}`) and
extracts visible text generically. `hr_department_origin` comes from
config/sources.json (the company's own white-labeled subdomain, e.g.
`https://{company}.mua.hrdepartment.com`), verified per-company before being
added.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import common

PAGE_SIZE = 100
MAX_PAGES = 10
_POSTING_HREF = re.compile(r"^/hr/ats/Posting/view/(\d+)")


def _origin(entry: dict[str, Any]) -> str:
    origin = entry.get("hr_department_origin")
    if not origin:
        raise common.AdapterError("Missing hr_department_origin in source config")
    return str(origin).rstrip("/")


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    origin = _origin(entry)
    company = entry["company"]
    inventory: list[dict[str, Any]] = []

    for page in range(1, MAX_PAGES + 1):
        url = f"{origin}/hr/ats/JobSearch/viewAll/jobSearchPaginationExternal_pageSize:{PAGE_SIZE}/jobSearchPaginationExternal_page:{page}"
        html_doc = common.get_html(session, url, timeout)
        soup = BeautifulSoup(html_doc, "html.parser")
        links = [a for a in soup.select("a[href]") if _POSTING_HREF.match(a.get("href") or "")]
        if not links:
            break

        new_links = 0
        for link in links:
            href = link.get("href") or ""
            match = _POSTING_HREF.match(href)
            if not match:
                continue
            job_id = match.group(1)
            title = link.get_text(strip=True)
            row = link.find_parent("tr")
            location = None
            posting_date = None
            if row is not None:
                cells = row.find_all("td")
                if len(cells) >= 3:
                    location = cells[1].get_text(" ", strip=True) or None
                    posting_date = cells[2].get_text(" ", strip=True) or None
            matched = common.keyword_matches(title, keywords)
            if not matched:
                continue
            new_links += 1
            job_url = urljoin(origin + "/", href.lstrip("/"))
            inventory.append(
                {
                    "company": company,
                    "job_title": title,
                    "job_id": job_id,
                    "location": location,
                    "posting_date": posting_date,
                    "job_url": job_url,
                    "source_keyword": matched,
                    "_platform_ref": {},
                }
            )

        if len(links) < PAGE_SIZE or new_links == 0:
            break

    return inventory


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
