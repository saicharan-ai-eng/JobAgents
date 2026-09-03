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

**This workflow is United States-only.** A posting must have `us_location_eligible: true` (see `CLAUDE.md`, "United States location eligibility", and `scripts/us_location_filter.py`) before it can be included, ranked, deduplicated, reported, stored in `state/seen_jobs.json`, or notified. Location eligibility is derived solely from the posting's structured `location` field — never job title, description, salary text, company headquarters, or the job-board domain — and fails closed on ambiguity.

This is an **incremental, new-postings-only** workflow. Every run re-scans lightweight listing metadata from every source, but only opens detail pages and runs full classification/ranking/reporting for postings that are genuinely unseen (a new job ID, or the same job ID with an explicitly newer, source-flagged repost). Postings already recorded in `state/seen_source_jobs.json` are never re-fetched or re-classified.

## 1. Initialize

Create a UTC `RUN_ID` in `YYYYMMDDTHHMMSSZ` format and make:

- `runs/<RUN_ID>/raw`
- `runs/<RUN_ID>/logs`

Read `CLAUDE.md`, `config/sources.json`, `config/fit_priorities.json`, and `config/notifications.json` before delegating. Preserve `state/seen_jobs.json` and `state/seen_source_jobs.json` — never reset or delete either.

## 2. Dispatch every configured source's agent in bounded batches

Read the `sources` array from `config/sources.json`. **Do not hardcode a source count or a fixed list of agent names.** For every entry in that array, spawn the subagent named in its `agent` field, passing `RUN_ID` and that entry's `slug`/`company`/`listing_url` (Workday fields when `type` is `workday`; the entry's `platform` and its platform-specific identifier field, e.g. `greenhouse_board_token`, when `type` is `generic`). If a new source is later added to `config/sources.json`, it is dispatched automatically the next time this skill runs — no edit to this file is needed.

Many sources now share the same `generic-ats-scraper` agent (see CLAUDE.md "Retrieval strategy") — its actual work is a single deterministic `scripts/run_source.py` call, not open-ended browsing, so it is cheap to run many of in one pass. Even so, do not spawn all configured sources' agents in one unbounded burst once the source count is large: dispatch in batches of roughly 8–10 concurrent agents, waiting for each batch to finish before starting the next, so no single run attempts dozens of simultaneous subagent calls. One slow or blocked source must never delay or prevent the rest of the run from completing — each source keeps its own ~5-minute fetch budget regardless of batch position.

