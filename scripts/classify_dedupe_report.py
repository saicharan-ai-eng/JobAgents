#!/usr/bin/env python3
"""Classify, career-rank, deduplicate, and report qualifying job postings."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dedup_lib import DedupInvariantError, date_sort_value, deduplicate_included, job_sort_key, normalize_key
from source_history import TERMINAL_EXCLUSION_STATUSES
from us_location_filter import annotate_us_location

# Single source of truth for the bounded early-career experience cap (see
# CLAUDE.md "Experience" / the 2026-08-14 <=3-year policy migration). Every
# comparison against the cap in this file reads this constant -- never a
# scattered literal -- so the policy can be re-tuned in one place.
MAX_REQUIRED_EXPERIENCE_YEARS = 3

DOMAIN_PATTERNS: dict[str, re.Pattern[str]] = {
    "AI": re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])", re.I),
    "Artificial Intelligence": re.compile(r"\bartificial intelligence\b", re.I),
    "Machine Learning": re.compile(r"\bmachine learning\b", re.I),
    "ML": re.compile(r"(?<![A-Za-z])ML(?![A-Za-z])", re.I),
    "Deep Learning": re.compile(r"\bdeep learning\b", re.I),
    "LLM": re.compile(r"(?<![A-Za-z])LLMs?(?![A-Za-z])", re.I),
    "Large Language Model": re.compile(r"\blarge language models?\b", re.I),
    "Generative AI": re.compile(r"\bgenerative AI\b|(?<![A-Za-z0-9])Gen[- ]?AI(?![A-Za-z0-9])", re.I),
    "Computer Vision": re.compile(r"\bcomputer vision\b", re.I),
    "NLP": re.compile(r"(?<![A-Za-z])NLP(?![A-Za-z])|\bnatural language processing\b", re.I),
    "CUDA": re.compile(r"(?<![A-Za-z])CUDA(?![A-Za-z])", re.I),
    "GPU": re.compile(r"(?<![A-Za-z])GPUs?(?![A-Za-z])", re.I),
    "Data Center AI": re.compile(r"\bdata cent(?:er|re) AI\b", re.I),
    "MLOps": re.compile(r"(?<![A-Za-z])MLOps(?![A-Za-z])", re.I),
    "AI Infrastructure": re.compile(r"\bAI infrastructure\b", re.I),
    "Accelerated Computing": re.compile(r"\baccelerated computing\b", re.I),
    "NVIDIA": re.compile(r"\bNVIDIA\b", re.I),
    "Model Training": re.compile(r"\bmodel training\b|\bdistributed training\b", re.I),
    "Inference": re.compile(r"\binference\b", re.I),
    "AI Cloud": re.compile(r"\bAI cloud\b|\bcloud AI\b", re.I),
    "Data Science": re.compile(r"\bdata science\b", re.I),
}

EXPLICIT_JUNIOR = re.compile(
    r"\b(?<!non-)(?<!non )(intern(?:ship)?|co-?op|new grad(?:uate)?|university grad(?:uate)?|early career|entry[- ]level)\b",
    re.I,
)
SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|director|manager|head|distinguished|fellow|vice president|vp)\b",
    re.I,
)
JUNIOR_TITLE = re.compile(
    r"\b(associate|junior|entry[- ]level|early career|new grad(?:uate)?|intern(?:ship)?)\b|\b(engineer|scientist|developer|analyst)\s+(?:I|1)\b",
    re.I,
)
RANGE = re.compile(r"\b(\d+)\s*(?:-|–|—|to)\s*(\d+)\s+years?\b", re.I)
PLUS = re.compile(r"\b(\d+)\s*\+\s*years?\b", re.I)
MINIMUM = re.compile(r"\b(?:minimum of|at least|minimum)\s*(\d+)\s+years?\b", re.I)
# "N or more years" is a distinct open-ended-floor phrasing not covered by
# MINIMUM (which only matches a "minimum of/at least/minimum" *prefix*) --
# e.g. "3 or more years" must be treated exactly like "3+ years" (excluded
# under the <=3-year cap), not silently unparsed.
OR_MORE = re.compile(r"\b(\d+)\s*(?:or\s+more)\s+years?\b", re.I)
SINGLE = re.compile(r"\b(\d+)\s+years?\s+(?:of\s+)?(?:relevant\s+|professional\s+|industry\s+)?experience\b", re.I)

# A genuine alternative-qualification-path connector must *structurally*
# bridge two complete requirement clauses -- i.e. the entire span of text
# between one year-requirement match and the next is nothing but the
# connector itself (optionally "or a/an/the <degree>", "or alternatively",
# "alternatively", plus a trailing linking word). This deliberately rejects
# any "or" that merely occurs somewhere in an unrelated technology list
# ("Python or Java"), noun-phrase list ("cloud or on-premises systems"),
# Oxford-comma list ("CUDA, C++, or Python"), or elsewhere in a longer
# sentence/paragraph between the two numbers ("...software engineering,
# platform engineering, or developer productivity roles, with at least...") --
# none of those are alternative *year* requirements, and the previous
# whole-zone `\bor\b` search falsely matched all of them, silently replacing a
# governing senior year figure (e.g. 15+) with an unrelated smaller number
# that happened to appear later in the same qualifications block.
_DEGREE_BRIDGE_TERM = (
    r"(?:bachelor|master|ph\.?d|doctorate|associate|undergraduate|graduate)(?:'s)?\s*(?:degree)?"
    r"|advanced degree|equivalent(?:\s+degree)?|degree"
)
GENUINE_ALT_PATH_BRIDGE = re.compile(
    r"^\s*(?:(?:of\s+)?experience\s+)?[,;]?\s*(?:or\s+alternatively|alternatively|or)\b"
    r"(?:\s+(?:a|an|the))?"
    r"(?:\s+(?:" + _DEGREE_BRIDGE_TERM + r"))?"
    r"\s*(?:with|and|plus|,)?\s*$",
    re.I,
)

REQUIRED_HEADING = re.compile(
    r"(?:Basic|Required|Minimum)\s+Qualifications\b\s*:?"
    r"|\bWhat\s+we\s+need\s+to\s+see\s*:?"
    r"|\bWhat\s+you(?:'ll| will)\s+need\s*:?"
    r"|\bMust\s+[Hh]aves?\s*:?",
    re.I,
)
PREFERRED_HEADING = re.compile(
    r"(?:Preferred|Desired)\s+Qualifications\b\s*:?"
    r"|\bNice[- ]to[- ]haves?\s*:?"
    r"|\bBonus\s+[Pp]oints?\s*:?"
    r"|\bWays\s+to\s+stand\s+out(?:\s+from\s+the\s+crowd)?\s*:?"
    r"|\bGood\s+to\s+have\s*:?"
    r"|\bPreferred\s+Skills\s*:?",
    re.I,
)

# Generic multi-discipline candidate-pipeline postings: score on whatever
# specialization happens to be listed among many, not on the actual role.
FUNNEL_PATTERN = re.compile(
    r"central pipeline of (?:software|hardware)?\s*(?:development )?engineer"
    r"|considered for (?:current and future|future and current)\s+[a-z ]{0,40}openings"
    r"|building a (?:central )?pipeline of (?:software|hardware) (?:development )?engineer",
    re.I,
)
FUNNEL_TITLE_EXEMPT = re.compile(
    r"\b(?:ML|machine learning|inference|GPU|CUDA|Neuron|Trainium|Inferentia|AI infrastructure|AI/ML infrastructure)\b",
    re.I,
)

# Labels that must never establish domain eligibility on their own, because
# a match is at least as likely to come from a bare mention of the term
# itself, or a company/product *name*, as from a genuine description of
# hands-on AI/ML/GPU work:
#   - "AI" / "ML": generic two-letter tokens, trivially over-matched.
#   - "NVIDIA": the hiring company's own name -- appears in essentially every
#     NVIDIA posting by definition (boilerplate mission statement, "About
#     NVIDIA" sections, etc.), so it is not evidence the *role* does
#     AI/ML/GPU work. Requires a real second signal (CUDA, GPU computing,
#     TensorRT, inference, model serving, accelerated computing, etc.) --
#     see run 20260812T152540Z manual-review corrections (Senior Memory
#     Subsystem Firmware Engineer, ASIC Verification Engineer, Lab
#     Operations Site Supervisor, all excluded for exactly this reason).
#   - "AI Cloud": matches "Google Cloud AI"/"AI cloud" -- frequently a
#     product/org *name* (e.g. Google's "Google Cloud AI" org label) rather
#     than a technical claim.
# This is the general "vendor/company name cannot establish eligibility
# alone" rule; extend this set if another source later adds a similar bare
# company/product-name-driven domain pattern.
NEEDS_CORROBORATION_LABELS = {"AI", "ML", "NVIDIA", "AI Cloud"}
TITLE_DOMAIN_HINT = re.compile(
    r"\bAI\b|\bML\b|\bGPU\b|\bCUDA\b|\binference\b|\bdeep learning\b|\bdata science\b|\baccelerated computing\b",
    re.I,
)

# Body/title fit-signal labels that are too generic to unlock Priority 1/2 on
# their own; they still contribute score, but need a second, more specific
# signal to actually unlock the tier.
GENERIC_P1_ONLY_LABELS = {"AI infrastructure", "AI/ML infrastructure title (title)"}
GENERIC_P2_ONLY_LABELS = {"Distributed systems", "GPU"}

MARKETING_TITLE = re.compile(
    r"\bads?\s+specialist\b|\badvertising\b|\bmarketing\b|\bcampaign\s+manager\b|\bbrand\b"
    r"|\bcontent\s+marketing\b|\bSEO\b|\bgrowth\s+marketing\b|\bdemand\s+generation\b"
    r"|\bPR\s+specialist\b|\bpublic\s+relations\b|\bcorporate\s+communications\b|\bmedia\s+relations\b"
    r"|\bcommunications\s+(?:specialist|manager|lead)\b",
    re.I,
)
HR_TITLE = re.compile(
    r"\bhuman\s+resources\b|\bHRBP\b|\bHR\s+business\s+partner\b|\bpeople\s+partner\b"
    r"|\btalent\s+acquisition\b|\brecruiter\b|\brecruiting\b|\bpeople\s+operations\b",
    re.I,
)
SALES_TITLE = re.compile(
    r"\baccount executive\b|\bsales representative\b|\binside sales\b"
    r"|\bbusiness development representative\b|\bbusiness development manager\b|\bBDR\b|\bSDR\b|\bsales academy\b"
    r"|\bcustomer acquisition representative\b|\baccount manager\b|\brenewals? sales\b",
    re.I,
)
SUPPORT_TITLE = re.compile(
    r"\btechnical support\b|\bsupport engineer\b|\bcustomer support\b|\bhelp ?desk\b|\bcustomer service\b",
    re.I,
)
CONSULTING_TITLE = re.compile(
    r"\bconsultant\b|\bproserve\b|\bprofessional services\b|\bdelivery consultant\b",
    re.I,
)
CUSTOMER_FACING_TECH_TITLE = re.compile(
    r"\bsolutions? architect\b|\bsolution engineer\b|\bsales engineer\b|\bpre-?sales\b"
    r"|\bfield engineer\b|\btechnical account manager\b",
    re.I,
)
RESEARCH_TITLE = re.compile(
    r"\bapplied scientist\b|\bresearch scientist\b|\bresearch engineer\b|\bresearch software engineer\b"
    r"|\bph\.?d\.?\s*intern\b|\bresearch intern\b",
    re.I,
)
HARDWARE_NON_AI_TITLE = re.compile(
    r"\bsignal integrity\b|\bpower integrity\b|\bcontrols? engineer\b|\bmechanical engineer\b"
    r"|\bRF engineer\b|\bPCB engineer\b|\belectrical engineer\b",
    re.I,
)
ENGINEERING_TITLE = re.compile(
    r"\bengineer\b|\bdeveloper\b|\bscientist\b|\bSDE\b|\barchitect\b",
    re.I,
)

HANDS_ON_SIGNAL = re.compile(
    r"\bhands[- ]on\b(?!\s+experience)[^.]{0,120}\b(?:build|building|implement|implementing|develop|developing"
    r"|deploy|deploying|code|coding|architect(?:ing)? and build)\b",
    re.I,
)

# Generic corporate self-description / benefits / EEO boilerplate that recurs
# near-verbatim across a company's job postings. A *whole paragraph* matching
# one of these is dropped before domain/role-function signal matching so
# that, e.g., a company's stock "...platform for AI, IoT and the cloud"
# tagline cannot by itself qualify an otherwise unrelated posting. This list
# is intentionally unchanged from its original form/granularity (paragraph-
# level only) -- it is production-proven at that granularity.
BOILERPLATE_PARA_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bis a leading provider of open source software\b", re.I),
    re.compile(r"\bis a pioneering tech firm\b", re.I),
    re.compile(r"\bis a growing international software company\b", re.I),
    re.compile(r"\bis an equal opportunity employer\b", re.I),
    re.compile(r"\bwe are proud to foster a workplace free from discrimination\b", re.I),
    re.compile(r"^about\s+[A-Z][\w&.\-]*\s*$", re.I),
    re.compile(r"\bwhat we offer (?:colleagues|you)\b", re.I),
    re.compile(r"\bpersonal learning and development budget\b", re.I),
    re.compile(r"\bpriority pass\b.{0,40}\btravel upgrades\b", re.I),
]

# Boilerplate *sentences* -- matched only at sentence granularity (see
# strip_boilerplate), never at whole-paragraph granularity. These recurring
# mission-statement / org-description sentences are typically embedded
# *inside* a larger paragraph alongside genuine role content -- sometimes in
# the very same "paragraph" as real content because the source concatenated
# text without a blank-line break (observed on both Workday CXS descriptions,
# see workday_fetch.py strip_html, and on some Google Cloud postings where
# two logically-distinct paragraphs are joined by a single "\n"). Matching
# these at paragraph granularity would drop the entire surrounding paragraph
# -- including the genuine content -- instead of just the boilerplate
# sentence; sentence-level matching removes only the sentence itself.
BOILERPLATE_SENTENCE_PATTERNS: list[re.Pattern[str]] = [
    # NVIDIA's near-verbatim reused mission-statement intro, which recurs
    # across unrelated postings (Build/DevOps, DPU Platform, CAD Tools, CAD
    # Automation, etc. -- see run 20260812T152540Z manual-review corrections)
    # and by itself mentions AI/GPU/Accelerated Computing regardless of the
    # role's actual content.
    re.compile(r"\btapping into the unlimited potential of AI\b", re.I),
    re.compile(r"\bour GPU acts as the brains of computers, robots,? and self-driving cars\b", re.I),
    re.compile(r"\bNVIDIA has been transforming computer graphics,? PC gaming,? and accelerated computing\b", re.I),
    # Google's near-verbatim reused SWE-posting intro sentence (recurs across
    # 100+ unrelated Google Cloud postings) and the "AI and Infrastructure"
    # hiring org's own reused self-description sentences -- same run's Google
    # Cloud manual-review corrections.
    re.compile(r"\bengineers who bring fresh ideas from all areas\b", re.I),
    re.compile(r"\bAI and Infrastructure team is redefining\b", re.I),
    re.compile(r"\bdelivering AI and Infrastructure at unparalleled scale\b", re.I),
    re.compile(r"\bempowering the development of our cutting-edge AI models\b", re.I),
    re.compile(r"\bshaping the future of world-leading hyperscale computing\b", re.I),
]

# Recurring hiring-org / product-umbrella *names* that get appended to job
# titles (and reused description paragraphs) regardless of what the specific
# role actually does -- e.g. Google Cloud's "AI and Infrastructure" org is
# appended to postings across many unrelated teams. These are stripped from
# the domain-scope text outright (not just sentence-boilerplate) so a title
# suffix like "Software Engineer, Network Automation, AI and Infrastructure"
# can't smuggle a domain match in through the org name alone. This is the
# same principle as NEEDS_CORROBORATION_LABELS below, applied to multi-word
# org names rather than single tokens.
ORG_UMBRELLA_NAME_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bAI and Infrastructure\b", re.I),
    re.compile(r"\bGoogle Cloud AI\b", re.I),
]

# A domain term that appears only as one item in a "such as X, Y, and Z" /
# "like X, Y and Z" example list describes the company's ecosystem or
# partners, not this role's actual responsibilities. The 200-char window is
# evidence-based: Google's real reused SWE-intro list ("...including
# information retrieval, distributed computing, large-scale system design,
# networking and data storage, security, artificial intelligence, natural
# language processing...") puts "artificial intelligence" ~116 chars after
# "including" -- comfortably inside 200 but outside the previous 80-char cap,
# which is why that recurring list wasn't being recognized as one.
LIST_CONTEXT_MARKER = re.compile(r"\b(?:such as|like|including|e\.g\.)\b", re.I)
LIST_CONTEXT_MAX_SPAN = 200

AI_ML_GPU_HANDSON_TERM = re.compile(
    r"\bAI\b|\bML\b|\bGPU\b|\bCUDA\b|\bNPU\b|\bmachine learning\b|\bdeep learning\b"
    r"|\binference\b|\bmodels?\b|\baccelerat(?:ed|or|ors)\b",
    re.I,
)
IMPLEMENTATION_VERB = re.compile(
    r"\b(build(?:ing)?|implement(?:ing)?|develop(?:ing)?|deploy(?:ing)?|design(?:ing)?"
    r"|architect(?:ing)?|code|coding|optimi[sz](?:e|ing)|integrat(?:e|ing)|engineer(?:ing)?)\b",
    re.I,
)

DEFAULT_TIERS = {
    1: "Excellent fit — Inference & AI Infrastructure",
    2: "Strong adjacent fit — GPU & Systems",
    3: "General AI/ML fit",
    4: "Low-priority backup",
}

ROLE_FUNCTIONS = {
    "hands_on_engineering",
    "research",
    "customer_facing_technical",
    "consulting",
    "support",
    "sales",
    "marketing",
    "hr",
    "hardware_non_ai",
    "other",
}


def strip_boilerplate(text: str) -> str:
    """Drop generic corporate boilerplate (company self-description, EEO
    statement, benefits section, reused mission-statement intro) rather than
    role content.

    Two passes, both strictly additive (can only remove more, never less):
    1. Paragraph-level (blank-line delimited) -- the original mechanism,
       needed for patterns anchored to a whole standalone paragraph like a
       bare "About Acme" heading.
    2. Sentence-level -- required because some sources (Workday CXS via
       workday_fetch.py's strip_html) collapse all whitespace to single
       spaces, so a description is one giant paragraph with no blank-line
       breaks at all. Without this pass, a boilerplate sentence embedded in
       an otherwise-genuine Workday description could never be recognized
       (paragraph-splitting would either miss it entirely or, worse, have to
       drop the *entire* description to remove it).
    """
    if not text:
        return text
    paragraphs = re.split(r"\n\s*\n", text)
    kept_paragraphs = [p for p in paragraphs if not any(pat.search(p) for pat in BOILERPLATE_PARA_PATTERNS)]
    stage1 = "\n\n".join(kept_paragraphs)

    sentences = re.split(r"(?<=[.!?])\s+", stage1)
    kept_sentences = [s for s in sentences if not any(pat.search(s) for pat in BOILERPLATE_SENTENCE_PATTERNS)]
    return " ".join(kept_sentences)


def strip_org_umbrella_names(text: str) -> str:
    """Scrub recurring hiring-org/product-umbrella name strings (see
    ORG_UMBRELLA_NAME_PATTERNS) out of the text entirely -- these are names,
    not descriptions of work, and can appear in a title suffix where no
    sentence-level boilerplate stripping would ever reach them."""
    if not text:
        return text
    for pattern in ORG_UMBRELLA_NAME_PATTERNS:
        text = pattern.sub(" ", text)
    return text


def _sentence_span(text: str, start: int, end: int) -> tuple[int, int]:
    s = text.rfind(".", 0, start)
    s = 0 if s == -1 else s + 1
    e = text.find(".", end)
    e = len(text) if e == -1 else e
    return s, e


def _is_example_list_mention(text: str, start: int, end: int) -> bool:
    """True if the match at [start:end) is one item in a 'such as X, Y, Z'
    style example list, rather than a standalone claim about this role's own
    responsibilities. Requires the marker ("such as"/"like"/"including"/
    "e.g.") to appear shortly *before* the match with no clause break in
    between -- i.e. the match must actually be inside that specific list,
    not merely share a sentence with an unrelated "including"/comma."""
    s, _e = _sentence_span(text, start, end)
    preceding = text[s:start]
    marker_matches = list(LIST_CONTEXT_MARKER.finditer(preceding))
    if not marker_matches:
        return False
    marker_end_abs = s + marker_matches[-1].end()
    between = text[marker_end_abs:start]
    if re.search(r"[.;:]", between) or len(between) > LIST_CONTEXT_MAX_SPAN:
        return False
    return True


def _domain_scope_text(job: dict[str, Any]) -> str:
    """Text scope used for domain eligibility and hands-on-signal checks:
    title/header + general description + required-qualifications text, with
    boilerplate paragraphs stripped and Preferred/Nice-to-have text excluded
    (a nice-to-have skill does not establish that the role requires it)."""
    header_text = "\n".join(
        str(x) for x in [job.get("job_title"), job.get("experience_level_text"), job.get("team_department")] if x
    )
    desc = "\n".join(str(x) for x in [job.get("short_description"), job.get("full_description_text")] if x)
    required_text, _preferred_text, general_text = split_qualification_sections(desc)
    scoped = "\n".join([header_text, general_text, required_text])
    return strip_org_umbrella_names(strip_boilerplate(scoped))


def _has_hands_on_domain_signal(text: str, window: int = 100) -> bool:
    for m in AI_ML_GPU_HANDSON_TERM.finditer(text):
        span_start = max(0, m.start() - window)
        span_end = min(len(text), m.end() + window)
        if IMPLEMENTATION_VERB.search(text[span_start:span_end]):
            return True
    return False


def _normalize_legacy_fields(posting: dict[str, Any]) -> None:
    """Read old human_review_* fields if present; only ever write the new agent_review_* names."""
    if "human_review_correction" in posting:
        posting.setdefault("agent_review_correction", posting.pop("human_review_correction"))
    if "human_review_note" in posting:
        posting.setdefault("agent_review_note", posting.pop("human_review_note"))


def load_source_files(raw_dir: Path) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for path in sorted(raw_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            sources.append(
                {
                    "company": path.stem,
                    "source_url": None,
                    "fetched_at": None,
                    "status": "failed",
                    "reason": f"Invalid JSON: {type(exc).__name__}: {exc}",
                    "raw_posting_count": 0,
                    "postings": [],
                }
            )
            continue
        postings = data.get("postings") if isinstance(data.get("postings"), list) else []
        for posting in postings:
            if isinstance(posting, dict):
                _normalize_legacy_fields(posting)
        data["postings"] = postings
        data["raw_posting_count"] = len(postings)
        data.setdefault("slug", path.stem)
        sources.append(data)
    return sources


def load_fit_config(path: Path) -> tuple[dict[int, str], list[dict[str, Any]], list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    tiers = {
        int(item["priority"]): str(item["label"])
        for item in data.get("tiers", [])
        if isinstance(item, dict) and "priority" in item and "label" in item
    }
    if not tiers:
        tiers = dict(DEFAULT_TIERS)

    def compile_signals(items: Any) -> list[dict[str, Any]]:
        compiled: list[dict[str, Any]] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            pattern = str(item.get("pattern") or "").strip()
            if not label or not pattern:
                continue
            compiled.append(
                {
                    "label": label,
                    "regex": re.compile(pattern, re.I),
                    "points": max(0, int(item.get("points", 0))),
                    "priority": min(4, max(1, int(item.get("priority", 4)))),
                }
            )
        return compiled

    return tiers, compile_signals(data.get("signals")), compile_signals(data.get("title_signals"))


def combine_text(job: dict[str, Any]) -> str:
    fields = [
        job.get("job_title"),
        job.get("experience_level_text"),
        job.get("team_department"),
        job.get("short_description"),
        job.get("full_description_text"),
    ]
    return "\n".join(str(x) for x in fields if x)


def split_qualification_sections(text: str) -> tuple[str, str, str]:
    """Split description text into (required_text, preferred_text, general_text).

    If no explicit Required/Preferred heading is found, everything is returned
    as general_text so existing full-weight scoring behavior is preserved.
    """
    if not text:
        return "", "", ""
    headings: list[tuple[int, int, str]] = []
    for m in REQUIRED_HEADING.finditer(text):
        headings.append((m.start(), m.end(), "required"))
    for m in PREFERRED_HEADING.finditer(text):
        headings.append((m.start(), m.end(), "preferred"))
    if not headings:
        return "", "", text
    headings.sort(key=lambda h: h[0])
    general = text[: headings[0][0]]
    required_parts: list[str] = []
    preferred_parts: list[str] = []
    for i, (_start, end, kind) in enumerate(headings):
        section_end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        content = text[end:section_end]
        (required_parts if kind == "required" else preferred_parts).append(content)
    return "\n".join(required_parts), "\n".join(preferred_parts), general


def _year_matches(text: str) -> list[tuple[int, int, int, str, bool]]:
    """Each match is (start, end, years, raw_text, open_ended).

    `open_ended` is True for PLUS ("5+ years"), MINIMUM ("at least 5 years" /
    "minimum of 5 years"), and OR_MORE ("5 or more years") matches: all three
    state only a floor with no stated maximum. RANGE ("2-5 years") and SINGLE
    ("5 years of experience") matches are closed/bounded figures. An
    open-ended floor is NOT the same as a bounded maximum -- "5+ years" must
    never be treated as equivalent to "up to 5 years" (see classify_experience).
    """
    found: list[tuple[int, int, int, str, bool]] = []
    for m in PLUS.finditer(text):
        found.append((m.start(), m.end(), int(m.group(1)), m.group(0), True))
    for m in RANGE.finditer(text):
        lo, hi = int(m.group(1)), int(m.group(2))
        found.append((m.start(), m.end(), max(lo, hi), m.group(0), False))
    for m in MINIMUM.finditer(text):
        found.append((m.start(), m.end(), int(m.group(1)), m.group(0), True))
    for m in OR_MORE.finditer(text):
        found.append((m.start(), m.end(), int(m.group(1)), m.group(0), True))
    for m in SINGLE.finditer(text):
        found.append((m.start(), m.end(), int(m.group(1)), m.group(0), False))
    found.sort(key=lambda x: x[0])
    # Drop matches whose span overlaps an already-kept match (same underlying number).
    filtered: list[tuple[int, int, int, str, bool]] = []
    last_end = -1
    for start, end, years, raw, open_ended in found:
        if start < last_end:
            continue
        filtered.append((start, end, years, raw, open_ended))
        last_end = end
    return filtered


def _figure_qualifies(years: int, open_ended: bool) -> bool:
    """A required-experience figure qualifies under the <=N-year cap only
    when it is a genuinely BOUNDED figure (an exact number or a closed
    range) at or under the cap. An open-ended lower bound -- "N+ years",
    "at least N years", "minimum N years", "N or more years" -- never
    qualifies on its own, no matter how low its floor is: "1+ years" and
    "2+ years" are excluded exactly like "5+ years", because none of them
    state a genuine upper bound (see CLAUDE.md "Experience", 2026-08-15
    open-ended-lower-bound fix -- a mandatory requirement must have a
    genuine upper bound of <=MAX_REQUIRED_EXPERIENCE_YEARS to qualify;
    an open-ended floor below the cap is still open-ended, not bounded)."""
    return (not open_ended) and years <= MAX_REQUIRED_EXPERIENCE_YEARS


def _prefer_alt_candidate(
    current: tuple[int, str, bool], candidate: tuple[int, str, bool]
) -> bool:
    """Decide whether `candidate` should replace `current` as the resolved
    figure for a genuine alternative-qualification path (see
    GENUINE_ALT_PATH_BRIDGE). A genuine either/or path is eligible if ANY
    branch offers a real bounded qualifying figure, so a candidate that
    itself qualifies (see _figure_qualifies) is always preferred over one
    that does not -- e.g. "5+ years OR 3 years" must resolve to the bounded
    "3 years" (qualifies), not the numerically-smaller-but-open-ended "5+"
    even though 5 > 3 is not itself the deciding factor here. When both (or
    neither) candidate qualifies, prefer the smaller raw year figure, same
    as prior behavior, so already-covered cases (e.g. two bounded figures,
    or two open-ended figures) are unaffected."""
    cur_years, _cur_raw, cur_open_ended = current
    cand_years, _cand_raw, cand_open_ended = candidate
    cur_qualifies = _figure_qualifies(cur_years, cur_open_ended)
    cand_qualifies = _figure_qualifies(cand_years, cand_open_ended)
    if cand_qualifies != cur_qualifies:
        return cand_qualifies
    return cand_years < cur_years


def resolve_required_years(text: str) -> tuple[int, str, bool] | None:
    """Resolve the governing year-requirement figure in a block of text.

    Consecutive year-mentions are treated as a single stacked (AND)
    requirement -> take the maximum, UNLESS the entire span of text between
    them is nothing but a genuine alternative-qualification-path connector
    (see GENUINE_ALT_PATH_BRIDGE), in which case they are distinct
    qualification paths -> prefer whichever branch qualifies under the cap
    (see _prefer_alt_candidate), falling back to the smaller raw figure when
    both or neither branch qualifies. An "or" that merely appears somewhere
    inside a longer, unrelated clause between the two numbers does not count
    -- only a connector that structurally *is* the entire bridge between the
    two requirement clauses does.

    Returns (years, raw_text, open_ended) where `open_ended` describes
    whether the governing match itself was an unbounded floor (see
    _year_matches).
    """
    matches = _year_matches(text)
    if not matches:
        return None
    result_years, result_raw, result_open_ended = matches[0][2], matches[0][3], matches[0][4]
    for i in range(1, len(matches)):
        prev_end = matches[i - 1][1]
        cur_start, _cur_end, cur_years, cur_raw, cur_open_ended = matches[i]
        connector_zone = text[prev_end:cur_start]
        if GENUINE_ALT_PATH_BRIDGE.match(connector_zone):
            if _prefer_alt_candidate(
                (result_years, result_raw, result_open_ended), (cur_years, cur_raw, cur_open_ended)
            ):
                result_years, result_raw, result_open_ended = cur_years, cur_raw, cur_open_ended
        else:
            if cur_years > result_years:
                result_years, result_raw, result_open_ended = cur_years, cur_raw, cur_open_ended
    return result_years, result_raw, result_open_ended


# --- needs_review.json ambiguity detection -----------------------------
# Additive, non-blocking signals only: nothing below this point ever changes
# an include/exclude decision. It exists solely to flag genuinely-unresolved
# structural cases into runs/<RUN_ID>/needs_review.json for optional,
# compact follow-up review -- the deterministic pipeline finishes the run
# and produces a decision regardless (see CLAUDE.md "CREATE A REVIEW QUEUE" /
# "CLAUDE REVIEW BOUNDARY"). Reason codes are a controlled vocabulary so the
# review queue stays machine-parseable; not every code is actively triggered
# yet (US-location and domain-gate ambiguity are deliberately fail-closed
# with no ambiguity carve-out per CLAUDE.md, so those two are reserved for a
# future, more surgical trigger rather than flooding the queue today).
REASON_MALFORMED_SOURCE_DATA = "malformed_source_data"
REASON_AMBIGUOUS_EXPERIENCE_PATH = "ambiguous_experience_path"
REASON_CONFLICTING_REQUIRED_QUALIFICATIONS = "conflicting_required_qualifications"
REASON_AMBIGUOUS_US_ELIGIBILITY = "ambiguous_us_eligibility"  # reserved, not yet triggered
REASON_AMBIGUOUS_DOMAIN_RELEVANCE = "ambiguous_domain_relevance"  # reserved, not yet triggered

_WEAK_ALT_PATH_HINT = re.compile(r"\b(?:or|alternatively|equivalent|in lieu of)\b", re.I)
_CLAUSE_BREAK = re.compile(r"[.!?;]")
_MAX_WEAK_BRIDGE_ZONE = 70


def _detect_ambiguous_experience_path(text: str) -> dict[str, Any] | None:
    """A narrow, high-precision structural-ambiguity signal, independent of
    (and never overriding) resolve_required_years' own decision: two
    year-number matches connected by a short, clause-break-free zone that
    contains a weak alternative-path hint (or/alternatively/equivalent/in
    lieu of) but does not structurally satisfy GENUINE_ALT_PATH_BRIDGE --
    *and* choosing stacked (max) vs. alternative (min) would flip the
    <=MAX_REQUIRED_EXPERIENCE_YEARS outcome. Deliberately narrow: it must not
    fire on "cloud or on-premises systems, including 3 years...", "3 years
    ... Python or Java. 5 years ...", nested "including" clauses, or any
    already-recognized genuine bridge -- see test_alt_path_connector.py and
    test_open_ended_experience.py, which this must keep passing unchanged.
    """
    matches = _year_matches(text)
    for i in range(1, len(matches)):
        prev_end = matches[i - 1][1]
        cur_start = matches[i][0]
        zone = text[prev_end:cur_start]
        if len(zone) > _MAX_WEAK_BRIDGE_ZONE or _CLAUSE_BREAK.search(zone):
            continue
        if not _WEAK_ALT_PATH_HINT.search(zone):
            continue
        if GENUINE_ALT_PATH_BRIDGE.match(zone):
            continue  # already confidently resolved as a genuine bridge
        prev = (matches[i - 1][2], matches[i - 1][3], matches[i - 1][4])
        cur = (matches[i][2], matches[i][3], matches[i][4])
        # Two readings of the same pair of figures: STACKED (AND) takes the
        # numerically larger figure, keeping its own open-endedness; ALTERNATIVE
        # (OR) prefers whichever branch genuinely qualifies (see
        # _prefer_alt_candidate), falling back to the smaller figure. Ambiguous
        # only when these two readings disagree about whether the posting
        # qualifies -- open-endedness of each individual figure now matters,
        # not just its bare number (e.g. "2+ years" vs "2 years" already
        # disagree on qualification despite an identical number).
        stacked = cur if cur[0] > prev[0] else prev
        alt = cur if _prefer_alt_candidate(prev, cur) else prev
        stacked_qualifies = _figure_qualifies(stacked[0], stacked[2])
        alt_qualifies = _figure_qualifies(alt[0], alt[2])
        if stacked_qualifies != alt_qualifies:
            return {
                "reason_code": REASON_AMBIGUOUS_EXPERIENCE_PATH,
                "evidence": (
                    f"{matches[i - 1][3]!r} and {matches[i][3]!r} connected by {zone.strip()!r}: unclear "
                    f"whether this is a stacked requirement ({stacked[1]!r}) or an alternative-"
                    f"qualification path ({alt[1]!r}), which changes the <="
                    f"{MAX_REQUIRED_EXPERIENCE_YEARS}-year eligibility outcome."
                ),
            }
    return None


def _detect_malformed_source_data(job: dict[str, Any]) -> dict[str, Any] | None:
    has_body = bool(
        (job.get("full_description_text") or "").strip()
        or (job.get("short_description") or "").strip()
        or (job.get("experience_level_text") or "").strip()
    )
    if has_body:
        return None
    return {
        "reason_code": REASON_MALFORMED_SOURCE_DATA,
        "evidence": "No description, short_description, or experience_level_text present -- nothing to evaluate experience/domain fit against.",
    }


def detect_ambiguous_signals(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact, additive ambiguity flags for this posting (see the module
    note above). Independently re-derives its own required/general text
    scope rather than sharing state with classify_experience, matching this
    file's existing pattern of each classify_* helper scoping its own view
    (see classify_fit's own split_qualification_sections call)."""
    malformed = _detect_malformed_source_data(job)
    if malformed:
        return [malformed]  # nothing else can be meaningfully evaluated without body text

    flags: list[dict[str, Any]] = []
    full_text = combine_text(job)
    desc = "\n".join(str(x) for x in [job.get("short_description"), job.get("full_description_text")] if x)
    required_text, _preferred_text, general_text = split_qualification_sections(desc)
    header_text = "\n".join(str(x) for x in [job.get("experience_level_text"), job.get("team_department")] if x)
    scoped_text = "\n".join([header_text, general_text, required_text]).strip()

    explicit = EXPLICIT_JUNIOR.search(full_text)
    if explicit and required_text.strip():
        required_only = resolve_required_years(required_text)
        if required_only:
            years, raw, open_ended = required_only
            if not _figure_qualifies(years, open_ended):
                flags.append(
                    {
                        "reason_code": REASON_CONFLICTING_REQUIRED_QUALIFICATIONS,
                        "evidence": (
                            f"Explicit junior/early-career wording ({explicit.group(0)!r}) is contradicted by a "
                            f"mandatory Required-Qualifications figure ({raw!r}) that does not qualify under the "
                            f"<={MAX_REQUIRED_EXPERIENCE_YEARS}-year cap (open-ended lower bounds never qualify, "
                            f"regardless of floor); the required figure governs and the posting was excluded."
                        ),
                    }
                )

    exp_path = _detect_ambiguous_experience_path(scoped_text if scoped_text else full_text)
    if exp_path:
        flags.append(exp_path)

    return flags


def classify_experience(job: dict[str, Any]) -> tuple[bool, str, str | None, bool]:
    title = str(job.get("job_title") or "")
    full_text = combine_text(job)

    desc = "\n".join(str(x) for x in [job.get("short_description"), job.get("full_description_text")] if x)
    required_text, _preferred_text, general_text = split_qualification_sections(desc)
    header_text = "\n".join(str(x) for x in [job.get("experience_level_text"), job.get("team_department")] if x)

    # Prioritize Required/Basic/Minimum Qualifications text (plus general prose
    # and header fields); Preferred-only text is deliberately excluded here.
    scoped_text = "\n".join([header_text, general_text, required_text]).strip()

    explicit = EXPLICIT_JUNIOR.search(full_text)
    if explicit:
        # A genuine internship/new-grad/entry-level/early-career signal is
        # accepted on its own -- UNLESS the posting's own MANDATORY
        # (Required-Qualifications-scoped, never preferred/general) text
        # states a year figure that itself exceeds the cap: "3+ years
        # required; Master's preferred, early-career culture" is still a 3+
        # requirement and must exclude (see CLAUDE.md "Required/preferred
        # cases"). Deliberately scoped to the *required* heading only (not
        # general prose, which is full of incidental non-requirement year
        # mentions -- "join a team with 15+ years of combined experience" --
        # that must never flip a genuine internship to excluded).
        required_only = resolve_required_years(required_text) if required_text.strip() else None
        if required_only:
            years, raw, open_ended = required_only
            if not _figure_qualifies(years, open_ended):
                return False, "Excluded", raw, False
        label = "Internship" if re.search(r"intern|co-?op", explicit.group(0), re.I) else "Entry-Level"
        source_wording = job.get("experience_level_text") or explicit.group(0)
        return True, label, str(source_wording), False

    if SENIOR_TITLE.search(title):
        return False, "Excluded", "Senior-level title", False

    resolved = resolve_required_years(scoped_text)
    if resolved is None:
        # No explicit required section (or no numbers in it) -- fall back to
        # the full text so sources without clean headings still work.
        resolved = resolve_required_years(full_text)
    if resolved:
        years, raw, open_ended = resolved
        # An open-ended floor ("1+ years", "2+ years", "3+ years", "at least
        # N years", "minimum of N years", "N or more years") states no
        # maximum at all -- it is NEVER equivalent to a bounded "maximum of N
        # years or less" and must exclude regardless of how low its floor is
        # ("2+ years" is REJECTED exactly like "3+ years", not accepted --
        # see CLAUDE.md "Experience", 2026-08-15 open-ended-lower-bound fix).
        # Only a genuinely bounded figure (an exact number, or a closed
        # range) at or under the cap qualifies.
        if _figure_qualifies(years, open_ended):
            return True, "Entry-Level", raw, False
        return False, "Excluded", raw, False

    if JUNIOR_TITLE.search(title):
        return True, "Entry-Level", job.get("experience_level_text") or "Inferred from junior title", True

    return False, "Excluded", job.get("experience_level_text") or "Experience level not established", False


def classify_domain(job: dict[str, Any]) -> list[str]:
    """Domain matches scoped to actual role content: boilerplate paragraphs are
    stripped, Preferred/Nice-to-have-only mentions don't count, and a term that
    only appears as one item in a "such as X, Y, Z" example list is ignored
    (it describes the company's ecosystem/partners, not this role's work)."""
    text = _domain_scope_text(job)
    matches: list[str] = []
    for label, pattern in DOMAIN_PATTERNS.items():
        for m in pattern.finditer(text):
            if not _is_example_list_mention(text, m.start(), m.end()):
                matches.append(label)
                break
    return matches


def domain_eligible(domain_matches: list[str], title: str) -> bool:
    """A bare 'AI'/'ML'/'NVIDIA'/'AI Cloud' mention alone should not qualify a
    posting unless the title itself signals relevance, or a second distinct
    domain signal exists (see NEEDS_CORROBORATION_LABELS)."""
    if not domain_matches:
        return False
    if set(domain_matches) - NEEDS_CORROBORATION_LABELS:
        return True
    if TITLE_DOMAIN_HINT.search(title):
        return True
    if len(set(domain_matches)) >= 2:
        return True
    return False


def classify_role_function(job: dict[str, Any]) -> str:
    title = str(job.get("job_title") or "")
    if MARKETING_TITLE.search(title):
        return "marketing"
    if SALES_TITLE.search(title):
        return "sales"
    if HR_TITLE.search(title):
        return "hr"
    if SUPPORT_TITLE.search(title):
        return "support"
    if CONSULTING_TITLE.search(title):
        return "consulting"
    if CUSTOMER_FACING_TECH_TITLE.search(title):
        return "customer_facing_technical"
    if RESEARCH_TITLE.search(title):
        return "research"
    if HARDWARE_NON_AI_TITLE.search(title):
        return "hardware_non_ai"
    if ENGINEERING_TITLE.search(title):
        return "hands_on_engineering"
    return "other"


def is_generic_funnel(text: str, title: str) -> bool:
    if not FUNNEL_PATTERN.search(text):
        return False
    return not FUNNEL_TITLE_EXEMPT.search(title)


def classify_fit(
    job: dict[str, Any],
    tiers: dict[int, str],
    body_signals: list[dict[str, Any]],
    title_signals: list[dict[str, Any]],
) -> dict[str, Any]:
    title = str(job.get("job_title") or "")
    header_text = "\n".join(str(x) for x in [job.get("experience_level_text"), job.get("team_department")] if x)
    desc = "\n".join(str(x) for x in [job.get("short_description"), job.get("full_description_text")] if x)
    required_text, preferred_text, general_text = split_qualification_sections(desc)
    full_weight_text = "\n".join([header_text, general_text, required_text])

    matches: list[dict[str, Any]] = []
    seen_labels: set[str] = set()

    for signal in body_signals:
        label = signal["label"]
        if label in seen_labels:
            continue
        if signal["regex"].search(full_weight_text):
            matches.append({**signal, "effective_points": signal["points"], "base_label": label})
            seen_labels.add(label)
        elif preferred_text and signal["regex"].search(preferred_text):
            reduced = max(1, round(signal["points"] * 0.4)) if signal["points"] > 0 else 0
            matches.append(
                {
                    **signal,
                    "label": f"{label} (preferred)",
                    "effective_points": reduced,
                    "base_label": label,
                }
            )
            seen_labels.add(label)

    for signal in title_signals:
        title_label = f"{signal['label']} (title)"
        if title_label in seen_labels:
            continue
        if signal["regex"].search(title):
            matches.append(
                {**signal, "label": title_label, "effective_points": signal["points"], "base_label": title_label}
            )
            seen_labels.add(title_label)

    # The bare "NVIDIA" company-name fit signal must not contribute to the
    # score on its own -- every NVIDIA posting mentions the company name, so
    # by itself it is not evidence of AI/ML/GPU work. Same corroboration
    # requirement as domain eligibility (see NEEDS_CORROBORATION_LABELS):
    # require at least one real priority-1/2/3 signal (CUDA, GPU, inference,
    # model serving, machine learning, etc.) alongside it.
    if "NVIDIA" in seen_labels:
        has_substantive_signal = any(m["base_label"] != "NVIDIA" and int(m["priority"]) <= 3 for m in matches)
        if not has_substantive_signal:
            matches = [m for m in matches if m["base_label"] != "NVIDIA"]
            seen_labels.discard("NVIDIA")

    tier1_labels = {m["base_label"] for m in matches if int(m["priority"]) == 1}
    tier1_blocked = bool(tier1_labels) and tier1_labels <= GENERIC_P1_ONLY_LABELS

    tier2_labels = {m["base_label"] for m in matches if int(m["priority"]) == 2}
    tier2_blocked = bool(tier2_labels) and tier2_labels <= GENERIC_P2_ONLY_LABELS

    available_priorities: list[int] = []
    for m in matches:
        p = int(m["priority"])
        if p == 1 and tier1_blocked:
            continue
        if p == 2 and tier2_blocked:
            continue
        available_priorities.append(p)
    priority = min(available_priorities, default=4)

    score = min(100, sum(int(round(m["effective_points"])) for m in matches))
    label = tiers.get(priority, DEFAULT_TIERS[priority])
    matched_labels = [
        str(m["label"]) for m in sorted(matches, key=lambda x: (int(x["priority"]), -x["effective_points"], x["label"]))
    ]
    reason = f"Priority {priority}: {label}."
    if matched_labels:
        reason += " Matched: " + ", ".join(matched_labels) + "."
    else:
        reason += " Qualifies for the broad AI/ML filter but has no stronger specialization signal."
    if tier1_blocked:
        reason += (
            " Note: generic 'AI infrastructure' phrase alone was not corroborated by a specific"
            " inference/GPU/ML-systems signal, so Priority 1 was not granted."
        )
    if tier2_blocked and priority != 1:
        reason += (
            " Note: standalone distributed-systems/GPU mention alone was not corroborated by a"
            " second accelerator/ML-systems signal, so Priority 2 was not granted."
        )

    return {
        "fit_priority": priority,
        "fit_label": label,
        "fit_score": score,
        "fit_keywords_matched": matched_labels,
        "fit_reason": reason,
    }


def apply_role_function_priority_cap(role_function: str, priority: int, text: str) -> tuple[int, bool]:
    if role_function in {"customer_facing_technical", "consulting", "support"} and priority < 3:
        if not HANDS_ON_SIGNAL.search(text):
            return 3, True
    return priority, False


def escape_cell(value: Any) -> str:
    return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def render_table(jobs: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Priority Fit | Score | Company | Job Title | Level | Experience Required | Location | AI/ML Keywords | Fit Signals | Posted Date | Direct Apply Link |",
        "|---|---:|---|---|---|---|---|---|---|---|---|",
    ]
    for job in jobs:
        link = job.get("job_url")
        link_cell = f"[Apply]({link})" if link else "—"
        lines.append(
            "| "
            + " | ".join(
                [
                    escape_cell(job.get("fit_label")),
                    escape_cell(job.get("fit_score")),
                    escape_cell(job.get("company")),
                    escape_cell(job.get("job_title")),
                    escape_cell(job.get("level_classification")),
                    escape_cell(job.get("experience_required")),
                    escape_cell(job.get("location")),
                    escape_cell(", ".join(job.get("relevance_keywords_matched") or [])),
                    escape_cell(", ".join(job.get("fit_keywords_matched") or [])),
                    escape_cell(job.get("posting_date")),
                    link_cell,
                ]
            )
            + " |"
        )
    return lines


def write_shortlist(path: Path, jobs: list[dict[str, Any]], timestamp: str) -> None:
    priority_jobs = [job for job in jobs if int(job.get("fit_priority") or 4) <= 2]
    lines = [
        "# Priority AI Inference/GPU Job Shortlist",
        "",
        f"Generated: {timestamp}",
        "",
        "This shortlist contains qualifying Priority 1 and Priority 2 roles. The complete broad list remains in `report.md`.",
        "",
    ]
    if priority_jobs:
        lines.extend(render_table(priority_jobs))
    else:
        lines.append("No Priority 1 or Priority 2 roles were found in this run.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def classify_posting(job: dict[str, Any], tiers: dict[int, str], body_signals: list[dict[str, Any]], title_signals: list[dict[str, Any]]) -> dict[str, Any]:
    _normalize_legacy_fields(job)
    title = str(job.get("job_title") or "")
    full_text = combine_text(job)

    exp_ok, level, experience_required, inferred = classify_experience(job)
    domain_matches = classify_domain(job)
    domain_ok = domain_eligible(domain_matches, title)
    us_location = annotate_us_location(job)
    us_location_ok = bool(us_location["us_location_eligible"])
    fit = classify_fit(job, tiers, body_signals, title_signals)
    role_function = classify_role_function(job)

    funnel = is_generic_funnel(full_text, title)
    capped = False
    priority = fit["fit_priority"]
    if funnel:
        priority = 4
    else:
        priority, capped = apply_role_function_priority_cap(role_function, priority, full_text)
    if priority != fit["fit_priority"]:
        fit = dict(fit)
        fit["fit_priority"] = priority
        fit["fit_label"] = tiers.get(priority, DEFAULT_TIERS[priority])
        if funnel:
            fit["fit_reason"] += (
                " Forced to Priority 4: generic multi-discipline recruiting-funnel posting with no"
                " specific ML/inference/GPU/accelerator team named in the title."
            )
        elif capped:
            fit["fit_reason"] += (
                " Capped at Priority 3: customer-facing/consulting/support role without a clear"
                " hands-on implementation signal."
            )

    sales_excluded = role_function == "sales"
    marketing_excluded = role_function == "marketing"
    hr_excluded = role_function == "hr"
    customer_facing_ai_excluded = False
    if role_function == "customer_facing_technical" and not _has_hands_on_domain_signal(_domain_scope_text(job)):
        customer_facing_ai_excluded = True

    include = (
        exp_ok
        and domain_ok
        and us_location_ok
        and not sales_excluded
        and not marketing_excluded
        and not hr_excluded
        and not customer_facing_ai_excluded
    )

    exclusion_reasons: list[str] = []
    if not exp_ok:
        exclusion_reasons.append("experience_not_qualifying")
    if not domain_ok:
        exclusion_reasons.append("domain_gate_generic_ai_only")
    if not us_location_ok:
        exclusion_reasons.append("non_us_or_ambiguous_location")
    if sales_excluded:
        exclusion_reasons.append("sales_role_excluded")
    if marketing_excluded:
        exclusion_reasons.append("marketing_role_excluded")
    if hr_excluded:
        exclusion_reasons.append("hr_role_excluded")
    if customer_facing_ai_excluded:
        exclusion_reasons.append("customer_facing_role_lacks_hands_on_ai_ml_gpu_signal")

    ambiguity_flags = detect_ambiguous_signals(job)

    enriched = dict(job)
    enriched.update(
        {
            "experience_pass": exp_ok,
            "domain_pass": domain_ok,
            "include": include,
            "level_classification": level,
            "experience_required": experience_required,
            "experience_inferred": inferred,
            "relevance_keywords_matched": domain_matches,
            "role_function": role_function,
            "generic_funnel_detected": funnel,
            "exclusion_reasons": exclusion_reasons or None,
            **us_location,
            **fit,
            "match_reason": (
                f"Experience: {experience_required}; Domain: {', '.join(domain_matches)}; "
                f"Career fit: {fit['fit_label']} ({fit['fit_score']}/100)"
                if include
                else None
            ),
            # Additive/non-blocking -- see detect_ambiguous_signals. Never
            # affects `include` or any other decision above; only feeds
            # runs/<RUN_ID>/needs_review.json for optional follow-up review.
            "ambiguity_flags": ambiguity_flags or None,
        }
    )
    return enriched


# Stage-A-only exclusion statuses (see run_source.py): the identity was
# rejected using lightweight Stage-A metadata alone (confirmed non-US
# location, or an unconditionally-excluded title family -- senior/marketing/
# sales/HR), so its Stage-B detail fetch was deliberately skipped and every
# detail field is null by design. Still committed to state/seen_source_jobs
# .json for history purposes (see run_source.py / source_history.
# build_exclusion_stub()), but never classified, ranked, reported, or
# notified. Imported directly from source_history (never a separately
# maintained literal here) so this set can never silently drift out of sync
# with what build_exclusion_stub() considers a valid terminal exclusion --
# see TERMINAL_EXCLUSION_STATUSES's own docstring for the full contract,
# including excluded_out_of_scope (2026-08-15), which extends this same
# contract to a source-specific required_scope judgment (e.g. Google Cloud
# confirming a posting is not actually Cloud-scoped).
EXCLUDED_AT_STAGE_A = TERMINAL_EXCLUSION_STATUSES


def _compact_review_record(job: dict[str, Any], flags: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact needs_review.json entry: identity + why, never the full
    description text -- keeps any downstream Claude review prompt small
    (CLAUDE.md 'Pass only compact unresolved records/evidence')."""
    return {
        "source_slug": job.get("source_slug"),
        "company": job.get("company"),
        "job_id": job.get("job_id"),
        "job_title": job.get("job_title"),
        "location": job.get("location"),
        "job_url": job.get("job_url"),
        "posting_date": job.get("posting_date"),
        "deterministic_include_decision": job.get("include"),
        "level_classification": job.get("level_classification"),
        "experience_required": job.get("experience_required"),
        "reason_codes": [f["reason_code"] for f in flags],
        "evidence": [f["evidence"] for f in flags],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--fit-config", default="config/fit_priorities.json")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    raw_dir = run_dir / "raw"
    sources = load_source_files(raw_dir)
    tiers, body_signals, title_signals = load_fit_config(Path(args.fit_config))

    classified: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    stage_a_excluded_count = 0
    for source in sources:
        for job in source.get("postings", []):
            if not isinstance(job, dict):
                continue
            # Stage-A already confirmed this identity as non-US, or as an
            # unconditionally-excluded title family, and skipped its Stage-B
            # detail fetch (see run_source.py). Never run it through
            # classification/ranking -- it must never appear in filtered.json,
            # deduplicated.json, reports, or notifications.
            if job.get("processing_status") in EXCLUDED_AT_STAGE_A:
                stage_a_excluded_count += 1
                continue
            job.setdefault("source_slug", source.get("slug"))
            result = classify_posting(job, tiers, body_signals, title_signals)
            classified.append(result)
            flags = result.get("ambiguity_flags")
            if flags:
                needs_review.append(_compact_review_record(result, flags))

    included = [job for job in classified if job.get("include")]
    try:
        deduped = deduplicate_included(classified)
    except DedupInvariantError as exc:
        print(json.dumps({"error": str(exc), "run_dir": str(run_dir)}, indent=2))
        return 1

    (run_dir / "filtered.json").write_text(json.dumps(classified, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "deduplicated.json").write_text(json.dumps(deduped, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "needs_review.json").write_text(
        json.dumps(needs_review, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # Incremental Stage-A/B counters (listing records scanned, unseen
    # discovered, previously-processed, detail pages fetched) computed by the
    # existing source_history.py summarize aggregator -- merged in here so
    # report.md is complete after this one deterministic pass, and a
    # downstream reporting step never needs to re-open every raw file's full
    # Stage-A inventory just to recompute counts Python already has (see
    # CLAUDE.md "TOKEN-EFFICIENCY DESIGN PRINCIPLES"). Only reachable when
    # this module is actually executed as the CLI entry point (see the
    # sys.path note below), so it never affects in-process unit tests that
    # import individual functions without calling main().
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import source_history  # noqa: PLC0415

    incremental = source_history.summarize_run(run_dir)

    timestamp = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Prioritized AI/ML/NVIDIA Early-Career Job Discovery Report",
        "",
        "Qualifying jobs are ranked for an AI inference, GPU, and AI-infrastructure career path. Broad matches are retained rather than discarded.",
        "",
    ]
    lines.extend(render_table(deduped))

    attempted = len(sources)
    successful = sum(1 for source in sources if source.get("status") in {"success", "partial"})
    unavailable = [source for source in sources if source.get("status") in {"blocked", "failed"}]
    raw_total = sum(len(source.get("postings") or []) for source in sources)
    tier_counts = Counter(int(job.get("fit_priority") or 4) for job in deduped)

    lines.extend(
        [
            "",
            "## Career-Fit Summary",
            "",
            f"- Priority 1 — Excellent inference/infrastructure fit: {tier_counts.get(1, 0)}",
            f"- Priority 2 — Strong GPU/systems adjacent fit: {tier_counts.get(2, 0)}",
            f"- Priority 3 — General AI/ML fit: {tier_counts.get(3, 0)}",
            f"- Priority 4 — Low-priority backup: {tier_counts.get(4, 0)}",
            "- Ranking affects ordering only; all jobs still had to pass the early-career and AI/ML relevance filters.",
            "",
            "## Run Summary",
            "",
            f"- Run timestamp: {timestamp}",
            f"- Total sources attempted: {attempted}",
            f"- Sources successful or partial: {successful}",
            f"- Sources blocked or failed: {len(unavailable)}",
            f"- Listing records scanned (Stage A): {incremental['listing_records_scanned']}",
            f"- Already-seen, skipped (previously processed): {incremental['previously_processed_count']}",
            f"- Unseen postings discovered (Stage A/B boundary): {incremental['unseen_postings_discovered']}",
            f"- Stage-A deterministic rejections (non-US location or senior/marketing/sales/HR title -- detail fetch skipped): {stage_a_excluded_count}",
            f"- Detail pages fetched (Stage B): {incremental['detail_pages_fetched']}",
            f"- Total raw postings scanned: {raw_total}",
            f"- Deterministic classification decisions made: {len(classified)}",
            f"- Postings passing both filters before deduplication: {len(included)}",
            f"- Final deduplicated postings: {len(deduped)}",
            f"- Needs-review (genuinely ambiguous, deterministic pipeline still completed the run): {len(needs_review)}",
            "",
            "These are different stages: listing/unseen/detail-fetch counts measure this run's incremental "
            "scan, while included/deduplicated counts measure final qualifying postings -- see `notification.json` "
            "for new-job/new-Priority-1/new-Priority-2 counts, a further distinct stage.",
            "",
            "## Sources Unavailable This Run",
            "",
        ]
    )
    if unavailable:
        for source in unavailable:
            status = source.get("status") or "failed"
            reason = source.get("reason") or "No reason provided"
            next_step = "Manual check required" if status == "blocked" else "Retry with alternate permitted fetch strategy"
            lines.append(f"- **{source.get('company', 'Unknown')}** — {status}: {reason}. Suggested next step: {next_step}.")
    else:
        lines.append("None.")

    lines.extend(["", "## Source Audit Log", ""])
    for source in sources:
        lines.append(
            f"- **{source.get('company', 'Unknown')}** — status={source.get('status')}; "
            f"URL={source.get('source_url') or '—'}; fetched_at={source.get('fetched_at') or '—'}; "
            f"raw_count={len(source.get('postings') or [])}; reason={source.get('reason') or '—'}"
        )

    report_path = run_dir / "report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    shortlist_path = run_dir / "priority_shortlist.md"
    write_shortlist(shortlist_path, deduped, timestamp)

    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "raw": raw_total,
                "listing_records_scanned": incremental["listing_records_scanned"],
                "previously_processed_count": incremental["previously_processed_count"],
                "unseen_postings_discovered": incremental["unseen_postings_discovered"],
                "stage_a_excluded_count": stage_a_excluded_count,
                "detail_pages_fetched": incremental["detail_pages_fetched"],
                "classified": len(classified),
                "included": len(included),
                "deduped": len(deduped),
                "needs_review_count": len(needs_review),
                "priority_counts": {str(priority): tier_counts.get(priority, 0) for priority in range(1, 5)},
                "report": str(report_path),
                "priority_shortlist": str(shortlist_path),
                "needs_review": str(run_dir / "needs_review.json"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
