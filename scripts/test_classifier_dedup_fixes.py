#!/usr/bin/env python3
"""Regression tests for the Canonical-run audit fix:

- deduplication fails closed and never emits more records than were included
- marketing/ads roles are excluded
- sales/solutions-engineer roles need a real hands-on AI/ML/GPU signal
- generic infra (kernel/observability/containers/dev-tooling) needs a real
  AI/ML/GPU signal describing actual responsibilities, not boilerplate
- a genuine Edge AI engineering role still qualifies
- report.md / deduplicated.json / notification.json counts are internally
  consistent (no drift between what's reported and what's on disk)
"""

from __future__ import annotations

import importlib.util
import json
import re
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


classifier = _load("classifier_dedup_fixes", "classify_dedupe_report.py")
dedup_lib = _load("dedup_lib_dedup_fixes", "dedup_lib.py")


def make_job(title: str, description: str, experience: str = "0-2 years of experience", **overrides) -> dict[str, str]:
    job = {
        "company": "Canonical",
        "job_title": title,
        "job_id": overrides.get("job_id", title),
        "location": "Raleigh, NC",
        "posting_date": "2026-01-01",
        "experience_level_text": experience,
        "job_url": "https://job-boards.greenhouse.io/canonical/jobs/0000000",
        "team_department": overrides.get("team_department", "Engineering"),
        "short_description": description[:120],
        "full_description_text": description,
    }
    job.update({k: v for k, v in overrides.items() if k not in {"job_id", "team_department"}})
    return job


def run_classify(job: dict[str, str], tiers, body_signals, title_signals) -> dict:
    return classifier.classify_posting(dict(job), tiers, body_signals, title_signals)


