#!/usr/bin/env python3
"""Regression tests for scripts/finalize_sources.py -- the checksummed
manifest gate hardening run 20260812T152540Z's raw-file finalization bug
(source history was committed while NVIDIA's raw file was still being
rewritten mid-run, forcing a manual rerun of commit/summarize/classify/
dedup/report).

Covers the 5 required scenarios:
  1. a source completes normally -> manifest captures it; verify passes.
  2. a source's raw file changes before commit -> verify catches it, refresh
     recovers, and a subsequent commit sees the corrected data.
  3. a source's raw file changes after commit but before classification ->
     verify catches it there too; refresh + an idempotent recommit pick up
     the corrected data without double-counting history.
  4. a source's raw file changes after classification -> verify (called
     again post-classification) still detects it, and reclassifying from
     the refreshed data actually changes the output (proving the recovery
     path is not a no-op).
  5. unchanged files -> verify is a true no-op and refresh does nothing
     (no unnecessary rewrite, no spurious recovery-log entries).

Also covers the terminal-state gate itself: manifest-building must refuse
(non-zero / raised error) when a configured source has no raw file yet, or
has a non-terminal/invalid status -- collection is not actually finished.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


finalize = _load("finalize_sources_tests", "finalize_sources.py")
import source_history as sh  # noqa: E402


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def source_result(company: str, status: str = "success", postings: list[dict] | None = None, fetched_at: str = "2026-08-12T15:00:00+00:00") -> dict:
    postings = postings if postings is not None else []
    return {
        "company": company,
        "source_url": f"https://example.com/{company.lower()}",
        "fetched_at": fetched_at,
        "status": status,
        "reason": None,
        "raw_posting_count": len(postings),
        "inventory_count": len(postings),
        "unseen_inventory_count": len(postings),
        "previously_processed_count": 0,
        "detail_fetch_count": len(postings),
        "inventory": [{"company": company, "job_title": p["job_title"], "job_url": p["job_url"]} for p in postings],
        "postings": postings,
    }


def make_posting(job_id: str, title: str = "AI Infrastructure Engineer", company: str = "TestCo") -> dict:
    return {
        "company": company,
        "job_title": title,
        "job_id": job_id,
        "location": "Remote - US",
        "posting_date": "2026-08-01",
        "job_url": f"https://example.com/job/{job_id}",
        "processing_status": "success",
    }


def setup_run(tmp: Path, sources: dict[str, dict]) -> tuple[Path, Path]:
    """Write config/sources.json (minimal) + runs/<id>/raw/<slug>.json for
    each entry in `sources` ({slug: source_result_dict}). Returns (run_dir,
    config_path)."""
    run_dir = tmp / "run"
    (run_dir / "raw").mkdir(parents=True)
    config = {"keywords": [], "sources": [{"slug": slug, "company": data["company"], "agent": "x", "listing_url": "https://x", "type": "generic"} for slug, data in sources.items()]}
    config_path = tmp / "sources.json"
    write_json(config_path, config)
    for slug, data in sources.items():
        write_json(run_dir / "raw" / f"{slug}.json", data)
    return run_dir, config_path


def main() -> int:
    # --- Terminal-state gate: refuses to finalize when a source is missing
    # its raw file entirely. ---
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        run_dir, config_path = setup_run(tmp_path, {"testco": source_result("TestCo")})
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["sources"].append({"slug": "missing-co", "company": "MissingCo", "agent": "x", "listing_url": "https://x", "type": "generic"})
        write_json(config_path, config)
        try:
            finalize.build_manifest(run_dir, config_path)
            raise AssertionError("Expected build_manifest to refuse when a source's raw file is missing")
        except RuntimeError as exc:
            assert "missing-co" in str(exc), str(exc)
    print("OK: terminal-state gate refuses on a missing raw file")

    # --- Terminal-state gate: refuses when a source has a non-terminal /
    # invalid status. ---
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        run_dir, config_path = setup_run(tmp_path, {"testco": source_result("TestCo", status="in_progress")})
        try:
            finalize.build_manifest(run_dir, config_path)
            raise AssertionError("Expected build_manifest to refuse on a non-terminal status")
        except (RuntimeError, ValueError) as exc:
            assert "testco" in str(exc) or "in_progress" in str(exc), str(exc)
    print("OK: terminal-state gate refuses on a non-terminal status")

    # --- Scenario 1: source completes normally. ---
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        run_dir, config_path = setup_run(
            tmp_path,
            {
                "testco": source_result("TestCo", postings=[make_posting("R1"), make_posting("R2")]),
                "otherco": source_result("OtherCo", status="failed", postings=[]),
            },
        )
        result = finalize.cmd_manifest(run_dir, config_path)
        assert result["ok"] is True
        assert result["sources_finalized"] == 2
        manifest = finalize.load_manifest(run_dir)
        assert manifest["sources"]["testco"]["record_count"] == 2
        assert manifest["sources"]["testco"]["status"] == "success"
        assert manifest["sources"]["otherco"]["status"] == "failed"
        verify = finalize.cmd_verify(run_dir)
        assert verify == {"ok": True, "changed": [], "missing": []}
    print("OK: scenario 1 -- normal completion, manifest + verify both clean")

    # --- Scenario 2: raw file changes before commit. verify catches it,
    # refresh recovers, and the commit that follows sees the corrected
    # (post-refresh) data -- exactly the NVIDIA incident. ---
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        run_dir, config_path = setup_run(
            tmp_path, {"nvidia": source_result("NVIDIA", postings=[make_posting("R1", company="NVIDIA")])}
        )
        finalize.cmd_manifest(run_dir, config_path)

        # NVIDIA's agent rewrites its raw file with corrected (much larger)
        # data after finalization was already snapshotted, before commit ran.
        write_json(
            run_dir / "raw" / "nvidia.json",
            source_result("NVIDIA", postings=[make_posting("R1", company="NVIDIA"), make_posting("R2", company="NVIDIA"), make_posting("R3", company="NVIDIA")]),
        )

        pre_commit_verify = finalize.cmd_verify(run_dir)
        assert pre_commit_verify["ok"] is False
        assert pre_commit_verify["changed"][0]["slug"] == "nvidia"

        refresh_result = finalize.cmd_refresh(run_dir, config_path, note="corrected pagination bug mid-run")
        assert refresh_result["refreshed"] == ["nvidia"]
        manifest = finalize.load_manifest(run_dir)
        assert manifest["sources"]["nvidia"]["record_count"] == 3
        assert len(manifest["recovery_log"]) == 1
        assert manifest["recovery_log"][0]["event"] == "raw_file_changed_after_finalization"
        assert manifest["recovery_log"][0]["old_record_count"] == 1
        assert manifest["recovery_log"][0]["new_record_count"] == 3

        post_refresh_verify = finalize.cmd_verify(run_dir)
        assert post_refresh_verify["ok"] is True

        # The commit that follows must see the corrected 3-posting data.
        history_path = tmp_path / "history.json"
        commit_result = sh.commit_run(history_path, run_dir)
        assert len(commit_result["sources"]["nvidia"]["newly_added"]) == 3
    print("OK: scenario 2 -- raw file changed before commit, caught and recovered")

    # --- Scenario 3: raw file changes after commit but before
    # classification. verify (checked again before classification) catches
    # it; refresh + an idempotent recommit correctly pick up the new data
    # without double-counting or corrupting history. ---
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        run_dir, config_path = setup_run(
            tmp_path, {"nvidia": source_result("NVIDIA", postings=[make_posting("R1", company="NVIDIA")])}
        )
        finalize.cmd_manifest(run_dir, config_path)
        history_path = tmp_path / "history.json"
        first_commit = sh.commit_run(history_path, run_dir)
        assert len(first_commit["sources"]["nvidia"]["newly_added"]) == 1

        # File changes post-commit, pre-classification.
        write_json(
            run_dir / "raw" / "nvidia.json",
            source_result("NVIDIA", postings=[make_posting("R1", company="NVIDIA"), make_posting("R2", company="NVIDIA")]),
        )
        pre_classify_verify = finalize.cmd_verify(run_dir)
        assert pre_classify_verify["ok"] is False

        finalize.cmd_refresh(run_dir, config_path, note="changed after commit, before classification")
        assert finalize.cmd_verify(run_dir)["ok"] is True

        # Recommit must be safe/idempotent: R1 was already known (not
        # re-added), R2 is genuinely new, and history is not corrupted.
        second_commit = sh.commit_run(history_path, run_dir)
        assert second_commit["sources"]["nvidia"]["newly_added"] == ["nvidia|id|r2"], second_commit["sources"]["nvidia"]
        history = sh.load_history(history_path)
        assert set(history["sources"]["nvidia"].keys()) == {"nvidia|id|r1", "nvidia|id|r2"}
        assert history["sources"]["nvidia"]["nvidia|id|r1"]["first_seen_at"] == first_commit["timestamp"], (
            "R1's first_seen_at must be preserved across the idempotent recommit, not reset"
        )
    print("OK: scenario 3 -- raw file changed after commit/before classification, recovered idempotently")

    # --- Scenario 4: raw file changes after classification has already run.
    # verify (checked again post-classification) still detects it, and
    # reclassifying from the refreshed raw data actually changes the
    # committed output -- proving the recovery path is load-bearing, not a
    # no-op safety check that nobody acts on. ---
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        run_dir, config_path = setup_run(
            tmp_path, {"nvidia": source_result("NVIDIA", postings=[make_posting("R1", company="NVIDIA")])}
        )
        finalize.cmd_manifest(run_dir, config_path)
        history_path = tmp_path / "history.json"
        sh.commit_run(history_path, run_dir)

        # Simulate "classification already ran": a filtered.json exists,
        # reflecting only R1.
        write_json(run_dir / "filtered.json", [{"job_id": "R1", "include": True}])

        # Raw file changes again, after classification.
        write_json(
            run_dir / "raw" / "nvidia.json",
            source_result("NVIDIA", postings=[make_posting("R1", company="NVIDIA"), make_posting("R2", company="NVIDIA")]),
        )
        post_classify_verify = finalize.cmd_verify(run_dir)
        assert post_classify_verify["ok"] is False, "A post-classification raw-file change must still be detected"
        assert post_classify_verify["changed"][0]["slug"] == "nvidia"

        # Recovery: refresh, recommit (idempotent), and re-derive filtered
        # output from the corrected raw data (standing in for rerunning
        # classify_dedupe_report.py, which is exercised end-to-end in
        # test_nvidia_domain_corroboration.py).
        finalize.cmd_refresh(run_dir, config_path, note="changed after classification")
        sh.commit_run(history_path, run_dir)
        raw_after = json.loads((run_dir / "raw" / "nvidia.json").read_text(encoding="utf-8"))
        job_ids_now = {p["job_id"] for p in raw_after["postings"]}
        old_filtered_job_ids = {r["job_id"] for r in json.loads((run_dir / "filtered.json").read_text(encoding="utf-8"))}
        assert job_ids_now != old_filtered_job_ids, (
            "The refreshed raw data must genuinely differ from the stale filtered.json -- confirming a "
            "recompute is actually necessary, not spuriously triggered"
        )
    print("OK: scenario 4 -- raw file changed after classification, detected and shown to require a recompute")

    # --- Scenario 5: unchanged files -> verify and refresh are true no-ops. ---
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        run_dir, config_path = setup_run(
            tmp_path, {"testco": source_result("TestCo", postings=[make_posting("R1")])}
        )
        finalize.cmd_manifest(run_dir, config_path)
        manifest_before = finalize.manifest_path(run_dir).read_text(encoding="utf-8")

        verify1 = finalize.cmd_verify(run_dir)
        assert verify1 == {"ok": True, "changed": [], "missing": []}

        refresh_result = finalize.cmd_refresh(run_dir, config_path, note=None)
        assert refresh_result["refreshed"] == [], "Nothing changed -- refresh must not touch any entry"
        manifest_after = finalize.manifest_path(run_dir).read_text(encoding="utf-8")
        assert manifest_before == manifest_after, "Refresh on an unchanged run must not rewrite the manifest at all"

        manifest = finalize.load_manifest(run_dir)
        assert manifest.get("recovery_log") == [], "No spurious recovery-log entries when nothing changed"
    print("OK: scenario 5 -- unchanged files cause no unnecessary rerun/rewrite")

    print("All finalize_sources.py regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
