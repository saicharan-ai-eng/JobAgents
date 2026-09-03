#!/usr/bin/env python3
"""Deterministic tests for the incremental two-stage scraping workflow.

Covers: unseen/previously-processed detection, formatting-insensitive
identity matching, repost detection, crash safety (nothing is marked
processed until commit), source-failure isolation, dynamic source-list
discovery, and the notification-layer behaviors for zero-new vs
exactly-one-new qualifying postings.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "source_history.py"
SPEC = importlib.util.spec_from_file_location("source_history", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {MODULE_PATH}")
sh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sh)

DETECT_SCRIPT = ROOT / "scripts" / "detect_new_jobs.py"
NOTIFICATIONS_CONFIG = ROOT / "config" / "notifications.json"


def make_history(entries: dict[str, dict]) -> dict:
    timestamp = "2026-01-01T00:00:00+00:00"
    return {
        "schema_version": sh.STATE_SCHEMA_VERSION,
        "created_at": timestamp,
        "updated_at": timestamp,
        "sources": {"dell": entries},
    }


def make_history_entry(**overrides) -> dict:
    entry = {
        "identity": "dell|id|r100",
        "company": "Dell",
        "job_id": "R100",
        "normalized_title": "software engineer i",
        "location": "austin texas",
        "job_url": "https://example.com/r100",
        "posting_date": "2026-01-01",
        "first_seen_at": "2026-01-01T00:00:00+00:00",
        "last_seen_at": "2026-01-01T00:00:00+00:00",
        "processing_status": "success",
    }
    entry.update(overrides)
    return entry


def make_posting(**overrides) -> dict:
    posting = {
        "company": "Dell",
        "job_title": "Software Engineer I",
        "job_id": "R100",
        "location": "Austin, Texas",
        "posting_date": "2026-01-01",
        "job_url": "https://example.com/r100",
        "job_url": "https://example.com/r100",
    }
    posting.update(overrides)
    return posting


def run_detect_new_jobs(run_dir: Path, state_file: Path) -> dict:
    subprocess.run(
        [
            sys.executable,
            str(DETECT_SCRIPT),
            "--run-dir",
            str(run_dir),
            "--state-file",
            str(state_file),
            "--config",
            str(NOTIFICATIONS_CONFIG),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    return json.loads((run_dir / "notification.json").read_text(encoding="utf-8"))


def main() -> int:
    # 1. An old job ID is not fetched or classified again: diff_inventory
    # must place it in previously_processed, not unseen.
    history = make_history({"dell|id|r100": make_history_entry()})
    inventory = [make_posting()]
    unseen, previously_processed = sh.diff_inventory(history, "dell", inventory)
    assert not unseen, f"Old job ID must not be marked unseen: {unseen}"
    assert len(previously_processed) == 1, "Old job ID must land in previously_processed"

    # 2. A new job ID is processed: an identity absent from history is unseen.
    history2 = make_history({"dell|id|r100": make_history_entry()})
    new_inventory = [make_posting(job_id="R200", job_url="https://example.com/r200")]
    unseen2, previously_processed2 = sh.diff_inventory(history2, "dell", new_inventory)
    assert len(unseen2) == 1 and unseen2[0]["job_id"] == "R200", "New job ID must be unseen"
    assert not previously_processed2

    # 3. The same ID with formatting changes (title casing/punctuation, location
    # formatting) is not treated as new -- identity is keyed on job_id alone
    # when present, and normalize() collapses formatting differences in the
    # fallback path too.
    history3 = make_history({"dell|id|r100": make_history_entry()})
    reformatted = [
        make_posting(job_title="SOFTWARE   ENGINEER, I!!", location="Austin,   TX")
    ]
    unseen3, previously_processed3 = sh.diff_inventory(history3, "dell", reformatted)
    assert not unseen3, "Formatting-only changes must not create a new identity"
    assert len(previously_processed3) == 1

    fallback_existing = sh.stable_identity(
        {"company": "Dell", "job_title": "Data Engineer", "location": "Austin, TX", "job_url": "https://x/1"}
    )
    fallback_reformatted = sh.stable_identity(
        {"company": "dell", "job_title": "Data   Engineer!!", "location": "austin tx", "job_url": "https://x/1"}
    )
    assert fallback_existing == fallback_reformatted, "Fallback identity must be formatting-insensitive"

    # 4. A clearly newer reposting date + explicit repost signal is marked a
    # repost and is eligible for reprocessing; a newer date ALONE, or a
    # repost_signal ALONE, must not trigger it (matches "explicitly newer ...
    # AND the source clearly identifies it as reposted").
    existing_entry = make_history_entry(posting_date="2026-01-01")
    genuine_repost = make_posting(posting_date="2026-03-01", repost_signal=True)
    assert sh.is_repost(existing_entry, genuine_repost), "Newer date + explicit signal must be a repost"

    date_only = make_posting(posting_date="2026-03-01")
    assert not sh.is_repost(existing_entry, date_only), "Newer date alone must not be a repost"

    signal_only = make_posting(posting_date="2026-01-01", repost_signal=True)
    assert not sh.is_repost(existing_entry, signal_only), "Repost signal without a newer date must not be a repost"

    older_with_signal = make_posting(posting_date="2025-12-01", repost_signal=True)
    assert not sh.is_repost(existing_entry, older_with_signal), "An older date must never count as a repost"

    history4 = make_history({"dell|id|r100": make_history_entry(posting_date="2026-01-01")})
    repost_inventory = [genuine_repost]
    unseen4, _ = sh.diff_inventory(history4, "dell", repost_inventory)
    assert len(unseen4) == 1, "A genuine repost must be eligible for reprocessing"

    history4b = copy.deepcopy(history4)
    processed = [dict(genuine_repost, processing_status="success")]
    result4 = sh.commit_processed(history4b, "dell", processed, "2026-03-01T00:00:00+00:00")
    assert result4["reposted"] == ["dell|id|r100"], result4
    entry = history4b["sources"]["dell"]["dell|id|r100"]
    assert entry["is_repost"] is True
    assert entry["first_seen_at"] == "2026-01-01T00:00:00+00:00", "first_seen_at must be preserved across a repost"
    assert entry["original_first_seen_at"] == "2026-01-01T00:00:00+00:00"
    assert entry["repost_detected_at"] == "2026-03-01T00:00:00+00:00"
    assert processed[0]["is_repost"] is True, "Repost fields must also be written onto the posting itself"
    assert processed[0]["original_first_seen_at"] == "2026-01-01T00:00:00+00:00"

    # Description edits / location-formatting / title punctuation changes
    # alone (no repost_signal) must never be treated as a new posting.
    history4c = make_history({"dell|id|r100": make_history_entry(posting_date="2026-01-01")})
    edited_only = [make_posting(posting_date="2026-01-01", job_title="Software Engineer I -- Updated")]
    unseen4c, prev4c = sh.diff_inventory(history4c, "dell", edited_only)
    assert not unseen4c, "A description/title edit without a repost signal must not be treated as new"

    # 5. A failed/crashed run must not permanently mark an unseen ID as
    # processed: diff_inventory alone never mutates history, so re-running it
    # on an untouched history file shows the same item still unseen.
    history5 = make_history({})
    inventory5 = [make_posting(job_id="R300", job_url="https://example.com/r300")]
    before_snapshot = copy.deepcopy(history5)
    unseen5a, _ = sh.diff_inventory(history5, "dell", inventory5)
    assert history5 == before_snapshot, "diff_inventory must never mutate history (no commit occurred)"
    # Simulate the crash: commit_processed is never called. Next run's diff
    # against the same on-disk history must still find it unseen.
    unseen5b, _ = sh.diff_inventory(history5, "dell", inventory5)
    assert len(unseen5a) == 1 and len(unseen5b) == 1 and unseen5a[0]["job_id"] == unseen5b[0]["job_id"] == "R300"

    # 6. A source failure (empty/blocked result) must not erase that source's
    # existing history.
    history6 = make_history({"dell|id|r100": make_history_entry(), "dell|id|r101": make_history_entry(job_id="R101", identity="dell|id|r101")})
    before6 = copy.deepcopy(history6["sources"]["dell"])
    failed_result = {"status": "failed", "reason": "site unreachable", "inventory": [], "postings": []}
    sh.commit_source_result(history6, "dell", failed_result, "2026-04-01T00:00:00+00:00")
    assert history6["sources"]["dell"] == before6, "A failed source's prior history must be left untouched"

    # 9. Adding a newly configured source is discovered dynamically -- no
    # hardcoded source list or count anywhere in the loading path.
    real_sources = sh.list_configured_sources(ROOT / "config" / "sources.json")
    # Deliberately not a hardcoded count: config/sources.json is the
    # authoritative, dynamically-discovered source list and its size grows
    # as sources are onboarded (9 originally, 76 after the 68-source-request
    # expansion -- 67 unique companies, since LangChain was requested under
    # two categories but added once). The real assertion is that loading is
    # dynamic and every entry can actually be dispatched.
    raw_source_count = len(json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))["sources"])
    assert len(real_sources) == raw_source_count
    assert len(real_sources) > 9, "expected the 68-source-expansion entries to be present"
    assert all(s["agent"] and s["agent"].endswith("-scraper") for s in real_sources), real_sources
    with tempfile.TemporaryDirectory() as td:
        extended_config_path = Path(td) / "sources.json"
        real_config = json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))
        real_config["sources"].append(
            {"slug": "example-co", "company": "Example Co", "agent": "example-co-scraper", "listing_url": "https://example.com/careers", "type": "standard"}
        )
        extended_config_path.write_text(json.dumps(real_config), encoding="utf-8")
        extended_sources = sh.list_configured_sources(extended_config_path)
        assert len(extended_sources) == raw_source_count + 1, "A newly added source must be picked up with zero code changes"
        assert any(s["slug"] == "example-co" and s["agent"] == "example-co-scraper" for s in extended_sources)

    # 10. Baseline seeding: a Stage-A-only inventory scan seeds history with
    # processing_status="baseline_existing", never touches an already-known
    # identity's real history, counts in-input duplicates, and a subsequent
    # diff_inventory against that same inventory reports everything as
    # previously_processed (never unseen).
    assert "baseline_existing" in sh.PROCESSING_STATUSES

    history10 = make_history({"dell|id|r100": make_history_entry(processing_status="success")})
    baseline_inventory = [
        make_posting(job_id="R100", job_url="https://example.com/r100"),  # already known -- must not be clobbered
        make_posting(job_id="R400", job_url="https://example.com/r400"),
        make_posting(job_id="R400", job_url="https://example.com/r400"),  # duplicate within this same scan
        make_posting(job_id="R401", job_url="https://example.com/r401"),
    ]
    seed_result = sh.commit_baseline_inventory(history10, "dell", baseline_inventory, "2026-08-06T00:00:00+00:00")
    assert seed_result["duplicate_identity_count"] == 1, seed_result
    assert sorted(seed_result["newly_added"]) == ["dell|id|r400", "dell|id|r401"]
    assert seed_result["already_known"] == ["dell|id|r100"]

    entry_r100 = history10["sources"]["dell"]["dell|id|r100"]
    assert entry_r100["processing_status"] == "success", "Baseline seeding must never overwrite a real processing status"
    entry_r400 = history10["sources"]["dell"]["dell|id|r400"]
    assert entry_r400["processing_status"] == "baseline_existing"
    assert entry_r400["first_seen_at"] == "2026-08-06T00:00:00+00:00"

    # A second Stage-A scan of the same listings must now show zero unseen.
    unseen10, previously_processed10 = sh.diff_inventory(history10, "dell", baseline_inventory)
    assert not unseen10, f"Everything seeded at baseline must be previously_processed, not unseen: {unseen10}"

    bad_status_raised = False
    try:
        sh.commit_baseline_inventory(history10, "dell", baseline_inventory, "2026-08-06T00:00:00+00:00", status="not-a-real-status")
    except ValueError:
        bad_status_raised = True
    assert bad_status_raised, "An unsupported processing_status must be rejected"

    # 7 & 8: notification-layer behavior via the real detect_new_jobs.py,
    # run against tiny fixture run directories (no live scraping/network).
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        state_file = td_path / "seen_jobs.json"

        baseline_job = {
            "company": "TestCo",
            "job_id": "OLD1",
            "job_title": "Existing Role",
            "location": "Remote",
            "posting_date": "2026-01-01",
            "job_url": "https://example.com/old1",
            "level_classification": "Entry-Level",
            "experience_required": "2+ years",
            "relevance_keywords_matched": ["AI"],
            "fit_priority": 2,
            "fit_label": "Strong adjacent fit — GPU & Systems",
            "fit_score": 40,
            "fit_keywords_matched": ["GPU"],
        }

        run1_dir = td_path / "run1"
        run1_dir.mkdir()
        (run1_dir / "deduplicated.json").write_text(json.dumps([baseline_job]), encoding="utf-8")
        notif1 = run_detect_new_jobs(run1_dir, state_file)
        assert notif1["baseline_created"] is True
        assert notif1["should_notify"] is False, "First run must establish a baseline, not alert"

        # 7. Zero unseen postings this run (same single job as baseline,
        # nothing new) -> no alert.
        run2_dir = td_path / "run2"
        run2_dir.mkdir()
        (run2_dir / "deduplicated.json").write_text(json.dumps([baseline_job]), encoding="utf-8")
        notif2 = run_detect_new_jobs(run2_dir, state_file)
        assert notif2["baseline_created"] is False
        assert notif2["should_notify"] is False, "Zero unseen postings must not alert"
        assert notif2["new_job_count"] == 0

        # 8. Exactly one new qualifying posting -> exactly one alert.
        new_job = {
            "company": "TestCo",
            "job_id": "NEW1",
            "job_title": "Freshly Posted Role",
            "location": "Remote",
            "posting_date": "2026-05-01",
            "job_url": "https://example.com/new1",
            "level_classification": "Entry-Level",
            "experience_required": "1+ years",
            "relevance_keywords_matched": ["AI", "Inference"],
            "fit_priority": 1,
            "fit_label": "Excellent fit — Inference & AI Infrastructure",
            "fit_score": 90,
            "fit_keywords_matched": ["vLLM"],
        }
        run3_dir = td_path / "run3"
        run3_dir.mkdir()
        (run3_dir / "deduplicated.json").write_text(json.dumps([baseline_job, new_job]), encoding="utf-8")
        notif3 = run_detect_new_jobs(run3_dir, state_file)
        assert notif3["should_notify"] is True
        assert notif3["new_job_count"] == 1, f"Expected exactly one new job, got {notif3['new_job_count']}"
        new_jobs_written = json.loads((run3_dir / "new_jobs.json").read_text(encoding="utf-8"))
        assert len(new_jobs_written) == 1
        assert new_jobs_written[0]["job_id"] == "NEW1"

    # === Baseline-completeness hardening: discovery_type classification ===

    partial_entry = {"baseline_status": "partial", "baseline_started_at": "2026-08-01T00:00:00+00:00"}
    complete_entry = {"baseline_status": "complete", "baseline_started_at": "2026-08-01T00:00:00+00:00"}

    # 11. An old, never-before-seen posting from a partial source is
    # baseline_catchup, not new_posting, and must not alert.
    old_posting = make_posting(job_id="OLD-1", job_url="https://example.com/old-1", posting_date="2026-07-15")
    dt = sh.classify_discovery_type(partial_entry, old_posting, None)
    assert dt == "baseline_catchup", f"Old posting on a partial source must be baseline_catchup, got {dt}"

    # 12. A posting reliably dated after baseline_started_at is new_posting.
    fresh_posting = make_posting(job_id="FRESH-1", job_url="https://example.com/fresh-1", posting_date="2026-08-10")
    dt = sh.classify_discovery_type(partial_entry, fresh_posting, None)
    assert dt == "new_posting", f"Posting dated after baseline_started_at must be new_posting, got {dt}"

    # 13. A missing/ambiguous posting_date on an incomplete source is
    # conservatively baseline_catchup, not new_posting.
    no_date_posting = make_posting(job_id="NODATE-1", job_url="https://example.com/nodate-1")
    no_date_posting["posting_date"] = None
    dt = sh.classify_discovery_type(partial_entry, no_date_posting, None)
    assert dt == "baseline_catchup", f"Missing-date posting on an incomplete source must be baseline_catchup, got {dt}"
    dt_failed = sh.classify_discovery_type({"baseline_status": "failed", "baseline_started_at": "2026-08-01T00:00:00+00:00"}, no_date_posting, None)
    assert dt_failed == "baseline_catchup"

    # 14. Merging original raw identities (baseline seeding) must not
    # overwrite a real, successfully-processed history entry.
    history14 = make_history({"dell|id|r500": make_history_entry(job_id="R500", identity="dell|id|r500", processing_status="success", first_seen_at="2026-01-01T00:00:00+00:00")})
    merge_result = sh.commit_baseline_inventory(
        history14, "dell", [make_posting(job_id="R500", job_url="https://example.com/r500")], "2026-08-06T00:00:00+00:00"
    )
    assert merge_result["already_known"] == ["dell|id|r500"]
    entry_after_merge = history14["sources"]["dell"]["dell|id|r500"]
    assert entry_after_merge["processing_status"] == "success", "Merging must never downgrade a real processing_status"
    assert entry_after_merge["first_seen_at"] == "2026-01-01T00:00:00+00:00", "Merging must never reset first_seen_at"

    # 15. A completed source handles a brand-new job ID normally (always
    # new_posting, regardless of date).
    dt_complete_old_date = sh.classify_discovery_type(complete_entry, old_posting, None)
    assert dt_complete_old_date == "new_posting", "A completed source must treat any unseen identity as new_posting"
    dt_complete_no_entry = sh.classify_discovery_type(None, fresh_posting, None)
    assert dt_complete_no_entry == "new_posting", "No baseline-status entry at all must default to new_posting (backward compatible)"

    # 16. baseline_catchup postings are still committed to source history
    # (they get fetched/processed, just never notified on).
    history16 = make_history({})
    catchup_posting = make_posting(job_id="CATCHUP-1", job_url="https://example.com/catchup-1", posting_date="2026-07-15", processing_status="success")
    baseline_status16 = {"dell": partial_entry}
    commit_result16 = sh.commit_processed(history16, "dell", [catchup_posting], "2026-08-06T00:00:00+00:00", baseline_status=baseline_status16)
    assert commit_result16["baseline_catchup"] == ["dell|id|catchup 1"]
    assert catchup_posting["discovery_type"] == "baseline_catchup"
    committed_entry = history16["sources"]["dell"]["dell|id|catchup 1"]
    assert committed_entry["processing_status"] == "success", "baseline_catchup postings are still recorded with their real fetch outcome"
    assert committed_entry["discovery_type"] == "baseline_catchup"

    # 17. End-to-end: only discovery_type=new_posting records reach
    # notification output, even when both jobs are absent from seen_jobs.json.
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        state_file = td_path / "seen_jobs.json"
        # Baseline run so this isn't a first-run (avoids baseline_created masking the gate).
        seed_dir = td_path / "seed"
        seed_dir.mkdir()
        (seed_dir / "deduplicated.json").write_text(json.dumps([]), encoding="utf-8")
        run_detect_new_jobs(seed_dir, state_file)

        mixed_dir = td_path / "mixed"
        mixed_dir.mkdir()
        new_posting_job = {
            "company": "TestCo", "job_id": "MIX-NEW", "job_title": "New Role", "location": "Remote",
            "posting_date": "2026-08-10", "job_url": "https://example.com/mix-new",
            "level_classification": "Entry-Level", "experience_required": "1+ years",
            "relevance_keywords_matched": ["AI"], "fit_priority": 1,
            "fit_label": "Excellent fit — Inference & AI Infrastructure", "fit_score": 80,
            "fit_keywords_matched": ["vLLM"], "discovery_type": "new_posting",
        }
        catchup_job = {
            "company": "TestCo", "job_id": "MIX-CATCHUP", "job_title": "Old Backfilled Role", "location": "Remote",
            "posting_date": "2026-07-01", "job_url": "https://example.com/mix-catchup",
            "level_classification": "Entry-Level", "experience_required": "1+ years",
            "relevance_keywords_matched": ["AI"], "fit_priority": 2,
            "fit_label": "Strong adjacent fit — GPU & Systems", "fit_score": 50,
            "fit_keywords_matched": ["GPU"], "discovery_type": "baseline_catchup",
        }
        (mixed_dir / "deduplicated.json").write_text(json.dumps([new_posting_job, catchup_job]), encoding="utf-8")
        notif17 = run_detect_new_jobs(mixed_dir, state_file)
        assert notif17["should_notify"] is True
        assert notif17["new_job_count"] == 1, f"Only the new_posting job should alert, got {notif17['new_job_count']}"
        new_jobs17 = json.loads((mixed_dir / "new_jobs.json").read_text(encoding="utf-8"))
        assert len(new_jobs17) == 1 and new_jobs17[0]["job_id"] == "MIX-NEW"
        # The catchup job must still be recorded in state/seen_jobs.json (second safeguard) even
        # though it never alerted, so it can never retroactively "become new" later.
        state_after = json.loads(state_file.read_text(encoding="utf-8"))
        assert "testco|id|mix catchup" in state_after["seen"], "baseline_catchup job must still be recorded as seen"

    # 18. Dell is fully disabled; NVIDIA/Google Cloud use date-verified catch-up
    # (not unconditionally enabled, not fully blocked) in the real production
    # baseline-status file, until their baselines are safely complete.
    real_status_path = ROOT / "state" / "source_baseline_status.json"
    if real_status_path.exists():
        real_status = json.loads(real_status_path.read_text(encoding="utf-8"))["sources"]
        assert real_status["dell"]["notification_mode"] == "disabled"
        assert real_status["dell"]["baseline_status"] == "failed"
        for slug in ("nvidia", "google-cloud"):
            assert real_status[slug]["notification_mode"] == "date_verified_only", (
                f"{slug} must use date-verified catch-up, not full or disabled"
            )
            assert real_status[slug]["baseline_status"] == "partial"
        for slug in ("hpe", "lenovo", "red-hat", "canonical", "microsoft-azure", "aws"):
            assert real_status[slug]["notification_mode"] == "full", f"{slug} should be fully notification-enabled"
            assert real_status[slug]["baseline_status"] == "complete"
        for slug, entry in real_status.items():
            assert "notifications_enabled" not in entry, (
                f"{slug}: legacy notifications_enabled must no longer be written"
            )

    # === notification_mode hardening ===

    full_entry = {"notification_mode": "full", "baseline_started_at": "2026-08-01T00:00:00+00:00"}
    date_verified_entry = {"notification_mode": "date_verified_only", "baseline_started_at": "2026-08-01T00:00:00+00:00"}
    disabled_entry = {"notification_mode": "disabled", "baseline_started_at": "2026-08-01T00:00:00+00:00"}

    # 19. Complete/full-mode source + brand-new job ID => new_posting (alerts).
    new_id_posting = make_posting(job_id="FULL-NEW", job_url="https://example.com/full-new", posting_date="2026-07-01")
    assert sh.classify_discovery_type(full_entry, new_id_posting, None) == "new_posting"

    # 20. Partial/date_verified_only source + reliable post-baseline date => new_posting (alerts).
    post_baseline = make_posting(job_id="DV-NEW", job_url="https://example.com/dv-new", posting_date="2026-08-10")
    assert sh.classify_discovery_type(date_verified_entry, post_baseline, None) == "new_posting"

    # 21. Partial source + missing date => baseline_catchup (no alert).
    missing_date = make_posting(job_id="DV-NODATE", job_url="https://example.com/dv-nodate")
    missing_date["posting_date"] = None
    assert sh.classify_discovery_type(date_verified_entry, missing_date, None) == "baseline_catchup"

    # 22. Partial source + pre-baseline date => baseline_catchup (no alert).
    pre_baseline = make_posting(job_id="DV-OLD", job_url="https://example.com/dv-old", posting_date="2026-07-01")
    assert sh.classify_discovery_type(date_verified_entry, pre_baseline, None) == "baseline_catchup"

    # 23. Partial source + explicit, post-baseline-dated repost => new_posting (alerts).
    existing_for_repost = make_history_entry(job_id="DV-REPOST", identity="testco|id|dv repost", posting_date="2026-07-01")
    explicit_repost = make_posting(job_id="DV-REPOST", job_url="https://example.com/dv-repost", posting_date="2026-08-15", repost_signal=True, company="TestCo")
    assert sh.classify_discovery_type(date_verified_entry, explicit_repost, existing_for_repost) == "new_posting"
    # But the same repost signal with a date that does NOT clear baseline is still catchup.
    weak_repost = make_posting(job_id="DV-REPOST2", job_url="https://example.com/dv-repost2", posting_date="2026-07-20", repost_signal=True)
    assert sh.classify_discovery_type(date_verified_entry, weak_repost, None) == "baseline_catchup"

    # 24. Disabled source => never new_posting, regardless of date evidence.
    assert sh.classify_discovery_type(disabled_entry, post_baseline, None) == "baseline_catchup"
    assert sh.classify_discovery_type(disabled_entry, explicit_repost, existing_for_repost) == "baseline_catchup"

    # 28. Old NVIDIA pages discovered during catch-up never alert, using the
    # real production baseline-status entry.
    if real_status_path.exists():
        nvidia_entry = json.loads(real_status_path.read_text(encoding="utf-8"))["sources"]["nvidia"]
        old_nvidia_posting = make_posting(
            company="NVIDIA", job_id="JR-OLDPAGE", job_url="https://jobs.nvidia.com/careers/job/old",
            posting_date="2026-07-01",
        )
        assert sh.classify_discovery_type(nvidia_entry, old_nvidia_posting, None) == "baseline_catchup", (
            "An old posting backfilled from NVIDIA's remaining pages must never alert"
        )
        # 29. A genuinely new NVIDIA job with a reliable post-baseline date does alert.
        new_nvidia_posting = make_posting(
            company="NVIDIA", job_id="JR-BRANDNEW", job_url="https://jobs.nvidia.com/careers/job/new",
            posting_date="2026-08-07",
        )
        assert sh.classify_discovery_type(nvidia_entry, new_nvidia_posting, None) == "new_posting", (
            "A reliably-dated new NVIDIA posting must not be downgraded merely because the source baseline is partial"
        )

    # 25/26/27: end-to-end write-order and duplicate-prevention safety via the
    # real detect_new_jobs.py, including a simulated crash-before-commit retry.
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        state_file = td_path / "seen_jobs.json"

        baseline_dir = td_path / "baseline"
        baseline_dir.mkdir()
        (baseline_dir / "deduplicated.json").write_text(json.dumps([]), encoding="utf-8")
        run_detect_new_jobs(baseline_dir, state_file)  # establish baseline (empty), non-first-run gate satisfied after this

        reliable_new_job = {
            "company": "TestCo", "job_id": "RELIABLE-1", "job_title": "Reliably New Role", "location": "Remote",
            "posting_date": "2026-08-10", "job_url": "https://example.com/reliable-1",
            "level_classification": "Entry-Level", "experience_required": "1+ years",
            "relevance_keywords_matched": ["AI"], "fit_priority": 1,
            "fit_label": "Excellent fit — Inference & AI Infrastructure", "fit_score": 88,
            "fit_keywords_matched": ["vLLM"], "discovery_type": "new_posting",
        }

        # --- 26. Simulated crash before the state commit: capture the
        # pre-run state, run detect_new_jobs.py, then discard its state write
        # (as if the process had crashed immediately after notification.json
        # was written but before the state commit landed), and confirm a
        # retry against the untouched old state reproduces the SAME alert
        # rather than losing it.
        pre_crash_state = state_file.read_text(encoding="utf-8")

        crash_dir = td_path / "crash_attempt"
        crash_dir.mkdir()
        (crash_dir / "deduplicated.json").write_text(json.dumps([reliable_new_job]), encoding="utf-8")
        notif_crash = run_detect_new_jobs(crash_dir, state_file)
        assert notif_crash["should_notify"] is True and notif_crash["new_job_count"] == 1
        # Simulate the crash: roll the state file back as if the commit at
        # the end of that run had never happened.
        state_file.write_text(pre_crash_state, encoding="utf-8")

        retry_dir = td_path / "retry_attempt"
        retry_dir.mkdir()
        (retry_dir / "deduplicated.json").write_text(json.dumps([reliable_new_job]), encoding="utf-8")
        notif_retry = run_detect_new_jobs(retry_dir, state_file)
        assert notif_retry["should_notify"] is True, "A crash before the state commit must allow the alert to be safely retried"
        assert notif_retry["new_job_count"] == 1
        retry_new_jobs = json.loads((retry_dir / "new_jobs.json").read_text(encoding="utf-8"))
        assert retry_new_jobs[0]["job_id"] == "RELIABLE-1"

        # This time let the commit actually land for real.
        # (notif_retry's run already committed state, since we didn't roll it back again.)

        # --- 25/27. A second run against the now-committed state must NOT
        # alert again for the same job (duplicate prevented after a
        # successful commit).
        second_run_dir = td_path / "second_run"
        second_run_dir.mkdir()
        (second_run_dir / "deduplicated.json").write_text(json.dumps([reliable_new_job]), encoding="utf-8")
        notif_second = run_detect_new_jobs(second_run_dir, state_file)
        assert notif_second["should_notify"] is False, "The same job must not alert a second time after a successful commit"
        assert notif_second["new_job_count"] == 0

    print("All source-history tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
