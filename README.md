# Claude Multi-Agent AI/ML/NVIDIA Job Discovery, Ranking, and Alerts

This is a runnable **Claude Code project inside the Claude Desktop app**. It searches every employer career site configured in `config/sources.json` (117 sources as of this writing, and growing — the file is authoritative and never a fixed list) incrementally: every run rescans lightweight listing metadata but only opens and classifies postings it hasn't processed before. It filters internships and 0–3-year AI/ML/GPU roles located in the United States, ranks them for an AI inference and GPU-infrastructure career path, remembers previously seen postings, and alerts only when a qualifying job is newly discovered.

Sources span the original AI/ML/NVIDIA-adjacent enterprise vendors (NVIDIA, Dell, HPE, Lenovo, Red Hat, Canonical, Microsoft/Azure, AWS, Google Cloud), a broad set of AI-native tech companies (AI labs/LLM, voice AI, GPU/infra cloud, AI agents/enterprise, AI security, dev tools/platform, creative AI, enterprise SaaS, fintech), and NVIDIA/Dell/Lenovo/Red Hat/Canonical channel-partner and hardware-OEM career sites (VARs, systems integrators, and server/storage OEMs). `config/sources.json` is the definitive, current list — check it rather than this file for an exact count.

**The production workflow is United States-only** (as of the 2026-08-06 US-only baseline migration). A posting must pass a deterministic US-location eligibility gate, in addition to the existing experience and AI/ML domain gates, before it can be included, ranked, deduplicated, reported, stored in `state/seen_jobs.json`, or notified. See "US-only location eligibility" below.

## What is included

- Every source configured in `config/sources.json` is dispatched automatically — adding an entry there is picked up on the next run, no code change required
- A handful of bespoke scraper subagents for sources that need one (the original enterprise vendors, e.g. `nvidia-scraper`, `hpe-scraper`), plus one shared `generic-ats-scraper` subagent reused across every source that runs on a supported ATS platform via the deterministic `scripts/run_source.py` driver and `scripts/adapters/` — a new company on an already-supported platform needs only a `config/sources.json` entry, not a new agent file. Supported platforms: Greenhouse, Ashby, Lever, SmartRecruiters, Workday, Teamtailor, Oracle Fusion Cloud HCM, Breezy HR, Rippling ATS, ADP Workforce Now, Pinpoint, Trakstar Hire, UltiPro/UKG, AgileATS, SAP SuccessFactors Career Site Builder, HR Department (Monster), JazzHR/ApplyToJob, iCIMS (classic and modern/Jibe), Jobvite, Comeet, and Eightfold AI PCSX
- 1 filter/classification and career-ranking subagent
- 1 deduplication subagent
- 1 prioritized aggregator/reporter subagent
- 1 new-job monitor/notification subagent
- A `/discover-ai-jobs` orchestration skill
- Workday CXS support (used by Dell, HPE, Red Hat, and available to any newly configured Workday-backed source)
- Browser support for JavaScript-heavy career sites through Playwright MCP, for the handful of sources that need it
- An incremental, two-stage fetch per source: a lightweight inventory scan (Stage A) followed by detail fetching only for genuinely unseen or explicitly reposted identities (Stage B) — see "Incremental workflow" below
- A Stage-A United States pre-filter: a posting whose listing-level location is already confirmably non-US skips its Stage-B detail fetch entirely (recorded `processing_status: "excluded_non_us"`, never classified/ranked/reported/notified) — see "US-only location eligibility" below
- Deterministic early-career, AI/ML, and United States-location eligibility filtering
- Transparent 1–4 career-fit ranking with a 0–100 score
- Persistent raw source-posting history in `state/seen_source_jobs.json` (what's been fetched) and qualifying-job notification history in `state/seen_jobs.json` (what's been alerted on) — two distinct layers
- `priority_shortlist.md`, `new_jobs.md`, `notification.json`, and best-effort Windows desktop alerts
- Explicit source failure and audit logging, isolated per source — one blocked or failed source never affects any other, even ones sharing the same platform adapter

## Incremental workflow

Every run performs a fresh Stage-A scan of each source's live listings (job ID, title, location, posting date, direct URL only — no detail pages opened). That inventory is diffed against `state/seen_source_jobs.json`: identities already recorded there are skipped, and only genuinely new job IDs (or a same-ID posting the source explicitly flags as reposted with a newer date) get a Stage-B detail fetch. Classification, ranking, deduplication, and reporting then run only on that run's unseen postings.

When a run's Stage-A/B scan turns up zero unseen postings across every source, the expensive classification/ranking/reporting subagents are skipped entirely — the run writes a minimal report stating "No newly posted jobs detected" and does not send a desktop notification. Raw source history is never erased when a source fails or a posting disappears from a listing; it is only ever added to.

