"""Bounded-concurrency driver for the 68 generic-ats-scraper sources.

Runs scripts/run_source.py once per slug via subprocess, with a thread pool
capping concurrency, so no single production run fires dozens of simultaneous
HTTP fetches at once. Prints a one-line status per completed source.
"""
import argparse
import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path


def run_one(slug: str, run_id: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "scripts/run_source.py", "--slug", slug, "--run-id", run_id],
        capture_output=True,
        text=True,
        timeout=300,
    )
    out_path = Path("runs") / run_id / "raw" / f"{slug}.json"
    status = "unknown"
    reason = None
    if out_path.exists():
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
            status = data.get("status")
            reason = data.get("reason")
        except Exception as exc:
            status = "failed"
            reason = f"could not parse output: {exc}"
    else:
        status = "failed"
        reason = f"no output file written (exit={proc.returncode}); stderr={proc.stderr[-500:]}"
    return {"slug": slug, "status": status, "reason": reason, "returncode": proc.returncode}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--slugs-file", required=True, help="Path to a JSON file with a list of slugs")
    parser.add_argument("--max-workers", type=int, default=8)
    args = parser.parse_args()

    slugs = json.loads(Path(args.slugs_file).read_text(encoding="utf-8"))
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = {pool.submit(run_one, slug, args.run_id): slug for slug in slugs}
        for fut in concurrent.futures.as_completed(futures):
            slug = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:
                res = {"slug": slug, "status": "failed", "reason": f"exception: {exc}", "returncode": -1}
            results.append(res)
            print(f"{res['slug']}: {res['status']} {res.get('reason') or ''}")

    ok = sum(1 for r in results if r["status"] == "success")
    partial = sum(1 for r in results if r["status"] == "partial")
    blocked = sum(1 for r in results if r["status"] == "blocked")
    failed = sum(1 for r in results if r["status"] not in ("success", "partial", "blocked"))
    print(f"\nDONE: {len(results)} sources | success={ok} partial={partial} blocked={blocked} failed={failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
