#!/usr/bin/env python3
"""Regression tests for the Workday CXS pagination hardening (run
20260812T152540Z pagination bug: NVIDIA inventory incorrectly truncated
480 -> corrected to 1,815; unseen postings incorrectly 10 -> corrected to
717).

Root cause: `total` was re-read from every page's response. Some Workday
tenants (observed on NVIDIA's CXS deployment) only report an accurate
`total` on the first page of a given search and return 0 on every
subsequent page of the *same* query -- trusting a fresh `total` every page
caused a spurious early break after just one page.

Covers, against both scripts/workday_fetch.py (paginate_keyword_search,
used standalone by Dell/HPE/Red Hat and now NVIDIA) and
scripts/adapters/workday.py (fetch_inventory, the generic-driver adapter
used by any future config-driven Workday source):

- more than one page
- more than 20 / 50 / 100 records
- correct offset progression
- correct termination only at true end-of-results
- no first-page repetition
- no accidental early stop (the exact NVIDIA total-per-page bug)
- duplicate-page detection (server ignoring offset / repeating a page)
- partial/failure handling (a request exception mid-pagination)
- an NVIDIA-style response (total accurate on page 1 only, 0 afterward)
- HPE/Red-Hat-style response (total accurate and consistent on every page)
- the max-pages safety ceiling is never silent when actually reached
"""

from __future__ import annotations

