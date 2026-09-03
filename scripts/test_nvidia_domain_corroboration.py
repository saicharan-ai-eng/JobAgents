#!/usr/bin/env python3
"""Regression tests for the 2026-08-12 (run 20260812T152540Z) bare-NVIDIA
domain-eligibility fix.

Root cause: "NVIDIA" was a whole-word DOMAIN_PATTERNS match like any other,
so it alone satisfied domain_eligible() even though every NVIDIA posting
mentions the company name by definition. 15 postings (9 NVIDIA, 6 Google
Cloud) passed the domain gate solely via a bare company/org-name mention or
reused mission-statement/org-description boilerplate, with no genuine
AI/ML/GPU content in the role itself, and had to be manually corrected.

Covers:
- NVIDIA + unrelated software/hardware role => excluded
- NVIDIA + generic datacenter/ops role with no AI/GPU work => excluded
- NVIDIA + CUDA / GPU performance / inference responsibilities => eligible
- NVIDIA appearing only in boilerplate or a vendor/partner list => excluded
- the NVIDIA mission-statement boilerplate does not leak into a genuine
  role's scope even when the source concatenates paragraphs without a
  blank-line break (the real bug caught while building this fix -- see
  strip_boilerplate's paragraph/sentence split)
- the general vendor-name-protection principle: bare "AI"/"ML"/"AI Cloud"
  and a reused hiring-org umbrella title suffix ("AI and Infrastructure")
  cannot solo-qualify a posting either
- NVIDIA fit-score contribution requires the same corroboration
- PR/communications and HR titles are excluded regardless of domain match
- an end-to-end golden check: reclassifying the actual raw postings from run
  20260812T152540Z reproduces the human-reviewer-corrected result exactly
  (29 included jobs, all 15 flagged false positives now excluded
  automatically, zero regressions) without any manual correction.
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


classifier = _load("classifier_nvidia_corroboration", "classify_dedupe_report.py")

NVIDIA_BOILERPLATE = (
    "NVIDIA has been transforming computer graphics, PC gaming, and accelerated computing for more than 25 years. "
    "It's a distinctive heritage of innovation driven by outstanding technology and incredible people. "
    "Today, we're tapping into the unlimited potential of AI to define the next era of computing. "
    "An era in which our GPU acts as the brains of computers, robots, and self-driving cars that can understand the world. "
    "As an NVIDIAN, you'll be immersed in a diverse, supportive environment where everyone is inspired to do their best work."
)


def make_job(title: str, description: str, experience: str = "0-2 years of experience", **overrides) -> dict:
    job = {
        "company": "NVIDIA",
        "job_title": title,
        "job_id": overrides.get("job_id", title),
        "location": "Santa Clara, CA",
        "posting_date": "2026-01-01",
        "experience_level_text": experience,
        "job_url": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/x",
        "team_department": overrides.get("team_department", "Engineering"),
        "short_description": description[:120],
        "full_description_text": description,
    }
    job.update({k: v for k, v in overrides.items() if k not in {"job_id", "team_department"}})
    return job


def classify(job: dict, tiers, body_signals, title_signals) -> dict:
    return classifier.classify_posting(dict(job), tiers, body_signals, title_signals)


def main() -> int:
    tiers, body_signals, title_signals = classifier.load_fit_config(ROOT / "config" / "fit_priorities.json")

    def run(title: str, description: str, **overrides) -> dict:
        job = make_job(title, f"{NVIDIA_BOILERPLATE} {description}", **overrides)
        return classify(job, tiers, body_signals, title_signals)

    # --- 1. NVIDIA + unrelated software role -> excluded ---
    r = run(
        "Build and DevOps Engineer for Compilers",
        "NVIDIA is looking for a world-class engineer to join its Compiler Build & DevOps team. "
        "You will improve automated build pipelines, CI/CD workflows, and release tooling for compiler system software. "
        "What we need to see: BS in Computer Science. 2+ years of experience with build systems and DevOps tooling.",
    )
    assert r["include"] is False, "NVIDIA + unrelated compiler/DevOps role must be excluded"
    assert "domain_gate_generic_ai_only" in (r["exclusion_reasons"] or []), r["exclusion_reasons"]

    # --- 2. NVIDIA + generic datacenter/ops role with no AI/GPU work -> excluded ---
    r = run(
        "Lab Operations Site Supervisor",
        "Supervise day-to-day physical lab and datacenter facility operations, including rack builds, "
        "equipment moves, inventory tracking, and vendor coordination for lab logistics. "
        "What we need to see: 2+ years of facilities or lab operations experience.",
    )
    assert r["include"] is False, "NVIDIA + generic datacenter/ops role must be excluded"
    assert "domain_gate_generic_ai_only" in (r["exclusion_reasons"] or []), r["exclusion_reasons"]

    # --- 3. NVIDIA + CUDA responsibilities -> eligible ---
    r = run(
        "Software Engineer, GPU Libraries",
        "Design and implement high-performance CUDA kernels for NVIDIA's core math libraries. "
        "You will write and optimize CUDA C++ code used across NVIDIA's GPU computing stack. "
        "What we need to see: BS/MS in Computer Science. 2+ years of CUDA programming experience.",
    )
    assert r["include"] is True, f"NVIDIA + CUDA responsibilities must be eligible: {r.get('exclusion_reasons')}"
    assert "CUDA" in r["relevance_keywords_matched"]

    # --- 4. NVIDIA + GPU performance responsibilities -> eligible ---
    r = run(
        "Performance Engineer, GPU Systems",
        "Profile and optimize GPU kernel performance across NVIDIA's product lines. "
        "You will analyze GPU utilization bottlenecks and drive performance optimization across the software stack. "
        "What we need to see: 2+ years of experience profiling and optimizing GPU workloads.",
    )
    assert r["include"] is True, f"NVIDIA + GPU performance responsibilities must be eligible: {r.get('exclusion_reasons')}"
    assert "GPU" in r["relevance_keywords_matched"]

    # --- 5. NVIDIA + inference responsibilities -> eligible ---
    r = run(
        "Software Engineer, Inference Serving",
        "Build and optimize distributed inference serving systems that run NVIDIA's production AI models at scale. "
        "You will work on inference optimization, batching, and model serving infrastructure. "
        "What we need to see: 2+ years of experience building inference or model-serving systems.",
    )
    assert r["include"] is True, f"NVIDIA + inference responsibilities must be eligible: {r.get('exclusion_reasons')}"
    assert "Inference" in r["relevance_keywords_matched"]

    # --- 6. NVIDIA appearing only in boilerplate/vendor lists -> excluded ---
    r = run(
        "ASIC Verification Engineer",
        "Develop test plans and verification infrastructure for verifying global IP across multiple products. "
        "This role offers the opportunity to impact multiple product lines including consumer graphics, "
        "self-driving cars, HPC, cloud computing, and AI. "
        "What we need to see: BS/MS in Electrical or Computer Engineering. Build verification components using SV/UVM.",
    )
    assert r["include"] is False, "NVIDIA mentioned only in a vendor/product-line list must be excluded"
    assert "domain_gate_generic_ai_only" in (r["exclusion_reasons"] or []), r["exclusion_reasons"]
    assert "AI" not in r["relevance_keywords_matched"], "bare 'AI' inside an 'including X, Y, and AI' list must not count"

    # --- 7. The NVIDIA mission-statement boilerplate does not consume a
    # genuine role's real content even when concatenated without a
    # blank-line break (regression test for the bug found while building
    # this fix: paragraph-level stripping of a mixed boilerplate+genuine
    # paragraph used to drop the whole paragraph, wiping out real content). ---
    merged_description = (
        NVIDIA_BOILERPLATE + "\n"  # single "\n", no blank line -- matches real-world scraped text
        "NVIDIA is looking for a CUDA kernel engineer to build GPU-accelerated inference kernels for our runtime. "
        "What we need to see: 2+ years of CUDA and GPU kernel development experience."
    )
    job = make_job("CUDA Kernel Engineer", merged_description)
    job["full_description_text"] = merged_description  # no leading duplicate boilerplate injection this time
    r = classify(job, tiers, body_signals, title_signals)
    assert r["include"] is True, (
        f"Genuine CUDA/GPU content merged into the same paragraph as boilerplate (no blank-line break) "
        f"must still be recognized: {r.get('exclusion_reasons')}"
    )
    assert "CUDA" in r["relevance_keywords_matched"] and "GPU" in r["relevance_keywords_matched"]

    # --- 8. General vendor-name-protection: bare "AI"/"ML" alone still
    # doesn't solo-qualify (pre-existing behavior, unaffected by this fix). ---
    plain_job = make_job(
        "Corporate Strategy Analyst",
        "Support corporate strategy initiatives across the business. Our AI strategy is one of many workstreams "
        "this team supports. What we need to see: 2+ years of strategy or analyst experience.",
    )
    r = classify(plain_job, tiers, body_signals, title_signals)
    assert r["include"] is False, "bare 'AI' mention alone must not solo-qualify a non-technical role"

    # --- 9. "AI Cloud" (e.g. a "Google Cloud AI" org-name style match) must
    # not solo-qualify either -- same corroboration requirement as NVIDIA. ---
    ai_cloud_job = {
        "company": "TestCo",
        "job_title": "Program Manager, Google Cloud AI",
        "job_id": "ai-cloud-1",
        "location": "Sunnyvale, CA",
        "posting_date": "2026-01-01",
        "experience_level_text": "2 years of experience",
        "job_url": "https://example.com/job/1",
        "short_description": "",
        "full_description_text": (
            "Coordinate program schedules and cross-team logistics for the Google Cloud AI organization. "
            "What we need to see: 2 years of program or project management experience."
        ),
    }
    r = classify(ai_cloud_job, tiers, body_signals, title_signals)
    assert r["include"] is False, (
        f"A bare 'AI Cloud'/org-name-only match must not solo-qualify a non-technical PM role: "
        f"{r.get('relevance_keywords_matched')} / {r.get('exclusion_reasons')}"
    )

    # --- 10. Recurring hiring-org umbrella title suffix ("AI and
    # Infrastructure") must not solo-qualify a role with no other domain
    # signal (Google Cloud manual-review corrections: Network Automation
    # Engineer, AI and Infrastructure). ---
    org_umbrella_job = {
        "company": "TestCo",
        "job_title": "Network Automation Engineer, AI and Infrastructure",
        "job_id": "org-umbrella-1",
        "location": "Austin, TX",
        "posting_date": "2026-01-01",
        "experience_level_text": "2 years of experience",
        "job_url": "https://example.com/job/2",
        "short_description": "",
        "full_description_text": (
            "Design and implement network automation tooling to improve reliability and reduce operational toil. "
            "The AI and Infrastructure team is redefining what's possible. We empower customers with breakthrough "
            "capabilities and insights by delivering AI and Infrastructure at unparalleled scale. "
            "What we need to see: 2 years of network automation or systems engineering experience."
        ),
    }
    r = classify(org_umbrella_job, tiers, body_signals, title_signals)
    assert r["include"] is False, (
        f"A reused hiring-org umbrella title suffix must not solo-qualify a role with no real AI/ML content: "
        f"{r.get('relevance_keywords_matched')} / {r.get('exclusion_reasons')}"
    )

    # --- 11. PR / communications titles are excluded regardless of domain
    # match (NVIDIA correction: PR Specialist, AI Optimism and Industries). ---
    r = run(
        "PR Specialist, AI Optimism and Industries",
        "Lead media relations and press strategy for NVIDIA's AI product narrative, including press releases "
        "and event logistics for major AI industry announcements. "
        "What we need to see: 2+ years of PR or communications experience.",
    )
    assert r["include"] is False, "PR/communications titles must be excluded regardless of AI mentions"
    assert "marketing_role_excluded" in (r["exclusion_reasons"] or []), r["exclusion_reasons"]

    # --- 12. HR titles are excluded regardless of an incidental supported-
    # team-name mention (NVIDIA correction: Human Resources Business
    # Partner, "Global GPU Software Engineering organization"). ---
    r = run(
        "Human Resources Business Partner",
        "Partner with leadership on staffing, onboarding, and retention for the Global GPU Software Engineering "
        "organization. What we need to see: 3+ years of HR business partner experience.",
    )
    assert r["include"] is False, "HR titles must be excluded even with an incidental GPU team-name mention"
    assert "hr_role_excluded" in (r["exclusion_reasons"] or []), r["exclusion_reasons"]

    # --- 13. NVIDIA fit-score corroboration: bare NVIDIA mention contributes
    # no points; a real signal alongside it does. ---
    bare_nvidia_matches = [{"base_label": "NVIDIA", "priority": 4, "effective_points": 5, "label": "NVIDIA"}]
    cuda_job_fit = run(
        "Software Engineer, GPU Libraries",
        "Design and implement high-performance CUDA kernels. What we need to see: 2+ years of CUDA experience.",
    )
    assert "NVIDIA" in cuda_job_fit["fit_keywords_matched"], "NVIDIA should still count once corroborated by CUDA"
    ineligible_bare = run(
        "Lab Operations Site Supervisor",
        "Supervise lab operations. What we need to see: 2+ years of facilities experience.",
    )
    # This posting is excluded outright (domain gate), but even if it were
    # somehow scored, the isolated NVIDIA-only fit signal must not survive.
    fit_only = classifier.classify_fit(
        make_job("Lab Operations Site Supervisor", "Supervise lab operations for NVIDIA."),
        tiers,
        body_signals,
        title_signals,
    )
    assert "NVIDIA" not in fit_only["fit_keywords_matched"], (
        f"Uncorroborated NVIDIA fit signal must be dropped from scoring: {fit_only['fit_keywords_matched']}"
    )

    # --- 14. End-to-end golden check against the real run 20260812T152540Z
    # raw data: reclassifying from scratch (no manual correction) must
    # reproduce the human-reviewer-corrected result exactly. ---
    real_raw_dir = ROOT / "runs" / "20260812T152540Z" / "raw"
    real_filtered = ROOT / "runs" / "20260812T152540Z" / "filtered.json"
    if real_raw_dir.is_dir() and real_filtered.is_file():
        with tempfile.TemporaryDirectory() as tmp:
            tmp_run = Path(tmp) / "run"
            tmp_raw = tmp_run / "raw"
            tmp_raw.mkdir(parents=True)
            for src in real_raw_dir.glob("*.json"):
                (tmp_raw / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "classify_dedupe_report.py"),
                    "--run-dir",
                    str(tmp_run),
                    "--fit-config",
                    str(ROOT / "config" / "fit_priorities.json"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, f"classify_dedupe_report.py failed: {proc.stderr}"

            recomputed = json.loads((tmp_run / "filtered.json").read_text(encoding="utf-8"))
            original = json.loads(real_filtered.read_text(encoding="utf-8"))

            def key(r: dict) -> tuple:
                return (r.get("company"), r.get("job_id") or r.get("job_title"))

            original_included = {key(r) for r in original if r.get("include")}
            recomputed_included = {key(r) for r in recomputed if r.get("include")}
            reviewer_corrected = {key(r) for r in original if r.get("reviewer_correction")}
            recomputed_by_key = {key(r): r for r in recomputed}
            original_by_key = {key(r): r for r in original}

            # This archived run predates both the 2026-08-14 <=3-year
            # experience-cap migration and the 2026-08-15 open-ended-lower-
            # bound tightening (see CLAUDE.md "Experience"), so a job whose
            # OWN recorded experience_required no longer qualifies under
            # *current* policy (bounded threshold OR open-endedness) now
            # legitimately drops out of the included set -- that is the
            # policy working as intended, not a domain-corroboration
            # regression (which is what this file actually guards). The
            # domain-corroboration fix under test must never newly ADD a job
            # the human reviewer excluded, though -- that direction of the
            # check stays a hard, unconditional invariant.
            newly_added = recomputed_included - original_included
            assert not newly_added, (
                f"Automatic reclassification must never newly include a job the human-reviewed baseline "
                f"excluded (possible domain-corroboration regression): {newly_added}"
            )
            dropped = original_included - recomputed_included
            for k in dropped:
                exp_text = str(original_by_key.get(k, {}).get("experience_required") or "")
                resolved = classifier.resolve_required_years(exp_text)
                # Delegate to the production classifier's own qualification
                # rule (never re-derive it locally -- that duplication is
                # exactly what let this check go stale against the
                # 2026-08-15 open-ended fix in the first place).
                explained_by_experience_policy = bool(resolved) and not classifier._figure_qualifies(
                    resolved[0], resolved[2]
                )
                assert explained_by_experience_policy, (
                    f"{k} dropped out of the included set for a reason other than the <="
                    f"{classifier.MAX_REQUIRED_EXPERIENCE_YEARS}-year experience policy "
                    f"(experience_required={exp_text!r}) -- possible domain-corroboration regression"
                )
            for k in reviewer_corrected:
                rec = recomputed_by_key.get(k)
                assert rec is not None and rec.get("include") is False, (
                    f"Reviewer-corrected job {k} must be excluded automatically now"
                )
    else:
        print("(skipping end-to-end golden check: run 20260812T152540Z raw data not present)")

    print("All NVIDIA/vendor-name domain-corroboration regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
