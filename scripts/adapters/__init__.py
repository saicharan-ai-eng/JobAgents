"""Shared, deterministic ATS/platform adapters.

Each adapter module exposes:

    fetch_inventory(entry: dict, session, keywords: list[str], timeout: int) -> list[dict]
        Stage A. Lightweight scan of every currently-listed posting for the
        given source config `entry` (from config/sources.json). Returns dicts
        with at least: company, job_title, job_id, location, posting_date,
        job_url, source_keyword, and an internal `_platform_ref` field the
        adapter itself may use in fetch_detail (never written to output).

    fetch_detail(entry: dict, item: dict, session, timeout: int) -> dict | None
        Stage B. Full detail for one unseen inventory item. Returns a dict
        with the full source-result posting contract fields (see
        CLAUDE.md / schemas/source-result.schema.json), or None on failure.

Adapters never decide US-location eligibility or seniority themselves --
they only carry the structured `location` field faithfully. They never log
in, solve CAPTCHAs, or bypass robots.txt/WAF blocks; a blocked/failed fetch
is surfaced as an exception with a specific message, which run_source.py
turns into a `blocked`/`failed` source-result status.
"""
