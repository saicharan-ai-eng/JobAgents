#!/usr/bin/env python3
"""Regression tests for the open-ended experience-requirement fix.

Root cause (original, 2026-08-xx): `_year_matches()` extracted the bare
number out of PLUS ("5+ years") and MINIMUM ("at least 5 years" / "minimum
of 5 years") matches and treated it exactly like a bounded figure. Since
`classify_experience()` only checked `years <= 5`, a posting stating an
*unbounded floor* of "5+ years" was wrongly treated as equivalent to a
*bounded maximum* of "up to 5 years" and passed the entry-level filter --
even though "5+ years" imposes no ceiling at all.

Root cause (2026-08-15 tightening): the original fix still only excluded an
open-ended floor when its own number was >= the cap (e.g. "3+ years" and
above), so a *sub-cap* open-ended floor like "1+ years" or "2+ years" was
still wrongly treated as qualifying -- production run 20260814T150341Z
included several jobs on exactly this basis. Per CLAUDE.md's Experience
rules, a mandatory requirement must have a genuine BOUNDED upper bound of
<=MAX_REQUIRED_EXPERIENCE_YEARS to qualify; an open-ended lower bound is
never bounded, no matter how low its floor is. `_figure_qualifies()` now
enforces this categorically: `(not open_ended) and years <= cap`. Genuine
alternative-qualification paths (see GENUINE_ALT_PATH_BRIDGE /
`_prefer_alt_candidate`) are unaffected -- a genuinely bounded branch at or
under the cap still makes the posting eligible even if a different branch in
the same alternative is open-ended or over the cap.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

SPEC = importlib.util.spec_from_file_location("open_ended_classifier", SCRIPTS / "classify_dedupe_report.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load classify_dedupe_report.py")
classifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(classifier)


def make_job(description: str, title: str = "Software Engineer, AI Infrastructure") -> dict[str, str]:
    return {
        "company": "TestCo",
        "job_title": title,
        "job_id": title,
        "location": "Remote - United States",
        "posting_date": "2026-08-11",
        "experience_level_text": None,
        "job_url": "https://example.com/direct-job",
        "team_department": "Engineering",
        "short_description": description[:200],
        "full_description_text": description,
    }


def main() -> int:
    # 1. The named regression: "5+ years" must NOT be treated as a bounded
    # maximum of 5 -- it must be excluded.
    job = make_job("Required Qualifications: 5+ years of experience with distributed systems and machine learning.")
    ok, level, raw, inferred = classifier.classify_experience(job)
    assert ok is False, f"'5+ years' must be excluded (open-ended, not bounded-5), got include={ok}"
    assert raw == "5+ years", raw

    # 2. Equivalent open-ended phrasings must behave the same way.
    for phrase in ("at least 5 years", "minimum of 5 years"):
        job = make_job(f"Required Qualifications: {phrase} of experience with distributed systems.")
        ok, level, raw, inferred = classifier.classify_experience(job)
        assert ok is False, f"{phrase!r} must be excluded (open-ended floor of 5), got include={ok}"

    # 3. Any higher open-ended minimum must also exclude (unchanged prior
    # behavior, still covered so the boundary fix doesn't regress it).
    job = make_job("Required Qualifications: 8+ years of experience with distributed systems.")
    ok, level, raw, inferred = classifier.classify_experience(job)
    assert ok is False, f"'8+ years' must be excluded, got include={ok}"

    # 4. 2026-08-14 <=3-year policy migration (see CLAUDE.md "Experience"):
    # the cap moved from 5 to classifier.MAX_REQUIRED_EXPERIENCE_YEARS (3).
    # 2026-08-15 tightening: EVERY open-ended floor excludes now, regardless
    # of how far below the cap its number is -- "0+"/"1+"/"2+" years are just
    # as excluded as "3+"/"4+" years, since none of them state a genuine
    # bounded upper limit.
    assert classifier.MAX_REQUIRED_EXPERIENCE_YEARS == 3, classifier.MAX_REQUIRED_EXPERIENCE_YEARS
    for phrase in ("0+ years", "1+ years", "2+ years", "3+ years", "4+ years"):
        job = make_job(f"Required Qualifications: {phrase} of experience with distributed systems.")
        ok, level, raw, inferred = classifier.classify_experience(job)
        assert ok is False, f"{phrase!r} is an open-ended floor and must exclude regardless of its number, got include={ok}"

    # 5. A bounded range with max exactly at the cap must pass; a bounded
    # range whose max exceeds the cap must fail -- even though neither is
    # open-ended (RANGE matches are never open_ended).
    job = make_job("Required Qualifications: 0-3 years of experience with distributed systems.")
    ok, level, raw, inferred = classifier.classify_experience(job)
    assert ok is True, f"'0-3 years' is a bounded range with max exactly at the cap and must pass, got include={ok}"
    job = make_job("Required Qualifications: 2-4 years of experience with distributed systems.")
    ok, level, raw, inferred = classifier.classify_experience(job)
    assert ok is False, f"'2-4 years' is a bounded range whose max (4) exceeds the cap and must exclude, got include={ok}"

    # 6. A plain stated figure of exactly the cap (not phrased as a floor)
    # must still pass -- SINGLE matches are not open-ended; one year above
    # the cap must fail even though it is equally not open-ended.
    job = make_job("Required Qualifications: 3 years of experience with distributed systems.")
    ok, level, raw, inferred = classifier.classify_experience(job)
    assert ok is True, f"'3 years of experience' (SINGLE, exactly at the cap) must pass, got include={ok}"
    job = make_job("Required Qualifications: 4 years of experience with distributed systems.")
    ok, level, raw, inferred = classifier.classify_experience(job)
    assert ok is False, f"'4 years of experience' (SINGLE, one above the cap) must exclude, got include={ok}"

    # 7. Stacked requirement: "3+ years... 5+ years..." -> governing max is
    # the open-ended 5+ -> must exclude.
    job = make_job(
        "Required Qualifications: 3+ years of experience with Python. "
        "5+ years of experience with distributed systems architecture."
    )
    ok, level, raw, inferred = classifier.classify_experience(job)
    assert ok is False, f"Stacked requirement governed by open-ended 5+ must exclude, got include={ok}"

    # 8. resolve_required_years() itself now returns a 3-tuple with the
    # open_ended flag as the third element.
    resolved = classifier.resolve_required_years("5+ years of experience.")
    assert resolved is not None and len(resolved) == 3, resolved
    assert resolved[0] == 5 and resolved[2] is True, resolved
    resolved = classifier.resolve_required_years("5 years of experience.")
    assert resolved is not None and resolved[0] == 5 and resolved[2] is False, resolved

    # 9. Bounded exact figures at 1 and 2 years must qualify (0 and 3 are
    # already covered by test_experience_threshold_3yr.py's ACCEPT matrix;
    # repeated here since this file is the dedicated open-ended/bounded
    # regression suite).
    for phrase, expected_years in (("1 year", 1), ("2 years", 2)):
        job = make_job(f"Required Qualifications: {phrase} of experience with distributed systems.")
        ok, level, raw, inferred = classifier.classify_experience(job)
        assert ok is True, f"exact {phrase!r} is a bounded figure at or under the cap and must qualify, got include={ok}"

    # 10. Explicit junior/new-grad wording does not rescue a mandatory
    # Required-Qualifications open-ended floor, even a sub-cap one like
    # "2+ years" -- this is the exact production bug (run 20260814T150341Z
    # included "System Software Engineer - Tegra" and similar NVIDIA
    # postings on this basis). No genuine alternative path is present here.
    junior_with_mandatory_2plus = make_job(
        "Required Qualifications: 2+ years of experience with distributed systems.",
        title="Software Engineer I, AI Infrastructure - Entry Level",
    )
    ok, level, raw, inferred = classifier.classify_experience(junior_with_mandatory_2plus)
    assert ok is False, (
        f"Junior-titled posting with a mandatory open-ended '2+ years' and no genuine alternative "
        f"path must exclude, got include={ok}, level={level!r}, raw={raw!r}"
    )

    # 11. Same junior wording, but this time the Required Qualifications text
    # presents a genuine alternative path where one branch is a real bounded
    # figure at or under the cap ("Bachelor's + 2+ years OR High School
    # Diploma + 3 years") -- the genuine bounded branch (3) rescues
    # eligibility even though the other branch (2+) is open-ended.
    junior_with_genuine_alt_path = make_job(
        "Required Qualifications: Bachelor's degree and 2+ years of experience with distributed systems, "
        "or High School Diploma and 3 years of experience with distributed systems.",
        title="Software Engineer I, AI Infrastructure - Entry Level",
    )
    ok, level, raw, inferred = classifier.classify_experience(junior_with_genuine_alt_path)
    assert ok is True, (
        f"Junior-titled posting with a genuine alternative path offering a bounded 3-year branch "
        f"must qualify via that branch even though the other branch (2+) is open-ended, got include={ok}"
    )

    # 12. resolve_required_years() alt-path selection must prefer a
    # genuinely-qualifying bounded branch over a numerically-smaller but
    # open-ended one -- "2+ years OR 3 years" must resolve to the bounded 3,
    # not the open-ended 2 (see _prefer_alt_candidate). Checked both
    # orderings since a genuine bridge must be order-independent.
    resolved = classifier.resolve_required_years(
        "Bachelor's degree and 2+ years of experience, or Master's degree and 3 years of experience."
    )
    assert resolved is not None and resolved[0] == 3 and resolved[2] is False, (
        f"genuine alt path '2+ OR 3' must resolve to the bounded qualifying branch (3, not open-ended), got {resolved}"
    )
    resolved = classifier.resolve_required_years(
        "Bachelor's degree and 3 years of experience, or Master's degree and 2+ years of experience."
    )
    assert resolved is not None and resolved[0] == 3 and resolved[2] is False, (
        f"genuine alt path '3 OR 2+' (reversed order) must still resolve to the bounded qualifying "
        f"branch (3), got {resolved}"
    )

    print("All open-ended experience regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