import importlib.util
import sys
import warnings
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workday_fetch = _load("workday_fetch_pagination_tests", "workday_fetch.py")
from adapters import workday as workday_adapter  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON payload")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Records every POST payload and returns pages in order (or via a
    callable for stateful/pathological servers)."""

    def __init__(self, pages: list[FakeResponse] | Any):
        self._pages = pages
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, json: dict[str, Any], timeout: int = 30) -> FakeResponse:  # noqa: A002
        self.requests.append(json)
        if callable(self._pages):
            return self._pages(json)
        if not self._pages:
            raise AssertionError("FakeSession ran out of queued pages")
        return self._pages.pop(0)


def _job_summary(job_id: str, title: str = "AI Infrastructure Engineer") -> dict[str, Any]:
    return {"title": title, "externalPath": f"/job/{job_id}", "bulletFields": [job_id], "locationsText": "Remote - US"}


def _page(job_ids: list[str], total: int | None) -> FakeResponse:
    payload: dict[str, Any] = {"jobPostings": [_job_summary(j) for j in job_ids]}
    if total is not None:
        payload["total"] = total
    return FakeResponse(200, payload)


COMMON_KW = dict(
    company="TestCo",
    origin="https://testco.wd5.myworkdayjobs.com",
    site="External",
    limit=10,
    max_pages=50,
    timeout=30,
    delay=0,
    sleep_fn=lambda _s: None,
)


def paginate(pages_or_fn, keyword="AI", **overrides) -> dict[str, Any]:
    session = FakeSession(pages_or_fn)
    kwargs = {**COMMON_KW, **overrides}
    result = workday_fetch.paginate_keyword_search(session, "https://endpoint/jobs", keyword, **kwargs)
    result["_session"] = session
    return result


def main() -> int:
    # --- 1. More than one page; correct offset progression. ---
    r = paginate(
        [
            _page([f"R{i}" for i in range(10)], total=22),
            _page([f"R{i}" for i in range(10, 20)], total=22),
            _page([f"R{i}" for i in range(20, 22)], total=22),
        ]
    )
    assert len(r["inventory_by_key"]) == 22, f"Expected 22 records, got {len(r['inventory_by_key'])}"
    offsets = [req["offset"] for req in r["_session"].requests]
    assert offsets == [0, 10, 20], f"Expected offsets [0, 10, 20], got {offsets}"
    assert not r["warnings"], f"Clean multi-page pagination should have no warnings: {r['warnings']}"

    # --- 2. More than 100 records (multiple full pages). ---
    total_records = 237
    full_pages = [
        _page([f"R{i}" for i in range(p * 10, min((p + 1) * 10, total_records))], total=total_records)
        for p in range((total_records + 9) // 10)
    ]
    r = paginate(full_pages)
    assert len(r["inventory_by_key"]) == total_records, (
        f"Expected {total_records} records across {len(full_pages)} pages, got {len(r['inventory_by_key'])}"
    )
    assert not r["warnings"]

    # --- 3. Correct termination only at true end-of-results: a page whose
    # size equals `limit` right at the total boundary must not be mistaken
    # for a short page, and no extra request is made once total is reached. ---
    r = paginate(
        [
            _page([f"R{i}" for i in range(10)], total=20),
            _page([f"R{i}" for i in range(10, 20)], total=20),  # exactly reaches total, full-size page
        ]
    )
    assert len(r["inventory_by_key"]) == 20
    assert len(r["_session"].requests) == 2, "Must not issue a third request once known_total is reached"

    # --- 4. No accidental early stop: the exact NVIDIA bug. total is
    # accurate (1815-equivalent, scaled down here) on page 1 only and
    # reported as 0 on every subsequent page of the same query. ---
    nvidia_total = 45
    nvidia_pages = [_page([f"R{i}" for i in range(p * 10, min((p + 1) * 10, nvidia_total))], total=0) for p in range(5)]
    nvidia_pages[0]._payload["total"] = nvidia_total  # only page 1 reports the real total
    r = paginate(nvidia_pages)
    assert len(r["inventory_by_key"]) == nvidia_total, (
        f"NVIDIA-style total-per-page bug: expected all {nvidia_total} records despite total=0 on pages 2+, "
        f"got {len(r['inventory_by_key'])} (this is the exact regression from run 20260812T152540Z)"
    )
    assert len(r["_session"].requests) == 5, f"Expected all 5 pages fetched, got {len(r['_session'].requests)}"

    # --- 5. NVIDIA-style response, explicitly named/shaped as such: total
    # correct on page 1, 0 afterward, terminates on the final short page. ---
    def nvidia_style_server(req: dict[str, Any]) -> FakeResponse:
        offset = req["offset"]
        remaining = max(0, 53 - offset)
        page_ids = [f"JR{offset + i}" for i in range(min(req["limit"], remaining))]
        total = 53 if offset == 0 else 0
        return _page(page_ids, total=total)

    r = paginate(nvidia_style_server, keyword="NVIDIA")
    assert len(r["inventory_by_key"]) == 53, f"NVIDIA-style server: expected 53 records, got {len(r['inventory_by_key'])}"
    assert not r["warnings"]

    # --- 6. HPE/Red-Hat-style response: total is accurate and consistent on
    # *every* page (the common/simple case) -- must still work correctly. ---
    def hpe_style_server(req: dict[str, Any]) -> FakeResponse:
        offset = req["offset"]
        remaining = max(0, 34 - offset)
        page_ids = [f"HPE{offset + i}" for i in range(min(req["limit"], remaining))]
        return _page(page_ids, total=34)  # accurate every time

    r = paginate(hpe_style_server, keyword="GPU")
    assert len(r["inventory_by_key"]) == 34, f"HPE-style server: expected 34 records, got {len(r['inventory_by_key'])}"
    assert not r["warnings"]

    # --- 7. Duplicate-page detection / no first-page repetition: the server
    # ignores `offset` entirely and keeps re-returning page 1. ---
    def stuck_offset_server(req: dict[str, Any]) -> FakeResponse:
        return _page([f"R{i}" for i in range(req["limit"])], total=999)  # claims way more exist

    r = paginate(stuck_offset_server, keyword="stuck")
    assert len(r["inventory_by_key"]) == 10, f"Expected only the 10 distinct IDs from the repeated page, got {len(r['inventory_by_key'])}"
    assert any("duplicate" in w.lower() or "zero new" in w.lower() for w in r["warnings"]), (
        f"Expected a duplicate-page warning, got: {r['warnings']}"
    )
    # Must stop quickly, not loop to max_pages (50) re-fetching the same page.
    assert len(r["_session"].requests) <= 2, f"Expected pagination to stop after detecting the stall, made {len(r['_session'].requests)} requests"

    # --- 8. Partial/failure handling: a request exception mid-pagination
    # stops that keyword's pagination but preserves already-collected data. ---
    def failing_on_page_2(req: dict[str, Any]) -> FakeResponse:
        if req["offset"] == 0:
            return _page([f"R{i}" for i in range(req["limit"])], total=30)
        raise ConnectionError("simulated network failure")

    r = paginate(failing_on_page_2, keyword="flaky")
    assert len(r["inventory_by_key"]) == 10, "Partial results from before the failure must be preserved"
    assert any("ConnectionError" in w for w in r["warnings"]), f"Expected a recorded connection-error warning: {r['warnings']}"

    # --- 9. Max-pages safety ceiling: a pathological server that always
    # returns a full, genuinely-new page forever must stop at max_pages and
    # say so explicitly -- never silently truncate. ---
    def endless_server(req: dict[str, Any]) -> FakeResponse:
        offset = req["offset"]
        return _page([f"E{offset + i}" for i in range(req["limit"])], total=None)  # no total ever provided

    r = paginate(endless_server, keyword="endless", max_pages=5)
    assert len(r["_session"].requests) == 5, f"Expected exactly max_pages=5 requests, got {len(r['_session'].requests)}"
    assert len(r["inventory_by_key"]) == 50
    assert any("safety ceiling" in w for w in r["warnings"]), f"Reaching max_pages must produce an explicit warning: {r['warnings']}"

    print("OK: workday_fetch.paginate_keyword_search (all 9 scenarios)")

    # =========================================================================
    # Same scenarios against scripts/adapters/workday.py's fetch_inventory,
    # which speaks the identical Workday CXS protocol and had the identical
    # un-hardened total-per-page bug before this fix.
    # =========================================================================

    def adapter_paginate(pages_or_fn, keywords=("AI",), max_pages_override: int | None = None):
        entry = {
            "company": "TestCo",
            "workday_origin": "https://testco.wd5.myworkdayjobs.com",
            "workday_tenant": "testco",
            "workday_site": "External",
        }
        session = FakeSession(pages_or_fn)
        old_max_pages = workday_adapter.MAX_PAGES
        if max_pages_override is not None:
            workday_adapter.MAX_PAGES = max_pages_override
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                inventory = workday_adapter.fetch_inventory(entry, session, list(keywords), 30)
        finally:
            workday_adapter.MAX_PAGES = old_max_pages
        return inventory, session, [str(w.message) for w in caught]

    # NVIDIA-style total-per-page bug via the adapter. Page size must track
    # the *requested* limit (DEFAULT_LIMIT=20), not a hardcoded stub size --
    # a page smaller than the real requested limit would look like a
    # legitimate short/final page and mask the very bug under test.
    def nvidia_style_server2(req: dict[str, Any]) -> FakeResponse:
        offset = req["offset"]
        limit = req["limit"]
        remaining = max(0, 47 - offset)
        page_ids = [f"AR{offset + i}" for i in range(min(limit, remaining))]
        total = 47 if offset == 0 else 0
        return _page(page_ids, total=total)

    inventory, session, msgs = adapter_paginate(nvidia_style_server2)
    assert len(inventory) == 47, f"adapters/workday.py: NVIDIA-style bug, expected 47 records, got {len(inventory)}"
    assert not msgs

    # Duplicate-page stall via the adapter.
    def stuck_offset_server2(req: dict[str, Any]) -> FakeResponse:
        return _page([f"S{i}" for i in range(req["limit"])], total=999)

    inventory, session, msgs = adapter_paginate(stuck_offset_server2)
    assert len(inventory) == workday_adapter.DEFAULT_LIMIT
    assert len(session.requests) <= 2, f"adapters/workday.py must stop on a duplicate-page stall, made {len(session.requests)} requests"
    assert any("duplicate" in m.lower() or "zero new" in m.lower() for m in msgs), msgs

    # Max-pages ceiling via the adapter -- must warn, never silently truncate.
    def endless_server2(req: dict[str, Any]) -> FakeResponse:
        offset = req["offset"]
        return _page([f"F{offset + i}" for i in range(req["limit"])], total=None)

    inventory, session, msgs = adapter_paginate(endless_server2, max_pages_override=4)
    assert len(session.requests) == 4
    assert len(inventory) == 4 * workday_adapter.DEFAULT_LIMIT
    assert any("safety ceiling" in m for m in msgs), msgs

    print("OK: adapters/workday.fetch_inventory (NVIDIA-style, duplicate-page, ceiling)")
    print("All Workday pagination regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
