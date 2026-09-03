#!/usr/bin/env python3
"""Regression tests for the 67-source expansion (shared ATS adapters +
run_source.py generic driver + config/sources.json integrity).

Covers, in order:
  1. source-config uniqueness (no duplicate slugs)
  2. duplicate LangChain prevention (exactly one entry)
  3. every entry's `platform` (when present) routes to a real, importable
     adapter module with the required interface -- adapter routing
  4. SmartRecruiters adapter pagination (multi-page fetch_inventory)
  5. Workday adapter pagination (multi-page fetch_inventory)
  6. deterministic job identity across repeated fetches of the same posting
  7. Stage-A US exclusion in run_source.py: a confirmed-non-US unseen item
     never reaches fetch_detail and is recorded excluded_non_us
  8. a US/ambiguous unseen item DOES reach fetch_detail (Stage-A is not
     over-aggressive)
  9. source failure isolation: an AdapterError from Stage A produces a
     well-formed blocked/failed result, not a crash
 10. shared-adapter failure isolation: two different companies on the same
     platform (Greenhouse) -- one blocked, one succeeds -- with no shared
     state leaking between the two independent invocations
 11. duplicate job handling within one adapter's own inventory page
 12. missing posting_date is carried through as None, never fabricated
 13. direct-application URL validation: every adapter's inventory job_url is
     a specific posting URL, never a bare board/search-results home page
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_source  # noqa: E402
from adapters import ashby, common, greenhouse, lever, smartrecruiters, teamtailor, workday  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON payload")
        return self._payload


class FakeSession:
    """Minimal stand-in for requests.Session. `responses` maps a URL prefix
    to either a single FakeResponse or a list of FakeResponses consumed in
    order (for pagination tests)."""

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls: list[tuple[str, str, dict]] = []

    def _resolve(self, method: str, url: str, kwargs: dict) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        for prefix, value in self.responses.items():
            if url.startswith(prefix):
                if isinstance(value, list):
                    if not value:
                        raise AssertionError(f"FakeSession ran out of queued responses for {prefix}")
                    return value.pop(0)
                return value
        raise AssertionError(f"FakeSession has no canned response for {method} {url}")

    def get(self, url: str, timeout: int = 30, **kwargs) -> FakeResponse:  # noqa: ARG002
        return self._resolve("GET", url, kwargs)

    def post(self, url: str, timeout: int = 30, **kwargs) -> FakeResponse:  # noqa: ARG002
        return self._resolve("POST", url, kwargs)


def test_source_config_uniqueness() -> None:
    config = json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))
    sources = config["sources"]
    slugs = [s["slug"] for s in sources]
    assert len(slugs) == len(set(slugs)), f"Duplicate slugs found: {[s for s in slugs if slugs.count(s) > 1]}"

    companies = [s["company"] for s in sources]
    langchain_entries = [s for s in sources if s["company"] == "LangChain"]
    assert len(langchain_entries) == 1, f"LangChain must appear exactly once, found {len(langchain_entries)}"
    assert companies.count("LangChain") == 1

    for s in sources:
        assert s.get("agent"), f"{s['slug']}: missing agent"
        assert s.get("company"), f"{s['slug']}: missing company"
        assert s.get("careers_home"), f"{s['slug']}: missing careers_home"
        assert s.get("listing_url"), f"{s['slug']}: missing listing_url"


def test_adapter_routing() -> None:
    config = json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))
    platforms_in_use = {s["platform"] for s in config["sources"] if "platform" in s}
    assert platforms_in_use, "Expected at least one config-driven platform entry"
    for platform in platforms_in_use:
        module_path = run_source.PLATFORM_MODULES.get(platform)
        assert module_path is not None, f"No adapter registered for platform {platform!r}"
        module = importlib.import_module(module_path)
        assert callable(getattr(module, "fetch_inventory", None)), f"{module_path} missing fetch_inventory"
        assert callable(getattr(module, "fetch_detail", None)), f"{module_path} missing fetch_detail"

    # Every source's platform-specific identifier field is actually present
    # (e.g. a greenhouse entry has greenhouse_board_token) -- catches a
    # config typo before it ever reaches a live fetch.
    required_field = {
        "greenhouse": "greenhouse_board_token",
        "ashby": "ashby_board_name",
        "lever": "lever_company_slug",
        "smartrecruiters": "smartrecruiters_company_id",
        "workday": "workday_tenant",
        "teamtailor": "teamtailor_domain",
    }
    for s in config["sources"]:
        platform = s.get("platform")
        if platform in required_field:
            assert s.get(required_field[platform]), f"{s['slug']}: platform={platform} missing {required_field[platform]}"


def test_smartrecruiters_pagination() -> None:
    entry = {"company": "TestCo", "smartrecruiters_company_id": "testco"}
    page1 = {
        "content": [{"id": "1", "name": "AI Infrastructure Engineer", "location": {"city": "SF", "country": "US"}, "releasedDate": "2026-08-01"}] * 100,
        "totalFound": 105,
    }
    page2 = {
        "content": [{"id": "101", "name": "MLOps Engineer", "location": {"city": "NYC", "country": "US"}, "releasedDate": "2026-08-02"}] * 5,
        "totalFound": 105,
    }
    session = FakeSession({"https://api.smartrecruiters.com/v1/companies/testco/postings": [FakeResponse(200, page1), FakeResponse(200, page2)]})
    inventory = smartrecruiters.fetch_inventory(entry, session, ["AI Infrastructure", "MLOps"], 30)
    assert len(inventory) == 105, f"Expected both pages combined (105), got {len(inventory)}"
    assert len(session.calls) == 2, f"Expected exactly 2 paginated requests, got {len(session.calls)}"


def test_workday_pagination() -> None:
    entry = {"company": "TestCo", "workday_origin": "https://testco.wd1.myworkdayjobs.com", "workday_tenant": "testco", "workday_site": "External"}
    page1 = {"jobPostings": [{"title": "AI Infrastructure Engineer", "externalPath": "/job/r1", "bulletFields": ["R1"], "locationsText": "Remote - US"}] * 20, "total": 22}
    page2 = {"jobPostings": [{"title": "MLOps Engineer", "externalPath": "/job/r2", "bulletFields": ["R2"], "locationsText": "Remote - US"}] * 2, "total": 22}
    session = FakeSession({"https://testco.wd1.myworkdayjobs.com/wday/cxs/testco/External/jobs": [FakeResponse(200, page1), FakeResponse(200, page2)]})
    inventory = workday.fetch_inventory(entry, session, ["AI Infrastructure"], 30)
    assert len(inventory) == 2, f"Expected 2 distinct job IDs (R1, R2) after de-duplication by key, got {len(inventory)}"
    ids = {item["job_id"] for item in inventory}
    assert ids == {"R1", "R2"}


def test_deterministic_job_identity() -> None:
    sys.path.insert(0, str(SCRIPTS))
    import source_history as sh

    posting = {"company": "Anthropic", "job_id": "5101378008", "job_title": "Account Executive", "location": "New York City, NY"}
    identity_1 = sh.stable_identity(posting)
    identity_2 = sh.stable_identity(dict(posting))
    assert identity_1 == identity_2
    # Re-fetching the same posting from Greenhouse a second time (identical
    # job dict from a fresh fetch_inventory call) must resolve to the same
    # identity, so it is recognized as already-seen and never re-fetched.
    gh_entry = {"company": "TestCo", "greenhouse_board_token": "testco"}
    job = {"id": 42, "title": "AI Infrastructure Engineer", "location": {"name": "Remote - US"}, "content": "Build things.", "absolute_url": "https://job-boards.greenhouse.io/testco/jobs/42"}
    session = FakeSession({"https://boards-api.greenhouse.io/v1/boards/testco/jobs": FakeResponse(200, {"jobs": [job]})})
    inv1 = greenhouse.fetch_inventory(gh_entry, session, ["AI Infrastructure"], 30)
    session2 = FakeSession({"https://boards-api.greenhouse.io/v1/boards/testco/jobs": FakeResponse(200, {"jobs": [job]})})
    inv2 = greenhouse.fetch_inventory(gh_entry, session2, ["AI Infrastructure"], 30)
    assert sh.stable_identity(inv1[0]) == sh.stable_identity(inv2[0])


def test_stage_a_us_exclusion(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    """A confirmed-non-US unseen item must never trigger fetch_detail and
    must be recorded with processing_status=excluded_non_us."""
    config = {
        "keywords": ["AI infrastructure"],
        "sources": [
            {
                "slug": "testco-nonus",
                "company": "TestCo",
                "agent": "generic-ats-scraper",
                "careers_home": "https://example.invalid",
                "listing_url": "https://example.invalid",
                "platform": "greenhouse",
                "greenhouse_board_token": "testco",
            }
        ],
    }
    config_path = tmp_path / "sources.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    history_path = tmp_path / "history.json"
    output_path = tmp_path / "out.json"

    us_job = {"id": 1, "title": "AI Infrastructure Engineer, US", "location": {"name": "San Francisco, CA"}, "content": "x", "absolute_url": "https://job-boards.greenhouse.io/testco/jobs/1"}
    non_us_job = {"id": 2, "title": "AI Infrastructure Engineer, Berlin", "location": {"name": "Berlin, Germany"}, "content": "x", "absolute_url": "https://job-boards.greenhouse.io/testco/jobs/2"}

    detail_calls: list[str] = []

    def fake_fetch_detail(entry, item, session, timeout):  # noqa: ANN001, ARG001
        detail_calls.append(item["job_id"])
        return {
            "company": "TestCo", "job_title": item["job_title"], "job_id": item["job_id"],
            "location": item["location"], "posting_date": None, "experience_level_text": None,
            "job_url": item["job_url"], "team_department": None, "short_description": None,
            "full_description_text": "detail fetched", "source_keyword": item["source_keyword"],
        }

    import adapters.greenhouse as gh_module

    monkeypatch.setattr(gh_module, "fetch_detail", fake_fetch_detail)
    monkeypatch.setattr(
        gh_module,
        "fetch_inventory",
        lambda entry, session, keywords, timeout: [
            {"company": "TestCo", "job_title": us_job["title"], "job_id": "1", "location": "San Francisco, CA",
             "posting_date": None, "job_url": us_job["absolute_url"], "source_keyword": "AI infrastructure", "_platform_ref": us_job},
            {"company": "TestCo", "job_title": non_us_job["title"], "job_id": "2", "location": "Berlin, Germany",
             "posting_date": None, "job_url": non_us_job["absolute_url"], "source_keyword": "AI infrastructure", "_platform_ref": non_us_job},
        ],
    )

    sys.argv = [
        "run_source.py", "--slug", "testco-nonus", "--output", str(output_path),
        "--config-file", str(config_path), "--history-file", str(history_path), "--delay", "0",
    ]
    rc = run_source.main()
    assert rc == 0
    result = json.loads(output_path.read_text(encoding="utf-8"))

    assert detail_calls == ["1"], f"fetch_detail must be called ONLY for the US identity, got calls={detail_calls}"
    by_id = {p["job_id"]: p for p in result["postings"]}
    assert by_id["1"]["processing_status"] == "success"
    assert by_id["1"]["full_description_text"] == "detail fetched"
    assert by_id["2"]["processing_status"] == "excluded_non_us"
    assert by_id["2"]["full_description_text"] is None, "excluded_non_us postings must never carry a fetched description"
    assert by_id["2"]["location"] == "Berlin, Germany", "location must still be carried through faithfully"
    assert result["detail_fetch_count"] == 1
    assert result["raw_posting_count"] == len(result["postings"]) == 2


def test_source_failure_isolation(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    """An AdapterError at Stage A must produce a well-formed blocked/failed
    result -- never a crash, never guessed data."""
    config = {
        "keywords": ["AI"],
        "sources": [
            {
                "slug": "testco-blocked",
                "company": "TestCo",
                "agent": "generic-ats-scraper",
                "careers_home": "https://example.invalid",
                "listing_url": "https://example.invalid",
                "platform": "greenhouse",
                "greenhouse_board_token": "doesnotexist",
            }
        ],
    }
    config_path = tmp_path / "sources.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    output_path = tmp_path / "out.json"

    import adapters.greenhouse as gh_module

    def raise_blocked(entry, session, keywords, timeout):  # noqa: ANN001, ARG001
        raise common.AdapterError("GET ... returned 403 (blocked/authentication required)")

    monkeypatch.setattr(gh_module, "fetch_inventory", raise_blocked)

    sys.argv = ["run_source.py", "--slug", "testco-blocked", "--output", str(output_path), "--config-file", str(config_path), "--history-file", str(tmp_path / "history.json")]
    rc = run_source.main()
    assert rc == 2
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["status"] == "blocked"
    assert "403" in result["reason"]
    assert result["postings"] == []
    assert result["inventory"] == []


def test_shared_adapter_failure_isolation(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    """Two different companies both routed through the SAME adapter module
    (Greenhouse): one blocked, one succeeds -- failure of one must never
    affect the other's independent run_source.py invocation."""
    config = {
        "keywords": ["AI"],
        "sources": [
            {"slug": "good-co", "company": "GoodCo", "agent": "generic-ats-scraper", "careers_home": "https://example.invalid", "listing_url": "https://example.invalid", "platform": "greenhouse", "greenhouse_board_token": "goodco"},
            {"slug": "bad-co", "company": "BadCo", "agent": "generic-ats-scraper", "careers_home": "https://example.invalid", "listing_url": "https://example.invalid", "platform": "greenhouse", "greenhouse_board_token": "badco"},
        ],
    }
    config_path = tmp_path / "sources.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    import adapters.greenhouse as gh_module

    good_job = {"id": 1, "title": "AI Engineer", "location": {"name": "Remote - US"}, "content": "x", "absolute_url": "https://job-boards.greenhouse.io/goodco/jobs/1"}

    def routed_fetch_inventory(entry, session, keywords, timeout):  # noqa: ANN001, ARG001
        if entry["greenhouse_board_token"] == "badco":
            raise common.AdapterError("GET ... returned HTTP 404")
        return gh_module.__dict__["_ORIGINAL_fetch_inventory"](entry, session, keywords, timeout)

    original_fetch_inventory = gh_module.fetch_inventory
    gh_module.__dict__["_ORIGINAL_fetch_inventory"] = original_fetch_inventory
    monkeypatch.setattr(gh_module, "fetch_inventory", routed_fetch_inventory)

    session_good = FakeSession({"https://boards-api.greenhouse.io/v1/boards/goodco/jobs": FakeResponse(200, {"jobs": [good_job]})})

    monkeypatch.setattr("adapters.common.new_session", lambda: session_good)
    out_good = tmp_path / "good.json"
    sys.argv = ["run_source.py", "--slug", "good-co", "--output", str(out_good), "--config-file", str(config_path), "--history-file", str(tmp_path / "history_good.json"), "--delay", "0"]
    rc_good = run_source.main()

    out_bad = tmp_path / "bad.json"
    sys.argv = ["run_source.py", "--slug", "bad-co", "--output", str(out_bad), "--config-file", str(config_path), "--history-file", str(tmp_path / "history_bad.json"), "--delay", "0"]
    rc_bad = run_source.main()

    assert rc_good == 0, "GoodCo must succeed even though BadCo (same adapter module) fails"
    assert rc_bad == 2, "BadCo must fail independently"
    good_result = json.loads(out_good.read_text(encoding="utf-8"))
    bad_result = json.loads(out_bad.read_text(encoding="utf-8"))
    assert good_result["status"] == "success"
    assert bad_result["status"] == "failed"
    assert good_result["inventory_count"] == 1, "GoodCo's result must not be contaminated by BadCo's failure"


