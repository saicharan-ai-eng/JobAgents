#!/usr/bin/env python3
"""Two-stage Stage-A/Stage-B fetcher for the AWS (Amazon Jobs) source.

Amazon's own public `search.json` endpoint
(https://www.amazon.jobs/en/search.json?business_category[]=amazon-web-services&base_query=<kw>)
already returns full posting detail (description, basic_qualifications,
preferred_qualifications, job_category, ...) inline for every listing in the
same paginated response used for the lightweight inventory scan. So Stage A
here is a lightweight *field-projection* of that same response (company,
job_title, job_id, location, posting_date, job_url, source_keyword only --
see CLAUDE.md "Required source-result contract"), and Stage B for the unseen
survivors is pure field-extraction from the already-fetched payload -- no
second HTTP request per job is needed or made. This mirrors the convention
established in prior AWS runs (see runs/20260817T030618Z/raw/aws.json).

Location fields: Amazon's raw `location` string uses a 2-letter country-code
prefix that collides with US state abbreviations (e.g. ISO alpha-2 "IL" for
Israel vs. the US state of Illinois), so we build the `location` value from
the structured `locations` list's `normalizedLocation` values instead
(joined with " | " for multi-location postings), matching prior-run
convention.

Usage:
    python scripts/aws_fetch.py --run-id <RUN_ID>
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import classify_dedupe_report as cdr  # noqa: E402
from us_location_filter import categorize_non_us_reason, evaluate_us_location  # noqa: E402

SEARCH_URL = "https://www.amazon.jobs/en/search.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 job-discovery-agent"
)
RESULT_LIMIT = 100
MAX_OFFSET_SAFETY = 20000  # hard stop to avoid runaway pagination

_TITLE_ONLY_EXCLUSIONS: list[tuple[str, re.Pattern[str]]] = [
    ("senior_title", cdr.SENIOR_TITLE),
    ("marketing_role_title", cdr.MARKETING_TITLE),
    ("sales_role_title", cdr.SALES_TITLE),
    ("hr_role_title", cdr.HR_TITLE),
]


def stage_a_title_exclusion_reason(title: str) -> str | None:
    if cdr.EXPLICIT_JUNIOR.search(title) or cdr.JUNIOR_TITLE.search(title):
        return None
    for reason, pattern in _TITLE_ONLY_EXCLUSIONS:
        if pattern.search(title):
            return reason
    return None


def fetch_page(business_category: str, keyword: str, offset: int, timeout: int) -> dict[str, Any]:
    params = {
        "business_category[]": business_category,
        "base_query": keyword,
        "result_limit": str(RESULT_LIMIT),
        "offset": str(offset),
    }
    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last_exc: Exception | None = None
    for attempt in range(2):  # one retry with alternate delay/backoff
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:  # noqa: PERF203
            last_exc = exc
            time.sleep(1.5)
    raise RuntimeError(f"search.json fetch failed for keyword={keyword!r} offset={offset}: {last_exc}")


def build_location(job: dict[str, Any]) -> str | None:
    raw_locations = job.get("locations") or []
    labels: list[str] = []
    for entry in raw_locations:
        try:
            parsed = json.loads(entry) if isinstance(entry, str) else entry
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            label = parsed.get("normalizedLocation")
            if label and label not in labels:
                labels.append(label)
    if labels:
        return " | ".join(labels)
    return job.get("normalized_location") or job.get("location") or None


def build_full_description(job: dict[str, Any]) -> str:
    parts: list[str] = []
    description = job.get("description") or ""
    basic_q = job.get("basic_qualifications") or ""
    preferred_q = job.get("preferred_qualifications") or ""
    parts.append(f"DESCRIPTION:\n{description}")
    if basic_q:
        parts.append(f"BASIC QUALIFICATIONS:\n{basic_q}")
    if preferred_q:
        parts.append(f"PREFERRED QUALIFICATIONS:\n{preferred_q}")
    return "\n\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--config-file", default="config/sources.json")
    parser.add_argument("--history-file", default="state/seen_source_jobs.json")
    parser.add_argument("--slug", default="aws")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--delay", type=float, default=0.2)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    config = json.loads((repo_root / args.config_file).read_text(encoding="utf-8"))
    keywords: list[str] = config.get("keywords", [])
    entry = next((s for s in config["sources"] if s.get("slug") == args.slug), None)
    if entry is None:
        raise SystemExit(f"No source configured with slug {args.slug!r}")
    company = entry["company"]
    source_url = entry.get("listing_url") or entry.get("careers_home")
    business_category = "amazon-web-services"

    run_dir = repo_root / "runs" / args.run_id
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir = run_dir / "logs"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}  # id_icims -> raw job dict (first keyword match wins)
    keyword_by_id: dict[str, str] = {}

    for keyword in keywords:
        offset = 0
        while True:
            try:
                page = fetch_page(business_category, keyword, offset, args.timeout)
            except RuntimeError as exc:
                errors.append(str(exc))
                break
            jobs = page.get("jobs") or []
            hits = page.get("hits") or 0
            for job in jobs:
                job_id = str(job.get("id_icims") or "").strip()
                if not job_id:
                    continue
                if job_id not in by_id:
                    by_id[job_id] = job
                    keyword_by_id[job_id] = keyword
            if not jobs:
                break
            offset += RESULT_LIMIT
            if offset >= hits or offset >= MAX_OFFSET_SAFETY:
                break
            if args.delay:
                time.sleep(args.delay)
        if args.delay:
            time.sleep(args.delay)

    # Build lightweight Stage-A inventory (public fields only).
    inventory: list[dict[str, Any]] = []
    for job_id, job in by_id.items():
        job_path = job.get("job_path") or ""
        job_url = f"https://www.amazon.jobs{job_path}" if job_path else job.get("url_next_step")
        inventory.append(
            {
                "company": company,
                "job_title": job.get("title"),
                "job_id": job_id,
                "location": build_location(job),
                "posting_date": job.get("posted_date"),
                "job_url": job_url,
                "source_keyword": keyword_by_id.get(job_id),
            }
        )

    inventory_file = scratch_dir / "aws_inventory_scratch.json"
    unseen_file = scratch_dir / "aws_unseen_scratch.json"
    inventory_file.write_text(json.dumps(inventory, indent=2, ensure_ascii=False), encoding="utf-8")

    diff_cmd = [
        sys.executable,
        str(repo_root / "scripts" / "source_history.py"),
        "--history-file",
        args.history_file,
        "diff-inventory",
        "--slug",
        args.slug,
        "--inventory-file",
        str(inventory_file),
        "--out-unseen",
        str(unseen_file),
    ]
    diff_result = subprocess.run(diff_cmd, cwd=repo_root, capture_output=True, text=True)
    if diff_result.returncode != 0:
        raise SystemExit(f"diff-inventory failed: {diff_result.stderr}")
    diff_summary = json.loads(diff_result.stdout)
    unseen_items: list[dict[str, Any]] = json.loads(unseen_file.read_text(encoding="utf-8"))

    confirmed_non_us: list[dict[str, Any]] = []
    title_rejected: list[tuple[dict[str, Any], str]] = []
    candidates: list[dict[str, Any]] = []
    for item in unseen_items:
        location = item.get("location")
        loc_eval = evaluate_us_location(location)
        if not loc_eval["us_location_eligible"] and categorize_non_us_reason(location) == "explicitly_non_us":
            confirmed_non_us.append(item)
            continue
        title_reason = stage_a_title_exclusion_reason(str(item.get("job_title") or ""))
        if title_reason:
            title_rejected.append((item, title_reason))
        else:
            candidates.append(item)

    postings: list[dict[str, Any]] = []

    for item in candidates:
        job_id = str(item.get("job_id"))
        job = by_id.get(job_id)
        if job is None:
            stub = dict(item)
            stub.update(
                {
                    "experience_level_text": None,
                    "team_department": None,
                    "short_description": None,
                    "full_description_text": None,
                    "processing_status": "failed",
                }
            )
            errors.append(f"detail lookup failed for job_id={job_id}: not found in fetched payload")
            postings.append(stub)
            continue
        posting = dict(item)
        posting.update(
            {
                "experience_level_text": job.get("basic_qualifications") or None,
                "team_department": job.get("job_category") or None,
                "short_description": job.get("description_short") or None,
                "full_description_text": build_full_description(job),
                "processing_status": "success",
            }
        )
        postings.append(posting)

    for item in confirmed_non_us:
        stub = dict(item)
        stub.update(
            {
                "experience_level_text": None,
                "team_department": None,
                "short_description": None,
                "full_description_text": None,
                "processing_status": "excluded_non_us",
                "stage_a_exclusion_reason": "non_us_location_stage_a",
            }
        )
        postings.append(stub)

    for item, title_reason in title_rejected:
        stub = dict(item)
        stub.update(
            {
                "experience_level_text": None,
                "team_department": None,
                "short_description": None,
                "full_description_text": None,
                "processing_status": "excluded_title_reject",
                "stage_a_exclusion_reason": title_reason,
            }
        )
        postings.append(stub)

    status = "partial" if errors else "success"

    result = {
        "company": company,
        "source_url": source_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reason": " | ".join(errors[:10]) if errors else None,
        "inventory_count": len(inventory),
        "unseen_inventory_count": diff_summary["unseen_inventory_count"],
        "previously_processed_count": diff_summary["previously_processed_count"],
        "detail_fetch_count": len(candidates),
        "raw_posting_count": len(postings),
        "inventory": inventory,
        "postings": postings,
    }

    output_path = raw_dir / "aws.json"
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    summary = {
        "status": status,
        "inventory_count": len(inventory),
        "unseen_inventory_count": diff_summary["unseen_inventory_count"],
        "previously_processed_count": diff_summary["previously_processed_count"],
        "detail_fetch_count": len(candidates),
        "excluded_non_us_count": len(confirmed_non_us),
        "excluded_title_reject_count": len(title_rejected),
        "output": str(output_path),
        "errors": errors,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
