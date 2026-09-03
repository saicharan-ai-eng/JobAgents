# 68-Source Expansion Report

Implementation and baseline-seeding record for the 2026-08-11 source expansion. This was an onboarding/baseline-seeding task, not a production notification run — no desktop notification was sent, and no historical posting was labeled `new_posting`.

## 1. Existing source count before expansion

9 (NVIDIA, Dell, HPE, Lenovo, Red Hat, Canonical, Microsoft/Azure, AWS, Google Cloud).

## 2. New unique companies requested

**67**, not 68. The request's category lists sum to 68 lines because **LangChain is listed twice** (once under "AI Agents / Enterprise", once under "Dev Tools / Platform") — the request itself flags this ("LangChain appears twice... add it only once") but also separately states "There should be 68 unique new companies," which is inconsistent with its own list once the duplicate is removed. Deduplicated, the list is 67 unique companies. LangChain was added once, with both category labels preserved in its `category` field (`"AI Agents / Enterprise; Dev Tools / Platform"`).

## 3. Total configured sources after expansion

**76** (9 + 67), read from `config/sources.json` at completion.

## 4. Companies successfully onboarded

**All 67** — every company was live-verified against its real careers/ATS endpoint and successfully baseline-seeded. None were skipped, none are placeholders.

## 5. Companies partial

None.

## 6. Companies blocked

None.

## 7. Companies failed/unimplemented

None.

## 8. Platform / adapter breakdown

| Platform | Count | Adapter |
|---|---|---|
| Greenhouse | 29 | `scripts/adapters/greenhouse.py` (pre-existing, reused) |
| Ashby | 32 | `scripts/adapters/ashby.py` (pre-existing, reused) |
| Lever | 5 | `scripts/adapters/lever.py` (pre-existing, reused) |
| Teamtailor | 1 (Lindy) | `scripts/adapters/teamtailor.py` (**new**) |

`scripts/adapters/smartrecruiters.py` and the new `scripts/adapters/workday.py` were verified with synthetic/mocked data (see item 15) but not needed by any of the 67 companies actually researched — none of them turned out to run on SmartRecruiters or Workday. They remain available for future sources.

Two companies (Black Forest Labs, Wayve) publish their board on **both** Greenhouse and Ashby simultaneously with near-identical job sets; Greenhouse was used as the single source of record for each to avoid double-counting the same postings. Three near-collisions with unrelated companies sharing a similar slug on a different platform were identified and excluded (Together AI vs. an unrelated "together" on SmartRecruiters; Glean vs. an unrelated "glean" on SmartRecruiters; Palantir vs. an unrelated "palantir" on SmartRecruiters) — confirmed by job-title mismatch, not assumed.

## 9. Stage-A inventory counts by company

Full per-company table (keyword-matched Stage-A inventory, from `runs/20260811T221625Z-baseline-seed/raw/*.json`):

| Company (slug) | Inventory | Company (slug) | Inventory |
|---|---|---|---|
| anthropic | 201 | legora | 57 |
| nebius | 363 | together-ai | 58 |
| crusoe | 362 | fireworks-ai | 58 |
| coreweave | 152 | wayve | 59 |
| helsing | 123 | synthesia | 67 |
| glean | 109 | baseten | 69 |
| langchain | 102 | mistral-ai / celonis | 72 each |
| sierra / elevenlabs | 63 each | modal | 31 |
| fluidstack | 55–56* | intercom | 31 |
| cohere | 68 | isomorphic-labs | 23 |
| perplexity | 37 | palantir | 34 |
| deepgram | 35 | attio | 4 |
| physicsx | 38 | contentful | 5 |
| arize-ai | 29 | workos | 5 |
| decagon | 25 | pigment | 3 |
| lovable | 25 | pinecone | 6 |
| prime-intellect | 26 | planetscale | 6 |
| runpod | 21 | n8n | 6 |
| vercel / spotify | 20 each | scaleway | 9 |
| vapi | 15 | supabase | 9 |
| hellofresh | 32 | photoroom / stability-ai | 2 each |
| parloa | 16 | polyai / airtable / amplemarket / clay-labs / zapier / qonto | 1 each |
| openrouter | 17 | lindy / trade-republic | 2 each |
| sumup | 18 | black-forest-labs / bland-ai | 10 each |
| n26 | 11 | hightouch | 12 |
| scandit | 11 | speechmatics | 12 |
| getyourguide | 8 | faculty | 44 |
| fal | 30 | lakera | 0 |

