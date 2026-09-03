#!/usr/bin/env python3
"""Regression tests for the ALT_PATH_CONNECTOR structural-bridge fix.

Root cause: resolve_required_years() used to search the *entire* span of
text between two year-requirement matches for a bare `\\bor\\b`, so any
unrelated "or" in a technology list ("Python or Java"), noun-phrase list
("cloud or on-premises"), or Oxford-comma list ("CUDA, C++, or Python") was
misread as proof of two alternative qualification paths, silently replacing
a governing senior year figure with a smaller unrelated number found later
in the same qualifications block.

The fix (GENUINE_ALT_PATH_BRIDGE) requires the *entire* connector zone
between the two year matches to be nothing but the connector itself (a
degree phrase, "or"/"alternatively", and a little clause-tail filler) -- not
merely contain a connector word somewhere in a longer unrelated sentence.

This file only reads a single archived raw posting (read-only, from an
existing run's raw/ directory) to reclassify one fixture job; it does not
scrape, notify, write to any run directory, or touch state/.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

SPEC = importlib.util.spec_from_file_location("alt_path_classifier", SCRIPTS / "classify_dedupe_report.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load classify_dedupe_report.py")
classifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(classifier)


def make_job(title: str, description: str, experience: str | None = None) -> dict[str, str]:
    return {
        "company": "TestCo",
        "job_title": title,
        "job_id": title,
        "location": "Remote",
        "posting_date": "2026-08-06",
        "experience_level_text": experience,
        "job_url": "https://example.com/direct-job",
        "team_department": "Engineering",
        "short_description": description[:120],
        "full_description_text": description,
    }


def main() -> int:
    tiers, body_signals, title_signals = classifier.load_fit_config(ROOT / "config" / "fit_priorities.json")

    # 1. 15+ years followed by an unrelated "cloud or on-premises" phrase,
    # plus a smaller *unrelated* year mention elsewhere -> governing figure
    # must remain 15, not fall to the smaller number.
    text_1 = (
        "15+ years of experience architecting platforms for cloud or on-premises systems, "
        "including 3 years of experience with Terraform."
    )
    resolved_1 = classifier.resolve_required_years(text_1)
    assert resolved_1 is not None and resolved_1[0] == 15, (
        f"'cloud or on-premises' must not create a false alt-path; expected 15, got {resolved_1}"
    )

    # 2. "3 years with Python or Java" must resolve to a single 3-year
    # requirement, not be split into alternative paths by the "or".
    text_2a = "3 years of experience with Python or Java."
    resolved_2a = classifier.resolve_required_years(text_2a)
    assert resolved_2a is not None and resolved_2a[0] == 3, (
        f"'Python or Java' alone must resolve to a single 3-year requirement, got {resolved_2a}"
    )
    # Reinforced: even with a second, later, *unrelated* (sentence-separated)
    # year mention, the in-list "or" must not bridge the two sentences --
    # this is a single stacked requirement -> take the governing max (5).
    text_2b = "3 years of experience with Python or Java. 5 years of experience with distributed systems."
    resolved_2b = classifier.resolve_required_years(text_2b)
    assert resolved_2b is not None and resolved_2b[0] == 5, (
        f"Unrelated 'or' inside one sentence must not bridge to a later sentence's year figure; "
        f"expected stacked max 5, got {resolved_2b}"
    )

    # 3. Bachelor's + 3 years OR Master's + 1 year -> valid minimum path is 1.
    text_3 = "Bachelor's degree and 3 years of experience OR Master's degree and 1 year of experience."
    resolved_3 = classifier.resolve_required_years(text_3)
    assert resolved_3 is not None and resolved_3[0] == 1, (
        f"Genuine degree-alternative bridge must take the minimum path (1), got {resolved_3}"
    )

    # 4. 5 years OR advanced degree + 2 years -> valid minimum path is 2.
    text_4 = "5 years of experience, or 2 years of experience with an advanced degree."
    resolved_4 = classifier.resolve_required_years(text_4)
    assert resolved_4 is not None and resolved_4[0] == 2, (
        f"Genuine ', or' bridge must take the minimum path (2), got {resolved_4}"
    )

    # 5. Nested "5 years, including 3 years, including 2 years" -> stays 5
    # (no alt-path connector at all -- "including" is a stacking word).
    text_5 = (
        "5 years of experience, including 3 years of experience with Python, "
        "including 2 years of experience with Go."
    )
    resolved_5 = classifier.resolve_required_years(text_5)
    assert resolved_5 is not None and resolved_5[0] == 5, (
        f"Nested 'including' clauses must stack to the governing max (5), got {resolved_5}"
    )

    # 6. Existing repo baseline cases (from test_fit_ranking.py) must still hold.
    baseline_nested = classifier.resolve_required_years(
        "5 years of experience with software development. "
        "3 years of experience with ML infrastructure. "
        "2 years of experience developing compilers."
    )
    assert baseline_nested is not None and baseline_nested[0] == 5, baseline_nested
    baseline_alt = classifier.resolve_required_years(
        "Bachelor's Degree AND 2+ years experience OR Master's Degree AND 0+ years experience"
    )
    assert baseline_alt is not None and baseline_alt[0] == 0, baseline_alt

    # 7. No Staff/Principal/Senior/Lead/Director/Manager role is admitted
    # solely because of an unrelated "or" -- the title-based SENIOR_TITLE
    # gate (independent of year-parsing) must still exclude these, and the
    # year-parsing fix must not accidentally under-resolve their real
    # requirement either.
    senior_titles = [
        "Staff Software Engineer, AI Platform",
        "Principal Machine Learning Engineer",
        "Senior AI Infrastructure Engineer",
        "Lead GPU Systems Engineer",
        "Director of AI Engineering",
        "Engineering Manager, ML Platform",
    ]
    for title in senior_titles:
        job = make_job(
            title,
            "15+ years of experience in software engineering, platform engineering, or developer "
            "productivity roles, with at least 3 years at a principal, staff, or architect level. "
            "Deep experience with AI or ML systems.",
        )
        result = classifier.classify_posting(dict(job), tiers, body_signals, title_signals)
        assert result["include"] is False, f"Senior title must be excluded regardless of stray 'or': {title}"

    # 8. Fixture reclassification of the one affected real posting: Lenovo,
    # job_id WD00102643 ("Consulting Engineer, AI Developer Productivity" /
    # actually "Principal Architect, AI Developer Productivity" per its full
    # description). Read-only from the already-archived raw/ file of a past
    # run; does not scrape, notify, write any run output, or touch state/.
    lenovo_raw_path = ROOT / "runs" / "20260806T142509Z" / "raw" / "lenovo.json"
    lenovo_raw = json.loads(lenovo_raw_path.read_text(encoding="utf-8"))
    lenovo_job = next(
        j for j in lenovo_raw["postings"] if j.get("job_id") == "WD00102643"
    )
    resolved_lenovo = classifier.resolve_required_years(classifier.combine_text(lenovo_job))
    assert resolved_lenovo is not None and resolved_lenovo[0] == 15, (
        f"Lenovo Principal Architect posting must resolve to 15+ years, got {resolved_lenovo}"
    )
    lenovo_result = classifier.classify_posting(dict(lenovo_job), tiers, body_signals, title_signals)
    assert lenovo_result["include"] is False, (
        f"Lenovo Principal Architect posting must remain excluded, got include={lenovo_result['include']}"
    )
    assert lenovo_result["experience_required"] is not None and "15" in lenovo_result["experience_required"], (
        f"Lenovo posting's recorded experience_required must reflect 15+ years, "
        f"got {lenovo_result['experience_required']!r}"
    )
    assert "experience_not_qualifying" in (lenovo_result["exclusion_reasons"] or []), lenovo_result["exclusion_reasons"]

    print(f"Lenovo fixture: include={lenovo_result['include']}, "
          f"experience_required={lenovo_result['experience_required']!r}, "
          f"level_classification={lenovo_result['level_classification']!r}")
    print("All ALT_PATH_CONNECTOR regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
