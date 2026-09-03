# AI/ML/NVIDIA Job Discovery — Project Rules

You are the orchestrator for a recurring, incremental job-discovery and new-job-monitoring workflow. The companies covered are whatever is configured in `config/sources.json` — every configured source, dynamically discovered; never a hardcoded count or list. `config/sources.json` started with nine enterprise AI/ML/NVIDIA-adjacent vendors and, as of the 2026-08-11 68-source-expansion request, also covers a broad set of AI-native and technology companies (AI labs/LLM, voice AI, GPU/infra cloud, AI agents/enterprise, AI security, dev tools/platform, creative AI, enterprise SaaS, fintech, and a few others). That file, not this sentence, is authoritative: adding a new entry (with its own `agent`) makes the orchestrator dispatch it automatically on the next run, with no code or skill change required. Most sources reuse the shared `generic-ats-scraper` agent and `scripts/run_source.py` driver (see "Retrieval strategy" below) rather than needing a bespoke agent file.

**As of the 2026-08-06 US-only baseline migration, the production workflow is United States-only.** A posting must pass a deterministic US-location eligibility gate — in addition to the existing early-career and AI/ML domain gates — before it can be included, ranked, deduplicated, reported, stored in `state/seen_jobs.json`, or notified. See "United States location eligibility" below.

The workflow retains broad qualifying early-career AI/ML roles located in the United States, then ranks them for this career path:

1. AI inference, model serving, and AI/ML infrastructure
2. GPU/CUDA, accelerated computing, distributed systems, and systems performance
3. General ML, LLM, MLOps, ML platform, and AI-cloud roles
4. Broad software/platform backup roles with genuine AI/ML relevance

## Non-negotiable rules

1. Never fabricate a posting, job ID, experience requirement, date, location, description, URL, source status, fit score, or notification-delivery result.
2. Every final job URL must be a direct posting URL, not a search-results or careers-home URL.
3. Respect robots.txt, terms of use, authentication, CAPTCHAs, and rate limits. Never bypass access controls.
4. If a source cannot be accessed, write a source result with `status` equal to `blocked`, `partial`, or `failed`, including a specific reason.
5. Preserve the source's original title and experience wording. Inferred classification must be separately labeled.
6. Every run performs a fresh Stage-A listing scan of every source — never silently reuse a stale inventory from a prior run. Stage-B detail fetching is deliberately skipped for postings already recorded in `state/seen_source_jobs.json`; that is the incremental workflow working as intended, not staleness.
7. Site scrapers collect broad AI/ML/GPU/NVIDIA-keyword-matched results. They do not make the final seniority or career-fit decision.
8. The filter/classifier applies both eligibility filters and deterministic career-fit ranking from `config/fit_priorities.json`.
9. Career-fit ranking changes ordering only. Never exclude an otherwise qualifying role merely because it is Priority 3 or Priority 4.
10. The deduplicator runs after classification and preserves fit fields.
11. The reporter explicitly lists every attempted source and all unavailable sources.
12. New-job detection occurs only after final deduplication and reporting.
13. Preserve `state/seen_jobs.json` (qualifying-job notification history) and `state/seen_source_jobs.json` (raw source-posting history) across runs. Never reset or delete either unless the user explicitly requests a new baseline.
14. Notify only for job identities that are absent from the persistent seen-job state. Duplicate search hits and recurring listings must not retrigger alerts. Raw source history follows the same principle one layer earlier: a posting already recorded in `state/seen_source_jobs.json` is not re-fetched or re-classified, only a genuinely new job ID or an explicitly source-flagged, newer-dated repost is.
15. The first successful run establishes a baseline and sends no alert by default. This behavior is configurable in `config/notifications.json`.
16. Local desktop notification delivery is best-effort and may be unavailable in cloud or noninteractive environments. Never claim delivery without `notification_delivery.json` confirming it.
17. Within reports and alerts, surface Priority 1 and Priority 2 jobs before broader matches.
18. A posting must have `us_location_eligible: true` before it can be included, ranked, deduplicated, reported, stored in `state/seen_jobs.json`, or notified. United States location eligibility is determined solely from the posting's structured `location` field (see "United States location eligibility" below) — never from job title, description text, salary currency, company headquarters, or the job-board domain. Location eligibility fails closed: ambiguous or unverifiable locations are excluded.

## Required source-result contract

Each site agent runs a **two-stage** fetch (Stage A lightweight inventory, Stage B unseen-only detail) and writes one JSON object to `runs/<RUN_ID>/raw/<slug>.json`:

