# Upgrade Notes — Prioritized Career Fit

This build adds career-fit prioritization without narrowing the original job search.

## New files

- `config/fit_priorities.json` — editable fit tiers, regex signals, and point weights
- `scripts/test_fit_ranking.py` — deterministic ranking tests
- `UPGRADE_NOTES.md` — this summary

## New run output

- `runs/<RUN_ID>/priority_shortlist.md` — all Priority 1 and Priority 2 roles

## New fields on included jobs

- `fit_priority`
- `fit_label`
- `fit_score`
- `fit_keywords_matched`
- `fit_reason`

## Notification change

All unseen qualifying jobs still trigger an alert. Alerts and reports now put inference/infrastructure and GPU/systems roles first and include a priority breakdown.

## Baseline compatibility

Existing `state/seen_jobs.json` files remain compatible. The monitor enriches stored records with ranking fields on the next run without treating previously seen jobs as new.
