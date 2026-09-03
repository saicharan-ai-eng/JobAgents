"""Generic merge of two workday_fetch.py passes for one source into the final raw file.

Usage: python scripts/merge_two_passes.py <run_id> <slug> <pass2_filename>

Merges <slug>.json (pass1) and <pass2_filename> (pass2, ceiling-hit keywords only,
deeper depth) into runs/<run_id>/raw/<slug>.json, deduping inventory and postings
by job_id, and combining partial-status reasons from both passes.
"""
import json
import sys
from pathlib import Path

run_id = sys.argv[1]
slug = sys.argv[2]
pass2_filename = sys.argv[3]
raw_dir = Path("runs") / run_id / "raw"

pass1 = json.loads((raw_dir / f"{slug}.json").read_text(encoding="utf-8"))
pass2 = json.loads((raw_dir / pass2_filename).read_text(encoding="utf-8"))

inv_by_id = {}
for rec in pass1.get("inventory", []) + pass2.get("inventory", []):
    inv_by_id[rec["job_id"]] = rec
merged_inventory = list(inv_by_id.values())

post_by_id = {}
for rec in pass1.get("postings", []) + pass2.get("postings", []):
    post_by_id[rec["job_id"]] = rec
merged_postings = list(post_by_id.values())

reasons = []
if pass1.get("reason"):
    reasons.append(f"[pass1] {pass1['reason']}")
if pass2.get("reason"):
    reasons.append(f"[pass2/deeper-pagination, ceiling-keywords-only] {pass2['reason']}")
merged_reason = " || ".join(reasons) if reasons else None

merged = dict(pass1)
merged["status"] = "partial" if merged_reason else "success"
merged["reason"] = merged_reason
merged["inventory_count"] = len(merged_inventory)
merged["inventory"] = merged_inventory
merged["postings"] = merged_postings
merged["unseen_inventory_count"] = len(merged_postings)
merged["raw_posting_count"] = len(merged_postings)
merged["detail_fetch_count"] = len(merged_postings)
merged["previously_processed_count"] = merged["inventory_count"] - merged["unseen_inventory_count"]

out_path = raw_dir / f"{slug}.json"
out_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps({
    "status": merged["status"],
    "inventory_count": merged["inventory_count"],
    "unseen_inventory_count": merged["unseen_inventory_count"],
    "raw_posting_count": merged["raw_posting_count"],
}, indent=2))
