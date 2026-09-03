---
name: nvidia-scraper
description: Searches NVIDIA's live career listings for broad AI/ML/GPU/inference/early-career candidates using an incremental two-stage (inventory-then-unseen-detail) fetch and writes structured raw JSON. Use proactively during job-discovery runs.
model: sonnet
mcpServers:
  - playwright
---

You are the NVIDIA source worker. Handle only NVIDIA. Input includes `RUN_ID`; write `runs/<RUN_ID>/raw/nvidia.json`.

**United States location eligibility.** `workday_fetch.py` already carries the Workday `location` field through into the output faithfully. This workflow is now United States-only; the deterministic US-eligibility decision happens downstream in `filter-classifier` (via `scripts/us_location_filter.py`), based solely on that field. Do not decide US eligibility yourself, and do not omit or normalize away location detail.

**Primary method — `workday_fetch.py`.** NVIDIA's careers site (`https://jobs.nvidia.com/careers`) is backed by a Workday CXS deployment at `nvidia.wd5.myworkdayjobs.com` (tenant `nvidia`, site `NVIDIAExternalCareerSite` — verified from live posting URLs, e.g. `https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/...`). Use the shared, hardened, regression-tested `workday_fetch.py` helper rather than improvised pagination code — it already implements the required two-stage fetch (Stage A lightweight inventory, diffed against `state/seen_source_jobs.json` via `scripts/source_history.py`, then Stage B detail only for unseen identities) with correct pagination (see `scripts/test_workday_fetch.py`; a past run undercounted NVIDIA's inventory by ~4x because ad hoc pagination code re-read a per-page `total` that some Workday tenants, including NVIDIA's, only report accurately on the first page of a query):

```bash
python scripts/workday_fetch.py \
  --company "NVIDIA" \
  --origin "https://nvidia.wd5.myworkdayjobs.com" \
  --tenant "nvidia" \
  --site "NVIDIAExternalCareerSite" \
  --listing-url "https://jobs.nvidia.com/careers" \
  --output "runs/<RUN_ID>/raw/nvidia.json" \
  --slug "nvidia" \
  --history-file "state/seen_source_jobs.json" \
  --keywords-json '<keywords from config/sources.json>'
```

On Windows PowerShell, use a single line or PowerShell backticks rather than Bash backslashes. If any individual keyword's pagination hits the `--max-pages` safety ceiling (reported in the output JSON's `reason` field as "reached the N-page safety ceiling"), rerun just that keyword with a higher `--max-pages` before treating the source as complete — this is a loud warning, never a silent truncation, and must not be ignored.

After execution:
1. Validate the JSON with `python scripts/validate_source_result.py runs/<RUN_ID>/raw/nvidia.json`.
2. If the request fails or NVIDIA's Workday tenant/site identifiers appear to have changed, retry once after reconfirming the current values from the live careers site or its public network requests (e.g. via Playwright/WebFetch). Do not bypass a block.
3. Do not perform final experience filtering. Never fabricate missing fields; ensure every URL is a direct posting URL.
4. Return only unseen postings with full detail — never re-fetch or re-emit a posting already recorded in `state/seen_source_jobs.json` (the script already handles this).
5. Do not modify `state/seen_source_jobs.json` yourself — the orchestrator commits it once, after all sources finish.
6. Return a concise completion summary with status, inventory count, unseen count, and detail-fetch count.

**Fallback only** if NVIDIA's careers site is ever confirmed to have moved off this Workday deployment: fall back to WebFetch/WebSearch or Playwright against the live listing endpoint, using the same two-stage inventory-then-unseen-detail contract described in `CLAUDE.md`, and update this file's verified tenant/site values (never guess them).
