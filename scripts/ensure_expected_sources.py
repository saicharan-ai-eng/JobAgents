#!/usr/bin/env python3
"""Create explicit failed records for any expected source that produced no file."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--config", default="config/sources.json")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    created: list[str] = []

    for source in config.get("sources", []):
        slug = source["slug"]
        path = raw_dir / f"{slug}.json"
        if path.exists():
            continue
        result = {
            "company": source["company"],
            "source_url": source["listing_url"],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "status": "failed",
            "reason": "Site agent produced no result file",
            "raw_posting_count": 0,
            "postings": [],
        }
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        created.append(str(path))

    print(json.dumps({"created_missing_records": created, "count": len(created)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
