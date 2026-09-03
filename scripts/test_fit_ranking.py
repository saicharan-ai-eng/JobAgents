#!/usr/bin/env python3
"""Small deterministic test suite for career-fit ranking and eligibility."""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "classify_dedupe_report.py"
SPEC = importlib.util.spec_from_file_location("classifier", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {MODULE_PATH}")
classifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(classifier)


def make_job(title: str, description: str, experience: str = "0-2 years of experience") -> dict[str, str]:
    return {
        "company": "TestCo",
        "job_title": title,
        "job_id": title,
        "location": "Raleigh, NC",
        "posting_date": "2026-08-05",
        "experience_level_text": experience,
        "job_url": "https://example.com/direct-job",
        "team_department": "Engineering",
        "short_description": description,
        "full_description_text": description,
    }


def main() -> int:
    tiers, body_signals, title_signals = classifier.load_fit_config(ROOT / "config" / "fit_priorities.json")

    # --- Original baseline tier cases ---
    cases = [
        (
            make_job(
                "AI Inference Engineer I",
                "Build vLLM model serving systems with KV cache optimization and distributed inference.",
            ),
            1,
        ),
        (
            make_job(
                "GPU Software Engineer I",
                "Develop CUDA GPU kernels using Triton and NCCL for accelerated computing.",
            ),
            2,
        ),
        (
            make_job(
                "Machine Learning Engineer I",
                "Build machine learning and MLOps pipelines on Kubernetes.",
            ),
            3,
        ),
        (
            make_job(
                "Software Engineer I",
                "Develop backend services for an AI-powered product.",
            ),
            4,
        ),
    ]

    for job, expected_priority in cases:
        experience_ok, *_ = classifier.classify_experience(job)
        domain_matches = classifier.classify_domain(job)
        fit = classifier.classify_fit(job, tiers, body_signals, title_signals)
        assert experience_ok, f"Expected early-career eligibility: {job['job_title']}"
        assert domain_matches, f"Expected domain eligibility: {job['job_title']}"
        assert fit["fit_priority"] == expected_priority, (
            f"{job['job_title']}: expected P{expected_priority}, got P{fit['fit_priority']} "
            f"({fit['fit_keywords_matched']})"
        )

    senior = make_job(
        "Senior AI Inference Engineer",
        "Optimize vLLM and TensorRT-LLM inference.",
        "7+ years of experience",
    )
    assert not classifier.classify_experience(senior)[0], "Senior role must be excluded"

    false_positive = make_job("Software Engineer I", "Own detailed retail reporting and email services.")
    assert not classifier.classify_domain(false_positive), "AI must not match inside unrelated words"

    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "ok.txt"
        path.write_text("ranking tests passed\n", encoding="utf-8")
        assert path.read_text(encoding="utf-8").strip() == "ranking tests passed"

    # --- Audit-fix regression tests ---

    # 1. Nested/stacked year requirements (AND) -> take the governing maximum.
    nested_text = (
        "5 years of experience with software development. "
        "3 years of experience with ML infrastructure. "
        "2 years of experience developing compilers."
    )
    resolved = classifier.resolve_required_years(nested_text)
    assert resolved is not None and resolved[0] == 5, f"Nested years: expected 5, got {resolved}"

    # 2. Alternative degree paths (OR) -> take the minimum valid path.
    alt_path_text = (
        "Bachelor's Degree AND 2+ years experience OR Master's Degree AND 0+ years experience"
    )
    resolved = classifier.resolve_required_years(alt_path_text)
    assert resolved is not None and resolved[0] == 0, f"Alt path: expected 0, got {resolved}"

    # 3. "non-internship" must not be classified as an internship.
    assert classifier.EXPLICIT_JUNIOR.search("3+ years of non-internship professional experience") is None, (
        "'non-internship' must not match the internship carve-out regex"
    )
    non_intern_job = make_job(
        "Software Development Engineer, AWS",
        "Basic Qualifications: 3+ years of non-internship professional software development experience. "
        "2+ years of non-internship design experience.",
        "3+ years of non-internship professional software development experience",
    )
    _exp_ok, level, _req, _inferred = classifier.classify_experience(non_intern_job)
    assert level != "Internship", f"non-internship phrasing must not classify as Internship, got {level}"

    # 4a. A bare "AI" mention alone, with a non-AI-signaling title, must fail the domain gate.
    boilerplate_job = make_job(
        "Business Operations Coordinator",
        "At Amazon, our culture is powered by AI.",
        "1+ years experience",
    )
    domain_matches = classifier.classify_domain(boilerplate_job)
    assert not classifier.domain_eligible(domain_matches, boilerplate_job["job_title"]), (
        "Bare 'AI' mention alone with a non-AI-signaling title must not pass the domain gate"
    )

    # 4b. Sales roles must be excluded even when AI/ML appears in boilerplate.
    sales_job = make_job(
        "Account Executive, SMB",
        "At Amazon, our culture is powered by AI. You will drive sales pipeline growth and "
        "close deals with SMB customers.",
        "entry-level",
    )
    sales_result = classifier.classify_posting(dict(sales_job), tiers, body_signals, title_signals)
    assert sales_result["role_function"] == "sales", f"Expected role_function=sales, got {sales_result['role_function']}"
    assert sales_result["include"] is False, "Sales roles must be excluded"
    assert "sales_role_excluded" in (sales_result["exclusion_reasons"] or []), (
        "Exclusion reason must record sales_role_excluded"
    )

    # 5. "Gen AI" (space-separated) and "Gen-AI" must be detected.
    genai_job = make_job(
        "Software Engineer",
        "We leverage Gen AI and Gen-AI tooling to build agentic systems.",
    )
    genai_domain = classifier.classify_domain(genai_job)
    assert "Generative AI" in genai_domain, f"'Gen AI' spacing variant not detected: {genai_domain}"
    genai_fit = classifier.classify_fit(genai_job, tiers, body_signals, title_signals)
    assert any("Generative AI" in label for label in genai_fit["fit_keywords_matched"]), (
        f"'Gen AI' should register as a Generative AI fit signal: {genai_fit['fit_keywords_matched']}"
    )

    # 6. Generic multi-discipline AWS recruiting funnel must be capped at Priority 4.
    funnel_job = make_job(
        "Software Development Engineer, AWS",
        "Amazon Web Services (AWS) is building a central pipeline of Software Development Engineer "
        "(SDE) talent for anticipated roles in 2026. This requisition supports hiring across all AWS "
        "SDE positions, from fungible SDE roles to specialized engineering positions in areas including "
        "Embedded Systems, Compiler Engineering, Artificial Intelligence/Machine Learning, Firmware "
        "Development, and Distributed Systems. Basic Qualifications: 2+ years of non-internship "
        "professional software development experience. Preferred Qualifications: Knowledge of ML "
        "frameworks including JAX, PyTorch, vLLM, SGLang, Dynamo, TorchXLA, and TensorRT.",
        "2+ years of non-internship professional software development experience",
    )
    funnel_result = classifier.classify_posting(dict(funnel_job), tiers, body_signals, title_signals)
    assert funnel_result["generic_funnel_detected"] is True, "Generic recruiting funnel was not detected"
    assert funnel_result["fit_priority"] == 4, (
        f"Generic recruiting funnel must be capped at Priority 4, got P{funnel_result['fit_priority']}"
    )

    # 7. A specific AWS Neuron/Inferentia role must remain Priority 1 or 2 (not swept up by the funnel cap).
    neuron_job = make_job(
        "Software Engineer, AWS Neuron Distributed Training - Performance Optimization",
        "AWS Neuron is the complete software stack for AWS Trainium and Inferentia cloud-scale machine "
        "learning accelerators. This role is responsible for distributed training performance "
        "optimization and inference for large multi-modal language models using PyTorch.",
        "2+ years of non-internship professional experience",
    )
    neuron_result = classifier.classify_posting(dict(neuron_job), tiers, body_signals, title_signals)
    assert neuron_result["fit_priority"] in (1, 2), (
        f"Specific Neuron/Inferentia role should remain P1/P2, got P{neuron_result['fit_priority']}"
    )
    assert neuron_result["generic_funnel_detected"] is False

    # 8. "AI infrastructure" alone (body + title) must not create Priority 1.
    ai_infra_only_job = make_job(
        "Cloud Solution Architect - Cloud & AI Infrastructure",
        "You will help customers migrate and modernize with AI infrastructure and cloud infrastructure.",
        "2+ years experience",
    )
    ai_infra_fit = classifier.classify_fit(ai_infra_only_job, tiers, body_signals, title_signals)
    assert ai_infra_fit["fit_priority"] != 1, (
        f"'AI infrastructure' alone must not create Priority 1, got P{ai_infra_fit['fit_priority']}"
    )

    # 9. Standalone "distributed systems" alone must not create Priority 2.
    dist_sys_only_job = make_job(
        "Software Development Engineer II",
        "You will build distributed systems at massive scale. This role focuses on storage placement.",
        "1+ years experience",
    )
    dist_sys_fit = classifier.classify_fit(dist_sys_only_job, tiers, body_signals, title_signals)
    assert dist_sys_fit["fit_priority"] != 2, (
        f"Standalone 'distributed systems' alone must not create Priority 2, got P{dist_sys_fit['fit_priority']}"
    )

    # 10. Preferred-only vLLM/SGLang/TensorRT signals must receive reduced (40%) weight vs. Required-section signals.
    preferred_only_job = make_job(
        "Software Development Engineer",
        "Required Qualifications: 2+ years of experience with distributed systems. "
        "Preferred Qualifications: Knowledge of ML frameworks including JAX, PyTorch, vLLM, SGLang, "
        "Dynamo, TorchXLA, and TensorRT.",
        "2+ years experience",
    )
    required_section_job = make_job(
        "Software Development Engineer",
        "Required Qualifications: 2+ years of experience with vLLM, SGLang, and TensorRT for model serving.",
        "2+ years experience",
    )
    pref_fit = classifier.classify_fit(preferred_only_job, tiers, body_signals, title_signals)
    req_fit = classifier.classify_fit(required_section_job, tiers, body_signals, title_signals)
    assert pref_fit["fit_score"] < req_fit["fit_score"], (
        f"Preferred-only signals ({pref_fit['fit_score']}) should score lower than Required-section "
        f"signals ({req_fit['fit_score']})"
    )
    assert any("(preferred)" in label for label in pref_fit["fit_keywords_matched"]), (
        f"Preferred-only matches should be labeled as such: {pref_fit['fit_keywords_matched']}"
    )

    # 11. "Hands-on experience in X" (a qualifications bullet about the candidate) must NOT
    # count as the role itself involving hands-on implementation -- a consulting/CFT/support
    # role should still be capped even when this phrasing appears.
    consultant_job = make_job(
        "Delivery Consultant - Data",
        "You'll work closely with customers to design, implement, and manage AWS solutions. "
        "Basic Qualifications: Hands-on experience in developing scalable, high-performance data "
        "pipelines and performance optimization work with machine learning workloads.",
        "3+ years experience",
    )
    consultant_result = classifier.classify_posting(dict(consultant_job), tiers, body_signals, title_signals)
    assert consultant_result["fit_priority"] >= 3, (
        f"Consulting role with only a candidate-side 'hands-on experience' qualification bullet "
        f"must still be capped at P3+, got P{consultant_result['fit_priority']}"
    )
    # But a role that itself describes hands-on implementation work must NOT be capped.
    hands_on_sa_job = make_job(
        "Solutions Architect, LLM Model Builder",
        "You will act as both a strategic technical expert and a hands-on advisor, helping partners "
        "build, benchmark, fine-tune, optimize, and deploy foundation model solutions using vLLM and "
        "TensorRT-LLM for production inference.",
        "5+ years experience",
    )
    hands_on_sa_result = classifier.classify_posting(dict(hands_on_sa_job), tiers, body_signals, title_signals)
    assert hands_on_sa_result["fit_priority"] == 1, (
        f"Genuinely hands-on customer-facing role must not be capped, got P{hands_on_sa_result['fit_priority']}"
    )

    print("All fit-ranking tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
