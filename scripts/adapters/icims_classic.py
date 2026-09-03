"""iCIMS classic candidate-experience portal adapter.

No JSON API exists on this older iCIMS deployment, but the search page is
plain, cookie-less, unauthenticated, robots.txt-permitted HTML (confirmed
via cold curl during onboarding research, 2026-09-02):
    GET {origin}/jobs/search?ss=1&in_iframe=1
Each posting is an `<li class="iCIMS_JobCardItem">` with title (`.title h3`),
location (`.header.left span`), direct URL (`.title a[href]`), and a
description snippet (`.description`). The direct job page
(`{origin}/jobs/{id}/{slug}/job`) is also plain unauthenticated HTML.
`{origin}/sitemap.xml` additionally lists every live job URL, which this
adapter uses as a secondary discovery source in case the search page's own
pagination misses anything (both are merged, deduplicated by URL).
`icims_origin` comes from config/sources.json, verified per-company before
being added.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import common

MAX_PAGES = 10
_JOB_URL = re.compile(r"/jobs/(\d+)/")


def _origin(entry: dict[str, Any]) -> str:
    origin = entry.get("icims_origin")
    if not origin:
        raise common.AdapterError("Missing icims_origin in source config")
    return str(origin).rstrip("/")


def _parse_search_page(html_doc: str, origin: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html_doc, "html.parser")
    results: list[dict[str, Any]] = []
    for card in soup.select("li.iCIMS_JobCardItem, .iCIMS_JobsTable tr"):
        link = card.select_one(".title a[href], a.title[href]") or card.select_one("a[href*='/jobs/']")
        if link is None:
            continue
        href = link.get("href") or ""
        if "/jobs/" not in href:
            continue
        title = link.get_text(strip=True)
        job_url = urljoin(origin + "/", href.lstrip("/"))
        loc_el = card.select_one(".header.left span, .location")
        location = loc_el.get_text(" ", strip=True) if loc_el else None
        match = _JOB_URL.search(job_url)
        job_id = match.group(1) if match else None
        results.append({"title": title, "job_url": job_url, "location": location, "job_id": job_id})
    return results


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    origin = _origin(entry)
    company = entry["company"]
    by_url: dict[str, dict[str, Any]] = {}

    for page in range(MAX_PAGES):
        html_doc = common.get_html(session, f"{origin}/jobs/search", timeout, params={"ss": 1, "in_iframe": 1, "pr": page * 10})
        rows = _parse_search_page(html_doc, origin)
        if not rows:
            break
        new_rows = 0
        for row in rows:
            if row["job_url"] in by_url:
                continue
            new_rows += 1
            by_url[row["job_url"]] = row
        if new_rows == 0 or len(rows) < 10:
            break

    inventory: list[dict[str, Any]] = []
    for row in by_url.values():
        matched = common.keyword_matches(row["title"], keywords)
        if not matched:
            continue
        inventory.append(
            {
                "company": company,
                "job_title": row["title"],
                "job_id": row["job_id"],
                "location": row["location"],
                "posting_date": None,
                "job_url": row["job_url"],
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
    description = common.extract_text(html_doc, ".iCIMS_JobContent, .description")
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
