#!/usr/bin/env python3
"""Regression tests for the 2026-08-15 bespoke-agent terminal-exclusion-stub
fix (Lenovo, Google Cloud).

Root cause (found via the read-only forensic audit of run 20260814T150341Z):
`scripts/run_source.py` (the shared generic-ATS driver) writes an explicit
stub record -- `processing_status: "excluded_non_us"` / `"excluded_title_
reject"` -- into `postings[]` for any unseen identity it confirms should be
excluded without a full Stage-B fetch, specifically so `commit_processed()`
can still record that identity into `state/seen_source_jobs.json`. The
bespoke `lenovo-scraper` and `google-cloud-scraper` agents never adopted
this convention: they simply omitted a confirmed-excluded identity from
`postings[]` entirely. Since `commit_processed()` only ever sees what's in
`postings[]`, those identities were never recorded as "seen" -- so
`diff_inventory()` returned them as "unseen" again on every subsequent run,
forever (or until delisted), even though the actual eligibility decision was
already, correctly, definitively known. Confirmed via commit logs: Lenovo's
`newly_added` was 5 while its true unseen count was 157 (152 never
committed); Google Cloud's `newly_added` was 3 against a true unseen of 37.

The fix: `scripts/source_history.build_exclusion_stub()` is a new, shared,
schema-compliant stub-builder (mirroring run_source.py's inline convention
exactly) that any source worker can call for a genuinely TERMINAL Stage-A
(or scope-inspection) disposition -- see `TERMINAL_EXCLUSION_STATUSES`. The
Lenovo and Google Cloud agent instructions (`.claude/agents/*.md`) were
updated to call it for their own confirmed-non-US (and, for Google Cloud,
confirmed-out-of-Cloud-scope) identities. Critically, an identity that was
merely deferred/time-budget-cut/left ambiguous must NEVER be stubbed --
doing so would incorrectly mark it "seen" and make it permanently
unretriable, which is worse than the bug being fixed. This file proves the
underlying mechanism is correct; it cannot unit-test the agents' own future
live-scraping behavior, only the deterministic contract they now rely on.

All fixtures use in-memory / tempfile-based history and run directories;
nothing here touches state/seen_source_jobs.json, state/seen_jobs.json, or
any real runs/ directory.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


source_history = _load("stub_fix_source_history", "source_history.py")
classifier = _load("stub_fix_classifier", "classify_dedupe_report.py")


def stage_a_item(job_id: str, title: str, location: str, url: str, company: str = "Lenovo", posting_date: str = "2026-08-14") -> dict:
    """A bare Stage-A inventory record -- exactly what a scraper has before
    ever considering a detail fetch. No detail-only fields present at all."""
    return {
        "company": company,
        "job_title": title,
        "job_id": job_id,
        "location": location,
        "posting_date": posting_date,
        "job_url": url,
        "source_keyword": "GPU",
    }


def main() -> int:
    # === 1. Lenovo Stage-A-confirmed non-US identities produce terminal
    # stub records. ===
    item = stage_a_item("WD00999001", "Field Sales Representative", "India, Karnataka, Bengaluru", "https://jobs.lenovo.com/en_US/careers/JobDetail/x/1")
    stub = source_history.build_exclusion_stub(item, "excluded_non_us")
    assert stub["processing_status"] == "excluded_non_us", stub
    assert stub["job_id"] == "WD00999001" and stub["company"] == "Lenovo" and stub["location"] == "India, Karnataka, Bengaluru", stub
    assert stub["job_url"] == item["job_url"] and stub["posting_date"] == item["posting_date"], stub
    for field in ("experience_level_text", "team_department", "short_description", "full_description_text"):
        assert stub[field] is None, f"{field} must be null in a Stage-A exclusion stub (no detail was fetched), got {stub[field]!r}"
    print("OK: test_1_lenovo_non_us_stub_shape")

    # Invalid processing_status must be rejected outright -- this helper must
    # never be usable to stub a non-terminal disposition by accident.
    try:
        source_history.build_exclusion_stub(item, "success")
        raise AssertionError("build_exclusion_stub must reject a non-terminal processing_status")
    except ValueError:
        pass
    print("OK: test_1b_non_terminal_status_rejected")

    # === 2. Those stubs are accepted by the schema/validator. ===
    with tempfile.TemporaryDirectory() as tmp:
        raw_dir = Path(tmp) / "raw"
        raw_dir.mkdir()
        raw_path = raw_dir / "lenovo.json"
        source_result = {
            "company": "Lenovo",
            "source_url": "https://jobs.lenovo.com/en_US/careers",
            "fetched_at": "2026-08-15T00:00:00+00:00",
            "status": "success",
            "reason": None,
            "inventory_count": 1,
            "unseen_inventory_count": 1,
            "previously_processed_count": 0,
            "detail_fetch_count": 0,
            "raw_posting_count": 1,
            "inventory": [item],
            "postings": [stub],
        }
        raw_path.write_text(json.dumps(source_result, indent=2), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "validate_source_result.py"), str(raw_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"validate_source_result.py must accept an excluded_non_us stub: {proc.stdout}\n{proc.stderr}"
        assert "VALID" in proc.stdout, proc.stdout
    print("OK: test_2_stub_passes_schema_validator")

    # === 3 & 4. commit_processed() records the stub in source history, and
    # the NEXT run's diff_inventory() no longer returns it as unseen. ===
    history = source_history.load_history(Path(tmp) / "does-not-exist.json")  # fresh empty history
    result = source_history.commit_processed(history, "lenovo", [stub], "2026-08-15T00:00:00+00:00")
    identity = source_history.stable_identity(stub)
    assert identity in history["sources"]["lenovo"], "commit_processed() must record the exclusion stub's identity"
    assert history["sources"]["lenovo"][identity]["processing_status"] == "excluded_non_us"
    assert result["newly_added"] == [identity], result
    print("OK: test_3_commit_processed_records_stub")

    unseen, previously_processed = source_history.diff_inventory(history, "lenovo", [item])
    assert unseen == [], f"the confirmed-non-US identity must NOT resurface as unseen on the next run: {unseen}"
    assert previously_processed == [item], previously_processed
    print("OK: test_4_next_run_diff_inventory_does_not_resurface")

    # === 5. Google Cloud Stage-A-confirmed exclusions behave the same way
    # (both excluded_non_us and excluded_out_of_scope). ===
    gc_non_us_item = stage_a_item("998877", "Site Reliability Engineer", "Warsaw, Poland", "https://google.com/about/careers/applications/jobs/results/998877-x/", company="Google Cloud")
    gc_non_us_stub = source_history.build_exclusion_stub(gc_non_us_item, "excluded_non_us")
    assert gc_non_us_stub["processing_status"] == "excluded_non_us"

    gc_scope_item = stage_a_item("112233", "Software Engineer III, Pixel Camera", "Mountain View, CA", "https://google.com/about/careers/applications/jobs/results/112233-x/", company="Google Cloud")
    gc_scope_stub = source_history.build_exclusion_stub(
        gc_scope_item, "excluded_out_of_scope", exclusion_reason="Pixel/consumer-hardware role, no Cloud org or product mentioned"
    )
    assert gc_scope_stub["processing_status"] == "excluded_out_of_scope"
    assert gc_scope_stub["stage_a_exclusion_reason"] == "Pixel/consumer-hardware role, no Cloud org or product mentioned"
    for field in ("experience_level_text", "team_department", "short_description", "full_description_text"):
        assert gc_scope_stub[field] is None, f"{field} must still be null in an excluded_out_of_scope stub, got {gc_scope_stub[field]!r}"

    gc_history = source_history.load_history(Path(tmp) / "does-not-exist-2.json")
    source_history.commit_processed(gc_history, "google-cloud", [gc_non_us_stub, gc_scope_stub], "2026-08-15T00:00:00+00:00")
    for stub_item, identity_item in ((gc_non_us_stub, gc_non_us_item), (gc_scope_stub, gc_scope_item)):
        gc_identity = source_history.stable_identity(stub_item)
        assert gc_identity in gc_history["sources"]["google-cloud"], f"{gc_identity} must be committed"
    unseen_gc, prev_gc = source_history.diff_inventory(gc_history, "google-cloud", [gc_non_us_item, gc_scope_item])
    assert unseen_gc == [], f"both confirmed-excluded Google Cloud identities must not resurface: {unseen_gc}"
    assert len(prev_gc) == 2, prev_gc
    print("OK: test_5_google_cloud_both_exclusion_types")

    # === 6. Deferred/unattempted Google Cloud identities remain retryable
    # and are NOT incorrectly committed. ===
    gc_deferred_item = stage_a_item(
        "445566", "Cloud Infrastructure Engineer", "Austin, TX",
        "https://google.com/about/careers/applications/jobs/results/445566-x/", company="Google Cloud",
    )
    gc_included_item = stage_a_item(
        "778899", "Software Engineer, GKE", "Sunnyvale, CA",
        "https://google.com/about/careers/applications/jobs/results/778899-x/", company="Google Cloud",
    )
    gc_included_posting = dict(gc_included_item)
    gc_included_posting.update(
        {
            "experience_level_text": "2 years of experience",
            "team_department": "Google Kubernetes Engine",
            "short_description": "Build GKE.",
            "full_description_text": "Build and operate Google Kubernetes Engine (GKE), part of Google Cloud.",
            "processing_status": "success",
        }
    )
    # This run's full Stage-A inventory includes all three identities, but
    # `postings[]` (what the agent actually writes and what commit_processed
    # sees) only ever contains the two that reached a terminal disposition --
    # gc_deferred_item is deliberately left out entirely, simulating a time-
    # budget cutoff / not-yet-reached candidate.
    run3_history = source_history.load_history(Path(tmp) / "does-not-exist-3.json")
    source_history.commit_processed(
        run3_history, "google-cloud", [gc_non_us_stub, gc_scope_stub, gc_included_posting], "2026-08-15T00:00:00+00:00"
    )
    full_inventory = [gc_non_us_item, gc_scope_item, gc_included_item, gc_deferred_item]
    unseen3, prev3 = source_history.diff_inventory(run3_history, "google-cloud", full_inventory)
    unseen3_ids = {source_history.stable_identity(i) for i in unseen3}
    assert source_history.stable_identity(gc_deferred_item) in unseen3_ids, (
        "a deferred/never-attempted identity must still come back unseen next run -- it must remain retryable"
    )
    assert len(unseen3) == 1, f"only the deferred identity should be unseen; the other three were committed: {unseen3}"
    print("OK: test_6_deferred_identity_stays_retryable")

    # === 7. No full-detail fetch is required for deterministic Stage-A
    # exclusions -- build_exclusion_stub() is a pure function over the
    # Stage-A item alone; proven by construction (it never accepts or
    # touches anything resembling a fetched detail payload) and by the
    # null-fields assertions in tests 1 and 5 above. Reinforced here: the
    # stub's own fields are byte-identical to the Stage-A item's fields,
    # confirming nothing beyond the inventory record was consulted.
    for key in item:
        assert stub[key] == item[key], f"stub must carry the Stage-A item's own {key!r} through unchanged, got {stub[key]!r} vs {item[key]!r}"
    print("OK: test_7_no_detail_fetch_required")

    # === 8. Existing baseline_catchup / new_posting notification semantics
    # remain unchanged for stubbed identities -- discovery_type is computed
    # identically regardless of whether the posting is a real fetch or an
    # exclusion stub, since classify_discovery_type() never inspects
    # processing_status. ===
    disabled_baseline = {"lenovo": {"notification_mode": "disabled", "baseline_started_at": "2026-08-01T00:00:00Z"}}
    full_baseline = {"lenovo": {"notification_mode": "full", "baseline_started_at": "2026-08-01T00:00:00Z"}}

    h_disabled = source_history.load_history(Path(tmp) / "does-not-exist-4.json")
    r_disabled = source_history.commit_processed(h_disabled, "lenovo", [stub], "2026-08-15T00:00:00+00:00", baseline_status=disabled_baseline)
    assert r_disabled["baseline_catchup"] == [identity], (
        f"a disabled-notification-mode source's exclusion stub must still classify as baseline_catchup exactly like "
        f"a real posting would, unaffected by the stub mechanism: {r_disabled}"
    )

    h_full = source_history.load_history(Path(tmp) / "does-not-exist-5.json")
    r_full = source_history.commit_processed(h_full, "lenovo", [dict(stub)], "2026-08-15T00:00:00+00:00", baseline_status=full_baseline)
    assert r_full["baseline_catchup"] == [], (
        f"a full-notification-mode source's exclusion stub must classify as new_posting (never baseline_catchup), "
        f"exactly like a real posting would: {r_full}"
    )
    assert h_full["sources"]["lenovo"][identity]["discovery_type"] == "new_posting", h_full["sources"]["lenovo"][identity]
    print("OK: test_8_discovery_type_semantics_unchanged")

    # === Bonus: excluded_out_of_scope must be skipped by classify_dedupe_
    # report.py exactly like excluded_non_us/excluded_title_reject -- never
    # classified, ranked, reported, or notified. ===
    assert "excluded_out_of_scope" in classifier.EXCLUDED_AT_STAGE_A, (
        "excluded_out_of_scope must be in classify_dedupe_report.py's Stage-A skip set"
    )
    # Note: this test harness loads classify_dedupe_report.py via a fresh
    # importlib.util spec, so its own `from source_history import ...`
    # resolves through sys.path to a *different* module instance than the
    # one loaded here directly -- `is` would always be False regardless of
    # correctness. Equality is what actually matters (same contents); the
    # "imported, not a separately maintained literal" property is a source-
    # level fact already verified by reading classify_dedupe_report.py's
    # `EXCLUDED_AT_STAGE_A = TERMINAL_EXCLUSION_STATUSES` assignment.
    assert classifier.EXCLUDED_AT_STAGE_A == source_history.TERMINAL_EXCLUSION_STATUSES, (
        f"EXCLUDED_AT_STAGE_A must match source_history.TERMINAL_EXCLUSION_STATUSES exactly: "
        f"{classifier.EXCLUDED_AT_STAGE_A} vs {source_history.TERMINAL_EXCLUSION_STATUSES}"
    )
    with tempfile.TemporaryDirectory() as tmp2:
        run_dir = Path(tmp2) / "run"
        raw_dir = run_dir / "raw"
        raw_dir.mkdir(parents=True)
        (raw_dir / "google-cloud.json").write_text(
            json.dumps(
                {
                    "company": "Google Cloud",
                    "source_url": "https://www.google.com/about/careers/applications/cloud",
                    "fetched_at": "2026-08-15T00:00:00+00:00",
                    "status": "success",
                    "reason": None,
                    "inventory_count": 1,
                    "unseen_inventory_count": 1,
                    "previously_processed_count": 0,
                    "detail_fetch_count": 0,
                    "raw_posting_count": 1,
                    "inventory": [gc_scope_item],
                    "postings": [gc_scope_stub],
                }
            ),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "classify_dedupe_report.py"),
                "--run-dir",
                str(run_dir),
                "--fit-config",
                str(ROOT / "config" / "fit_priorities.json"),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"classify_dedupe_report.py must not error on an excluded_out_of_scope-only raw file: {proc.stderr}"
        deduped = json.loads((run_dir / "deduplicated.json").read_text(encoding="utf-8"))
        assert deduped == [], f"an excluded_out_of_scope stub must never reach deduplicated.json: {deduped}"
    print("OK: bonus_excluded_out_of_scope_skipped_by_classifier")

    print("All bespoke-source-history-stub regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
