#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

REQUIRED_TOP = {"company", "source_url", "fetched_at", "status", "raw_posting_count", "postings"}
ALLOWED_STATUS = {"success", "partial", "blocked", "failed"}
ALLOWED_PROCESSING_STATUS = {"success", "failed", "blocked", "excluded_non_us", "excluded_title_reject", "excluded_out_of_scope"}
# Two-stage incremental-workflow fields are optional for backward compatibility
# with raw files written before this contract existed; when present, their
# shape is still checked.
OPTIONAL_INT_FIELDS = ("inventory_count", "unseen_inventory_count", "previously_processed_count", "detail_fetch_count")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    args = parser.parse_args()
    path = Path(args.path)
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = REQUIRED_TOP - set(data)
    errors: list[str] = []
    if missing:
        errors.append(f"missing top-level fields: {sorted(missing)}")
    if data.get("status") not in ALLOWED_STATUS:
        errors.append(f"invalid status: {data.get('status')!r}")
    postings = data.get("postings")
    if not isinstance(postings, list):
        errors.append("postings must be a list")
        postings = []
    if data.get("raw_posting_count") != len(postings):
        errors.append("raw_posting_count does not equal len(postings)")
    for index, job in enumerate(postings):
        if not isinstance(job, dict):
            errors.append(f"postings[{index}] is not an object")
            continue
        for field in ("company", "job_title", "job_url"):
            if not job.get(field):
                errors.append(f"postings[{index}] missing {field}")
        if "processing_status" in job and job["processing_status"] not in ALLOWED_PROCESSING_STATUS:
            errors.append(f"postings[{index}] invalid processing_status: {job['processing_status']!r}")

    for field in OPTIONAL_INT_FIELDS:
        if field in data and not isinstance(data[field], int):
            errors.append(f"{field} must be an integer when present")

    if "inventory" in data:
        inventory = data["inventory"]
        if not isinstance(inventory, list):
            errors.append("inventory must be a list when present")
        else:
            for index, item in enumerate(inventory):
                if not isinstance(item, dict):
                    errors.append(f"inventory[{index}] is not an object")
                    continue
                for field in ("company", "job_title", "job_url"):
                    if not item.get(field):
                        errors.append(f"inventory[{index}] missing {field}")

    if "unseen_inventory_count" in data and data["unseen_inventory_count"] != len(postings):
        errors.append("unseen_inventory_count does not equal len(postings)")

    if errors:
        print("INVALID")
        for error in errors:
            print(f"- {error}")
        return 2
    print(f"VALID: {path} ({len(postings)} postings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