\* Fluidstack: 55 at baseline-seed time; 56 ~11 minutes later at dry-run validation time — see item 20.

Sum across all 67: **2,881** Stage-A inventory records.

## 10. U.S.-eligible inventory counts by company

Computed by applying `scripts/us_location_filter.py` to each company's Stage-A `location` strings (a location-only, Stage-A-level approximation — the authoritative gate re-runs on full Stage-B detail during a real run). Totals: **1,339 of 2,881** Stage-A records evaluate as US-eligible from location text alone; the remainder are non-US or ambiguous (fail-closed). The full per-company breakdown is saved in `runs/20260811T221625Z-baseline-seed/` alongside the raw files; standouts include Nebius (116/363 US), Crusoe (352/362 US), CoreWeave (145/152 US), and several EU-headquartered companies with 0 US-eligible Stage-A records (Faculty, Fal, Bland AI, Prime Intellect, N26, SumUp, Wayve, Attio, Scaleway, Supabase) — expected, since Stage-A location strings for those boards are predominantly European.

## 11. Non-US exclusions by company

= (Stage-A inventory − US-eligible) per company in item 10; **1,542** Stage-A records total across all 67 companies. These were never given a Stage-B detail fetch when confirmably non-US (see item 15); ambiguous ones proceeded to Stage B per the fail-closed design, and none were classified, ranked, reported, or notified this run since baseline seeding never runs classification.

## 12. Baseline status for every new company

**All 67: `baseline_status: "complete"`.** Every platform used (Greenhouse, Ashby, Lever, Teamtailor) returns its entire current job set in a single unpaginated request, so each Stage-A scan is provably a full, non-partial snapshot — unlike the original nine sources, none of the 67 needed `"partial"` status. Recorded in `state/source_baseline_status.json`.

## 13. Notification mode for every new company

**All 67: `notification_mode: "full"`** — directly following from a `"complete"` baseline (see `resolve_notification_mode` in `scripts/source_history.py`). None required `"date_verified_only"` or `"disabled"`.

## 14. New shared adapters created

