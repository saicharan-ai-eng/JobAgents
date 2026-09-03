---
name: Discover AI Jobs
description: Runs the configured-sources incremental AI/ML/NVIDIA job workflow, fetching full detail only for genuinely new postings, ranks qualifying roles for inference/GPU career fit, monitors persistent history, and conditionally alerts on unseen jobs.
disable-model-invocation: true
argument-hint: "[optional run note]"
allowed-tools: Agent Bash Read Write Glob Grep
model: sonnet
effort: high
---

Run a fresh incremental job-discovery snapshot, rank newly-unseen qualifying roles for inference/GPU career fit, and compare the result with prior successful runs. Optional user note: `$ARGUMENTS`.

**This workflow is United States-only.** A posting must have `us_location_eligible: true` (see `AGENTS.md`, "United States location eligibility", and `scripts/us_location_filter.py`) before it can be included, ranked, deduplicated, reported, stored in `state/seen_jobs.json`, or notified. Location eligibility is derived solely from the posting's structured `location` field — never job title, description, salary text, company headquarters, or the job-board domain — and fails closed on ambiguity.

This is an **incremental, new-postings-only** workflow. Every run re-scans lightweight listing metadata from every source, but only opens detail pages and runs full classification/ranking/reporting for postings that are genuinely unseen (a new job ID, or the same job ID with an explicitly newer, source-flagged repost). Postings already recorded in `state/seen_source_jobs.json` are never re-fetched or re-classified.

## 1. Initialize

Create a UTC `RUN_ID` in `YYYYMMDDTHHMMSSZ` format and make:

- `runs/<RUN_ID>/raw`
- `runs/<RUN_ID>/logs`

Read `AGENTS.md`, `config/sources.json`, `config/fit_priorities.json`, and `config/notifications.json` before delegating. Preserve `state/seen_jobs.json` and `state/seen_source_jobs.json` — never reset or delete either.

## 2. Dispatch every configured source's agent in bounded batches

Read the `sources` array from `config/sources.json`. **Do not hardcode a source count or a fixed list of agent names.** For every entry in that array, spawn the subagent named in its `agent` field, passing `RUN_ID` and that entry's `slug`/`company`/`listing_url` (Workday fields when `type` is `workday`; the entry's `platform` and its platform-specific identifier field, e.g. `greenhouse_board_token`, when `type` is `generic`). If a new source is later added to `config/sources.json`, it is dispatched automatically the next time this skill runs — no edit to this file is needed.

Many sources now share the same `generic-ats-scraper` agent (see AGENTS.md "Retrieval strategy") — its actual work is a single deterministic `scripts/run_source.py` call, not open-ended browsing, so it is cheap to run many of in one pass. Even so, do not spawn all configured sources' agents in one unbounded burst once the source count is large: dispatch in batches of roughly 8–10 concurrent agents, waiting for each batch to finish before starting the next, so no single run attempts dozens of simultaneous subagent calls. One slow or blocked source must never delay or prevent the rest of the run from completing — each source keeps its own ~5-minute fetch budget regardless of batch position.

