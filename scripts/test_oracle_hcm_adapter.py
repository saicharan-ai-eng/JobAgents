#!/usr/bin/env python3
"""Regression tests for scripts/adapters/oracle_hcm.py -- the Dell Oracle
Fusion Cloud HCM ("Candidate Experience") adapter that replaces the
deprecated Workday endpoint Dell's config/sources.json entry used to target
(confirmed dead: HTTP 200 with an always-empty result set).

Covers:
- basic inventory fetch + field mapping (job_id, title, location, posting
  date, direct job_url)
- detail fetch field mapping (description assembled from the three Oracle
  description fields, experience extraction, team/department)
- direct-application URL correctness (public jobs.dell.com origin, never
  the raw API host, never a bare search page)
- the same pagination hardening applied proactively as the Workday adapter
  (see run 20260812T152540Z): known-total-once, short-page termination,
  duplicate-page-stall detection, max-pages safety ceiling with a loud
  warning
- Stage-A US-location pre-filtering integration via run_source.py (a
  confirmed-non-US unseen item skips Stage-B; a US/ambiguous item does not)
"""

from __future__ import annotations

import importlib
import sys
import warnings
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_source  # noqa: E402
from adapters import common, oracle_hcm  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON payload")
        return self._payload


class FakeSession:
    def __init__(self, responses_or_fn: Any):
        self._responses = responses_or_fn
        self.requests: list[dict[str, Any]] = []

    def get(self, url: str, timeout: int = 30, params: dict[str, Any] | None = None) -> FakeResponse:
        self.requests.append(params or {})
        if callable(self._responses):
            return self._responses(url, params or {})
        if not self._responses:
            raise AssertionError("FakeSession ran out of queued responses")
        return self._responses.pop(0)


ENTRY = {
    "company": "Dell",
    "oracle_hcm_origin": "https://enterpriseplatform.dell.com",
    "oracle_hcm_site_number": "CX_1001",
    "oracle_hcm_public_origin": "https://jobs.dell.com",
}


def _requisition(job_id: str, title: str = "AI Infrastructure Engineer", location: str = "Austin, TX, United States") -> dict[str, Any]:
    return {"Id": job_id, "Title": title, "PostedDate": "2026-08-01", "PrimaryLocation": location, "secondaryLocations": []}


def _search_response(requisitions: list[dict[str, Any]], total: int | None) -> FakeResponse:
    item: dict[str, Any] = {"requisitionList": requisitions}
    if total is not None:
        item["TotalJobsCount"] = total
    return FakeResponse(200, {"items": [item]})


