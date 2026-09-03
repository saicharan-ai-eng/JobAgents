#!/usr/bin/env python3
"""Regression tests for the deterministic US-only location eligibility filter
(scripts/us_location_filter.py) and its wiring into classify_dedupe_report.py.
"""

from __future__ import annotations

import importlib.util
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


us_loc = _load("us_location_filter_test", "us_location_filter.py")
classifier = _load("classifier_us_location_test", "classify_dedupe_report.py")


def assert_included(location: str, msg: str = "") -> None:
    result = us_loc.evaluate_us_location(location)
    assert result["us_location_eligible"] is True, (
        f"Expected {location!r} to be US-eligible ({msg}); got {result}"
    )


def assert_excluded(location: str, msg: str = "") -> None:
    result = us_loc.evaluate_us_location(location)
    assert result["us_location_eligible"] is False, (
        f"Expected {location!r} to be excluded ({msg}); got {result}"
    )


def make_job(title: str, location: str, **overrides) -> dict:
    job = {
        "company": overrides.pop("company", "TestCo"),
        "job_title": title,
        "job_id": title,
        "location": location,
        "posting_date": "2026-08-05",
        "experience_level_text": overrides.pop("experience_level_text", "0-2 years of experience"),
        "job_url": "https://example.com/direct-job",
        "team_department": "Engineering",
        "short_description": overrides.pop(
            "short_description", "Build machine learning and AI infrastructure systems."
        ),
        "full_description_text": overrides.pop(
            "full_description_text", "Build machine learning and AI infrastructure systems."
        ),
    }
    job.update(overrides)
    return job