```json
{
  "company": "NVIDIA",
  "source_url": "https://jobs.nvidia.com/careers",
  "fetched_at": "2026-08-05T12:00:00-04:00",
  "status": "success",
  "reason": null,
  "inventory_count": 492,
  "unseen_inventory_count": 1,
  "previously_processed_count": 491,
  "detail_fetch_count": 1,
  "raw_posting_count": 1,
  "inventory": [
    {
      "company": "NVIDIA",
      "job_title": "Software Engineering Intern, AI Infrastructure",
      "job_id": "JR1234567",
      "location": "Santa Clara, CA",
      "posting_date": "2026-08-01",
      "job_url": "https://...direct-posting...",
      "source_keyword": "AI Infrastructure"
    }
  ],
  "postings": [
    {
      "company": "NVIDIA",
      "job_title": "Software Engineering Intern, AI Infrastructure",
      "job_id": "JR1234567",
      "location": "Santa Clara, CA",
      "posting_date": "2026-08-01",
      "experience_level_text": "Internship",
      "job_url": "https://...direct-posting...",
      "team_department": "AI Infrastructure",
      "short_description": "...",
      "full_description_text": "...",
      "source_keyword": "AI Infrastructure",
      "processing_status": "success"
    }
  ]
}
```

`inventory` is the full Stage-A lightweight scan of everything currently listed (used only to refresh source-history freshness, never fed into classification). `postings` holds the Stage-B unseen (or explicitly source-flagged repost) postings with full detail — this is what classification/ranking/reporting consume, **except** any posting whose `processing_status` is `"excluded_non_us"` or `"excluded_title_reject"` (see below), which is present in `postings` for history-commit purposes only and is never classified. Use `null` when a field truly is unavailable. `raw_posting_count` and `unseen_inventory_count` must equal the length of `postings`. See `schemas/source-result.schema.json` for the full field list, including the optional `repost_signal` (set by the scraper), and `is_repost`/`original_first_seen_at`/`repost_detected_at` (set by `scripts/source_history.py commit`).

`processing_status` values: `"success"` and `"failed"` describe a real Stage-B detail-fetch attempt; `"blocked"` describes a source-level access failure. `"excluded_non_us"` marks an unseen identity whose Stage-A `location` was already confirmably non-US, so its Stage-B detail fetch was skipped entirely (`experience_level_text`, `team_department`, `short_description`, and `full_description_text` are all `null` by design — never fabricated). `"excluded_title_reject"` marks an unseen identity whose Stage-A `job_title` alone already unconditionally excludes it downstream — a senior/staff/principal/lead/director/manager-level title, or a marketing/sales/HR role family, using the exact same regexes `classify_dedupe_report.py` itself applies, so this is provably equivalent to (never a guess ahead of) that eventual decision; a junior/early-career signal in the title always wins first, so this never rejects a potentially-valid early-career posting, and it never decides experience-years or domain fit (those always require Stage-B text). It carries the same detail-fields-null contract as `excluded_non_us`, plus a `stage_a_exclusion_reason` field (`senior_title` / `marketing_role_title` / `sales_role_title` / `hr_role_title`). `"excluded_out_of_scope"` (added 2026-08-15) marks an unseen identity whose detail *was* fetched (there is no Stage-A shortcut for this judgment) but whose content confirmed it falls outside the source's own `required_scope` (e.g. a Google-general role that isn't actually Google Cloud-scoped) — same null-detail-fields shape, plus an optional `stage_a_exclusion_reason` explaining why. These three statuses were originally set only by `scripts/run_source.py` (see "Retrieval strategy"); as of 2026-08-15 the bespoke `lenovo-scraper` and `google-cloud-scraper` agents also write them (via `scripts/source_history.build_exclusion_stub()`) for their own confirmed-terminal Stage-A/scope exclusions, fixing a real bug where those identities were silently omitted from `postings[]` and re-surfaced as "unseen" every single run rather than ever being committed to history. All three statuses' identities are still committed to `state/seen_source_jobs.json` so they are never re-attempted, but `classify_dedupe_report.py` skips them outright before classification — they must never reach `filtered.json`, `deduplicated.json`, a report, or a notification. Never stub an identity that was merely deferred/time-budget-cut/left ambiguous — only a genuinely reached terminal disposition may be stubbed; an unstubbed, unattempted identity correctly stays retryable next run.

## Run and state directories

