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

`inventory` is the full Stage-A lightweight scan of everything currently listed (used only to refresh source-history freshness, never fed into classification). `postings` holds the Stage-B unseen (or explicitly source-flagged repost) postings with full detail — this is what classification/ranking/reporting consume, **except** any posting whose `processing_status` is `"excluded_non_us"` (see below), which is present in `postings` for history-commit purposes only and is never classified. Use `null` when a field truly is unavailable. `raw_posting_count` and `unseen_inventory_count` must equal the length of `postings`. See `schemas/source-result.schema.json` for the full field list, including the optional `repost_signal` (set by the scraper), and `is_repost`/`original_first_seen_at`/`repost_detected_at` (set by `scripts/source_history.py commit`).

`processing_status` values: `"success"` and `"failed"` describe a real Stage-B detail-fetch attempt; `"blocked"` describes a source-level access failure. `"excluded_non_us"` (set only by `scripts/run_source.py`, the generic config-driven driver — see "Retrieval strategy") marks an unseen identity whose Stage-A `location` was already confirmably non-US, so its Stage-B detail fetch was skipped entirely (`experience_level_text`, `team_department`, `short_description`, and `full_description_text` are all `null` by design — never fabricated). Its identity is still committed to `state/seen_source_jobs.json` so it is never re-attempted, but `classify_dedupe_report.py` skips it outright before classification — it must never reach `filtered.json`, `deduplicated.json`, a report, or a notification.

## Run and state directories

The orchestrator creates one UTC run ID in the form `YYYYMMDDTHHMMSSZ`, then creates:

- `runs/<RUN_ID>/raw/`
- `runs/<RUN_ID>/logs/`
- `runs/<RUN_ID>/filtered.json`
- `runs/<RUN_ID>/deduplicated.json`
- `runs/<RUN_ID>/report.md`
- `runs/<RUN_ID>/priority_shortlist.md`
- `runs/<RUN_ID>/new_jobs.json`
- `runs/<RUN_ID>/new_jobs.md`
- `runs/<RUN_ID>/notification.json`
- `runs/<RUN_ID>/notification_delivery.json` when local delivery is attempted

When a run discovers zero unseen postings across every source, `filtered.json` is not produced (the expensive classification agents are skipped entirely); `deduplicated.json` is written as `[]` and `report.md`/`notification.json` are written directly by the orchestrator.

Persistent monitoring data lives outside the timestamped run folders:

- `state/seen_jobs.json` — qualifying-job notification history (unchanged by this workflow).
- `state/seen_source_jobs.json` — raw source-posting history, keyed per source slug, documented in `scripts/source_history.py`. This is the layer that makes fetching incremental: a posting already recorded here is not re-opened or reprocessed unless it is a new job ID or an explicitly source-flagged, newer-dated repost.

A qualifying-job identity (`state/seen_jobs.json`) is `(company, job_id)` when a job ID exists, otherwise `(company, normalized_title, normalized_location)`. A raw source-posting identity (`state/seen_source_jobs.json`) is `(company, job_id)` when a job ID exists, otherwise `(company, normalized_title, normalized_location, direct_url)` — the fallback additionally includes the URL since raw history has to disambiguate before any eligibility filtering has happened.

## Retrieval strategy

