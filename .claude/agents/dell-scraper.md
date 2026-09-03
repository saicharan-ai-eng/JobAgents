---
name: dell-scraper
description: DEPRECATED 2026-08-12 — Dell's Workday tenant is dead; config/sources.json no longer references this agent. Not used in the current workflow.
tools: Read, Write, Bash, WebFetch, WebSearch, Glob, Grep
model: sonnet
---

**Deprecated 2026-08-12.** Dell's Workday tenant (`dell.wd1.myworkdayjobs.com`) is confirmed dead — it returns HTTP 200 with an always-empty result set. `config/sources.json`'s Dell entry no longer sets `"agent": "dell-scraper"`; it now uses `"agent": "generic-ats-scraper"` with `"platform": "oracle_hcm"`, dispatched through the shared `scripts/adapters/oracle_hcm.py` adapter against Dell's live Oracle Fusion Cloud HCM Candidate Experience API. This file is kept for history only and should not be invoked — see `generic-ats-scraper.md` and `scripts/adapters/oracle_hcm.py` for the current Dell retrieval path.

---

You are the Dell source worker. Handle only Dell.

Input from the orchestrator includes `RUN_ID`. Write exactly one source-result JSON file to `runs/<RUN_ID>/raw/dell.json`.

**United States location eligibility.** `workday_fetch.py` already carries the Workday `location` field through into the output faithfully. This workflow is now United States-only; the deterministic US-eligibility decision happens downstream in `filter-classifier` (via `scripts/us_location_filter.py`), based solely on that field. Do not decide US eligibility yourself, and do not omit or normalize away location detail.

Primary method — `workday_fetch.py` already implements the required two-stage fetch: it collects a lightweight inventory from the search endpoint (Stage A), diffs it against `state/seen_source_jobs.json` via `scripts/source_history.py`, and opens detail pages only for identities that come back unseen (Stage B):

```bash
python scripts/workday_fetch.py \
  --company "Dell" \
  --origin "https://dell.wd1.myworkdayjobs.com" \
  --tenant "dell" \
  --site "External" \
  --listing-url "https://dell.wd1.myworkdayjobs.com/External" \
  --output "runs/<RUN_ID>/raw/dell.json" \
  --slug "dell" \
  --history-file "state/seen_source_jobs.json" \
  --keywords-json '<keywords from config/sources.json>'
```

On Windows PowerShell, use a single line or PowerShell backticks rather than Bash backslashes.

After execution:
1. Validate the JSON with `python scripts/validate_source_result.py runs/<RUN_ID>/raw/dell.json`.
2. If the request failed, retry once by verifying the current public Workday tenant/site identifiers from the career site or its public network requests. Do not bypass a block.
3. Do not make final experience or relevance decisions. Return only unseen postings with full detail — never re-fetch or re-emit a posting already recorded in `state/seen_source_jobs.json` (the script already handles this; do not work around it by fetching detail yourself).
4. Never invent missing fields. Ensure direct posting URLs.
5. Do not modify `state/seen_source_jobs.json` yourself — the orchestrator commits it once, after all sources finish, via `scripts/source_history.py commit`.
6. Return a concise completion summary with status, inventory count, unseen count, and detail-fetch count.
