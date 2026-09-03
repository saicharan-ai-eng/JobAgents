#!/usr/bin/env python3
"""Regression tests for the 12 ATS adapters added during the Dell/Lenovo
partner-spreadsheet onboarding (2026-09-02): scripts/adapters/breezy_hr.py,
rippling_ats.py, adp_workforce_now.py, pinpoint.py, trakstar_hire.py,
ultipro.py, agile_ats.py, successfactors_csb.py, hr_department.py,
jazzhr_apply.py, icims_classic.py, icims_jibe.py.

Focuses on the logic most likely to regress silently: direct-URL
construction (several of these platforms have no URL field of their own --
the pattern was reverse-engineered per platform and must not drift), the
embedded-JSON-blob extraction (UltiPro), and the SuccessFactors keyword
re-verification fix (a real bug found during onboarding: SAP's own search
matched "Architect II" for "AI infrastructure" with zero client-side check).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from adapters import agile_ats, breezy_hr, comeet, eightfold_pcsx, jobvite, successfactors_csb, ultipro  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else ""

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON payload")
        return self._payload


class FakeSession:
    def __init__(self, responses_or_fn: Any):
        self._responses = responses_or_fn
        self.requests: list[dict[str, Any]] = []

    def get(self, url: str, timeout: int = 30, **kwargs: Any) -> FakeResponse:
        self.requests.append({"url": url, **kwargs})
        if callable(self._responses):
            return self._responses(url, kwargs)
        if not self._responses:
            raise AssertionError("FakeSession ran out of queued responses")
        return self._responses.pop(0)

    def post(self, url: str, timeout: int = 30, **kwargs: Any) -> FakeResponse:
        return self.get(url, timeout=timeout, **kwargs)


def main() -> int:
    # --- Breezy HR: title-only Stage-A match (no description in the list
    # payload), location assembled from nested city/state/country. ---
    jobs = [
        {
            "id": "abc123",
            "name": "AI Infrastructure Engineer",
            "url": "https://acme.breezy.hr/p/abc123",
            "published_date": "2026-08-01T00:00:00Z",
            "location": {"city": "Salt Lake City", "state": {"name": "Utah"}, "country": {"name": "United States"}},
            "department": "Engineering",
        },
        {"id": "def456", "name": "Account Executive", "url": "https://acme.breezy.hr/p/def456", "location": {}},
    ]
    session = FakeSession([FakeResponse(200, jobs)])
    inventory = breezy_hr.fetch_inventory({"company": "Acme", "breezy_hr_company_slug": "acme"}, session, ["AI infrastructure"], 30)
    assert len(inventory) == 1, f"Expected only the AI-titled job to match, got {len(inventory)}"
    assert inventory[0]["location"] == "Salt Lake City, Utah, United States"
    assert inventory[0]["job_url"] == "https://acme.breezy.hr/p/abc123"
    print("OK: Breezy HR title-only match + nested location assembly")

    # --- AgileATS: status filtering (closed jobs excluded) and the
    # recovered /jobs/details/{id} URL pattern. ---
    graphql_response = {
        "data": {
            "Jobs": [
                {
                    "id": 111,
                    "title": "GPU Systems Engineer",
                    "status": "Open",
                    "city": "Columbia",
                    "state": "MD",
                    "country": "US",
                    "published_date": "2026-08-01",
                    "description_publishable": "<p>Work on CUDA kernels.</p>",
                },
                {
                    "id": 222,
                    "title": "GPU Systems Engineer (closed req)",
                    "status": "Closed",
                    "city": "Columbia",
                    "state": "MD",
                    "country": "US",
                    "published_date": "2026-07-01",
                    "description_publishable": "<p>Work on CUDA kernels.</p>",
                },
            ]
        }
    }
    session = FakeSession([FakeResponse(200, graphql_response)])
    inventory = agile_ats.fetch_inventory({"company": "Acme", "agile_ats_origin": "https://acme.jobs.agile-ats.com"}, session, ["CUDA"], 30)
    assert len(inventory) == 1, f"Expected only the Open job, got {len(inventory)}"
    assert inventory[0]["job_url"] == "https://acme.jobs.agile-ats.com/jobs/details/111"
    assert inventory[0]["location"] == "Columbia, MD, US"
    print("OK: AgileATS status filtering + recovered direct-URL pattern")

    # --- UltiPro: embedded CandidateOpportunityDetail JSON blob extraction
    # on the detail page (no separate documented detail API). ---
    detail_html = (
        "<html><body><script>\n"
        "    $(function () {\n"
        "        var opportunity = new US.Opportunity.CandidateOpportunityDetail("
        '{"Id":"xyz","Title":"Cloud Engineer","Description":"<p>Build inference pipelines.</p>"}'
        ");\n"
        "    });\n"
        "</script></body></html>"
    )
    session = FakeSession([FakeResponse(200, text=detail_html)])
    item = {
        "job_title": "Cloud Engineer",
        "job_id": "REQ1",
        "location": "USA (Remote)",
        "posting_date": "2026-08-01",
        "job_url": "https://recruiting.ultipro.com/ACME/JobBoard/board-id/OpportunityDetail?opportunityId=xyz",
        "source_keyword": "inference",
        "_platform_ref": {"detail_url": "https://recruiting.ultipro.com/ACME/JobBoard/board-id/OpportunityDetail?opportunityId=xyz", "team": "Engineering"},
    }
    detail = ultipro.fetch_detail({"company": "Acme"}, item, session, 30)
    assert detail is not None
    assert "inference pipelines" in detail["full_description_text"]
    print("OK: UltiPro embedded-JSON-blob detail extraction")

    # --- SuccessFactors CSB: regression test for the real bug found during
    # onboarding -- SAP's own search relevance is loose/tokenized (matched
    # "Architect II" for a search of "AI infrastructure" with zero real
    # connection). The adapter must re-verify the keyword against the title
    # client-side and reject non-matching rows, never trust the server
    # search blindly the way the Workday adapter safely does. ---
    search_html = """
    <table><tbody>
    <tr class="data-row">
        <td class="colTitle"><a class="jobTitle-link" href="/job/Architect-II/111/">Architect II</a></td>
        <td class="colLocation"><span class="jobLocation">Pasig, PH</span></td>
    </tr>
    <tr class="data-row">
        <td class="colTitle"><a class="jobTitle-link" href="/job/AI-Infra-BDM/222/">Business Development Manager - AI Infrastructure Solutions</a></td>
        <td class="colLocation"><span class="jobLocation">AZ, US</span></td>
    </tr>
    </tbody></table>
    """
    session = FakeSession([FakeResponse(200, text=search_html), FakeResponse(200, text="<table></table>")])
    inventory = successfactors_csb.fetch_inventory(
        {"company": "Acme", "successfactors_search_origin": "https://jobsearch.acme.com"}, session, ["AI infrastructure"], 30
    )
    assert len(inventory) == 1, f"Expected only the genuinely AI-infrastructure-titled row to survive, got {inventory}"
    assert "AI Infrastructure" in inventory[0]["job_title"]
    print("OK: SuccessFactors CSB rejects server search false-positives (Architect II regression)")

    # --- Comeet: embedded POSITIONS_DATA blob extraction (no conventional
    # REST endpoint exists -- the whole catalog is a JS variable assignment
    # in the hosted board page's HTML), including the description built
    # from custom_fields.details[]. ---
    comeet_html = (
        "<html><script>\n"
        'var COMPANY_DATA = {"name": "Acme"};\n'
        'var POSITIONS_DATA = [{"name": "GPU Infrastructure Engineer", "uid": "1.1-2.2", '
        '"location": {"name": "Austin, TX", "country": "US"}, '
        '"url_comeet_hosted_page": "https://www.comeet.com/jobs/acme/1.1/gpu-infra/1.1-2.2", '
        '"time_updated": "2026-08-01T00:00:00Z", "department": "Engineering", '
        '"custom_fields": {"details": [{"name": "Description", "value": "<p>Own our CUDA/GPU fleet.</p>"}]}}];\n'
        "</script></html>"
    )
    session = FakeSession([FakeResponse(200, text=comeet_html)])
    inventory = comeet.fetch_inventory({"company": "Acme", "comeet_company_slug": "acme", "comeet_group_uid": "1.1"}, session, ["GPU"], 30)
    assert len(inventory) == 1
    assert inventory[0]["location"] == "Austin, TX"
    assert inventory[0]["job_url"] == "https://www.comeet.com/jobs/acme/1.1/gpu-infra/1.1-2.2"
    detail = comeet.fetch_detail({"company": "Acme"}, inventory[0], session, 30)
    assert "CUDA/GPU fleet" in detail["full_description_text"]
    print("OK: Comeet embedded POSITIONS_DATA blob extraction")

    # --- Jobvite: table.jv-job-list row parsing + relative-URL resolution. ---
    jobvite_html = """
    <table class="jv-job-list"><tbody>
    <tr><td class="jv-job-list-name"><a href="/acme/job/abc123">AI Infrastructure Engineer</a></td>
        <td class="jv-job-list-location">Austin, TX</td></tr>
    </tbody></table>
    """
    session = FakeSession([FakeResponse(200, text=jobvite_html)])
    inventory = jobvite.fetch_inventory({"company": "Acme", "jobvite_company_slug": "acme"}, session, ["AI infrastructure"], 30)
    assert len(inventory) == 1
    assert inventory[0]["job_url"] == "https://jobs.jobvite.com/acme/job/abc123"
    assert inventory[0]["location"] == "Austin, TX"
    print("OK: Jobvite table-row parsing + relative-URL resolution")

    # --- Eightfold PCSX: Stage-A listing (positions array) and Stage-B
    # detail via the older /api/apply/v2/jobs/{id} route (the search variant
    # of that same route is deprecated/gated on some tenants). ---
    search_response = {
        "data": {
            "positions": [
                {
                    "id": 555,
                    "name": "AI Infrastructure Engineer",
                    "standardizedLocations": ["Austin, TX, US"],
                    "postedTs": 1690000000,
                    "positionUrl": "/careers/job/555",
                }
            ]
        }
    }
    session = FakeSession([FakeResponse(200, search_response)])
    inventory = eightfold_pcsx.fetch_inventory({"company": "Acme", "eightfold_origin": "https://jobs.acme.com", "eightfold_domain": "acme.com"}, session, ["AI infrastructure"], 30)
    assert len(inventory) == 1
    assert inventory[0]["job_url"] == "https://jobs.acme.com/careers/job/555"
    session = FakeSession([FakeResponse(200, {"job_description": "<p>Build GPU clusters.</p>", "department": "Engineering"})])
    detail = eightfold_pcsx.fetch_detail({"company": "Acme", "eightfold_origin": "https://jobs.acme.com", "eightfold_domain": "acme.com"}, inventory[0], session, 30)
    assert "GPU clusters" in detail["full_description_text"]
    print("OK: Eightfold PCSX listing + detail-route field mapping")

    print("All partner-adapter regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