The orchestrator creates one UTC run ID in the form `YYYYMMDDTHHMMSSZ`, then creates:

- `runs/<RUN_ID>/raw/`
- `runs/<RUN_ID>/logs/`
- `runs/<RUN_ID>/source_manifest.json` — written by `scripts/finalize_sources.py manifest` once every configured source's raw file is in a terminal state (`success`/`partial`/`blocked`/`failed`); records each source's status, record count, SHA-256 checksum, and completion timestamp. `scripts/source_history.py commit` must not run until this manifest exists, and `finalize_sources.py verify` is re-checked before classification and again after reporting — if a raw file changed after being finalized, `finalize_sources.py refresh` recomputes just that source's entry and appends a recovery-log entry, and downstream outputs are invalidated and rerun from the corrected data rather than silently classifying stale raw data.
- `runs/<RUN_ID>/filtered.json`
- `runs/<RUN_ID>/deduplicated.json`
- `runs/<RUN_ID>/needs_review.json` — written by `scripts/classify_dedupe_report.py` alongside `filtered.json`; see "Deterministic-first processing and the review queue" below.
- `runs/<RUN_ID>/report.md`
- `runs/<RUN_ID>/priority_shortlist.md`
- `runs/<RUN_ID>/new_jobs.json`
- `runs/<RUN_ID>/new_jobs.md`
- `runs/<RUN_ID>/notification.json`
- `runs/<RUN_ID>/notification_delivery.json` when local delivery is attempted

When a run discovers zero unseen postings across every source, **or** every unseen identity was already Stage-A-rejected (confirmed non-US or an excluded title family) so zero detail pages were fetched, `filtered.json` and `needs_review.json` are not produced (the expensive classification agents are skipped entirely — see `scripts/source_history.py summarize`'s `detail_pages_fetched` counter); `deduplicated.json` is written as `[]` and `report.md`/`notification.json` are written directly by the orchestrator.

Persistent monitoring data lives outside the timestamped run folders:

- `state/seen_jobs.json` — qualifying-job notification history (unchanged by this workflow).
- `state/seen_source_jobs.json` — raw source-posting history, keyed per source slug, documented in `scripts/source_history.py`. This is the layer that makes fetching incremental: a posting already recorded here is not re-opened or reprocessed unless it is a new job ID or an explicitly source-flagged, newer-dated repost.

A qualifying-job identity (`state/seen_jobs.json`) is `(company, job_id)` when a job ID exists, otherwise `(company, normalized_title, normalized_location)`. A raw source-posting identity (`state/seen_source_jobs.json`) is `(company, job_id)` when a job ID exists, otherwise `(company, normalized_title, normalized_location, direct_url)` — the fallback additionally includes the URL since raw history has to disambiguate before any eligibility filtering has happened.

## Retrieval strategy

- Sources with `"type": "workday"` in `config/sources.json` (HPE and Red Hat): use the Workday CXS JSON API first with pagination. Use the included `scripts/workday_fetch.py` helper, which already implements the two-stage inventory/unseen-detail fetch via `scripts/source_history.py` and a hardened pagination loop (see `scripts/test_workday_fetch.py`). Do not rely on rendered HTML as the primary method.
- Sources with `"type": "standard"` (the original NVIDIA, Lenovo, Canonical, Microsoft/Azure, AWS, and Google Cloud sources): use public listing/search endpoints and keyword filters, collecting a lightweight Stage-A inventory before deciding (via `scripts/source_history.py diff-inventory`) which identities need a Stage-B detail fetch. Prefer structured JSON/API data when discoverable. NVIDIA's careers site is itself backed by a Workday CXS deployment (verified tenant/site in both `config/sources.json` and `nvidia-scraper.md`), so it also uses `scripts/workday_fetch.py` directly rather than improvised pagination code — see "2. WORKDAY PAGINATION REGRESSION" history below for why. Otherwise use WebFetch/WebSearch or the configured Playwright browser tooling.
- Sources with `"type": "generic"` and a `"platform"` field (every source added since the 68-source-expansion request, plus Dell as of 2026-08-12): dispatched to the shared `generic-ats-scraper` agent, which runs `python scripts/run_source.py --slug <slug> --run-id <RUN_ID>` — a deterministic driver that looks up the entry's `platform` (`greenhouse`, `ashby`, `lever`, `smartrecruiters`, `workday`, `teamtailor`, or `oracle_hcm`) and dispatches to the matching `scripts/adapters/<platform>.py` module. Adding a new company on an already-supported platform requires only a new `config/sources.json` entry with that platform's identifier field (`greenhouse_board_token`, `ashby_board_name`, `lever_company_slug`, `smartrecruiters_company_id`, `workday_origin`/`workday_tenant`/`workday_site`, `teamtailor_domain`, or `oracle_hcm_origin`/`oracle_hcm_site_number`/`oracle_hcm_public_origin`) — verified against the company's live career site before being added, never guessed from the company name — not a new agent file or a new script. Add a new adapter module under `scripts/adapters/` only when a company's platform is genuinely not one of the above, after verifying it via the live site (see "Source onboarding research standard" below). Dell moved to `"platform": "oracle_hcm"` (`scripts/adapters/oracle_hcm.py`) after its Workday tenant was confirmed deprecated (HTTP 200 with an always-empty result set); its live Oracle Fusion Cloud HCM Candidate Experience API was verified and reused instead. Dell's `notification_mode` remains `disabled` in `state/source_baseline_status.json` until an operator completes a baseline-seed-and-verify pass against the new adapter (see that file's `dell` entry for the exact procedure).
- Search every keyword in `config/sources.json`, including specialized terms such as vLLM, SGLang, TensorRT, Triton, NCCL, model serving, inference, CUDA, and GPU.
- Retry a failed source once with an alternate permitted strategy. Example: structured endpoint first, browser render second.
- Do not log into accounts, solve CAPTCHAs, rotate identities, evade blocks, or exceed reasonable request rates.
- Apply the US-location filter at Stage A whenever the lightweight inventory already carries reliable location metadata: a Stage-A record that is confirmed non-US should not trigger a Stage-B detail fetch. When Stage-A location metadata is missing, truncated, or ambiguous, proceed to Stage-B and re-apply the filter once full detail (which may include a fuller location string) is available — never skip Stage-B solely because Stage-A location was ambiguous. The filter is always re-applied after detail extraction regardless of the Stage-A outcome, since it is the authoritative, final gate.
- Also apply the title-only rejection at Stage A (`scripts/run_source.py`, shared by every `generic-ats-scraper`-routed source): a Stage-A `job_title` that unconditionally excludes downstream regardless of description content — senior/staff/principal/lead/director/manager-level, or a marketing/sales/HR role family — should not trigger a Stage-B detail fetch either (`processing_status: "excluded_title_reject"`). This is deliberately conservative: it never fires when the title also carries a junior/early-career signal, and it never attempts to decide experience-years or domain fit, both of which always require Stage-B text and remain the filter-classifier's job.