def test_duplicate_job_within_inventory() -> None:
    """SmartRecruiters/Workday key their inventory by job identity, so the
    same posting appearing twice across overlapping keyword searches is
    de-duplicated within a single Stage-A scan, not double-counted."""
    entry = {"company": "TestCo", "workday_origin": "https://testco.wd1.myworkdayjobs.com", "workday_tenant": "testco", "workday_site": "External"}
    same_job = {"title": "AI Infrastructure Engineer", "externalPath": "/job/r1", "bulletFields": ["R1"], "locationsText": "Remote - US"}
    page_ai = {"jobPostings": [same_job], "total": 1}
    page_ml = {"jobPostings": [same_job], "total": 1}
    session = FakeSession({"https://testco.wd1.myworkdayjobs.com/wday/cxs/testco/External/jobs": [FakeResponse(200, page_ai), FakeResponse(200, page_ml)]})
    inventory = workday.fetch_inventory(entry, session, ["AI infrastructure", "machine learning"], 30)
    assert len(inventory) == 1, f"Same job_id found under two different keyword searches must collapse to one inventory item, got {len(inventory)}"


def test_missing_posting_date_not_fabricated() -> None:
    entry = {"company": "TestCo", "lever_company_slug": "testco"}
    job = {"id": "abc123", "text": "AI Infrastructure Engineer", "categories": {"location": "Remote - US"}, "description": "Build things.", "hostedUrl": "https://jobs.lever.co/testco/abc123"}
    # Deliberately no createdAt field -- Lever's posting_date derivation must
    # yield None rather than inventing a date.
    session = FakeSession({"https://api.lever.co/v0/postings/testco": FakeResponse(200, [job])})
    inventory = lever.fetch_inventory(entry, session, ["AI Infrastructure"], 30)
    assert len(inventory) == 1
    assert inventory[0]["posting_date"] is None, "Missing source date must be None, never fabricated"


