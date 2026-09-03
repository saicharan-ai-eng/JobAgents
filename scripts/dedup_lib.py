#!/usr/bin/env python3
"""Shared deterministic helpers for sorting and deduplicating classified postings.

Used by both classify_dedupe_report.py (preliminary dedup pass) and
deduplicate.py (the authoritative, fail-closed dedup pass run after any
human/agent review of filtered.json).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from dateutil import parser as date_parser


def normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def parse_date(value: Any) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = date_parser.parse(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return datetime.min.replace(tzinfo=timezone.utc)


def date_sort_value(value: Any) -> int:
    parsed = parse_date(value)
    return (
        (((((parsed.year * 13) + parsed.month) * 32 + parsed.day) * 24 + parsed.hour) * 60 + parsed.minute) * 60
        + parsed.second
    )


def job_sort_key(job: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(job.get("fit_priority") or 4),
        -int(job.get("fit_score") or 0),
        -date_sort_value(job.get("posting_date")),
        normalize_key(job.get("company")),
        normalize_key(job.get("job_title")),
    )


def dedup_key(job: dict[str, Any]) -> tuple[str, ...]:
    company = normalize_key(job.get("company"))
    job_id = normalize_key(job.get("job_id"))
    if job_id:
        return (company, "id", job_id)
    return (company, "fallback", normalize_key(job.get("job_title")), normalize_key(job.get("location")))


class DedupInvariantError(ValueError):
    """Raised when the deduplication output would violate the include-only invariant."""


def deduplicate_included(filtered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate only include==True records from a filtered.json array.

    Fails closed (raises DedupInvariantError) rather than silently emitting an
    output that could contain excluded records or exceed the included count.
    """
    if not isinstance(filtered, list):
        raise DedupInvariantError(f"Expected filtered.json to be a JSON array, got {type(filtered).__name__}")

    included = [job for job in filtered if isinstance(job, dict) and job.get("include") is True]

    deduped_map: dict[tuple[str, ...], dict[str, Any]] = {}
    for job in included:
        key = dedup_key(job)
        existing = deduped_map.get(key)
        if existing is None:
            deduped_map[key] = job
            continue
        candidate_key = (int(job.get("fit_score") or 0), date_sort_value(job.get("posting_date")))
        existing_key = (int(existing.get("fit_score") or 0), date_sort_value(existing.get("posting_date")))
        if candidate_key > existing_key:
            deduped_map[key] = job

    deduped = sorted(deduped_map.values(), key=job_sort_key)

    if len(deduped) > len(included):
        raise DedupInvariantError(
            f"Deduplication invariant violated: {len(deduped)} deduplicated records exceeds "
            f"{len(included)} include==true records. Refusing to write output."
        )
    for job in deduped:
        if job.get("include") is not True:
            raise DedupInvariantError(
                f"Deduplicated output would contain a non-included record: {job.get('job_title')!r} "
                f"(include={job.get('include')!r})"
            )
    excluded_in_output = [job for job in deduped if job not in included]
    if excluded_in_output:
        raise DedupInvariantError("Deduplicated output contains a record not present in the included set.")

    return deduped