- Sources with `"type": "workday"` in `config/sources.json` (the original Dell, HPE, and Red Hat sources): use the Workday CXS JSON API first with pagination. Use the included `scripts/workday_fetch.py` helper, which already implements the two-stage inventory/unseen-detail fetch via `scripts/source_history.py`. Do not rely on rendered HTML as the primary method.
- Sources with `"type": "standard"` (the original NVIDIA, Lenovo, Canonical, Microsoft/Azure, AWS, and Google Cloud sources): use public listing/search endpoints and keyword filters, collecting a lightweight Stage-A inventory before deciding (via `scripts/source_history.py diff-inventory`) which identities need a Stage-B detail fetch. Prefer structured JSON/API data when discoverable. Otherwise use WebFetch/WebSearch or the configured Playwright browser tooling.
- Sources with `"type": "generic"` and a `"platform"` field (every source added since the 68-source-expansion request): dispatched to the shared `generic-ats-scraper` agent, which runs `python scripts/run_source.py --slug <slug> --run-id <RUN_ID>` — a deterministic driver that looks up the entry's `platform` (`greenhouse`, `ashby`, `lever`, `smartrecruiters`, `workday`, or `teamtailor`) and dispatches to the matching `scripts/adapters/<platform>.py` module. Adding a new company on an already-supported platform requires only a new `config/sources.json` entry with that platform's identifier field (`greenhouse_board_token`, `ashby_board_name`, `lever_company_slug`, `smartrecruiters_company_id`, `workday_origin`/`workday_tenant`/`workday_site`, or `teamtailor_domain`) — verified against the company's live career site before being added, never guessed from the company name — not a new agent file or a new script. Add a new adapter module under `scripts/adapters/` only when a company's platform is genuinely not one of the above, after verifying it via the live site (see "Source onboarding research standard" below).
- Search every keyword in `config/sources.json`, including specialized terms such as vLLM, SGLang, TensorRT, Triton, NCCL, model serving, inference, CUDA, and GPU.
- Retry a failed source once with an alternate permitted strategy. Example: structured endpoint first, browser render second.
- Do not log into accounts, solve CAPTCHAs, rotate identities, evade blocks, or exceed reasonable request rates.
- Apply the US-location filter at Stage A whenever the lightweight inventory already carries reliable location metadata: a Stage-A record that is confirmed non-US should not trigger a Stage-B detail fetch. When Stage-A location metadata is missing, truncated, or ambiguous, proceed to Stage-B and re-apply the filter once full detail (which may include a fuller location string) is available — never skip Stage-B solely because Stage-A location was ambiguous. The filter is always re-applied after detail extraction regardless of the Stage-A outcome, since it is the authoritative, final gate.

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

Include when at least one is true:

- Explicit label: Intern, Internship, Co-op, New Grad, University Grad, Early Career, Entry Level.
- A clearly stated required experience range has a maximum of 5 years or less.
- No explicit bound exists, but the title strongly indicates junior level: Associate, Engineer I, Scientist I, Developer I, Analyst I, Intern, New Grad, Early Career. Mark `experience_inferred: true`.

Exclude when any of these applies unless the posting is explicitly an internship/new-grad role:

- Title contains Senior, Sr., Staff, Principal, Lead, Director, Manager, Head, Distinguished, Fellow, Vice President, or VP.
- Requirement is `5+ years` or any higher open-ended minimum.
- Minimum required experience is greater than 5 years.
- Experience cannot reasonably be established and the title is not clearly junior.

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

Use the config as the source of truth for exact regexes and point values. Do not invent fit scores manually. Human review may correct a parser error only when preserved source text proves the correction.

## Reporting and notifications

The main Markdown report columns are:

`Priority Fit | Score | Company | Job Title | Level | Experience Required | Location | AI/ML Keywords | Fit Signals | Posted Date | Direct Apply Link`

Sort by:

1. `fit_priority` ascending
2. `fit_score` descending
3. posted date newest first
4. company and title

Also create `priority_shortlist.md` containing all Priority 1 and Priority 2 roles. Append a run summary, career-fit breakdown, and `Sources Unavailable This Run` section to the full report. The run summary must distinguish listing records scanned, unseen postings discovered, and full detail pages fetched (from `scripts/source_history.py summarize`) from the final new-qualifying-job and new-Priority-1/Priority-2 counts (from `notification.json`) — these are different stages and must not be collapsed into one number.

The new-job monitor must:

1. Compare the final deduplicated list (this run's unseen, qualifying postings only) with `state/seen_jobs.json`.
2. Create the state file on the first run and treat current jobs as the baseline unless configured otherwise.
3. Write only unseen jobs to `new_jobs.json` and `new_jobs.md`.
4. Preserve ranking fields in monitoring state and alert output.
5. Set `notification.json.should_notify` to true when one or more unseen, US-location-eligible qualifying jobs exist, regardless of tier. A job with `us_location_eligible: false` must never reach the deduplicated list in the first place (the location gate runs upstream, in the filter-classifier), so the monitor never needs to re-check location itself — but it must never notify for a record that lacks `us_location_eligible: true`.
6. Put Priority 1 and Priority 2 new jobs first and include a priority breakdown.
7. Attempt a local desktop alert only when `should_notify` is true and desktop alerts are enabled.
8. Surface direct application links in every alert response.