**One:** `scripts/adapters/teamtailor.py` (Teamtailor public `jobs.json` JSON Feed API, discovered in use by Lindy after their careers page's actual CDN fingerprint and public feed were inspected directly — an earlier AI-assisted guess that Lindy used Ashby, based only on URL shape, was verified and found wrong before being trusted). Also added: `scripts/adapters/workday.py` (generalizes the existing production `scripts/workday_fetch.py` logic behind the shared adapter interface, for future Workday-backed sources — not used by any of the 67 since none of them turned out to run on Workday) and the new generic driver `scripts/run_source.py` plus the single shared agent `.claude/agents/generic-ats-scraper.md` that all 67 new sources dispatch to instead of a bespoke agent file each.

## 15. Tests added

- `scripts/test_open_ended_experience.py` — the "5+ years" regression fix (item 16 below)
- `scripts/test_source_expansion.py` — 11 scenarios: source-config uniqueness, duplicate-LangChain prevention, adapter routing (every `platform` value resolves to a real adapter with the right interface), SmartRecruiters pagination, Workday pagination, deterministic job identity, Stage-A US exclusion (confirmed-non-US never reaches `fetch_detail`), source failure isolation, shared-adapter failure isolation (two companies on the same Greenhouse adapter, one blocked, one succeeds, no state leakage), duplicate-job-within-inventory handling, missing-posting-date handling (never fabricated), and direct-application-URL validation across every adapter
- `scripts/test_source_history.py` — two pre-existing assertions hardcoded to the old count (`== 9`, `== 10`) were de-hardcoded to read the live source count dynamically, consistent with the "never hardcode a source count" rule

## 16. All test results

Every test file passes, and `python -m compileall -f scripts` is clean:

```
test_alt_path_connector.py         PASS
test_classifier_dedup_fixes.py     PASS
test_fit_ranking.py                PASS
test_open_ended_experience.py      PASS  (new)
test_source_expansion.py           PASS  (new, 11/11 scenarios)
test_source_history.py             PASS  (2 assertions de-hardcoded)
test_us_location_filter.py         PASS
```

A genuine, previously-unfixed regression was found and fixed as part of Phase 7's explicit requirement: `classify_dedupe_report.py` was treating an open-ended floor like `"5+ years"` or `"at least 5 years"` as if it were a *bounded maximum* of 5 (`5 <= 5` passed), silently admitting roles with no actual experience ceiling. Fixed by tagging PLUS/MINIMUM regex matches as `open_ended=True` and excluding when `open_ended and years >= 5`; sub-5 open-ended floors (`"3+ years"`) are unaffected. Verified against all 9 existing regression tests plus 8 new boundary cases.

## 17. `state/seen_source_jobs.json` count before and after

- Before: **9 sources, 4,441 identities** (SHA-256 `0efe3e89bc7b5b7d78aa88e784f8920eb6d9f7dd6c252921de25b17184c5d1f9`, matching `backups/pre_68_source_expansion/PRE_EXPANSION_RECORD.json`)
- After: **76 sources, 7,322 identities** (+2,881, exactly matching the sum of all 67 companies' Stage-A inventory in item 9)

## 18. Confirmation `state/seen_jobs.json` was not incorrectly reset

**Confirmed untouched.** SHA-256 before and after this entire expansion: `8fc74a2ac55d4ad00f7694113a5c430534ffb49264f9fb60e14db7a78e98a349` — byte-identical, verified immediately before baseline seeding and again after the final dry-run validation.

## 19. Confirmation no migration notification was sent

**Confirmed.** No desktop notification script was invoked at any point. The only new-job-detection check run was a read-only dry-run validation (item 20) against a scratch **copy** of `state/seen_jobs.json`, never the real file, and it never called any notification-delivery step.

## 20. Dry-run `new_job_count` and `should_notify` result

`new_job_count: 0`, `should_notify: false`, `baseline_created: false`. Verified two ways:

1. Re-ran the real, normal (non-`--inventory-only`) two-stage fetch for all 67 new sources against the real `state/seen_source_jobs.json` (read-only diff — `source_history.py commit` was never invoked): **66 of 67 sources correctly reported 0 unseen postings.**
2. **One genuine exception, left exactly as specified for this scenario:** Fluidstack posted one brand-new role ("Principal Operations Engineer, Mechanical", US Remote) roughly 11 minutes after its baseline was seeded — a real, live, post-baseline posting, not a test artifact. Per the migration instructions ("record it for review but DO NOT notify during migration"), it was carried through Stage B and classification for review, correctly **excluded** by the existing filters (senior title + non-AI/ML domain — worked exactly as designed), and was **not committed** into `state/seen_source_jobs.json` (Fluidstack's identity count remains 55, not 56) and **not notified**. It will be picked up normally as a genuine `new_posting` candidate the next time the real `/discover-ai-jobs` workflow runs, which is the correct, intended behavior for a system now live.

Full artifacts: `runs/20260811T221625Z-baseline-seed/` (Stage-A fetch + baseline commit summaries) and `runs/20260811T222753Z-dry-run-validation/` (dry-run re-fetch, classification, and notification-check outputs).