## Source onboarding research standard

Before adding any new `config/sources.json` entry, verify — never guess — its platform and identifier by actually hitting the candidate endpoint or inspecting the live careers page, and confirm the response is genuinely that company's (job titles/domain must be plausible; a same-named board on a different platform, or a stale/unrelated company sharing a similar slug, both happen in practice and must be ruled out by inspection, not assumed). Only add an adapter under `scripts/adapters/` for a platform actually confirmed in use by a real source. Do not use Google, LinkedIn, Indeed, or third-party aggregators as the authoritative inventory source — they may be used only to locate the company's own official careers page or ATS board, never as the data source itself.

## United States location eligibility

Implemented deterministically in `scripts/us_location_filter.py` and enforced in `scripts/classify_dedupe_report.py`. Evaluated **only** from the posting's structured `location` field.

Include only postings clearly located in:

- One or more of the 50 United States, or Washington, D.C.
- An explicit "United States" / "USA" / "U.S." / "US" marker
- "Remote - United States" or an equivalent explicit US-remote marker
- A multi-location posting with at least one explicit United States work location (other listed locations may be non-US)

Exclude:

- Jobs based only outside the United States, even for a US-headquartered employer
- Listings marked only "Remote", "Global", "Worldwide", or "Multiple Locations" with no accompanying US marker
- Ambiguous locations that cannot be verified as US-based (fail closed)
- Roles where the only United States reference is company headquarters, legal/EEO boilerplate, salary currency (e.g. "USD"), or an unrelated office address

Never infer United States eligibility from: employer headquarters, company nationality, USD compensation, the job-board domain, or equal-opportunity/legal boilerplate.

Each processed record must contain:

- `us_location_eligible`: true or false
- `us_location_reason`: auditable explanation
- `normalized_us_locations`: matched state/D.C./country labels
- `location_inferred`: true when eligibility relied on a bare state/D.C. signal rather than an explicit "United States"/"USA"/"US" marker