Each dispatched agent must run its own **two-stage** fetch (see its own instructions and `CLAUDE.md`'s source-result contract):

- **Stage A — inventory**: collect lightweight listing metadata (job_id, title, location, posting_date, direct URL) for every currently-listed posting. Do not open detail pages yet. When the Stage-A `location` string already reliably identifies the posting as non-US (e.g. a listing site that always returns a precise country/state), skip the Stage-B detail fetch for that identity — there is no need to open a detail page for a posting that will fail the US-location gate regardless of its content. When Stage-A location is missing, truncated, or ambiguous, do not use that alone to decide; proceed to Stage B so the authoritative filter can evaluate the fuller detail-page location. For sources routed through the shared `generic-ats-scraper` (`scripts/run_source.py`), the same Stage-A pass also skips the detail fetch for a title that unconditionally excludes regardless of description content — senior/staff/principal/lead/director/manager title, or a marketing/sales/HR role family — recorded as `processing_status: "excluded_title_reject"` (a junior/early-career signal in the title always wins first, so this is conservative). This does not decide experience-years or domain fit, which always require Stage-B text.
- **Stage B — unseen detail**: diff that inventory against `state/seen_source_jobs.json` (via `python scripts/source_history.py diff-inventory`) and open detail pages only for identities that come back unseen (new job ID, or a same-ID posting the source explicitly flags as reposted with a newer posting date) and not already confirmed non-US or title-rejected at Stage A. Write only those unseen, fully-detailed postings into the source-result's `postings` array — never re-fetch or re-emit a previously-processed posting's full detail. The US-location filter and experience/domain rules are re-applied downstream in `filter-classifier` regardless of the Stage-A outcome; Stage-A skipping is an optimization, not the authoritative gate.

Instruct each agent not to make the final experience or fit decision, and not to handle another source's slug. Wait for every dispatched agent to finish, then run `python scripts/ensure_expected_sources.py --run-dir runs/<RUN_ID>` so any missing output becomes an explicit failed source record — a source that produced nothing is recorded as failed, never silently treated as "no jobs."

## 3. Finalize source collection

Do not commit source history until every configured source is confirmed in a terminal state (`success`/`partial`/`blocked`/`failed`) — this is the checkpoint that hardens against a source agent still rewriting its raw file after collection was assumed complete (run 20260812T152540Z: NVIDIA's raw file was still being corrected when history was committed, forcing a manual rerun of commit/summarize/classify/dedup/report). Run:

```text
python scripts/finalize_sources.py manifest --run-dir runs/<RUN_ID>
```

This asserts every configured source's raw file exists with a terminal status, then computes and persists a SHA-256 checksum, record count, status, and completion timestamp for each into `runs/<RUN_ID>/source_manifest.json`. A nonzero exit means some source is still missing or invalid — resolve that (rerun `ensure_expected_sources.py`, or dispatch the source's agent again) before continuing; do not proceed to step 4 on a failed finalize.

## 4. Commit raw source history

Before committing, verify the raw files have not changed since finalization:

```text
python scripts/finalize_sources.py verify --run-dir runs/<RUN_ID>
```

If this reports any changed or missing file (a source agent kept writing after finalization), recover before committing:

```text
python scripts/finalize_sources.py refresh --run-dir runs/<RUN_ID> --note "<why, e.g. NVIDIA pagination fix landed mid-run>"
```

`refresh` recomputes the manifest entry only for the affected source(s), leaves every other entry untouched, and appends a recovery-log entry to the manifest recording what changed. Re-run `verify` until it reports `ok: true`, then commit:

```text
python scripts/source_history.py commit --run-dir runs/<RUN_ID>
```

This updates `state/seen_source_jobs.json` from every source's raw result — for **every** encountered posting, including ones that will later fail the AI/ML or experience filters. If a run is interrupted before this step, no posting is marked processed and it remains eligible for Stage B on the next run. A source recorded as `blocked`/`failed`/`partial` never has its prior history erased; its previously known postings are left untouched, not assumed gone. `commit` is safe/idempotent to rerun (e.g. after a refresh) — an already-recorded identity is left with its original `first_seen_at` and only `last_seen_at` advances; nothing is double-counted.

## 5. Check whether any unseen postings exist

Run:

```text
python scripts/source_history.py summarize --run-dir runs/<RUN_ID>
```

This aggregates, across every source: listing records scanned, unseen postings discovered, detail pages fetched, and previously-processed count. `detail_pages_fetched` is the precise "actionable records" signal — it is 0 both when `unseen_postings_discovered` is 0 (nothing new at all) *and* when every unseen identity was already Stage-A-rejected (confirmed non-US, or an unconditionally-excluded senior/marketing/sales/HR title) before reaching Stage B, since `run_source.py` never opens a detail page in either case. `unseen_postings_discovered - detail_pages_fetched` (both already in this summary) is the count of identities Stage-A rejected without a detail fetch, useful for the final response but not itself a reason to skip the short-circuit.

**If `detail_pages_fetched` is 0 across all sources**, take the short-circuit path and skip step 6 entirely (no filter-classifier, deduplicator, or aggregator-reporter subagent call — there is nothing new requiring classification, and nothing genuinely ambiguous can exist if nothing was even fetched):

- Write `runs/<RUN_ID>/deduplicated.json` as `[]`, `runs/<RUN_ID>/needs_review.json` as `[]`, and a minimal `runs/<RUN_ID>/report.md` stating no newly posted jobs were found this run, with the per-source scan counts from the summarize step (including how many were Stage-A-rejected before any detail fetch) and any unavailable sources listed.
- Write `runs/<RUN_ID>/notification.json` directly with `should_notify: false`, `new_job_count: 0`, and a message of "No newly posted jobs detected."
- Do not invoke `new-job-monitor` and do not run `notify_if_new_jobs.py` — there is nothing to alert on.
- This short-circuit only ever skips the classify/dedupe/report/notify stages that would otherwise operate on zero records — it never skips or weakens source finalization (step 3) or the history commit (step 4), both of which already ran before this check.
- Go straight to step 8 (final response).

Otherwise, continue to step 6 with only the unseen postings collected this run.

## 6. Filter, classify, rank, deduplicate, and report

Before classifying, re-verify the raw files have not changed since the last check (`python scripts/finalize_sources.py verify --run-dir runs/<RUN_ID>`) — a source could in principle still rewrite its raw file between the commit in step 4 and this step. If it reports a change, `refresh` (as in step 4), then **re-run `python scripts/source_history.py commit --run-dir runs/<RUN_ID>`** (safe/idempotent) before proceeding, so history reflects the corrected data too.

Invoke `filter-classifier` with `RUN_ID`. It inspects every raw file's `postings` array (already scoped to this run's unseen postings only), preserves original source text, applies the US-location eligibility gate (`us_location_eligible`, `us_location_reason`, `normalized_us_locations`, `location_inferred` — derived solely from the `location` field, fail-closed), and applies `config/fit_priorities.json` deterministically. Every included job must have `us_location_eligible: true`, retain broad eligibility, and carry `fit_priority`, `fit_label`, `fit_score`, `fit_keywords_matched`, and `fit_reason`. Do not discard qualifying Priority 3 or Priority 4 jobs — but a non-US or ambiguous-location job is never included regardless of tier.

