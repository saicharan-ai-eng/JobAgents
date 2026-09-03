#!/usr/bin/env python3
"""Deterministic United States location eligibility filter.

Evaluates *only* a posting's structured ``location`` field. It never reads
job title, description, salary text, or company metadata, so headquarters
mentions, EEO/legal boilerplate, USD compensation strings, or the employer's
nationality can never establish US eligibility -- only an explicit United
States / USA / U.S. / state / Washington D.C. marker in the location field
itself can.

Fails closed: any location that cannot be positively matched to a US signal
is treated as not-US-eligible, including bare "Remote", "Global",
"Worldwide", or "Multiple Locations" with no accompanying US marker, and any
single-token city name with no state or country qualifier.
"""

from __future__ import annotations

import re
from typing import Any

US_STATE_NAMES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}

# "Georgia" collides with the country of Georgia; a bare mention must not be
# treated as the US state without an explicit US/USA/United States marker
# elsewhere in the same segment.
AMBIGUOUS_STATE_NAMES = {"georgia"}

US_STATE_ABBREVS = set(US_STATE_NAMES.values()) | {"DC"}

# Two-letter US state abbreviations that collide with a real ISO 3166-1
# alpha-2 country code (e.g. IL = Illinois or Israel, CA = California or
# Canada, IN = Indiana or India). Structured location fields from several
# ATS platforms (observed: Eightfold's "standardizedLocations") emit bare
# ISO country codes, which would otherwise be misread as US states. For
# these ambiguous codes only, a bare-token match is trusted solely in the
# strict USPS "City, ST" two-part comma shape (e.g. "Los Angeles, CA") --
# the overwhelmingly common real-world US format -- and rejected in a bare
# standalone segment ("IL") or a three-plus-part "City, Region, CC" shape
# ("Herzliya, Tel Aviv District, IL"), both of which are the actual shapes
# ISO country codes appear in. Fails closed per CLAUDE.md when ambiguous.
AMBIGUOUS_STATE_ABBREVS = {
    "AL", "AZ", "AR", "CA", "CO", "DE", "GA", "ID", "IL", "IN", "KY", "LA",
    "ME", "MD", "MA", "MN", "MS", "MO", "MT", "NE", "NC", "PA", "SC", "SD",
    "TN", "VA",
}

# Explicit country-level markers. Case-sensitive for the bare "US"/"USA"
# tokens (no legitimate location field uses a lowercase "us"), case-
# insensitive for the spelled-out phrase.
_UNITED_STATES_PHRASE = re.compile(r"\bunited states(?:\s+of\s+america)?\b", re.I)
_US_DOTTED = re.compile(r"(?<![A-Za-z])U\.S\.A?\.?(?![A-Za-z])")
_USA_TOKEN = re.compile(r"(?<![A-Za-z])USA(?![A-Za-z])")
_US_TOKEN = re.compile(r"(?<![A-Za-z])US(?![A-Za-z])")

_DC_PATTERN = re.compile(r"\bWashington,?\s*D\.?C\.?\b|\bDistrict of Columbia\b", re.I)

_STATE_ABBREV_TOKEN = re.compile(r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])")

_TRUNCATION_SUFFIX = re.compile(r"^\+\s*\d+\s+more$", re.I)

# Top-level multi-location separators used across the various source
# formats observed in raw postings ("|" and ";"), plus "/" for a compact
# slash-separated form.
_SEGMENT_SPLIT = re.compile(r"\s*[|;/]\s*")


def _has_explicit_country_marker(segment: str) -> bool:
    return bool(
        _UNITED_STATES_PHRASE.search(segment)
        or _US_DOTTED.search(segment)
        or _USA_TOKEN.search(segment)
        or _US_TOKEN.search(segment)
    )


def _find_state_abbrev(segment: str) -> str | None:
    comma_parts = [p.strip() for p in segment.split(",")]
    for token in _STATE_ABBREV_TOKEN.findall(segment):
        if token not in US_STATE_ABBREVS:
            continue
        if token in AMBIGUOUS_STATE_ABBREVS:
            # Only trust in the strict two-part "City, ST" shape; a bare
            # standalone segment or a 3+-part "City, Region, CC" shape is
            # the ISO-country-code convention, not a US postal address.
            if len(comma_parts) == 2 and comma_parts[-1].split()[:1] == [token]:
                return token
            continue
        return token
    lowered = segment.lower()
    for name, abbrev in US_STATE_NAMES.items():
        if name in AMBIGUOUS_STATE_NAMES:
            continue
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return abbrev
    return None


