"""Trakstar Hire (formerly Recruiterbox) adapter.

No JSON API exists, but a fully public, unauthenticated RSS 2.0 feed does:
    GET https://{company_slug}.hire.trakstar.com/jobfeeds/{company_slug}

Confirmed unauthenticated (cold curl, no cookies, `Content-Type:
application/rss+xml`) during onboarding research (2026-09-02). Returns every
open posting in one un-paginated feed, including a full HTML `<description>`
and custom namespaced `<job:locationCity>` / `<job:locationState>` /
`<job:locationCountry>` / `<job:positionType>` / `<job:team>` tags -- richer,
structured location data than most JSON-API platforms this project has
onboarded. Stage B is field extraction from the same payload Stage A already
fetched, not a second network call. `company_slug` comes from
config/sources.json (`trakstar_company_slug`), verified per-company before
being added.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from . import common

JOB_NS = "https://recruiterbox.com/rss/job/"


def _company_slug(entry: dict[str, Any]) -> str:
    slug = entry.get("trakstar_company_slug")
    if not slug:
        raise common.AdapterError("Missing trakstar_company_slug in source config")
    return str(slug)


def _job_field(item: ET.Element, name: str) -> str | None:
    el = item.find(f"{{{JOB_NS}}}{name}")
    if el is not None and el.text:
        return el.text.strip()
    return None


def _location_str(item: ET.Element) -> str | None:
    parts = [_job_field(item, "locationCity"), _job_field(item, "locationState"), _job_field(item, "locationCountry")]
    joined = ", ".join(p for p in parts if p)
    return joined or None


def _fetch_items(entry: dict[str, Any], session, timeout: int) -> list[ET.Element]:
    slug = _company_slug(entry)
    url = f"https://{slug}.hire.trakstar.com/jobfeeds/{slug}"
    try:
        response = session.get(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise common.AdapterError(f"GET {url} failed: {type(exc).__name__}: {exc}") from exc
    if response.status_code in (401, 403):
        raise common.AdapterError(f"GET {url} returned {response.status_code} (blocked/authentication required)")
    if response.status_code >= 400:
        raise common.AdapterError(f"GET {url} returned HTTP {response.status_code}")
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise common.AdapterError(f"GET {url} did not return valid XML/RSS: {exc}") from exc
    channel = root.find("channel")
    if channel is None:
        raise common.AdapterError(f"Trakstar Hire feed {slug!r}: no <channel> element found")
    return channel.findall("item")


def fetch_inventory(entry: dict[str, Any], session, keywords: list[str], timeout: int) -> list[dict[str, Any]]:
    company = entry["company"]
    items = _fetch_items(entry, session, timeout)
    inventory: list[dict[str, Any]] = []
    for item in items:
        title = (item.findtext("title") or "").strip()
        description = common.strip_html(item.findtext("description"))
        matched = common.keyword_matches(f"{title}\n{description}", keywords)
        if not matched:
            continue
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        job_id = guid.rsplit("/", 1)[-1] if guid else None
        inventory.append(
            {
                "company": company,
                "job_title": title,
                "job_id": job_id or None,
                "location": _location_str(item),
                "posting_date": (item.findtext("pubDate") or "").strip() or None,
                "job_url": link,
                "source_keyword": matched,
                "_platform_ref": {
                    "description": description,
                    "team": _job_field(item, "team"),
                },
            }
        )
    return inventory


def fetch_detail(entry: dict[str, Any], item: dict[str, Any], session, timeout: int) -> dict[str, Any] | None:
    ref = item.get("_platform_ref") or {}
    description = ref.get("description") or ""
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