def main() -> int:
    tiers, body_signals, title_signals = classifier.load_fit_config(ROOT / "config" / "fit_priorities.json")

    # --- Direct location-string cases from the migration spec ---
    assert_included("Raleigh, NC", "explicit state abbreviation")
    assert_included("San Jose, California", "explicit full state name")
    assert_included("Washington, D.C.", "explicit D.C. marker")
    assert_included("Remote - US", "explicit US token")
    assert_included("Remote, United States", "explicit country phrase")
    assert_included("United States - Remote", "explicit country phrase")

    assert_excluded("Remote only", "no country/state marker")
    assert_excluded("Worldwide remote", "no country/state marker")
    assert_excluded("Global remote", "no country/state marker")
    assert_excluded("Multiple Locations", "no US marker at all")
    assert_excluded("Toronto, Canada", "non-US country")
    assert_excluded("Bengaluru, India", "non-US country")

    assert_included("London / New York", "at least one US-eligible segment (New York = state name)")
    assert_excluded("London / Toronto", "no segment carries a US marker")

    # US company, India-based role -> location field alone governs, company name/nationality irrelevant.
    india_role = make_job(
        "Software Engineer I, AI Infrastructure",
        "Bengaluru, Karnataka, India",
        company="American Innovations Corp (US Company)",
    )
    india_result = classifier.classify_posting(dict(india_role), tiers, body_signals, title_signals)
    assert india_result["us_location_eligible"] is False, india_result
    assert india_result["include"] is False, "US company with India-based role must be excluded"

    # US headquarters mentioned only in boilerplate description text, but the
    # actual location field is non-US -> must still exclude. Confirms the
    # filter never scans description/company text for stray US mentions.
    hq_boilerplate_role = make_job(
        "Machine Learning Engineer I",
        "Toronto, Canada",
        full_description_text=(
            "Our US headquarters is located in Santa Clara, California, and we are proud to be "
            "an equal opportunity employer operating globally. This role is based in our Toronto "
            "office and builds machine learning and AI pipelines."
        ),
        short_description="Builds machine learning and AI pipelines from our Toronto office.",
    )
    hq_result = classifier.classify_posting(dict(hq_boilerplate_role), tiers, body_signals, title_signals)
    assert hq_result["us_location_eligible"] is False, hq_result
    assert hq_result["include"] is False, "Boilerplate US HQ mention must not establish US eligibility"

    # USD salary language with a non-US location must not establish eligibility.
    usd_salary_role = make_job(
        "Machine Learning Engineer I",
        "Toronto, Canada",
        full_description_text=(
            "This role builds machine learning and AI infrastructure. Compensation: $120,000 - "
            "$150,000 USD annually."
        ),
    )
    usd_result = classifier.classify_posting(dict(usd_salary_role), tiers, body_signals, title_signals)
    assert usd_result["us_location_eligible"] is False, usd_result
    assert usd_result["include"] is False, "USD salary language must not establish US eligibility"

    # A state abbreviation embedded inside an unrelated word must not match:
    # "ME" (Maine) must not fire inside "MEXICO", "IN" (Indiana) must not
    # fire inside "Inverness".
    assert_excluded("Mexico City, CDMX, Mexico", "'ME' inside 'MEXICO' must not match Maine")
    assert_excluded("Inverness, Scotland, United Kingdom", "'IN' inside 'Inverness' must not match Indiana")

    # --- Additional real-world source-format coverage ---
    assert_included("US, CA, Santa Clara", "Workday-style US country-code prefix")
    assert_included("Austin, TX, United States", "suffix full country name")
    assert_included("Charlotte, North Carolina, United States of America", "full state + full country suffix")
    assert_included("Remote US NC", "HPE-style Remote US <state> with no commas")
    assert_included(
        "US, CA, Santa Clara; US, MA, Westford; US, TX, Austin; US, NC, Durham",
        "multi-location, all segments US",
    )
    assert_included(
        "Addison, TX, USA; Atlanta, GA, USA; +3 more",
        "multi-location with a truncated '+N more' tail must not block eligibility",
    )
    assert_included(
        "United States, Washington, Redmond | United States, California, Mountain View",
        "pipe-separated multi-location, all US",
    )
    assert_included(
        "Bengaluru, Karnataka, India; Sunnyvale, CA, USA",
        "multi-location with at least one explicit US segment must include",
    )

    assert_excluded("Remote Ireland", "non-US remote country")
    assert_excluded("Remote UK", "non-US remote country")
    assert_excluded("Home based - Worldwide", "no US marker")
    assert_excluded("Home based - EMEA", "no US marker")
    assert_excluded(
        "Israel, Tel Aviv; Israel, Yokneam; Israel, Raanana",
        "multi-location, no segment is US",
    )
    assert_excluded(
        "India, Multiple Locations, Multiple Locations | Singapore, Singapore, Singapore",
        "multi-location, no segment is US",
    )
    assert_excluded("Georgia", "bare 'Georgia' is ambiguous with the country, not the US state")
    assert_included("Georgia, USA", "explicit USA marker resolves the Georgia ambiguity")

    # --- State-abbreviation / ISO country-code collisions (found 2026-08-18
    # via a real Eightfold/Microsoft-Azure posting: "Herzliya, Tel Aviv
    # District, IL; IL" was misread as Illinois when IL is Israel's ISO
    # 3166-1 alpha-2 code). A bare 2-letter token match must not be trusted
    # for abbreviations that collide with a real country code unless the
    # segment is in the strict two-part "City, ST" USPS shape. ---
    assert_excluded(
        "Herzliya, Tel Aviv District, IL; IL",
        "IL is Israel's ISO country code here, not Illinois -- 3-part and bare-segment shapes",
    )
    assert_excluded(
        "Bengaluru, Karnataka, IN",
        "IN is India's ISO country code in a 3-part shape, not Indiana",
    )
    assert_excluded(
        "Toronto, Ontario, CA",
        "CA is Canada's ISO country code in a 3-part shape, not California",
    )
    assert_excluded("IL", "a bare standalone ISO country code segment must not match Illinois")
    assert_excluded("CA", "a bare standalone ISO country code segment must not match California")

    # The classic USPS "City, ST" two-part shape must still work for every
    # ambiguous abbreviation -- this is the overwhelmingly common real-world
    # US format and must not regress.
    assert_included("Los Angeles, CA", "2-part USPS shape must still resolve to California")
    assert_included("Chicago, IL", "2-part USPS shape must still resolve to Illinois")
    # Known residual limitation: a strict 2-part "City, CC" shape using a
    # bare ISO country code is indistinguishable from "City, ST" without a
    # city/country gazetteer, so it is trusted like any other state code.
    # The observed real-world bug (Eightfold's 3-part "City, Region, CC" and
    # bare-segment shapes) is fully closed; this narrower gap is not.
    assert_included("Bengaluru, IN", "2-part shape trusted at face value -- documented residual gap")
    assert_included(
        "Herzliya, IL, USA",
        "explicit USA marker overrides the IL ambiguity regardless of shape",
    )

    # --- classify_posting integration: a real US posting must carry all four
    # required fields and pass the include gate; a non-US posting of
    # identical content must be excluded solely on location grounds. ---
    us_job = make_job("Machine Learning Engineer I", "Raleigh, NC")
    us_result = classifier.classify_posting(dict(us_job), tiers, body_signals, title_signals)
    for field in ("us_location_eligible", "us_location_reason", "normalized_us_locations", "location_inferred"):
        assert field in us_result, f"classify_posting output missing required field {field!r}"
    assert us_result["us_location_eligible"] is True
    assert us_result["include"] is True
    assert "non_us_or_ambiguous_location" not in (us_result["exclusion_reasons"] or [])

    non_us_job = make_job("Machine Learning Engineer I", "Toronto, Canada")
    non_us_result = classifier.classify_posting(dict(non_us_job), tiers, body_signals, title_signals)
    assert non_us_result["us_location_eligible"] is False
    assert non_us_result["include"] is False
    assert "non_us_or_ambiguous_location" in (non_us_result["exclusion_reasons"] or [])

    print("All US-location eligibility tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
