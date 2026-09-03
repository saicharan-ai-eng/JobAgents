"""One-off merge of NVIDIA's multi-pass workday_fetch.py outputs for a single run.

Merges pass1 (all 18 keywords, shallow depth) and pass2 (ceiling-hit keywords,
deeper depth) into the final runs/<RUN_ID>/raw/nvidia.json, deduping inventory
and postings by job_id, and combining the partial-status reasons from both
passes into one final reason string.
"""
import json
import sys
from pathlib import Path

run_id = sys.argv[1]
raw_dir = Path("runs") / run_id / "raw"

pass1 = json.loads((raw_dir / "nvidia.json").read_text(encoding="utf-8"))
pass2 = json.loads((raw_dir / "nvidia_pass2.json").read_text(encoding="utf-8"))

# Merge inventory by job_id
inv_by_id = {}
for rec in pass1.get("inventory", []) + pass2.get("inventory", []):
    inv_by_id[rec["job_id"]] = rec
merged_inventory = list(inv_by_id.values())

# Merge postings by job_id (unseen, fully-detailed postings)
post_by_id = {}
for rec in pass1.get("postings", []) + pass2.get("postings", []):
    post_by_id[rec["job_id"]] = rec
merged_postings = list(post_by_id.values())

reasons = []
if pass1.get("reason"):
    reasons.append(f"[pass1/depth10] {pass1['reason']}")
if pass2.get("reason"):
    reasons.append(f"[pass2/depth30, ceiling-keywords-only] {pass2['reason']}")
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

out_path = raw_dir / "nvidia.json"
out_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps({
    "status": merged["status"],
    "inventory_count": merged["inventory_count"],
    "unseen_inventory_count": merged["unseen_inventory_count"],
    "raw_posting_count": merged["raw_posting_count"],
}, indent=2))