def test_direct_application_url_validation() -> None:
    """Every adapter must emit a specific posting URL, not a bare
    board/search-results home page."""
    cases = [
        (greenhouse, {"greenhouse_board_token": "testco"}, "https://boards-api.greenhouse.io/v1/boards/testco/jobs",
         {"jobs": [{"id": 1, "title": "AI Engineer", "location": {"name": "Remote - US"}, "content": "x", "absolute_url": "https://job-boards.greenhouse.io/testco/jobs/1"}]}),
        (ashby, {"ashby_board_name": "testco"}, "https://api.ashbyhq.com/posting-api/job-board/testco",
         {"jobs": [{"id": "j1", "title": "AI Engineer", "location": "Remote - US", "descriptionHtml": "x", "jobUrl": "https://jobs.ashbyhq.com/testco/j1"}]}),
        (lever, {"lever_company_slug": "testco"}, "https://api.lever.co/v0/postings/testco",
         [{"id": "abc", "text": "AI Engineer", "categories": {"location": "Remote - US"}, "description": "x", "hostedUrl": "https://jobs.lever.co/testco/abc"}]),
    ]
    for adapter, entry, url_prefix, payload in cases:
        entry = {"company": "TestCo", **entry}
        session = FakeSession({url_prefix: FakeResponse(200, payload)})
        inventory = adapter.fetch_inventory(entry, session, ["AI"], 30)
        assert len(inventory) == 1, adapter.__name__
        job_url = inventory[0]["job_url"]
        assert job_url, f"{adapter.__name__}: job_url must not be empty"
        # A bare board/company home page has no distinct trailing path
        # segment beyond the company/board token itself.
        assert job_url.rstrip("/").count("/") >= 4, f"{adapter.__name__}: job_url {job_url!r} looks like a board home page, not a direct posting URL"

    # Teamtailor and SmartRecruiters use their own richer payload shapes;
    # verified separately here for the same property.
    tt_entry = {"company": "TestCo", "teamtailor_domain": "https://careers.testco.example"}
    tt_payload = {"items": [{"id": "x", "title": "AI Engineer", "url": "https://careers.testco.example/jobs/123-ai-engineer", "date_published": "2026-08-01",
                              "_jobposting": {"title": "AI Engineer", "description": "x", "identifier": {"value": 123}, "jobLocation": [{"address": {"addressLocality": "SF", "addressRegion": "CA", "addressCountry": "US"}}]}}]}
    tt_session = FakeSession({"https://careers.testco.example/jobs.json": FakeResponse(200, tt_payload)})
    tt_inventory = teamtailor.fetch_inventory(tt_entry, tt_session, ["AI"], 30)
    assert tt_inventory[0]["job_url"] == "https://careers.testco.example/jobs/123-ai-engineer"

    sr_entry = {"company": "TestCo", "smartrecruiters_company_id": "testco"}
    sr_payload = {"content": [{"id": "p1", "name": "AI Engineer", "location": {"city": "SF", "country": "US"}, "releasedDate": "2026-08-01", "ref": "https://jobs.smartrecruiters.com/testco/p1-ai-engineer"}], "totalFound": 1}
    sr_session = FakeSession({"https://api.smartrecruiters.com/v1/companies/testco/postings": FakeResponse(200, sr_payload)})
    sr_inventory = smartrecruiters.fetch_inventory(sr_entry, sr_session, ["AI"], 30)
    assert sr_inventory[0]["job_url"] == "https://jobs.smartrecruiters.com/testco/p1-ai-engineer"


