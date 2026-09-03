#!/usr/bin/env python3
"""Fetch Workday CXS postings with pagination and job-detail enrichment.

This helper intentionally stays generic because Workday tenants can vary. It
uses documented/public career-site endpoints only and fails closed: errors are
recorded rather than replaced with guessed data.

Two-stage incremental fetch: Stage A collects a lightweight inventory (job_id,
title, location, posting_date, direct URL) from the search endpoint only --
no detail pages are opened. That inventory is diffed against
state/seen_source_jobs.json (via source_history.py); Stage B then opens detail
pages only for identities that are unseen (or a clearly-signaled repost).
Already-processed postings are never re-fetched.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import source_history

DEFAULT_KEYWORDS = [
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "generative AI",
    "LLM",
    "computer vision",
    "CUDA",
    "GPU",
    "MLOps",
    "AI infrastructure",
    "accelerated computing",
    "inference",
    "NVIDIA",
]

EXPERIENCE_PATTERNS = [
    re.compile(r"\b(?:minimum of\s+|at least\s+)?\d+\s*(?:-|–|to)\s*\d+\s+years?\b", re.I),
    re.compile(r"\b\d+\+?\s+years?\b", re.I),
    re.compile(r"\b(?:internship|intern|co-op|new grad(?:uate)?|university grad(?:uate)?|early career|entry level)\b", re.I),
]


def strip_html(value: Any) -> str:
    if value is None:
        return ""
    text = BeautifulSoup(html.unescape(str(value)), "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def nested(data: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        cur: Any = data
        ok = True
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                ok = False
                break
            cur = cur[part]
        if ok and cur not in (None, "", [], {}):
            return cur
    return None


def extract_experience(text: str) -> str | None:
    matches: list[str] = []
    for pattern in EXPERIENCE_PATTERNS:
        matches.extend(match.group(0) for match in pattern.finditer(text))
    if not matches:
        return None
    # Preserve source wording while avoiding an enormous field.
    unique = list(dict.fromkeys(matches))
    return "; ".join(unique[:5])


def build_direct_url(origin: str, site: str, external_path: str) -> str:
    if not external_path:
        return ""
    if external_path.startswith("http://") or external_path.startswith("https://"):
        return external_path
    # Public Workday links conventionally include the site segment before /job/.
    if external_path.startswith("/job/"):
        return f"{origin.rstrip('/')}/{site}{external_path}"
    return urljoin(f"{origin.rstrip('/')}/{site}/", external_path.lstrip("/"))


def fetch_detail(session: requests.Session, origin: str, tenant: str, site: str, external_path: str, timeout: int) -> dict[str, Any]:
    detail_url = f"{origin.rstrip('/')}/wday/cxs/{tenant}/{site}{external_path}"
    response = session.get(detail_url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def normalize_posting(
    company: str,
    origin: str,
    tenant: str,
    site: str,
    summary: dict[str, Any],
    keyword: str,
    detail: dict[str, Any] | None,
) -> dict[str, Any]:
    detail = detail or {}
    posting_info = detail.get("jobPostingInfo") if isinstance(detail.get("jobPostingInfo"), dict) else {}

    external_path = str(first_nonempty(summary.get("externalPath"), posting_info.get("externalPath")) or "")
    description_html = first_nonempty(
        posting_info.get("jobDescription"),
        detail.get("jobDescription"),
        summary.get("jobDescription"),
    )
    description = strip_html(description_html)

    title = first_nonempty(
        summary.get("title"),
        posting_info.get("title"),
        detail.get("title"),
    )
    job_id = first_nonempty(
        posting_info.get("jobReqId"),
        detail.get("jobReqId"),
        summary.get("bulletFields", [None])[0] if isinstance(summary.get("bulletFields"), list) and summary.get("bulletFields") else None,
    )
    location = first_nonempty(
        posting_info.get("location"),
        detail.get("location"),
        summary.get("locationsText"),
        summary.get("location"),
    )
    posting_date = first_nonempty(
        posting_info.get("postedOn"),
        detail.get("postedOn"),
        summary.get("postedOn"),
    )
    team = first_nonempty(
        posting_info.get("jobFamily"),
        posting_info.get("jobFamilyGroup"),
        detail.get("jobFamily"),
        detail.get("jobFamilyGroup"),
    )

    return {
        "company": company,
        "job_title": str(title or "").strip(),
        "job_id": str(job_id).strip() if job_id is not None else None,
        "location": str(location).strip() if location is not None else None,
        "posting_date": str(posting_date).strip() if posting_date is not None else None,
        "experience_level_text": extract_experience(description),
        "job_url": build_direct_url(origin, site, external_path),
        "team_department": str(team).strip() if team is not None else None,
        "short_description": description[:500] if description else None,
        "full_description_text": description or None,
        "source_keyword": keyword,
    }


def paginate_keyword_search(
    session: requests.Session,
    endpoint: str,
    keyword: str,
    company: str,
    origin: str,
    site: str,
    limit: int,
    max_pages: int,
    timeout: int,
    delay: float = 0.0,
    sleep_fn: Any = time.sleep,
) -> dict[str, Any]:
    """Paginate one keyword's Workday CXS search to exhaustion.

    Returns a dict with `inventory_by_key`, `external_path_by_key`,
    `skipped_stub_count`, and `warnings` (never raises for pagination-shape
    issues -- only a request-level exception is recorded as a warning and
    stops that keyword's pagination).

    Termination is checked in this order on every page:
      1. Request failure -- record a warning, stop this keyword.
      2. Empty/malformed page -- true end-of-results, stop cleanly.
      3. Duplicate-page stall -- a non-empty page that contributed zero new
         job identities (the server ignored `offset` and re-returned an
         already-seen page). Stop rather than loop to `max_pages` re-fetching
         the same page.
      4. Known total reached -- `total` is captured once, from the first
         page only, and trusted for the rest of this keyword's pagination.
         Some Workday tenants (observed on NVIDIA's CXS deployment) only
         report an accurate `total` on page 1 and return 0 on every
         subsequent page of the same query; trusting a fresh `total` on
         every page would cause a spurious early break after just one page.
      5. Short page (fewer items than `limit`) -- true end-of-results.
      6. `max_pages` safety ceiling -- always recorded as a warning (never a
         silent truncation): reaching it means pagination stopped without
         hitting any of the four real termination signals above, e.g.
         because the search backend paginates further than expected for an
         unusually broad keyword. Raise `--max-pages` if this occurs.
    """
    inventory_by_key: dict[str, dict[str, Any]] = {}
    external_path_by_key: dict[str, str] = {}
    warnings: list[str] = []
    skipped_stub_count = 0
    offset = 0
    known_total: int | None = None

    for _page in range(max_pages):
        payload = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": keyword}
        try:
            response = session.post(endpoint, json=payload, timeout=timeout)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{keyword!r} offset {offset}: {type(exc).__name__}: {exc}")
            break

        items = data.get("jobPostings") or data.get("jobPostingsList") or []
        if not isinstance(items, list) or not items:
            break

        new_keys = 0
        for summary in items:
            if not isinstance(summary, dict):
                continue
            item, external_path = collect_inventory_item(company, origin, site, summary, keyword)
            if item is None:
                skipped_stub_count += 1
                continue
            key = item.get("job_id") or item.get("job_url")
            if not key:
                continue
            key = str(key)
            if key not in inventory_by_key:
                new_keys += 1
            inventory_by_key[key] = item
            if external_path:
                external_path_by_key[key] = external_path

        if known_total is None:
            candidate_total = data.get("total")
            if isinstance(candidate_total, int):
                known_total = candidate_total

        page_size = len(items)
        offset += page_size

        if new_keys == 0:
            warnings.append(
                f"{keyword!r} offset {offset - page_size}: page returned {page_size} item(s) but zero new "
                "job identities -- stopping (duplicate/stalled page; the server may be ignoring offset)"
            )
            break
        if isinstance(known_total, int) and offset >= known_total:
            break
        if page_size < limit:
            break
        if delay:
            sleep_fn(delay)
    else:
        warnings.append(
            f"{keyword!r}: reached the {max_pages}-page safety ceiling (offset={offset}, "
            f"known_total={known_total}) without a confirmed end-of-results signal -- results for this "
            "keyword may be incomplete; rerun with a higher --max-pages if so"
        )

    return {
        "inventory_by_key": inventory_by_key,
        "external_path_by_key": external_path_by_key,
        "skipped_stub_count": skipped_stub_count,
        "warnings": warnings,
    }


def collect_inventory_item(company: str, origin: str, site: str, summary: dict[str, Any], keyword: str) -> tuple[dict[str, Any] | None, str]:
    """Build a Stage-A lightweight inventory item from a search-result summary
    only (no detail fetch). Returns (item, external_path). Returns (None, "")
    when the search endpoint returned a stub record with no usable identity
    (no title and no external path) beyond a bare job_id -- this occurs
    occasionally for restricted/inaccessible Workday postings and cannot be
    turned into a direct posting URL or a trustworthy title without
    fabricating data."""
    external_path = str(first_nonempty(summary.get("externalPath")) or "")
    title = first_nonempty(summary.get("title"))
    job_id = first_nonempty(
        summary.get("bulletFields", [None])[0]
        if isinstance(summary.get("bulletFields"), list) and summary.get("bulletFields")
        else None
    )
    if not title and not external_path:
        # No usable title and no direct-link path: the source itself exposed
        # nothing beyond a bare job_id for this record. Skip rather than
        # fabricate a title or URL.
        return None, ""
    location = first_nonempty(summary.get("locationsText"), summary.get("location"))
    posting_date = first_nonempty(summary.get("postedOn"))
    item = {
        "company": company,
        "job_title": str(title or "").strip(),
        "job_id": str(job_id).strip() if job_id is not None else None,
        "location": str(location).strip() if location is not None else None,
        "posting_date": str(posting_date).strip() if posting_date is not None else None,
        "job_url": build_direct_url(origin, site, external_path),
        "source_keyword": keyword,
    }
    return item, external_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--listing-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--slug", required=True, help="Source slug matching config/sources.json, e.g. 'dell'")
    parser.add_argument("--history-file", default="state/seen_source_jobs.json")
    parser.add_argument("--keywords-json", help="JSON array of search strings")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--max-pages",
        type=int,
        default=300,
        help="Safety ceiling per keyword (default 300 pages = 6000 records at the default --limit of 20). "
        "Real termination is driven by the search endpoint's own total/short-page signals; this ceiling only "
        "guards against a runaway loop and is always logged as a warning if actually reached.",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--delay", type=float, default=0.35)
    parser.add_argument(
        "--inventory-only",
        action="store_true",
        help="Stage A only: collect lightweight listing metadata and stop. Never opens a detail page, "
        "never diffs against history, never runs Stage B. Used for source-history baseline seeding.",
    )
    args = parser.parse_args()

    keywords = DEFAULT_KEYWORDS
    if args.keywords_json:
        parsed = json.loads(args.keywords_json)
        if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
            raise ValueError("--keywords-json must be a JSON string array")
        keywords = parsed

    endpoint = f"{args.origin.rstrip('/')}/wday/cxs/{args.tenant}/{args.site}/jobs"
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; AIJobDiscovery/1.0; respectful point-in-time research)",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )

    # --- Stage A: lightweight inventory only, no detail pages opened yet. ---
    inventory_by_key: dict[str, dict[str, Any]] = {}
    external_path_by_key: dict[str, str] = {}
    errors: list[str] = []
    any_success = False
    skipped_stub_count = 0

    for keyword in keywords:
        result = paginate_keyword_search(
            session,
            endpoint,
            keyword,
            args.company,
            args.origin,
            args.site,
            args.limit,
            args.max_pages,
            args.timeout,
            delay=args.delay,
        )
        if result["inventory_by_key"] or not result["warnings"]:
            any_success = True
        inventory_by_key.update(result["inventory_by_key"])
        external_path_by_key.update(result["external_path_by_key"])
        skipped_stub_count += result["skipped_stub_count"]
        errors.extend(result["warnings"])

    inventory = list(inventory_by_key.values())
    if skipped_stub_count:
        errors.append(
            f"{skipped_stub_count} search-result record(s) skipped: source returned no title and no "
            "external path (restricted/inaccessible stub record), so no title or direct URL could be "
            "populated without fabricating data"
        )

    if args.inventory_only:
        # Stop here: baseline-seeding mode. No history diff, no detail pages,
        # no postings array -- just the Stage-A inventory itself.
        status = "success" if any_success and not errors else ("partial" if any_success else "failed")
        result = {
            "company": args.company,
            "source_url": args.listing_url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "reason": " | ".join(errors[:10]) if errors else None,
            "inventory_count": len(inventory),
            "inventory": inventory,
        }
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"status": status, "inventory_count": len(inventory), "output": str(output_path)}, indent=2))
        return 0 if any_success else 2

    # --- Diff against persistent source history: only unseen (or clearly
    # reposted) identities get a Stage-B detail fetch. ---
    history = source_history.load_history(Path(args.history_file))
    unseen_items, previously_processed_items = source_history.diff_inventory(history, args.slug, inventory)

    # --- Stage B: detail fetch only for unseen identities. ---
    postings: list[dict[str, Any]] = []
    for item in unseen_items:
        key = item.get("job_id") or item.get("job_url")
        external_path = external_path_by_key.get(str(key), "")
        detail = None
        if external_path:
            try:
                detail = fetch_detail(session, args.origin, args.tenant, args.site, external_path, args.timeout)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"detail {external_path}: {type(exc).__name__}: {exc}")
        summary_stub = {
            "title": item.get("job_title"),
            "externalPath": external_path,
            "bulletFields": [item.get("job_id")] if item.get("job_id") else [],
            "locationsText": item.get("location"),
            "postedOn": item.get("posting_date"),
        }
        normalized = normalize_posting(
            args.company, args.origin, args.tenant, args.site, summary_stub, item.get("source_keyword") or "", detail
        )
        normalized["processing_status"] = "success" if detail is not None else "failed"
        postings.append(normalized)
        time.sleep(args.delay)

    if any_success and errors:
        status = "partial"
    elif any_success:
        status = "success"
    else:
        status = "failed"

    result = {
        "company": args.company,
        "source_url": args.listing_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reason": " | ".join(errors[:10]) if errors else None,
        "inventory_count": len(inventory),
        "unseen_inventory_count": len(unseen_items),
        "previously_processed_count": len(previously_processed_items),
        "detail_fetch_count": len(postings),
        "raw_posting_count": len(postings),
        "inventory": inventory,
        "postings": postings,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": status,
                "inventory_count": len(inventory),
                "unseen_inventory_count": len(unseen_items),
                "previously_processed_count": len(previously_processed_items),
                "detail_fetch_count": len(postings),
                "output": str(output_path),
            },
            indent=2,
        )
    )
    return 0 if any_success else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