Regression tests: `scripts/test_us_location_filter.py`.

## Final eligibility rules

A posting is included only if it passes all three:

### United States location

See "United States location eligibility" above.

### Experience

**As of the 2026-08-14 experience-policy migration, the bounded cap is `MAX_REQUIRED_EXPERIENCE_YEARS = 3` (centralized in `scripts/classify_dedupe_report.py`), not 5.** Every comparison against the cap reads that one constant.

Include when at least one is true:

- Explicit label: Intern, Internship, Co-op, New Grad, University Grad, Early Career, Entry Level — provided the posting's own **Required**-Qualifications-scoped text (never preferred/general text, which is full of incidental non-requirement year mentions) does not *also* state a mandatory year figure that itself exceeds the cap. "Entry-level culture; 4+ years required" still excludes; a genuine internship/new-grad posting with no contradictory required figure is unaffected.
- A clearly stated required experience path (an exact figure, or a bounded range) has a maximum of 3 years or less: 0, 1, 2, 3, "0–3", "1–3", "2–3", or "up to 3 years" all qualify.
- No explicit bound exists, but the title strongly indicates junior level: Associate, Engineer I, Scientist I, Developer I, Analyst I, Intern, New Grad, Early Career. Mark `experience_inferred: true`.
- A genuine alternative-qualification path (e.g. "Bachelor's + 5 years OR Master's + 3 years") has at least one path at or under the cap — the parser takes the minimum across a structurally genuine alternative connector, the maximum across a stacked/AND requirement (see `GENUINE_ALT_PATH_BRIDGE` in `scripts/classify_dedupe_report.py`; do not infer an alternative path merely because two qualifications appear in different bullets or share an unrelated "or").

Exclude when any of these applies unless the posting is explicitly an internship/new-grad role with no contradictory required-scope figure (see above):

- Title contains Senior, Sr., Staff, Principal, Lead, Director, Manager, Head, Distinguished, Fellow, Vice President, or VP.
- Requirement is an **open-ended floor at or above the cap** — `3+ years`, `4+ years`, `5+ years`, "at least 3 years", "minimum 3 years", or "3 or more years" — regardless of higher open-ended minimums. `3+ years` is REJECTED: it is an unbounded lower bound, not a bounded three-year requirement, even though the bare number equals the cap.
- A bounded/exact figure exceeds the cap: `4 years`, `5 years`, `2–4 years`, `3–4 years`, `3–5 years`, `4–5 years`, etc.
- Experience cannot reasonably be established and the title is not clearly junior.

Preferred-only qualifications never override a passing required-scope figure (`required 2 years, preferred 5 years` remains eligible), and a preferred-only early-career phrase never rescues a mandatory required-scope figure above the cap (`3+ required; Master's preferred` remains excluded).

Regression tests: `scripts/test_open_ended_experience.py`, `scripts/test_alt_path_connector.py`, `scripts/test_experience_threshold_3yr.py`.

### Domain

Require whole-word or phrase matches for one or more of:

AI, Artificial Intelligence, Machine Learning, ML, Deep Learning, LLM, Large Language Model, Generative AI, GenAI, Computer Vision, NLP, CUDA, GPU, Data Center AI, MLOps, AI Infrastructure, Accelerated Computing, NVIDIA, Model Training, Inference, Distributed Training, AI Cloud.

Do not match `AI` as a substring inside unrelated words such as retail, detail, email, or maintain.

## Career-fit ranking

After a job passes both eligibility filters, calculate the deterministic ranking in `scripts/classify_dedupe_report.py` using `config/fit_priorities.json`.

Each included job must contain:

- `fit_priority`: integer 1–4
- `fit_label`: human-readable tier
- `fit_score`: integer 0–100
- `fit_keywords_matched`: exact matched ranking signals
- `fit_reason`: auditable explanation

The tiers are:

1. **Excellent fit — Inference & AI Infrastructure**: vLLM, SGLang, TensorRT-LLM, inference engines, model serving, distributed inference, inference optimization, serving systems, KV cache, batching, quantization, AI/ML infrastructure, or ML systems.
2. **Strong adjacent fit — GPU & Systems**: CUDA, GPU kernels, TensorRT, Triton, NCCL, accelerated computing, distributed systems/training, HPC, compiler/kernel engineering, PyTorch, or performance engineering.
3. **General AI/ML fit**: MLOps, ML platform, machine learning, deep learning, GenAI, LLM, AI cloud, model training, computer vision, NLP, Kubernetes, or Docker.
4. **Low-priority backup**: passes the broad eligibility rules but contains only generic AI/ML/NVIDIA relevance without stronger specialization signals.