Its deterministic step (`classify_dedupe_report.py`) also writes `runs/<RUN_ID>/needs_review.json` — a compact list of only the records the parser could not confidently resolve on its own (see reason codes in `CLAUDE.md` "CREATE A REVIEW QUEUE"). `filter-classifier`'s own semantic-review step is conditioned on this file: empty (the normal case) means it skips that step entirely rather than reading `filtered.json` end-to-end. Do not instruct it, or any other agent in this run, to open every raw catalog or every unseen posting's full description "just to be sure" — the deterministic script already made every decision it could, and compact per-job evidence in `needs_review.json` is what any remaining semantic judgment should be based on, not full job catalogs.

Invoke `deduplicator` with `RUN_ID`. Final ordering must be priority ascending, score descending, posted date newest first, then company/title.

Invoke `aggregator-reporter` with `RUN_ID`. Verify that both `runs/<RUN_ID>/report.md` and `runs/<RUN_ID>/priority_shortlist.md` exist, that the audit log names every source configured in `config/sources.json` (not a hardcoded count), and that the report distinguishes: listing records scanned, unseen postings discovered, detail pages fetched, postings passing filters, and final deduplicated count. Priority 1 and Priority 2 roles must appear first in both files.

After reporting completes, run `python scripts/finalize_sources.py verify --run-dir runs/<RUN_ID>` one last time. If it now reports a change (a raw file was rewritten after classification already ran against it), the just-produced `filtered.json`, `deduplicated.json`, `report.md`, and `priority_shortlist.md` are stale relative to the corrected raw data: `refresh` the manifest, recommit source history, and rerun filter-classifier/deduplicator/aggregator-reporter from the corrected inputs before continuing. Never classify, report, or notify from raw data known to be stale. State clearly in the final response if this recovery occurred (which source, and that outputs were regenerated).

## 7. Detect unseen qualifying jobs and notify conditionally

Invoke `new-job-monitor` with `RUN_ID` only after the reports and final deduplicated data are complete (and confirmed non-stale per step 6).

Required outputs:

- `runs/<RUN_ID>/new_jobs.json`
- `runs/<RUN_ID>/new_jobs.md`
- `runs/<RUN_ID>/notification.json`
- `runs/<RUN_ID>/notification_delivery.json` only when desktop delivery was attempted
- persistent `state/seen_jobs.json`

The first completed run establishes the baseline unless `notify_on_first_run` is enabled in `config/notifications.json`. It must not flood the user with every existing job by default. Later alerts include all unseen qualifying roles, ordered strongest-fit first, and the report must call out how many are new Priority 1 and new Priority 2 jobs specifically. Notify only for `new_posting` records with `us_location_eligible: true` — since the location gate already runs upstream in `filter-classifier`, every record reaching `deduplicated.json` is already US-eligible, but never notify for a record missing that field.

## 8. Final response

Read `notification.json` and follow these rules:

- When `baseline_created: true`, say the baseline was established, give the number of stored matching jobs, and clearly state that no alert was sent.
- When `should_notify: false` (including the step-5 short-circuit), say no newly posted jobs were found and no alert was sent — report "No newly posted jobs detected" verbatim when that was the short-circuit reason. Keep this concise, but mention unavailable sources.
- When `should_notify: true`, begin with `🚨 NEW JOB ALERT`, give the priority breakdown (explicitly call out new Priority 1 and new Priority 2 counts), and show every new job with fit label, score, location, posting date, and direct application link. List Priority 1 and Priority 2 jobs first. Give desktop-notification delivery status and the `new_jobs.md` path.

Always provide the run summary — including listing records scanned, unseen postings discovered, Stage-A deterministic rejections (non-US location or excluded title family, before any detail fetch), detail pages fetched, and the `needs_review.json` count, alongside new qualifying jobs and new Priority 1/Priority 2 counts — unavailable sources, and local paths to `report.md`, `priority_shortlist.md`, `new_jobs.md`, `filtered.json`, `deduplicated.json`, and `needs_review.json` (when they were produced this run). These are read directly from `report.md`'s Run Summary / `classify_dedupe_report.py`'s own printed counts, not recomputed by hand. Never claim a desktop or mobile notification was delivered unless the relevant output explicitly confirms it.

Never claim success for a source without a valid source-result file. Never fabricate missing jobs or URLs. If a source returned zero unseen postings successfully, report zero rather than labeling it failed.