def main() -> int:
    tiers, body_signals, title_signals = classifier.load_fit_config(ROOT / "config" / "fit_priorities.json")

    # 1. 91 excluded + 6 included -> exactly 6 deduplicated, each unique.
    filtered = []
    for i in range(91):
        filtered.append(
            {
                "company": "TestCo",
                "job_id": f"excluded-{i}",
                "job_title": f"Excluded Role {i}",
                "location": "Remote",
                "include": False,
                "fit_priority": 4,
                "fit_score": 0,
                "posting_date": "2026-01-01",
            }
        )
    for i in range(6):
        filtered.append(
            {
                "company": "TestCo",
                "job_id": f"included-{i}",
                "job_title": f"Included Role {i}",
                "location": "Remote",
                "include": True,
                "fit_priority": 2,
                "fit_score": 50,
                "posting_date": "2026-01-01",
            }
        )
    deduped = dedup_lib.deduplicate_included(filtered)
    assert len(deduped) == 6, f"Expected 6 deduplicated records, got {len(deduped)}"
    assert all(job["include"] is True for job in deduped), "Deduplicated output must only contain include==true"

    # 1b. Dedup must fail closed if a non-included record would leak through.
    corrupted = list(filtered)
    corrupted.append(
        {
            "company": "TestCo",
            "job_id": "included-0",  # collides with an included record's key
            "job_title": "Included Role 0",
            "location": "Remote",
            "include": True,
            "fit_priority": 4,
            "fit_score": 999999,  # would "win" the merge if logic were broken
            "posting_date": "2026-01-01",
        }
    )
    # This should still produce exactly 6 (same key, higher score wins) --
    # verifying the invariant check doesn't false-positive on legitimate merges.
    deduped2 = dedup_lib.deduplicate_included(corrupted)
    assert len(deduped2) == 6, f"Legitimate merge should still yield 6, got {len(deduped2)}"

    try:
        dedup_lib.deduplicate_included("not a list")  # type: ignore[arg-type]
        raise AssertionError("Expected DedupInvariantError for non-list input")
    except dedup_lib.DedupInvariantError:
        pass

    # 2. Junior Ads Specialist (marketing/advertising) must be excluded.
    ads_job = make_job(
        "Junior Ads Specialist",
        "Support marketing team members with setting up and monitoring paid ads campaigns. "
        "Build on automation and AI to improve paid ads performance. Develop the adtech stack.",
        team_department="Marketing",
    )
    ads_result = run_classify(ads_job, tiers, body_signals, title_signals)
    assert ads_result["role_function"] == "marketing", f"Expected marketing, got {ads_result['role_function']}"
    assert ads_result["include"] is False, "Junior Ads Specialist must be excluded"
    assert "marketing_role_excluded" in (ads_result["exclusion_reasons"] or [])

    # 3. Generic sales-engineer text (AI/ML only as an "interest", no hands-on
    # implementation requirement) must not qualify.
    sales_eng_job = make_job(
        "Ubuntu Sales Engineer (Entry-Level)",
        "We are hiring a Sales Engineer to help customers adopt our open source platform. "
        "You will deliver presentations and demos, collect customer requirements, and work with "
        "the sales team to close deals. Interest in private clouds, machine learning and AI, data "
        "and analytics. Intermediate level Python programming skills.",
    )
    sales_eng_result = run_classify(sales_eng_job, tiers, body_signals, title_signals)
    assert sales_eng_result["role_function"] == "customer_facing_technical", sales_eng_result["role_function"]
    assert sales_eng_result["include"] is False, "Generic sales-engineer role must not qualify"
    assert "customer_facing_role_lacks_hands_on_ai_ml_gpu_signal" in (sales_eng_result["exclusion_reasons"] or [])

    # A sales/solutions engineer that genuinely requires hands-on AI/ML/GPU
    # implementation must still qualify.
    real_sales_eng_job = make_job(
        "Sales Engineer - AI Inference",
        "You will work hands-on with customers to build, deploy, and optimize GPU inference "
        "pipelines using our AI/ML platform, including live proof-of-concept implementations.",
    )
    real_sales_eng_result = run_classify(real_sales_eng_job, tiers, body_signals, title_signals)
    assert real_sales_eng_result["include"] is True, "Genuinely hands-on AI solutions engineer must qualify"

    # 4. Generic Linux/kernel/container/observability work, without a specific
    # AI/ML/GPU signal describing actual responsibilities, must not qualify
    # merely because company boilerplate mentions "AI" elsewhere.
    boilerplate_intro = (
        "Canonical is a leading provider of open source software and operating systems to the "
        "global enterprise and technology markets. Our platform, Ubuntu, is very widely used in "
        "breakthrough enterprise initiatives such as public cloud, data science, AI, engineering "
        "innovation, and IoT.\n\n"
    )
    boilerplate_outro = (
        "\n\nCanonical is a pioneering tech firm at the forefront of the global move to open source. "
        "As the company that publishes Ubuntu, one of the most important open source projects and "
        "the platform for AI, IoT and the cloud, we are changing the world.\n\n"
        "Canonical is an equal opportunity employer\n\n"
        "We are proud to foster a workplace free from discrimination."
    )

    kernel_job = make_job(
        "Ubuntu Linux Kernel Engineer - Silicon Enablement",
        boilerplate_intro
        + "There is strong demand from silicon manufacturers such as NVIDIA, Xilinx, MediaTek, and "
        "Qualcomm to provide Ubuntu Linux to their customers. You will diagnose and resolve issues "
        "in the kernel, submit and review kernel patches, and improve tooling and automation for "
        "kernel delivery."
        + boilerplate_outro,
    )
    kernel_result = run_classify(kernel_job, tiers, body_signals, title_signals)
    assert kernel_result["include"] is False, (
        f"Kernel/silicon-enablement role with only boilerplate/vendor-list AI mentions must not qualify: "
        f"{kernel_result['relevance_keywords_matched']}"
    )

    observability_job = make_job(
        "Junior Software Developer - Observability",
        boilerplate_intro
        + "You will develop a cloud-native monitoring stack that composes best-in-class open-source "
        "monitoring tools on Kubernetes and OpenStack. You will write, test, and document high "
        "quality code, debug issues, and review code produced by other engineers."
        + boilerplate_outro,
    )
    observability_result = run_classify(observability_job, tiers, body_signals, title_signals)
    assert observability_result["include"] is False, (
        f"Generic observability/Kubernetes role with only boilerplate AI mentions must not qualify: "
        f"{observability_result['relevance_keywords_matched']}"
    )

    dev_tooling_job = make_job(
        "Go (Golang) Software Engineer, Developer Tooling and Containers",
        boilerplate_intro
        + "You will build a developer experience tool deeply integrated with modern IDEs and SDKs "
        "from publishers like NVIDIA, Intel, AMD and others, freeing developers from tedious setup "
        "across multiple industry domains such as Robotics, MLOps, IoT. "
        "Nice-to-have skills: Experience with AI/ML and/or CUDA/OpenVINO."
        + boilerplate_outro,
    )
    dev_tooling_result = run_classify(dev_tooling_job, tiers, body_signals, title_signals)
    assert dev_tooling_result["include"] is False, (
        f"Dev-tooling role where AI/ML/CUDA only appears as a vendor name-drop or a nice-to-have "
        f"must not qualify: {dev_tooling_result['relevance_keywords_matched']}"
    )

    # 5. A genuine Edge AI engineering role (real inference/accelerator
    # responsibilities, not boilerplate) must remain included.
    edge_ai_job = make_job(
        "Software Engineer - Edge AI",
        boilerplate_intro
        + "Your role will be to help us provide easy-to-deploy, secure, and customizable edge AI "
        "solutions, specifically focusing on Inference Snaps. You will design and implement "
        "silicon-optimized application packages for AI/ML inference, develop and maintain "
        "open-source AI/ML inference application packages, and build optimized AI solutions for "
        "local inferencing on Ubuntu. Hardware accelerators: GPU, NPU."
        + boilerplate_outro,
    )
    edge_ai_result = run_classify(edge_ai_job, tiers, body_signals, title_signals)
    assert edge_ai_result["include"] is True, "Genuine Edge AI inference role must remain included"
    assert "Inference" in edge_ai_result["relevance_keywords_matched"], edge_ai_result["relevance_keywords_matched"]

    # 6. report.md / deduplicated.json counts must be internally consistent.
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        raw_dir = run_dir / "raw"
        raw_dir.mkdir()
        postings = [ads_job, sales_eng_job, kernel_job, observability_job, dev_tooling_job, edge_ai_job, real_sales_eng_job]
        (raw_dir / "canonical.json").write_text(
            json.dumps(
                {
                    "company": "Canonical",
                    "source_url": "https://example.com",
                    "fetched_at": "2026-01-01T00:00:00Z",
                    "status": "success",
                    "reason": None,
                    "postings": postings,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "classify_dedupe_report.py"), "--run-dir", str(run_dir)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"classify_dedupe_report.py failed: {proc.stderr}"

        deduped_on_disk = json.loads((run_dir / "deduplicated.json").read_text(encoding="utf-8"))
        report_text = (run_dir / "report.md").read_text(encoding="utf-8")
        m = re.search(r"Final deduplicated postings: (\d+)", report_text)
        assert m is not None, "report.md missing 'Final deduplicated postings' line"
        reported_count = int(m.group(1))
        assert reported_count == len(deduped_on_disk), (
            f"report.md says {reported_count} deduplicated postings but deduplicated.json has "
            f"{len(deduped_on_disk)}"
        )
        assert len(deduped_on_disk) == 2, (
            f"Expected exactly 2 qualifying postings (Edge AI + real hands-on solutions engineer), "
            f"got {len(deduped_on_disk)}: {[j['job_title'] for j in deduped_on_disk]}"
        )
        titles = {j["job_title"] for j in deduped_on_disk}
        assert titles == {"Software Engineer - Edge AI", "Sales Engineer - AI Inference"}, titles

        # 7. Priority breakdown in notification.json must sum exactly to new_job_count.
        state_file = run_dir / "seen_jobs.json"
        proc2 = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "detect_new_jobs.py"),
                "--run-dir",
                str(run_dir),
                "--state-file",
                str(state_file),
                "--config",
                str(run_dir / "no_such_config.json"),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert proc2.returncode == 0, f"detect_new_jobs.py failed: {proc2.stderr}"
        notification = json.loads((run_dir / "notification.json").read_text(encoding="utf-8"))
        breakdown_sum = sum(notification["new_jobs_by_priority"].values())
        assert breakdown_sum == notification["new_job_count"], (
            f"Priority breakdown sums to {breakdown_sum} but new_job_count is "
            f"{notification['new_job_count']}"
        )

    print("All classifier/dedup audit-fix regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
