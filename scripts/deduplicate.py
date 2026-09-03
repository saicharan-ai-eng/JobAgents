#!/usr/bin/env python3
"""Deterministically deduplicate runs/<RUN_ID>/filtered.json into deduplicated.json.

Reads filtered.json, keeps only include==true records, deduplicates by
(company, job_id) when a job ID exists (otherwise by company/normalized
title/location), and fails closed (nonzero exit, no file written) if the
include-only invariant would ever be violated -- e.g. more deduplicated
records than included records, or a non-included record slipping through.

Run this after filtered.json has been reviewed/corrected (by the
filter-classifier agent or a human) so deduplication itself never depends on
a subagent's judgment.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dedup_lib import DedupInvariantError, deduplicate_included


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    filtered_path = run_dir / "filtered.json"
    filtered = json.loads(filtered_path.read_text(encoding="utf-8"))

    included_count = sum(1 for job in filtered if isinstance(job, dict) and job.get("include") is True)
    excluded_count = len(filtered) - included_count

    try:
        deduped = deduplicate_included(filtered)
    except DedupInvariantError as exc:
        print(json.dumps({"error": str(exc), "run_dir": str(run_dir)}, indent=2))
        return 1

    (run_dir / "deduplicated.json").write_text(
        json.dumps(deduped, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "total_filtered": len(filtered),
                "included": included_count,
                "excluded": excluded_count,
                "deduplicated": len(deduped),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