## US-only location eligibility

Every candidate posting is evaluated by `scripts/us_location_filter.py`, using **only** the posting's structured `location` field — never job title, description, salary text, company headquarters, or the job-board domain. A posting must resolve to `us_location_eligible: true` before it can be included, ranked, deduplicated, reported, stored in `state/seen_jobs.json`, or notified. Location eligibility **fails closed**: anything that cannot be positively matched to a US signal is excluded.

Included:

- One or more of the 50 US states, or Washington, D.C.
- An explicit "United States" / "USA" / "U.S." / "US" marker
- "Remote - United States" or an equivalent explicit US-remote marker
- A multi-location posting with at least one explicit US location (other listed locations may be non-US)

Excluded:

- Non-US locations, even for a US-headquartered employer
- Bare "Remote", "Global", "Worldwide", or "Multiple Locations" with no accompanying US marker
- Any location that is ambiguous or cannot be verified as US-based
- A posting whose only US reference is company headquarters, legal/EEO boilerplate, salary currency (e.g. "USD"), or an unrelated office address

Each processed record carries four fields: `us_location_eligible`, `us_location_reason`, `normalized_us_locations`, and `location_inferred`. Regression tests live in `scripts/test_us_location_filter.py`.

A confirmably non-US posting is additionally short-circuited at Stage A itself, before its full detail is ever fetched: `scripts/run_source.py` records it with `processing_status: "excluded_non_us"` (identity still committed to `state/seen_source_jobs.json` so it's never re-attempted) and `classify_dedupe_report.py` skips it outright — it never reaches `filtered.json`, `deduplicated.json`, a report, or a notification. An ambiguous or missing Stage-A location is never treated as non-US this way; it proceeds to Stage B so the fail-closed authoritative gate above can re-evaluate the fuller detail-page location.

## Career-fit priorities

Every role must first pass the same broad early-career, AI/ML relevance, and US-location eligibility rules. Ranking happens afterward and never removes a qualifying role.

1. **Excellent fit — Inference & AI Infrastructure**  
   vLLM, SGLang, TensorRT-LLM, inference engines, model serving, distributed inference, inference optimization, KV cache, batching, quantization, AI/ML infrastructure, and ML systems.

2. **Strong adjacent fit — GPU & Systems**  
   CUDA, GPU kernels, TensorRT, Triton, NCCL, accelerated computing, distributed systems/training, performance engineering, HPC, compilers, and PyTorch.

3. **General AI/ML fit**  
   MLOps, ML platforms, general ML/deep learning, GenAI, LLMs, AI cloud, model training, computer vision, NLP, Kubernetes, and Docker.

4. **Low-priority backup**  
   Qualifying early-career software, cloud, backend, or platform roles that have genuine AI/ML relevance but no stronger specialization signal.

Exact signals, regexes, weights, and tier mappings live in:

```text
config/fit_priorities.json
```

Each included record receives:

```text
fit_priority
fit_label
fit_score
fit_keywords_matched
fit_reason
```

The full report is ordered by priority, score, posting date, company, and title. All Priority 1 and Priority 2 jobs are also copied into `priority_shortlist.md`.

## Alert behavior

The first completed run creates a baseline from currently open matching jobs. It intentionally sends no alert, preventing a flood of notifications for roles that existed before monitoring began.

Every later run compares its final ranked and deduplicated jobs against the baseline and all prior runs:

- New identity: added to `new_jobs.json` and triggers an alert.
- Previously seen identity: remains in the full report but does not retrigger an alert.
- Identity rule: `(company, job_id)`, or `(company, normalized title, location)` when no ID exists.
- All fit tiers can trigger a new-job notification, but higher-priority roles appear first.

Change `notify_on_first_run` in `config/notifications.json` only when you deliberately want the first run to alert for every current match.

## Recommended environment

- Claude Desktop with Claude Code access
- A paid Claude plan or Anthropic API billing
- Python 3.10 or newer
- Node.js LTS and `npx` for Playwright MCP

## Windows setup

1. Extract the project somewhere easy to find, such as:

   ```text
   C:\Users\<you>\Documents\claude-ai-job-agent
   ```

2. Open PowerShell in the extracted project folder.

3. Create and activate a virtual environment:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

4. Confirm Node and `npx` are available:

   ```powershell
   node --version
   npx --version
   ```

   If they are missing, install the current Node.js LTS release. The included `.mcp.json` uses `npx` to launch Playwright MCP when Claude needs browser rendering.

5. In Claude Desktop, open **Claude Code**, then open this project folder. Accept workspace trust only after reviewing the project files.

6. Paste the contents of `BOOTSTRAP_PROMPT.txt` into Claude Code for a setup-only audit.

7. After the audit passes, start a live run by typing:

   ```text
   /discover-ai-jobs
   ```

8. Approve reasonable network and script permissions when prompted. Never approve instructions that attempt to bypass CAPTCHAs, authentication, rate limits, or other controls.

## Output

Every run gets a UTC folder such as:

```text
runs/20260805T160500Z/
├── raw/
│   ├── nvidia.json
│   ├── dell.json
│   └── ...
├── filtered.json
├── deduplicated.json
├── report.md
├── priority_shortlist.md
├── new_jobs.json
├── new_jobs.md
├── notification.json
└── notification_delivery.json   # only when desktop delivery was attempted
```

Persistent history is stored separately, in two layers:

```text
state/seen_source_jobs.json   # raw source-posting history: what's been fetched, per source
state/seen_jobs.json          # qualifying-job notification history: what's been alerted on
```

Do not delete either file unless you want to establish a completely new baseline.

## Notification channels

### Local Claude Code run

When new jobs exist, the project attempts a Windows desktop notification using PowerShell and writes the delivery result to `notification_delivery.json`. This requires an interactive Windows desktop session. It will not display locally when the run occurs entirely in a cloud environment.

### Scheduled Claude/Cowork run

After one successful manual baseline run, use `SCHEDULE_PROMPT.txt` as the task instruction when creating a recurring Claude task. Keep Claude notifications enabled on desktop/mobile. The project performs deterministic new-job comparison and reports priority counts when unseen jobs exist.

A platform may still show a generic task-completion notification when no new jobs exist. The project itself will not label that completion as a new-job alert.

## Architecture

- The **main Claude Code session** is the orchestrator.
- Files under `.claude/agents/` define: a bespoke subagent for each of the original enterprise-vendor sources, one shared `generic-ats-scraper` subagent reused across every config-driven ATS source, and 4 fixed pipeline subagents (filter-classifier, deduplicator, aggregator-reporter, new-job-monitor). The count of *sources* grows automatically as entries are added to `config/sources.json`; the count of *agent files* barely grows at all, since most new sources reuse the generic one.
- `scripts/run_source.py` is the shared, deterministic Stage-A/Stage-B driver behind `generic-ats-scraper`: it looks up a source's entry by slug, dispatches to the matching `scripts/adapters/<platform>.py` module, applies the Stage-A US pre-filter and title-only rejection, and diffs against `state/seen_source_jobs.json`. Company-specific values (board tokens, tenant IDs, company slugs, custom domains) live entirely in `config/sources.json`, never hardcoded in a script.
- `.claude/skills/discover-ai-jobs/SKILL.md` defines the slash command and reads `config/sources.json` to decide which agents to dispatch; it does not hardcode a source count, and dispatches sources in bounded batches rather than all at once.
- Configured source workers run with bounded concurrency, each performing its own two-stage inventory/unseen-detail fetch; one source's failure (or a whole platform adapter's outage) never blocks or corrupts any other source's independent result.
- `scripts/source_history.py commit` records every encountered posting into `state/seen_source_jobs.json` before classification begins.
- When zero unseen postings are found, filtering/ranking/deduplication/reporting are skipped entirely for that run.
- Otherwise, filtering/ranking, deduplication, and reporting run sequentially on that run's unseen postings only.
- The new-job monitor runs last and owns historical comparison (against `state/seen_jobs.json`) and conditional alert delivery.