def _iter_segments(location: str) -> list[str]:
    cleaned = location.replace("﻿", "").strip()
    if not cleaned:
        return []
    return [s.strip() for s in _SEGMENT_SPLIT.split(cleaned) if s.strip()]


def _evaluate_segment(segment: str) -> tuple[bool, str | None, bool]:
    """Returns (is_us, normalized_label, is_explicit_country_marker)."""
    if _TRUNCATION_SUFFIX.match(segment):
        return False, None, False
    if _DC_PATTERN.search(segment):
        return True, "Washington, D.C.", True
    explicit = _has_explicit_country_marker(segment)
    state = _find_state_abbrev(segment)
    if explicit:
        return True, state or "United States", True
    if state:
        return True, state, False
    return False, None, False


def evaluate_us_location(location: Any) -> dict[str, Any]:
    """Evaluate a raw ``location`` field value for US eligibility.

    Fails closed: returns ``us_location_eligible: False`` whenever no
    segment carries a positive, explicit-enough US signal.
    """
    location_str = str(location).strip() if location not in (None, "") else ""
    if not location_str:
        return {
            "us_location_eligible": False,
            "us_location_reason": "No location data provided; cannot verify US eligibility.",
            "normalized_us_locations": [],
            "location_inferred": False,
        }

    segments = _iter_segments(location_str)
    if not segments:
        return {
            "us_location_eligible": False,
            "us_location_reason": f"Location {location_str!r} contained no parseable segment.",
            "normalized_us_locations": [],
            "location_inferred": False,
        }

    matches: list[tuple[str, bool]] = []
    for segment in segments:
        is_us, label, explicit = _evaluate_segment(segment)
        if is_us and label:
            matches.append((label, explicit))

    if not matches:
        return {
            "us_location_eligible": False,
            "us_location_reason": (
                f"No segment of location {location_str!r} carries an explicit United States, "
                "state/D.C., marker; treated as non-US or ambiguous and excluded (fail-closed)."
            ),
            "normalized_us_locations": [],
            "location_inferred": False,
        }

    normalized = sorted({label for label, _explicit in matches})
    location_inferred = not all(explicit for _label, explicit in matches)
    reason = (
        "US-eligible: matched "
        + ", ".join(normalized)
        + f" in location {location_str!r}."
    )
    return {
        "us_location_eligible": True,
        "us_location_reason": reason,
        "normalized_us_locations": normalized,
        "location_inferred": location_inferred,
    }


def annotate_us_location(job: dict[str, Any]) -> dict[str, Any]:
    """Return the four US-location fields for a posting dict, derived solely
    from ``job["location"]``."""
    return evaluate_us_location(job.get("location"))


# Reporting-only helper: buckets an excluded location into "explicitly
# non-US" (a real, identifiable non-US place) vs "ambiguous/unresolvable"
# (no location data, or only generic placeholders like "Remote"/"Global"/
# "Worldwide"/"Multiple Locations"/"Home based" with no identifiable place).
# This never affects eligibility -- it is purely a breakdown label for
# migration/run reporting.
_PLACEHOLDER_ONLY = re.compile(
    r"^(?:remote|global|worldwide|multiple locations|home based(?:\s*-\s*\w+)?|"
    r"remote\s*-?\s*(?:emea|worldwide|global)?)$",
    re.I,
)
_PUERTO_RICO = re.compile(r"\bpuerto rico\b", re.I)


def categorize_non_us_reason(location: Any) -> str:
    """Returns 'explicitly_non_us' or 'ambiguous_or_unresolvable' for a
    location that has already been determined not US-eligible."""
    location_str = str(location).strip() if location not in (None, "") else ""
    if not location_str:
        return "ambiguous_or_unresolvable"
    segments = _iter_segments(location_str)
    if not segments:
        return "ambiguous_or_unresolvable"
    identified_non_us = False
    for segment in segments:
        if _TRUNCATION_SUFFIX.match(segment):
            continue
        if _PLACEHOLDER_ONLY.match(segment.strip()):
            continue
        if _PUERTO_RICO.search(segment):
            identified_non_us = False
            continue
        # Any segment with actual alphabetic place content beyond a bare
        # placeholder is treated as an identified (non-US) place.
        if re.search(r"[A-Za-z]", segment):
            identified_non_us = True
    return "explicitly_non_us" if identified_non_us else "ambiguous_or_unresolvable"