def main() -> int:
    tests = [
        test_source_config_uniqueness,
        test_adapter_routing,
        test_smartrecruiters_pagination,
        test_workday_pagination,
        test_deterministic_job_identity,
        test_duplicate_job_within_inventory,
        test_missing_posting_date_not_fabricated,
        test_direct_application_url_validation,
    ]
    for test in tests:
        test()
        print(f"OK: {test.__name__}")

    # The remaining tests need lightweight monkeypatch/tmp_path fixtures;
    # run them with a tiny hand-rolled substitute so this file stays
    # runnable directly with `python` (no pytest dependency required),
    # consistent with every other test_*.py in this repo.
    import shutil
    import tempfile

    class _Monkeypatch:
        def __init__(self):
            self._restores: list[tuple[Any, str, Any]] = []

        def setattr(self, target, name_or_value, value=None):
            if isinstance(target, str):
                module = importlib.import_module(target.rsplit(".", 1)[0])
                attr = target.rsplit(".", 1)[1]
                self._restores.append((module, attr, getattr(module, attr)))
                setattr(module, attr, name_or_value)
                return
            self._restores.append((target, name_or_value, getattr(target, name_or_value)))
            setattr(target, name_or_value, value)

        def undo(self):
            for obj, attr, old in reversed(self._restores):
                setattr(obj, attr, old)

    fixture_tests = [
        test_stage_a_us_exclusion,
        test_source_failure_isolation,
        test_shared_adapter_failure_isolation,
    ]
    for test in fixture_tests:
        mp = _Monkeypatch()
        tmp_dir = Path(tempfile.mkdtemp(prefix="source_expansion_test_"))
        try:
            test(mp, tmp_dir)
            print(f"OK: {test.__name__}")
        finally:
            mp.undo()
            shutil.rmtree(tmp_dir, ignore_errors=True)

    print("All source-expansion regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