## Test commands

Run the deterministic ranking tests:

```powershell
python scripts/test_fit_ranking.py
```

Run the deterministic incremental-workflow tests:

```powershell
python scripts/test_source_history.py
```

Run the deterministic US-only location eligibility tests:

```powershell
python scripts/test_us_location_filter.py
```

Run the open-ended-experience regression test (e.g. "5+ years" must never be treated as a bounded maximum of 5):

```powershell
python scripts/test_open_ended_experience.py
```

Run the shared-adapter / config-driven-source expansion tests (config integrity, adapter routing and pagination, Stage-A US exclusion, source and shared-adapter failure isolation, direct-URL validation):

```powershell
python scripts/test_source_expansion.py
```

Run syntax checks:

```powershell
python -m compileall scripts
```

Useful Claude Code prompts:

```text
List the installed project subagents and explain the execution order without running them.
```

```text
Run only the Dell scraper with a temporary run ID, validate its output, and stop before classification.
```

```text
Audit the latest report for non-direct application links, missing source statuses, or unsupported fit scores.
```

```text
Read the latest notification.json and explain whether a new-job alert should have been sent, including the fit-tier breakdown.
```

## Important limitation

Career sites change their APIs and page structures. The Workday helper is deterministic, but non-Workday workers may require browser rendering or future endpoint/selector updates. The workflow is fail-closed: unavailable sources are reported instead of replaced with guessed data.
