

````markdown
# Multi-Agent AI/ML Job Discovery Monitor

A Claude Code workflow that searches company career sites for newly posted early-career roles in AI, machine learning, inference, GPU computing, CUDA, MLOps, and NVIDIA-adjacent technologies.

It focuses mainly on internships, new-grad roles, entry-level positions, and jobs requiring approximately 0–5 years of experience.

## Monitored Companies

The current sources are:

- NVIDIA
- Dell
- HPE
- Lenovo
- Red Hat
- Canonical
- Microsoft / Azure
- AWS
- Google Cloud

Sources are loaded dynamically from `config/sources.json`.

## How It Works

The project uses separate scraper agents for each company, followed by agents for filtering, ranking, deduplication, reporting, and notifications.

Each run follows an incremental process:

1. Scan lightweight job-listing metadata.
2. Compare job identities against `state/seen_source_jobs.json`.
3. Fetch full details only for unseen or unresolved postings.
4. Filter for early-career AI/ML/GPU relevance.
5. Rank qualifying roles by career fit.
6. Compare results against `state/seen_jobs.json`.
7. Notify only when a genuinely new qualifying job is found.

Previously processed jobs are not repeatedly opened or classified.

## Job Priorities

- **Priority 1:** AI inference, model serving, LLM infrastructure, vLLM, SGLang, TensorRT-LLM, distributed inference
- **Priority 2:** CUDA, GPU systems, Triton, NCCL, TensorRT, ML compilers, accelerator runtimes, HPC
- **Priority 3:** General AI/ML, GenAI, MLOps, computer vision, NLP, applied science
- **Priority 4:** Lower-priority adjacent or backup roles

The classifier excludes senior roles, sales, marketing, unrelated infrastructure jobs, and postings where AI appears only in company boilerplate.

## Notifications

Only postings marked as:

```text
discovery_type: new_posting
````

can trigger a notification.

Historical baseline jobs, catch-up records, duplicate postings, and previously processed jobs do not alert again.

The workflow runs daily at:

```text
10:00 AM America/New_York
```

Because it uses local files, Python, Playwright, and Claude Desktop, the laptop must be awake and connected to the internet.

## Reports

Each run creates a timestamped folder under:

```text
runs/<RUN_ID>/
```

Important files include:

* `report.md` — qualifying roles from the run
* `priority_shortlist.md` — Priority 1 and 2 roles
* `new_jobs.md` — genuinely new qualifying jobs
* `notification.json` — notification decision
* `notification_delivery.json` — delivery status

The corrected initial baseline is stored in:

```text
runs/20260805T183643Z-reclassified/
```

## Safety

The workflow does not fabricate jobs, bypass CAPTCHAs, bypass authentication, ignore rate limits, or silently treat failed sources as empty.

Blocked or unavailable sources are reported clearly.

