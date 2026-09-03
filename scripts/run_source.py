#!/usr/bin/env python3
"""Generic two-stage Stage-A/Stage-B driver for config-driven ATS sources.

Dispatches to scripts/adapters/<platform>.py based on the `platform` field of
the matching entry in config/sources.json for the given --slug. This is the
shared engine behind every source added after the original nine (see
CLAUDE.md): onboarding a new company only requires a config/sources.json
entry (plus a new adapters/*.py module the *first* time a genuinely new
platform shows up) -- never a bespoke fetch script or a heavy per-company
browser agent (see CLAUDE.md Phase 8 / performance requirements).

The original Dell/HPE/Red Hat sources keep using scripts/workday_fetch.py
directly, unchanged -- this script does not replace it, only extends the same
approach to every subsequently added source.

Two-stage contract (see CLAUDE.md "Required source-result contract"):
  Stage A -- fetch_inventory(): lightweight listing scan (no detail pages).
  Stage B -- fetch_detail(): full detail, but only for identities that come
             back unseen from `source_history.diff_inventory` AND are not
             already confirmed non-US from Stage-A location data alone.

United States Stage-A pre-filter (CLAUDE.md "Retrieval strategy" / "United
States location eligibility"): an unseen inventory item whose location string
is already positively resolvable to *non*-US (evaluate_us_location fails AND
categorize_non_us_reason says "explicitly_non_us", e.g. "Berlin, Germany") is
never sent to Stage B -- no full description is fetched. It is still recorded
into the raw source-result's `postings` array with
`processing_status: "excluded_non_us"` so its identity is committed into
`state/seen_source_jobs.json` and never re-attempted, but
`classify_dedupe_report.py` skips those records outright: they are never
classified, ranked, reported, or notified. An item with missing/ambiguous
Stage-A location (e.g. bare "Remote", or no location at all) is NOT assumed
non-US here -- it proceeds to Stage B so the authoritative, fail-closed gate
in classify_dedupe_report.py can re-evaluate the fuller detail-page location.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import source_history  # noqa: E402
import classify_dedupe_report as cdr  # noqa: E402
from adapters import common  # noqa: E402
from us_location_filter import categorize_non_us_reason, evaluate_us_location  # noqa: E402

PLATFORM_MODULES: dict[str, str] = {
    "greenhouse": "adapters.greenhouse",
    "ashby": "adapters.ashby",
    "lever": "adapters.lever",
    "smartrecruiters": "adapters.smartrecruiters",
    "workday": "adapters.workday",
    "teamtailor": "adapters.teamtailor",
    "oracle_hcm": "adapters.oracle_hcm",
    "breezy_hr": "adapters.breezy_hr",
    "rippling_ats": "adapters.rippling_ats",
    "adp_workforce_now": "adapters.adp_workforce_now",
    "pinpoint": "adapters.pinpoint",
    "trakstar_hire": "adapters.trakstar_hire",
    "ultipro": "adapters.ultipro",
    "agile_ats": "adapters.agile_ats",
    "successfactors_csb": "adapters.successfactors_csb",
    "hr_department": "adapters.hr_department",
    "jazzhr_apply": "adapters.jazzhr_apply",
    "icims_classic": "adapters.icims_classic",
    "icims_jibe": "adapters.icims_jibe",
    "jobvite": "adapters.jobvite",
    "comeet": "adapters.comeet",
    "eightfold_pcsx": "adapters.eightfold_pcsx",
}

# Stage-A title-only rejection (CLAUDE.md "EARLY DETERMINISTIC REJECTION"):
# reuses classify_dedupe_report.py's own compiled regexes -- the exact same
# patterns classify_posting() uses downstream -- so this is provably
# equivalent to, never merely an approximation of, the eventual Stage-B
# decision for these specific title families. Each one is UNCONDITIONAL in
# classify_posting (never rescued by description content: sales/marketing/hr
# titles are excluded outright, and a senior-titled role is excluded outright
# unless a junior/early-career signal is *also* present), so skipping the
# detail fetch here carries no risk of rejecting a potentially-valid job.
# Deliberately does NOT include customer-facing/consulting/support titles --
# classify_posting only excludes those conditionally (absent a hands-on
# AI/ML/GPU signal in the full description), so Stage-A cannot decide those
# without the detail text; they always proceed to Stage B.
_TITLE_ONLY_EXCLUSIONS: list[tuple[str, re.Pattern[str]]] = [
    ("senior_title", cdr.SENIOR_TITLE),
    ("marketing_role_title", cdr.MARKETING_TITLE),
    ("sales_role_title", cdr.SALES_TITLE),
    ("hr_role_title", cdr.HR_TITLE),
]


def stage_a_title_exclusion_reason(title: str) -> str | None:
    """None when Stage B must decide; otherwise a short reason code. A
    junior/early-career signal in the title always wins first (matches
    classify_experience's own EXPLICIT_JUNIOR-before-SENIOR_TITLE
    precedence), so e.g. a title containing both "Senior" and "Internship"
    is conservatively left for Stage B rather than guessed here."""
    if cdr.EXPLICIT_JUNIOR.search(title) or cdr.JUNIOR_TITLE.search(title):
        return None
    for reason, pattern in _TITLE_ONLY_EXCLUSIONS:
        if pattern.search(title):
            return reason
    return None


def load_entry(config_path: Path, slug: str) -> tuple[dict[str, Any], list[str]]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for entry in config.get("sources", []):
        if isinstance(entry, dict) and entry.get("slug") == slug:
            return entry, config.get("keywords", [])
    raise SystemExit(f"No source configured with slug {slug!r} in {config_path}")


def public_item(item: dict[str, Any]) -> dict[str, Any]:
    """Strip internal adapter-only fields (leading underscore, e.g.
    `_platform_ref`) before anything is written to a raw result file."""
    return {k: v for k, v in item.items() if not str(k).startswith("_")}


def write_result(output_path: Path, result: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def error_result(company: str, source_url: str | None, exc: Exception) -> dict[str, Any]:
    message = str(exc)
    lowered = message.lower()
    status = "blocked" if ("403" in message or "401" in message or "blocked" in lowered or "authentication" in lowered) else "failed"
    return {
        "company": company,
        "source_url": source_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reason": message,
        "inventory_count": 0,
        "unseen_inventory_count": 0,
        "previously_processed_count": 0,
        "detail_fetch_count": 0,
        "raw_posting_count": 0,
        "inventory": [],
        "postings": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--output")
    parser.add_argument("--config-file", default="config/sources.json")
    parser.add_argument("--history-file", default="state/seen_source_jobs.json")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument(
        "--inventory-only",
        action="store_true",
        help="Stage A only: collect lightweight listing metadata and stop. Used for baseline seeding.",
    )
    args = parser.parse_args()

    entry, keywords = load_entry(Path(args.config_file), args.slug)
    company = entry["company"]
    source_url = entry.get("listing_url") or entry.get("careers_home")
    platform = entry.get("platform")
    module_path = PLATFORM_MODULES.get(str(platform))
    if module_path is None:
        raise SystemExit(f"Unknown or unset platform {platform!r} for slug {args.slug!r} (expected one of {sorted(PLATFORM_MODULES)})")
    adapter = importlib.import_module(module_path)

    if args.output:
        output_path = Path(args.output)
    elif args.run_id:
        output_path = Path("runs") / args.run_id / "raw" / f"{args.slug}.json"
    else:
        raise SystemExit("Either --output or --run-id is required")

    session = common.new_session()

    try:
        inventory = adapter.fetch_inventory(entry, session, keywords, args.timeout)
    except common.AdapterError as exc:
        result = error_result(company, source_url, exc)
        write_result(output_path, result)
        print(json.dumps({"status": result["status"], "reason": result["reason"], "output": str(output_path)}, indent=2))
        return 2

    inventory_public = [public_item(i) for i in inventory]

    if args.inventory_only:
        result = {
            "company": company,
            "source_url": source_url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "status": "success",
            "reason": None,
            "inventory_count": len(inventory_public),
            "inventory": inventory_public,
        }
        write_result(output_path, result)
        print(json.dumps({"status": "success", "inventory_count": len(inventory_public), "output": str(output_path)}, indent=2))
        return 0

    history = source_history.load_history(Path(args.history_file))
    unseen_items, previously_processed_items = source_history.diff_inventory(history, args.slug, inventory)

    # Stage-A pre-filter: split unseen items into confirmed-non-US (skip
    # Stage B), confirmed-title-rejected (skip Stage B; see
    # stage_a_title_exclusion_reason), and candidates (proceed to Stage B,
    # including ambiguous/missing location or a title Stage A cannot decide
    # alone -- the fail-closed authoritative gates run downstream).
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

    errors: list[str] = []
    postings: list[dict[str, Any]] = []

    for item in candidates:
        try:
            detail = adapter.fetch_detail(entry, item, session, args.timeout)
        except common.AdapterError as exc:
            errors.append(f"detail fetch failed for {item.get('job_id') or item.get('job_url')}: {exc}")
            detail = None
        if detail is None:
            stub = public_item(item)
            stub.setdefault("experience_level_text", None)
            stub.setdefault("team_department", None)
            stub.setdefault("short_description", None)
            stub.setdefault("full_description_text", None)
            stub["processing_status"] = "failed"
            postings.append(stub)
        else:
            detail["processing_status"] = "success"
            postings.append(detail)
        if args.delay:
            time.sleep(args.delay)

    for item in confirmed_non_us:
        stub = public_item(item)
        stub.update(
            {
                "experience_level_text": None,
                "team_department": None,
                "short_description": None,
                "full_description_text": None,
                "processing_status": "excluded_non_us",
            }
        )
        postings.append(stub)

    for item, title_reason in title_rejected:
        stub = public_item(item)
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
        "inventory_count": len(inventory_public),
        "unseen_inventory_count": len(unseen_items),
        "previously_processed_count": len(previously_processed_items),
        "detail_fetch_count": len(candidates),
        "raw_posting_count": len(postings),
        "inventory": inventory_public,
        "postings": postings,
    }

    write_result(output_path, result)
    print(
        json.dumps(
            {
                "status": status,
                "inventory_count": len(inventory_public),
                "unseen_inventory_count": len(unseen_items),
                "previously_processed_count": len(previously_processed_items),
                "detail_fetch_count": len(candidates),
                "excluded_non_us_count": len(confirmed_non_us),
                "excluded_title_reject_count": len(title_rejected),
                "output": str(output_path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