def main() -> int:
    # --- Basic inventory fetch + field mapping. ---
    session = FakeSession([_search_response([_requisition("R100"), _requisition("R101")], total=2)])
    inventory = oracle_hcm.fetch_inventory(ENTRY, session, ["AI"], 30)
    assert len(inventory) == 2, f"Expected 2 records, got {len(inventory)}"
    by_id = {i["job_id"]: i for i in inventory}
    assert by_id["R100"]["job_title"] == "AI Infrastructure Engineer"
    assert by_id["R100"]["location"] == "Austin, TX, United States"
    assert by_id["R100"]["posting_date"] == "2026-08-01"
    print("OK: basic inventory fetch + field mapping")

    # --- Direct-application URL correctness: public origin, site, job ID
    # only -- never the raw API host, never a bare search page. ---
    assert by_id["R100"]["job_url"] == "https://jobs.dell.com/en/sites/CX_1001/job/R100"
    assert "enterpriseplatform.dell.com" not in by_id["R100"]["job_url"]
    assert "search" not in by_id["R100"]["job_url"].lower()
    print("OK: direct-application URL uses the public origin, no search/API-host leakage")

    # --- Multi-location: secondary locations are appended when present. ---
    multi_req = _requisition("R200", location="Round Rock, TX, United States")
    multi_req["secondaryLocations"] = [{"Name": "Austin, TX, United States"}]
    session = FakeSession([_search_response([multi_req], total=1)])
    inventory = oracle_hcm.fetch_inventory(ENTRY, session, ["AI"], 30)
    assert "Round Rock" in inventory[0]["location"] and "Austin" in inventory[0]["location"], inventory[0]["location"]
    print("OK: secondary locations are folded into the location string")

    # --- Detail fetch: description assembled from the three Oracle text
    # fields, experience extracted, team/department populated. ---
    detail_payload = FakeResponse(
        200,
        {
            "items": [
                {
                    "Id": "R100",
                    "Title": "AI Infrastructure Engineer",
                    "PrimaryLocation": "Austin, TX, United States",
                    "PostedDate": "2026-08-01",
                    "JobFunction": "Software Engineering",
                    "ExternalDescriptionStr": "<p>Build inference infrastructure.</p>",
                    "ExternalResponsibilitiesStr": "<p>Own CUDA kernel performance.</p>",
                    "ExternalQualificationsStr": "<p>2+ years of experience with GPU programming.</p>",
                    "otherWorkLocations": [],
                }
            ]
        },
    )
    session = FakeSession([detail_payload])
    item = by_id["R100"]
    detail = oracle_hcm.fetch_detail(ENTRY, item, session, 30)
    assert detail is not None
    assert "inference infrastructure" in detail["full_description_text"]
    assert "CUDA kernel" in detail["full_description_text"]
    assert "GPU programming" in detail["full_description_text"]
    assert detail["experience_level_text"] and "2+" in detail["experience_level_text"] or "2" in (detail["experience_level_text"] or "")
    assert detail["team_department"] == "Software Engineering"
    assert detail["job_url"] == item["job_url"], "fetch_detail must preserve the Stage-A direct URL, never rebuild a different one"
    print("OK: detail fetch assembles description from all three Oracle text fields")

    # --- fetch_detail returns None (not a fabricated record) when the
    # inventory item has no usable job_id reference. ---
    assert oracle_hcm.fetch_detail(ENTRY, {"job_title": "X"}, FakeSession([]), 30) is None
    print("OK: fetch_detail refuses to fabricate a record with no job_id")

    # =========================================================================
    # Pagination hardening -- same scenarios as test_workday_fetch.py,
    # applied proactively to this adapter (same offset/limit/total REST
    # shape that had an undetected per-page-total bug on a different
    # vendor).
    # =========================================================================

    def paginate_with(fn, keyword="AI", max_pages=None):
        old = oracle_hcm.MAX_PAGES
        if max_pages is not None:
            oracle_hcm.MAX_PAGES = max_pages
        try:
            session = FakeSession(fn)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                inventory = oracle_hcm.fetch_inventory(ENTRY, session, [keyword], 30)
        finally:
            oracle_hcm.MAX_PAGES = old
        return inventory, session, [str(w.message) for w in caught]

    # NVIDIA-style total-per-page bug analog: TotalJobsCount accurate only
    # on the first page, absent (not even reported) afterward.
    def total_only_first_page(_url: str, params: dict[str, Any]) -> FakeResponse:
        finder = params["finder"]
        offset = int(finder.split("offset=")[1])
        remaining = max(0, 44 - offset)
        reqs = [_requisition(f"R{offset + i}") for i in range(min(oracle_hcm.DEFAULT_LIMIT, remaining))]
        return _search_response(reqs, total=44 if offset == 0 else None)

    inventory, session, msgs = paginate_with(total_only_first_page)
    assert len(inventory) == 44, f"Expected all 44 records despite TotalJobsCount missing on later pages, got {len(inventory)}"
    assert not msgs
    print("OK: total-per-page-bug analog -- no accidental early stop")

    # Duplicate-page stall: server ignores offset, keeps returning page 1.
    def stuck_offset(_url: str, params: dict[str, Any]) -> FakeResponse:
        return _search_response([_requisition(f"S{i}") for i in range(oracle_hcm.DEFAULT_LIMIT)], total=999)

    inventory, session, msgs = paginate_with(stuck_offset)
    assert len(inventory) == oracle_hcm.DEFAULT_LIMIT
    assert len(session.requests) <= 2, f"Must stop quickly on a stalled/duplicate page, made {len(session.requests)} requests"
    assert any("duplicate" in m.lower() or "zero new" in m.lower() for m in msgs), msgs
    print("OK: duplicate-page stall detected, no first-page repetition loop")

    # Max-pages safety ceiling: pathological server with no total, always a
    # full page of genuinely new records -- must stop and warn, not truncate silently.
    def endless(_url: str, params: dict[str, Any]) -> FakeResponse:
        finder = params["finder"]
        offset = int(finder.split("offset=")[1])
        return _search_response([_requisition(f"E{offset + i}") for i in range(oracle_hcm.DEFAULT_LIMIT)], total=None)

    inventory, session, msgs = paginate_with(endless, max_pages=4)
    assert len(session.requests) == 4
    assert len(inventory) == 4 * oracle_hcm.DEFAULT_LIMIT
    assert any("safety ceiling" in m for m in msgs), msgs
    print("OK: max-pages safety ceiling stops and warns, never silently truncates")

    # =========================================================================
    # Stage-A US-location pre-filtering integration (run_source.py).
    # =========================================================================
    import tempfile
    import json as jsonlib

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        config = {
            "keywords": ["AI infrastructure"],
            "sources": [
                {
                    "slug": "dell-test",
                    "company": "Dell",
                    "agent": "generic-ats-scraper",
                    "listing_url": "https://jobs.dell.com",
                    "type": "generic",
                    "platform": "oracle_hcm",
                    "oracle_hcm_origin": "https://enterpriseplatform.dell.com",
                    "oracle_hcm_site_number": "CX_1001",
                    "oracle_hcm_public_origin": "https://jobs.dell.com",
                }
            ],
        }
        config_path = tmp_path / "sources.json"
        config_path.write_text(jsonlib.dumps(config), encoding="utf-8")
        history_path = tmp_path / "history.json"
        output_path = tmp_path / "out.json"

        india_req = _requisition("D1", title="AI Infrastructure Engineer", location="Bengaluru, Karnataka, India")
        us_req = _requisition("D2", title="AI Infrastructure Engineer", location="Austin, TX, United States")

        def fixed_search(_url: str, _params: dict[str, Any]) -> FakeResponse:
            return _search_response([india_req, us_req], total=2)

        detail_calls: list[str] = []

        old_fetch_detail = oracle_hcm.fetch_detail

        def fake_fetch_detail(entry, item, session, timeout):  # noqa: ANN001
            detail_calls.append(item["job_id"])
            return old_fetch_detail(
                entry,
                item,
                FakeSession(
                    [
                        FakeResponse(
                            200,
                            {
                                "items": [
                                    {
                                        "Id": item["job_id"],
                                        "Title": item["job_title"],
                                        "PrimaryLocation": item["location"],
                                        "ExternalDescriptionStr": "<p>Build AI infrastructure.</p>",
                                    }
                                ]
                            },
                        )
                    ]
                ),
                timeout,
            )

        old_new_session = common.new_session
        oracle_hcm.fetch_detail = fake_fetch_detail
        common.new_session = lambda: FakeSession(fixed_search)
        try:
            sys.argv = [
                "run_source.py",
                "--slug",
                "dell-test",
                "--output",
                str(output_path),
                "--config-file",
                str(config_path),
                "--history-file",
                str(history_path),
                "--delay",
                "0",
            ]
            run_source.main()
        finally:
            oracle_hcm.fetch_detail = old_fetch_detail
            common.new_session = old_new_session

        result = jsonlib.loads(output_path.read_text(encoding="utf-8"))
        statuses = {p["job_id"]: p.get("processing_status") for p in result["postings"]}
        assert statuses.get("D1") == "excluded_non_us", (
            f"India-only posting must be Stage-A excluded (no detail fetch), got {statuses}"
        )
        assert statuses.get("D2") == "success", f"US posting must reach Stage-B detail fetch, got {statuses}"
        assert "D1" not in detail_calls, "Stage-B fetch_detail must never be called for a confirmed-non-US item"
        assert "D2" in detail_calls, "Stage-B fetch_detail must be called for a US item"
    print("OK: Stage-A US-location pre-filter integration (confirmed non-US skips detail fetch)")

    print("All Dell Oracle HCM adapter regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