Each dispatched agent must run its own **two-stage** fetch (see its own instructions and `AGENTS.md`'s source-result contract):

- **Stage A — inventory**: collect lightweight listing metadata (job_id, title, location, posting_date, direct URL) for every currently-listed posting. Do not open detail pages yet. When the Stage-A `location` string already reliably identifies the posting as non-US (e.g. a listing site that always returns a precise country/state), skip the Stage-B detail fetch for that identity — there is no need to open a detail page for a posting that will fail the US-location gate regardless of its content. When Stage-A location is missing, truncated, or ambiguous, do not use that alone to decide; proceed to Stage B so the authoritative filter can evaluate the fuller detail-page location.
- **Stage B — unseen detail**: diff that inventory against `state/seen_source_jobs.json` (via `python scripts/source_history.py diff-inventory`) and open detail pages only for identities that come back unseen (new job ID, or a same-ID posting the source explicitly flags as reposted with a newer posting date) and not already confirmed non-US at Stage A. Write only those unseen, fully-detailed postings into the source-result's `postings` array — never re-fetch or re-emit a previously-processed posting's full detail. The US-location filter is re-applied downstream in `filter-classifier` on the Stage-B detail location regardless of the Stage-A outcome; Stage-A skipping is an optimization, not the authoritative gate.

Instruct each agent not to make the final experience or fit decision, and not to handle another source's slug. Wait for every dispatched agent to finish, then run `python scripts/ensure_expected_sources.py --run-dir runs/<RUN_ID>` so any missing output becomes an explicit failed source record — a source that produced nothing is recorded as failed, never silently treated as "no jobs."

## 3. Commit raw source history

Run:

```text
python scripts/source_history.py commit --run-dir runs/<RUN_ID>
```

This updates `state/seen_source_jobs.json` from every source's raw result — for **every** encountered posting, including ones that will later fail the AI/ML or experience filters — and only after each source-result file has already been written and validated. If a run is interrupted before this step, no posting is marked processed and it remains eligible for Stage B on the next run. A source recorded as `blocked`/`failed`/`partial` never has its prior history erased; its previously known postings are left untouched, not assumed gone.

## 4. Check whether any unseen postings exist

Run:

```text
python scripts/source_history.py summarize --run-dir runs/<RUN_ID>
```

This aggregates, across every source: listing records scanned, unseen postings discovered, detail pages fetched, and previously-processed count.

**If `unseen_postings_discovered` is 0 across all sources**, take the short-circuit path and skip step 5 entirely (no filter-classifier, deduplicator, or aggregator-reporter subagent call — there is nothing new to classify):

- Write `runs/<RUN_ID>/deduplicated.json` as `[]` and a minimal `runs/<RUN_ID>/report.md` stating no newly posted jobs were found this run, with the per-source scan counts from the summarize step and any unavailable sources listed.
- Write `runs/<RUN_ID>/notification.json` directly with `should_notify: false`, `new_job_count: 0`, and a message of "No newly posted jobs detected."
- Do not invoke `new-job-monitor` and do not run `notify_if_new_jobs.py` — there is nothing to alert on.
- Go straight to step 7 (final response).

Otherwise, continue to step 5 with only the unseen postings collected this run.

## 5. Filter, classify, rank, deduplicate, and report

Invoke `filter-classifier` with `RUN_ID`. It inspects every raw file's `postings` array (already scoped to this run's unseen postings only), preserves original source text, applies the US-location eligibility gate (`us_location_eligible`, `us_location_reason`, `normalized_us_locations`, `location_inferred` — derived solely from the `location` field, fail-closed), and applies `config/fit_priorities.json` deterministically. Every included job must have `us_location_eligible: true`, retain broad eligibility, and carry `fit_priority`, `fit_label`, `fit_score`, `fit_keywords_matched`, and `fit_reason`. Do not discard qualifying Priority 3 or Priority 4 jobs — but a non-US or ambiguous-location job is never included regardless of tier.

Invoke `deduplicator` with `RUN_ID`. Final ordering must be priority ascending, score descending, posted date newest first, then company/title.

Invoke `aggregator-reporter` with `RUN_ID`. Verify that both `runs/<RUN_ID>/report.md` and `runs/<RUN_ID>/priority_shortlist.md` exist, that the audit log names every source configured in `config/sources.json` (not a hardcoded count), and that the report distinguishes: listing records scanned, unseen postings discovered, detail pages fetched, postings passing filters, and final deduplicated count. Priority 1 and Priority 2 roles must appear first in both files.

## 6. Detect unseen qualifying jobs and notify conditionally

Invoke `new-job-monitor` with `RUN_ID` only after the reports and final deduplicated data are complete.

Required outputs:

- `runs/<RUN_ID>/new_jobs.json`
- `runs/<RUN_ID>/new_jobs.md`
- `runs/<RUN_ID>/notification.json`
- `runs/<RUN_ID>/notification_delivery.json` only when desktop delivery was attempted
- persistent `state/seen_jobs.json`

The first completed run establishes the baseline unless `notify_on_first_run` is enabled in `config/notifications.json`. It must not flood the user with every existing job by default. Later alerts include all unseen qualifying roles, ordered strongest-fit first, and the report must call out how many are new Priority 1 and new Priority 2 jobs specifically. Notify only for `new_posting` records with `us_location_eligible: true` — since the location gate already runs upstream in `filter-classifier`, every record reaching `deduplicated.json` is already US-eligible, but never notify for a record missing that field.

## 7. Final response

Read `notification.json` and follow these rules:

- When `baseline_created: true`, say the baseline was established, give the number of stored matching jobs, and clearly state that no alert was sent.
- When `should_notify: false` (including the step-4 short-circuit), say no newly posted jobs were found and no alert was sent — report "No newly posted jobs detected" verbatim when that was the short-circuit reason. Keep this concise, but mention unavailable sources.
- When `should_notify: true`, begin with `🚨 NEW JOB ALERT`, give the priority breakdown (explicitly call out new Priority 1 and new Priority 2 counts), and show every new job with fit label, score, location, posting date, and direct application link. List Priority 1 and Priority 2 jobs first. Give desktop-notification delivery status and the `new_jobs.md` path.

Always provide the run summary — including listing records scanned, unseen postings discovered, and detail pages fetched, alongside new qualifying jobs and new Priority 1/Priority 2 counts — unavailable sources, and local paths to `report.md`, `priority_shortlist.md`, `new_jobs.md`, `filtered.json`, and `deduplicated.json` (when they were produced this run). Never claim a desktop or mobile notification was delivered unless the relevant output explicitly confirms it.

Never claim success for a source without a valid source-result file. Never fabricate missing jobs or URLs. If a source returned zero unseen postings successfully, report zero rather than labeling it failed.
