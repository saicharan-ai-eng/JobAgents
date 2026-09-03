#!/usr/bin/env python3
"""Regression tests for the 2026-08-14 <=3-year experience-policy migration.

Root change: CLAUDE.md's bounded early-career experience cap moved from 5
years to 3 years (`classify_dedupe_report.MAX_REQUIRED_EXPERIENCE_YEARS`).
This is not a blind literal substitution -- the boundary semantics (bounded
vs. open-ended, required vs. preferred, stacked vs. alternative-qualification
paths) are unchanged; only the threshold value moved, plus one new guard:
explicit junior/early-career wording no longer unconditionally overrides a
contradictory MANDATORY (Required-Qualifications-scoped) year figure above
the cap.

Covers the exact ACCEPT/REJECT matrix, required-vs-preferred precedence,
alternative-qualification-path safety, and the needs_review.json ambiguity
signals this migration introduces (see CLAUDE.md "Deterministic-first
processing and the review queue").

2026-08-15 addendum: the "boundary semantics... unchanged" note above no
longer holds for open-ended floors below the cap -- see
test_open_ended_experience.py for the full regression coverage of that
tightening ("1+"/"2+" years now reject, not just "3+" and above).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

SPEC = importlib.util.spec_from_file_location("threshold_3yr_classifier", SCRIPTS / "classify_dedupe_report.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load classify_dedupe_report.py")
classifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(classifier)


def make_job(description: str, title: str = "Software Engineer, AI Infrastructure", **overrides) -> dict:
    job = {
        "company": "TestCo",
        "job_title": title,
        "job_id": overrides.get("job_id", title),
        "location": "Remote - United States",
        "posting_date": "2026-08-14",
        "experience_level_text": None,
        "job_url": "https://example.com/direct-job",
        "team_department": "Engineering",
        "short_description": description[:200],
        "full_description_text": description,
    }
    job.update({k: v for k, v in overrides.items() if k not in {"job_id"}})
    return job


def assert_qualifies(description: str, msg: str) -> None:
    job = make_job(f"Required Qualifications: {description} of experience with distributed systems.")
    ok, level, raw, inferred = classifier.classify_experience(job)
    assert ok is True, f"{msg} ({description!r}) must qualify under the <=3-year cap, got include={ok}, raw={raw!r}"


def assert_rejects(description: str, msg: str) -> None:
    job = make_job(f"Required Qualifications: {description} of experience with distributed systems.")
    ok, level, raw, inferred = classifier.classify_experience(job)
    assert ok is False, f"{msg} ({description!r}) must be rejected under the <=3-year cap, got include={ok}, raw={raw!r}"


def main() -> int:
    assert classifier.MAX_REQUIRED_EXPERIENCE_YEARS == 3, classifier.MAX_REQUIRED_EXPERIENCE_YEARS

    # --- MUST QUALIFY ---------------------------------------------------
    assert_qualifies("0 years", "exact 0 years")
    assert_qualifies("1 year", "exact 1 year")
    assert_qualifies("2 years", "exact 2 years")
    assert_qualifies("3 years", "exact 3 years")
    assert_qualifies("0-3 years", "0-3 years bounded range")
    assert_qualifies("1-3 years", "1-3 years bounded range")
    assert_qualifies("2-3 years", "2-3 years bounded range")

    up_to_job = make_job("Required Qualifications: up to 3 years of experience with distributed systems.")
    ok, level, raw, inferred = classifier.classify_experience(up_to_job)
    assert ok is True, f"'up to 3 years' must qualify, got include={ok}"

    alt_path_5_or_3 = make_job(
        "Required Qualifications: Bachelor's degree and 5 years of experience OR Master's degree and "
        "3 years of experience."
    )
    ok, level, raw, inferred = classifier.classify_experience(alt_path_5_or_3)
    assert ok is True, f"Bachelor's + 5 OR Master's + 3 must qualify via the 3-year alt path, got include={ok}"
    resolved = classifier.resolve_required_years(classifier.combine_text(alt_path_5_or_3))
    assert resolved is not None and resolved[0] == 3, (
        f"Bachelor's + 5 OR Master's + 3 must resolve to the minimum genuine path (3), got {resolved}"
    )

    alt_path_4_or_2 = make_job(
        "Required Qualifications: Bachelor's degree and 4 years of experience OR Master's degree and "
        "2 years of experience."
    )
    ok, level, raw, inferred = classifier.classify_experience(alt_path_4_or_2)
    assert ok is True, f"Bachelor's + 4 OR Master's + 2 must qualify via the 2-year alt path, got include={ok}"

    # --- MUST FAIL --------------------------------------------------------
    assert_rejects("4 years", "exact 4 years")
    assert_rejects("5 years", "exact 5 years")
    assert_rejects("2-4 years", "2-4 years bounded range (max exceeds cap)")
    assert_rejects("3-4 years", "3-4 years bounded range (max exceeds cap)")
    assert_rejects("3-5 years", "3-5 years bounded range (max exceeds cap)")
    assert_rejects("4-5 years", "4-5 years bounded range (max exceeds cap)")
    assert_rejects("4+ years", "4+ years open-ended floor")
    assert_rejects("5+ years", "5+ years open-ended floor")
    assert_rejects("3+ years", "3+ years -- MUST BE EXCLUDED: an open-ended lower bound, not a bounded 3-year requirement")
    assert_rejects("at least 3 years", "'at least 3 years' open-ended floor")
    assert_rejects("minimum 3 years", "'minimum 3 years' used as an open-ended lower bound")
    assert_rejects("3 or more years", "'3 or more years' open-ended floor (OR_MORE pattern)")

    # 2026-08-15 tightening: a sub-cap open-ended floor no longer qualifies --
    # "2+ years" is excluded exactly like "3+ years", since neither states a
    # genuine bounded upper limit (see test_open_ended_experience.py for the
    # full regression coverage of this fix).
    assert_rejects("2+ years", "2+ years -- sub-cap open-ended floor, no genuine bounded upper limit")
    assert_rejects("1+ years", "1+ years -- sub-cap open-ended floor, no genuine bounded upper limit")

    # --- Required vs. preferred precedence --------------------------------

    # 1. required 2 years, preferred 5 years -> remains eligible (preferred
    # never overrides a passing required figure).
    req2_pref5 = make_job(
        "Required Qualifications: 2 years of experience with distributed systems. "
        "Preferred Qualifications: 5 years of experience with large-scale systems."
    )
    ok, level, raw, inferred = classifier.classify_experience(req2_pref5)
    assert ok is True, f"required 2 / preferred 5 must remain eligible, got include={ok}"

    # 2. required 4 years, preferred early-career wording -> reject (the new
    # contradiction guard: explicit junior wording anywhere in the posting
    # does not rescue a mandatory required-scope figure above the cap).
    req4_pref_entry_level = make_job(
        "Required Qualifications: 4 years of experience with distributed systems. "
        "Preferred Qualifications: Great for entry-level candidates who are eager to learn and grow."
    )
    ok, level, raw, inferred = classifier.classify_experience(req4_pref_entry_level)
    assert ok is False, (
        f"required 4 / preferred early-career wording must reject (mandatory figure governs), got include={ok}"
    )

    # 3. 3+ required, Master's preferred -> reject.
    req_3plus_pref_masters = make_job(
        "Required Qualifications: 3+ years of experience with distributed systems. "
        "Preferred Qualifications: Master's degree in Computer Science."
    )
    ok, level, raw, inferred = classifier.classify_experience(req_3plus_pref_masters)
    assert ok is False, f"3+ required / Master's preferred must reject, got include={ok}"

    # 4. Sanity: a genuine internship/new-grad posting with NO contradictory
    # required-scope figure is unaffected by the new guard.
    genuine_internship = make_job(
        "Required Qualifications: Currently pursuing a Bachelor's degree in Computer Science. "
        "Strong programming fundamentals.",
        title="Software Engineering Intern, Summer 2027",
    )
    ok, level, raw, inferred = classifier.classify_experience(genuine_internship)
    assert ok is True and level == "Internship", (
        f"Genuine internship with no contradictory required figure must remain eligible, got ok={ok}, level={level}"
    )

    # --- Alternative-qualification-path safety -----------------------------

    # An "or" connecting two qualifications in different bullets/sentences,
    # with no genuine bridge, must be read as a STACKED requirement (take
    # the max) -- never inferred as an alternative path merely because two
    # numbers happen to co-occur.
    bulleted = (
        "Required Qualifications:\n"
        "- 5 years of experience with backend systems.\n"
        "- 2 years of experience with Kubernetes."
    )
    resolved = classifier.resolve_required_years(bulleted)
    assert resolved is not None and resolved[0] == 5, (
        f"Two qualifications in separate bullets must stack to the max (5), not be read as an alt path: {resolved}"
    )
    bulleted_job = make_job(bulleted)
    ok, level, raw, inferred = classifier.classify_experience(bulleted_job)
    assert ok is False, f"Stacked bulleted requirement (max=5) must exclude under the 3-year cap, got include={ok}"

    unrelated_or = (
        "Required Qualifications: 5 years of experience with Python or Java. "
        "2 years of experience with distributed systems."
    )
    resolved = classifier.resolve_required_years(unrelated_or)
    assert resolved is not None and resolved[0] == 5, (
        f"'Python or Java' must not create a false alt-path bridge to an unrelated later figure: {resolved}"
    )

    # --- needs_review.json ambiguity signals (additive; never change the
    # deterministic include/exclude decision made above) --------------------

    # conflicting_required_qualifications: the same required-4/preferred-
    # entry-level posting above must also be flagged for optional review,
    # while its deterministic decision (excluded) is unchanged.
    flags = classifier.detect_ambiguous_signals(req4_pref_entry_level)
    reason_codes = [f["reason_code"] for f in flags]
    assert classifier.REASON_CONFLICTING_REQUIRED_QUALIFICATIONS in reason_codes, (
        f"required-4/preferred-entry-level posting must flag conflicting_required_qualifications: {reason_codes}"
    )

    # malformed_source_data: a posting with no description/experience text
    # at all must be flagged, never silently excluded with no explanation.
    empty_job = make_job("", title="Mystery Role")
    empty_job["short_description"] = None
    empty_job["full_description_text"] = None
    empty_job["experience_level_text"] = None
    flags = classifier.detect_ambiguous_signals(empty_job)
    reason_codes = [f["reason_code"] for f in flags]
    assert classifier.REASON_MALFORMED_SOURCE_DATA in reason_codes, (
        f"posting with no body text at all must flag malformed_source_data: {reason_codes}"
    )

    # A deterministically resolvable posting (the overwhelming majority of
    # real cases) must NOT be flagged -- the review queue stays empty for
    # straightforward cases like plain "2 years of experience".
    clean_job = make_job("Required Qualifications: 2 years of experience with distributed systems.")
    flags = classifier.detect_ambiguous_signals(clean_job)
    assert flags == [], f"A deterministically resolvable posting must not enter the review queue, got {flags}"

    # A genuinely structurally ambiguous posting -- a short, clause-break-
    # free connector containing a weak alternative-path hint ("or similar
    # hands-on experience") that does not match the strict genuine-bridge
    # pattern, where reading it as stacked vs. alternative flips the <=3-year
    # outcome -- must be flagged ambiguous_experience_path.
    ambiguous_job = make_job(
        "Required Qualifications: 2 years of experience or similar hands-on experience, "
        "5 years of experience with production systems."
    )
    flags = classifier.detect_ambiguous_signals(ambiguous_job)
    reason_codes = [f["reason_code"] for f in flags]
    assert classifier.REASON_AMBIGUOUS_EXPERIENCE_PATH in reason_codes, (
        f"structurally ambiguous stacked-vs-alternative posting must flag ambiguous_experience_path: {reason_codes}"
    )
    # The deterministic decision is still made regardless (additive, never
    # blocking) -- resolve_required_years' own stacking rule still governs.
    ok, level, raw, inferred = classifier.classify_experience(ambiguous_job)
    assert ok is False, "Ambiguity flagging must not block the deterministic decision from being made"

    print("All <=3-year experience-threshold regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