Use the config as the source of truth for exact regexes and point values. Do not invent fit scores manually. Agent/human review may correct a parser error only when preserved source text proves the correction, and only for a job actually listed in `needs_review.json` (see below) — never a wholesale re-review of `filtered.json`.

## Deterministic-first processing and the review queue

Normal operation must not require Claude to reason over every catalog, job description, or already-seen posting. The pipeline is deterministic-first end to end:

```
Stage A (deterministic adapters) → source-history diff → US-eligibility check (Stage A where sufficient)
  → unseen/unresolved only → Stage-A title rejection (senior/marketing/sales/HR)
  → Stage B (detail fetch, survivors only) → deterministic classifier/ranker → deterministic dedup/report
  → Claude review ONLY for entries in needs_review.json (usually empty)
```

`scripts/classify_dedupe_report.py` writes `runs/<RUN_ID>/needs_review.json` alongside `filtered.json` — a compact list (identity, title, URL, `reason_codes`, `evidence`; never full description text) of only the records the deterministic parser could not confidently resolve on its own. This is strictly additive: every job still gets a deterministic `include`/`level_classification` decision and flows through `filtered.json`/`deduplicated.json`/the report regardless of whether it is also flagged here — `needs_review.json` never blocks or gates the run, it only marks candidates for optional follow-up. Reason codes are a controlled vocabulary (`malformed_source_data`, `ambiguous_experience_path`, `conflicting_required_qualifications`; `ambiguous_us_eligibility` and `ambiguous_domain_relevance` are reserved but not yet triggered, since both of those gates are deliberately fail-closed with no ambiguity carve-out).

`filter-classifier` reads `needs_review.json`, not `filtered.json` wholesale, to decide whether any semantic review is needed. When it is `[]` — the normal case — no per-job Claude reasoning happens at all for this run's classification step. When it has entries, only those specific jobs' preserved text is read, never the full catalog. Do not pass entire raw source files, full Stage-A inventories, or every unseen posting's description into any agent; pass only the compact `needs_review.json` records that actually need a second look, and batch them into one review pass rather than one agent invocation per job. A source/run with zero actionable unseen records (see "Run and state directories" above) skips this entire stage, including the review queue.

## Reporting and notifications

The main Markdown report columns are:

`Priority Fit | Score | Company | Job Title | Level | Experience Required | Location | AI/ML Keywords | Fit Signals | Posted Date | Direct Apply Link`

Sort by:

1. `fit_priority` ascending
2. `fit_score` descending
3. posted date newest first
4. company and title

Also create `priority_shortlist.md` containing all Priority 1 and Priority 2 roles. Append a run summary, career-fit breakdown, and `Sources Unavailable This Run` section to the full report. The run summary must distinguish listing records scanned, previously-processed (already-seen, skipped), unseen postings discovered, Stage-A deterministic rejections (non-US location or excluded title family — no detail fetch), full detail pages fetched (all from `scripts/source_history.py summarize` plus `classify_dedupe_report.py`'s own count), and the `needs_review.json` count, from the final new-qualifying-job and new-Priority-1/Priority-2 counts (from `notification.json`) — these are different stages and must not be collapsed into one number. `scripts/classify_dedupe_report.py` already writes all of this into `report.md` in one deterministic pass; `aggregator-reporter` verifies it rather than recomputing it from raw source files.

The new-job monitor must:

1. Compare the final deduplicated list (this run's unseen, qualifying postings only) with `state/seen_jobs.json`.
2. Create the state file on the first run and treat current jobs as the baseline unless configured otherwise.
3. Write only unseen jobs to `new_jobs.json` and `new_jobs.md`.
4. Preserve ranking fields in monitoring state and alert output.
5. Set `notification.json.should_notify` to true when one or more unseen, US-location-eligible qualifying jobs exist, regardless of tier. A job with `us_location_eligible: false` must never reach the deduplicated list in the first place (the location gate runs upstream, in the filter-classifier), so the monitor never needs to re-check location itself — but it must never notify for a record that lacks `us_location_eligible: true`.
6. Put Priority 1 and Priority 2 new jobs first and include a priority breakdown.
7. Attempt a local desktop alert only when `should_notify` is true and desktop alerts are enabled.
8. Surface direct application links in every alert response.
