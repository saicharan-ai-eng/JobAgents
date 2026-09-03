#!/usr/bin/env python3
"""Regression tests for the 2026-08-14 deterministic-first / token-efficient
daily-execution migration (see CLAUDE.md "Deterministic-first processing and
the review queue").

Covers, in order (matching CLAUDE.md's "REQUIRED EFFICIENCY REGRESSION
TESTS" categories):
  1. Zero-actionable run: classify_dedupe_report.py produces valid minimal
     artifacts (empty deduplicated.json/needs_review.json) with no error,
     and detect_new_jobs.py in turn produces no notification -- without
     mutating any real state file.
  2. Already-seen postings are not re-fetched (run_source.py + history).
  3. A clearly-senior/marketing/sales/HR-titled Stage-A record is rejected
     without a detail fetch (run_source.py, processing_status=
     excluded_title_reject).
  4. A deterministically-resolvable Stage-B record never enters
     needs_review.json.
  5. needs_review.json is actually written to disk (run-level, not just
     the function-level check in test_experience_threshold_3yr.py) with a
     useful reason code/evidence for a genuinely ambiguous record.
  6. baseline_catchup never notifies (detect_new_jobs.py, run-level).
  7. A previously-seen qualifying job never re-notifies (detect_new_jobs.py,
     run-level, second run against existing state).
  8. Dell remains notification-disabled and unbaselined by this task
     (read-only check against the real state/source_baseline_status.json).
  9. Source finalization (finalize_sources.py) is unaffected by the new
     excluded_title_reject processing_status.

All fixtures use tempfile-based run directories / history files; nothing
under state/ or runs/ in the real repository is read for mutation, and only
one read-only check (#8) reads the real state/source_baseline_status.json.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import detect_new_jobs  # noqa: E402
import finalize_sources  # noqa: E402
import run_source  # noqa: E402
import source_history  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(self, responses: dict[str, Any]):
        self.responses = responses

    def get(self, url: str, timeout: int = 30, **kwargs) -> FakeResponse:  # noqa: ARG002
        for prefix, value in self.responses.items():
            if url.startswith(prefix):
                return value
        raise AssertionError(f"FakeSession has no canned response for GET {url}")


class _Patch:
    """Minimal monkeypatch substitute so this file stays runnable directly
    with `python` (no pytest dependency), consistent with every other
    test_*.py in this repo."""

    def __init__(self):
        self._restores: list[tuple[Any, str, Any]] = []

    def setattr(self, obj: Any, name: str, value: Any) -> None:
        self._restores.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self) -> None:
        for obj, name, old in reversed(self._restores):
            setattr(obj, name, old)


def make_config(tmp_path: Path, slug: str = "testco") -> Path:
    config = {
        "keywords": ["AI infrastructure"],
        "sources": [
            {
                "slug": slug,
                "company": "TestCo",
                "agent": "generic-ats-scraper",
                "careers_home": "https://example.invalid",
                "listing_url": "https://example.invalid",
                "platform": "greenhouse",
                "greenhouse_board_token": "testco",
            }
        ],
    }
    config_path = tmp_path / "sources.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def test_already_seen_not_refetched(tmp_path: Path) -> None:
    """CLAUDE.md efficiency test #2: an identity already in
    state/seen_source_jobs.json must never trigger a Stage-B detail fetch on
    a later run, even though it still appears in the source's live Stage-A
    inventory."""
    config_path = make_config(tmp_path)
    history_path = tmp_path / "history.json"
    output_path = tmp_path / "out.json"

    job = {
        "id": 1,
        "title": "AI Infrastructure Engineer",
        "location": {"name": "San Francisco, CA"},
        "content": "Build AI infrastructure.",
        "absolute_url": "https://job-boards.greenhouse.io/testco/jobs/1",
    }

    # Seed history as if this identity was already committed by a prior run.
    history = source_history.load_history(history_path)
    inventory_item = {
        "company": "TestCo",
        "job_title": job["title"],
        "job_id": "1",
        "location": "San Francisco, CA",
        "job_url": job["absolute_url"],
    }
    source_history.commit_processed(
        history,
        "testco",
        [{**inventory_item, "processing_status": "success", "full_description_text": "already fetched"}],
        source_history.utc_now(),
    )
    source_history.atomic_write_json(history_path, history)

    detail_calls: list[str] = []

    def fake_fetch_detail(entry, item, session, timeout):  # noqa: ANN001, ARG001
        detail_calls.append(item["job_id"])
        raise AssertionError("fetch_detail must never be called for an already-seen identity")

    import adapters.greenhouse as gh_module

    patch = _Patch()
    patch.setattr(gh_module, "fetch_detail", fake_fetch_detail)
    patch.setattr(
        gh_module,
        "fetch_inventory",
        lambda entry, session, keywords, timeout: [{**inventory_item, "_platform_ref": job}],
    )
    try:
        sys.argv = [
            "run_source.py", "--slug", "testco", "--output", str(output_path),
            "--config-file", str(config_path), "--history-file", str(history_path), "--delay", "0",
        ]
        rc = run_source.main()
        assert rc == 0
        result = json.loads(output_path.read_text(encoding="utf-8"))
        assert detail_calls == [], "fetch_detail was called for an already-seen identity"
        assert result["unseen_inventory_count"] == 0
        assert result["previously_processed_count"] == 1
        assert result["detail_fetch_count"] == 0
        assert result["postings"] == [], "an already-seen identity must not appear in postings at all"
    finally:
        patch.undo()


def test_stage_a_title_rejection(tmp_path: Path) -> None:
    """CLAUDE.md efficiency test #3: a clearly senior/marketing/sales/HR
    title is rejected using Stage-A metadata alone, with zero detail
    fetches -- while a job whose title carries a junior/early-career signal
    is conservatively left for Stage B, never guessed at Stage A."""
    config_path = make_config(tmp_path)
    history_path = tmp_path / "history.json"
    output_path = tmp_path / "out.json"

    senior_job = {"id": 1, "title": "Senior Software Engineer, AI Infrastructure", "location": {"name": "Austin, TX"}, "content": "x", "absolute_url": "https://job-boards.greenhouse.io/testco/jobs/1"}
    marketing_job = {"id": 2, "title": "Marketing Coordinator, AI Products", "location": {"name": "Austin, TX"}, "content": "x", "absolute_url": "https://job-boards.greenhouse.io/testco/jobs/2"}
    intern_job = {"id": 3, "title": "AI Infrastructure Engineering Intern", "location": {"name": "Austin, TX"}, "content": "x", "absolute_url": "https://job-boards.greenhouse.io/testco/jobs/3"}

    detail_calls: list[str] = []

    def fake_fetch_detail(entry, item, session, timeout):  # noqa: ANN001, ARG001
        detail_calls.append(item["job_id"])
        return {
            "company": "TestCo", "job_title": item["job_title"], "job_id": item["job_id"],
            "location": item["location"], "posting_date": None, "experience_level_text": None,
            "job_url": item["job_url"], "team_department": None, "short_description": None,
            "full_description_text": "detail fetched", "source_keyword": item["source_keyword"],
        }

    def fake_fetch_inventory(entry, session, keywords, timeout):  # noqa: ANN001, ARG001
        items = []
        for j in (senior_job, marketing_job, intern_job):
            items.append(
                {
                    "company": "TestCo", "job_title": j["title"], "job_id": str(j["id"]),
                    "location": j["location"]["name"], "posting_date": None, "job_url": j["absolute_url"],
                    "source_keyword": "AI infrastructure", "_platform_ref": j,
                }
            )
        return items

    import adapters.greenhouse as gh_module

    patch = _Patch()
    patch.setattr(gh_module, "fetch_detail", fake_fetch_detail)
    patch.setattr(gh_module, "fetch_inventory", fake_fetch_inventory)
    try:
        sys.argv = [
            "run_source.py", "--slug", "testco", "--output", str(output_path),
            "--config-file", str(config_path), "--history-file", str(history_path), "--delay", "0",
        ]
        rc = run_source.main()
        assert rc == 0
        result = json.loads(output_path.read_text(encoding="utf-8"))

        assert detail_calls == ["3"], f"only the intern posting should reach Stage B, got calls={detail_calls}"
        by_id = {p["job_id"]: p for p in result["postings"]}
        assert by_id["1"]["processing_status"] == "excluded_title_reject"
        assert by_id["1"]["stage_a_exclusion_reason"] == "senior_title"
        assert by_id["1"]["full_description_text"] is None, "Stage-A-rejected postings must never carry fetched text"
        assert by_id["2"]["processing_status"] == "excluded_title_reject"
        assert by_id["2"]["stage_a_exclusion_reason"] == "marketing_role_title"
        assert by_id["3"]["processing_status"] == "success", "a title with a junior/intern signal must reach Stage B"
        assert result["detail_fetch_count"] == 1
        rejected_count = sum(1 for p in result["postings"] if p["processing_status"] == "excluded_title_reject")
        assert rejected_count == 2, f"expected exactly 2 title-rejected postings, got {rejected_count}"
    finally:
        patch.undo()


def _write_raw_source(raw_dir: Path, slug: str, postings: list[dict[str, Any]], status: str = "success") -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{slug}.json").write_text(
        json.dumps(
            {
                "company": "TestCo",
                "source_url": "https://example.com",
                "fetched_at": "2026-08-14T00:00:00Z",
                "status": status,
                "reason": None,
                "raw_posting_count": len(postings),
                "postings": postings,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_zero_actionable_run_produces_valid_minimal_artifacts(tmp_path: Path) -> None:
    """CLAUDE.md efficiency test #1: a run where every unseen identity was
    already Stage-A-rejected (zero detail fetches) must still produce a
    valid, correctly-empty deduplicated.json/needs_review.json -- and the
    resulting empty deduplicated.json must, in turn, produce should_notify:
    false without touching any real state file."""
    run_dir = tmp_path / "run"
    raw_dir = run_dir / "raw"
    _write_raw_source(
        raw_dir,
        "testco",
        [
            {
                "company": "TestCo", "job_title": "Senior Staff Engineer", "job_id": "1",
                "location": "Berlin, Germany", "posting_date": None, "job_url": "https://example.com/1",
                "experience_level_text": None, "team_department": None, "short_description": None,
                "full_description_text": None, "processing_status": "excluded_non_us",
            },
            {
                "company": "TestCo", "job_title": "Senior Software Engineer", "job_id": "2",
                "location": "Austin, TX", "posting_date": None, "job_url": "https://example.com/2",
                "experience_level_text": None, "team_department": None, "short_description": None,
                "full_description_text": None, "processing_status": "excluded_title_reject",
                "stage_a_exclusion_reason": "senior_title",
            },
        ],
    )

    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "classify_dedupe_report.py"), "--run-dir", str(run_dir)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"classify_dedupe_report.py failed on a zero-actionable run: {proc.stderr}"

    deduped = json.loads((run_dir / "deduplicated.json").read_text(encoding="utf-8"))
    needs_review = json.loads((run_dir / "needs_review.json").read_text(encoding="utf-8"))
    filtered = json.loads((run_dir / "filtered.json").read_text(encoding="utf-8"))
    assert deduped == [], "a run with zero actionable records must produce an empty deduplicated.json"
    assert needs_review == [], "a run with zero actionable records must produce an empty needs_review.json"
    assert filtered == [], "Stage-A-rejected postings must never be classified at all"

    report_text = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "Stage-A deterministic rejections" in report_text

    state_file = run_dir / "seen_jobs.json"
    proc2 = subprocess.run(
        [
            sys.executable, str(SCRIPTS / "detect_new_jobs.py"),
            "--run-dir", str(run_dir), "--state-file", str(state_file),
            "--config", str(run_dir / "no_such_notifications_config.json"),
        ],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc2.returncode == 0, f"detect_new_jobs.py failed: {proc2.stderr}"
    notification = json.loads((run_dir / "notification.json").read_text(encoding="utf-8"))
    assert notification["should_notify"] is False
    assert notification["new_job_count"] == 0


def test_deterministic_record_never_enters_review_queue(tmp_path: Path) -> None:
    """CLAUDE.md efficiency test #4: a clean, unambiguous Stage-B record is
    fully resolved by the deterministic classifier and never appears in
    needs_review.json."""
    run_dir = tmp_path / "run"
    raw_dir = run_dir / "raw"
    _write_raw_source(
        raw_dir,
        "testco",
        [
            {
                "company": "TestCo", "job_title": "GPU Software Engineer I", "job_id": "clean-1",
                "location": "Austin, TX", "posting_date": "2026-08-14",
                "job_url": "https://example.com/clean-1", "team_department": "Engineering",
                "experience_level_text": "2 years of experience",
                "short_description": "Build CUDA GPU kernels for inference.",
                "full_description_text": (
                    "Required Qualifications: 2 years of experience with CUDA and GPU kernel development."
                ),
                "processing_status": "success",
            }
        ],
    )
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "classify_dedupe_report.py"), "--run-dir", str(run_dir)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    needs_review = json.loads((run_dir / "needs_review.json").read_text(encoding="utf-8"))
    assert needs_review == [], f"a clean, unambiguous record must not enter needs_review.json, got {needs_review}"
    deduped = json.loads((run_dir / "deduplicated.json").read_text(encoding="utf-8"))
    assert len(deduped) == 1 and deduped[0]["job_id"] == "clean-1"


def test_ambiguous_record_written_to_needs_review_file(tmp_path: Path) -> None:
    """CLAUDE.md efficiency test #5: a genuinely ambiguous record is written
    to needs_review.json on disk with a reason code and evidence, alongside
    (not instead of) its own deterministic decision in filtered.json."""
    run_dir = tmp_path / "run"
    raw_dir = run_dir / "raw"
    _write_raw_source(
        raw_dir,
        "testco",
        [
            {
                "company": "TestCo", "job_title": "Software Engineer", "job_id": "ambiguous-1",
                "location": "Austin, TX", "posting_date": "2026-08-14",
                "job_url": "https://example.com/ambiguous-1", "team_department": "Engineering",
                "experience_level_text": None,
                "short_description": None,
                "full_description_text": (
                    "Required Qualifications: 2 years of experience or similar hands-on experience, "
                    "5 years of experience with production AI/ML systems."
                ),
                "processing_status": "success",
            }
        ],
    )
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "classify_dedupe_report.py"), "--run-dir", str(run_dir)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    needs_review = json.loads((run_dir / "needs_review.json").read_text(encoding="utf-8"))
    assert len(needs_review) == 1, f"expected exactly one ambiguous record, got {needs_review}"
    record = needs_review[0]
    assert record["job_id"] == "ambiguous-1"
    assert record["source_slug"] == "testco"
    assert "ambiguous_experience_path" in record["reason_codes"]
    assert record["evidence"], "needs_review.json entries must carry evidence, not just a bare reason code"
    assert "full_description_text" not in record, "needs_review.json must stay compact -- no full description text"


def test_baseline_catchup_never_notifies(tmp_path: Path) -> None:
    """CLAUDE.md efficiency test #6/#7: a baseline_catchup-flagged job never
    triggers a notification, and a job already recorded in seen_jobs.json
    never re-notifies on a later run -- exercised directly against
    detect_new_jobs.py, the new-job-monitor's own deterministic engine."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state_file = tmp_path / "seen_jobs.json"
    config_arg = ["--config", str(tmp_path / "no_such_config.json")]

    # Run 0: establish the seen_jobs.json baseline on an empty result first,
    # so the *first-run* baseline rule (CLAUDE.md item 15 -- a separate rule
    # from the discovery_type gate under test here) doesn't confound the
    # rest of this test: every later run below has state_file already on
    # disk, so baseline_created is false and only discovery_type governs.
    (run_dir / "deduplicated.json").write_text(json.dumps([]), encoding="utf-8")
    proc0 = subprocess.run(
        [sys.executable, str(SCRIPTS / "detect_new_jobs.py"), "--run-dir", str(run_dir), "--state-file", str(state_file), *config_arg],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc0.returncode == 0, proc0.stderr
    assert json.loads((run_dir / "notification.json").read_text(encoding="utf-8"))["baseline_created"] is True

    catchup_job = {
        "company": "TestCo", "job_id": "catchup-1", "job_title": "GPU Engineer I",
        "location": "Austin, TX", "posting_date": "2026-08-01", "job_url": "https://example.com/catchup-1",
        "include": True, "fit_priority": 2, "fit_score": 40, "us_location_eligible": True,
        "discovery_type": "baseline_catchup",
    }
    (run_dir / "deduplicated.json").write_text(json.dumps([catchup_job]), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "detect_new_jobs.py"), "--run-dir", str(run_dir), "--state-file", str(state_file), *config_arg],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    notification = json.loads((run_dir / "notification.json").read_text(encoding="utf-8"))
    assert notification["baseline_created"] is False, "this is not the first run -- baseline_created must be false"
    assert notification["should_notify"] is False, "a baseline_catchup-only run must never notify"
    assert notification["new_job_count"] == 0
    new_jobs = json.loads((run_dir / "new_jobs.json").read_text(encoding="utf-8"))
    assert new_jobs == [], "baseline_catchup job must never appear in new_jobs.json"
    # It is still recorded as seen, silently, so it is never re-evaluated.
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert detect_new_jobs.stable_job_key(catchup_job) in state["seen"]

    # Second run: a genuinely new_posting job now exists alongside the
    # already-seen catchup job -- only the new one may notify, and the
    # previously-seen one (even though its discovery_type is now
    # irrelevant -- it's simply already in state) must never re-notify.
    new_posting_job = {
        "company": "TestCo", "job_id": "fresh-1", "job_title": "ML Platform Engineer I",
        "location": "Austin, TX", "posting_date": "2026-08-14", "job_url": "https://example.com/fresh-1",
        "include": True, "fit_priority": 3, "fit_score": 30, "us_location_eligible": True,
        "discovery_type": "new_posting",
    }
    (run_dir / "deduplicated.json").write_text(json.dumps([catchup_job, new_posting_job]), encoding="utf-8")
    proc2 = subprocess.run(
        [
            sys.executable, str(SCRIPTS / "detect_new_jobs.py"),
            "--run-dir", str(run_dir), "--state-file", str(state_file),
            "--config", str(tmp_path / "no_such_config.json"),
        ],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc2.returncode == 0, proc2.stderr
    notification2 = json.loads((run_dir / "notification.json").read_text(encoding="utf-8"))
    assert notification2["should_notify"] is True
    assert notification2["new_job_count"] == 1, (
        f"only the genuinely new_posting job may notify; the already-seen catchup job must not "
        f"re-notify: {notification2}"
    )
    new_jobs2 = json.loads((run_dir / "new_jobs.json").read_text(encoding="utf-8"))
    assert {j["job_id"] for j in new_jobs2} == {"fresh-1"}


def test_dell_remains_disabled_and_unbaselined() -> None:
    """CLAUDE.md efficiency test #8: this task must never enable Dell
    notifications or seed its baseline. Read-only check against the real
    state/source_baseline_status.json -- never mutated by this test file."""
    status_path = ROOT / "state" / "source_baseline_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    dell = status["sources"]["dell"]
    assert dell["notification_mode"] == "disabled", (
        f"Dell must remain notification-disabled; got {dell['notification_mode']!r}"
    )
    assert "blocking_reason" in dell.get("progress", {}), (
        "Dell's baseline-seed-and-verify procedure must still be recorded as not yet performed"
    )


def test_finalization_unaffected_by_new_processing_status(tmp_path: Path) -> None:
    """CLAUDE.md efficiency test #9: source finalization (manifest/verify)
    must continue to work correctly for a raw file containing the new
    excluded_title_reject stub postings -- the short-circuit and Stage-A
    optimizations must never bypass checksum/manifest correctness."""
    run_dir = tmp_path / "run"
    raw_dir = run_dir / "raw"
    config_path = make_config(tmp_path, slug="testco")
    _write_raw_source(
        raw_dir,
        "testco",
        [
            {
                "company": "TestCo", "job_title": "Senior Engineer", "job_id": "1",
                "location": "Austin, TX", "posting_date": None, "job_url": "https://example.com/1",
                "experience_level_text": None, "team_department": None, "short_description": None,
                "full_description_text": None, "processing_status": "excluded_title_reject",
                "stage_a_exclusion_reason": "senior_title",
            }
        ],
    )
    result = finalize_sources.cmd_manifest(run_dir, config_path)
    assert result["ok"] is True
    assert result["sources_finalized"] == 1
    manifest = finalize_sources.load_manifest(run_dir)
    assert manifest["sources"]["testco"]["status"] == "success"
    assert manifest["sources"]["testco"]["record_count"] == 1
    verify = finalize_sources.cmd_verify(run_dir)
    assert verify["ok"] is True, f"verify must pass immediately after manifest: {verify}"


def main() -> int:
    fixture_tests = [
        test_already_seen_not_refetched,
        test_stage_a_title_rejection,
        test_zero_actionable_run_produces_valid_minimal_artifacts,
        test_deterministic_record_never_enters_review_queue,
        test_ambiguous_record_written_to_needs_review_file,
        test_baseline_catchup_never_notifies,
        test_finalization_unaffected_by_new_processing_status,
    ]
    for test in fixture_tests:
        with tempfile.TemporaryDirectory() as tmp:
            test(Path(tmp))
        print(f"OK: {test.__name__}")

    test_dell_remains_disabled_and_unbaselined()
    print("OK: test_dell_remains_disabled_and_unbaselined")

    print("All deterministic-first efficiency regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
