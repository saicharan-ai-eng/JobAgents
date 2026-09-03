"""Helpers shared by every platform adapter."""

from __future__ import annotations

import html
import re
from typing import Any

import requests
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (compatible; AIJobDiscovery/1.0; respectful point-in-time research)"

EXPERIENCE_PATTERNS = [
    re.compile(r"\b(?:minimum of\s+|at least\s+)?\d+\s*(?:-|–|to)\s*\d+\s+years?\b", re.I),
    re.compile(r"\b\d+\+?\s+years?\b", re.I),
    re.compile(r"\b(?:internship|intern|co-op|new grad(?:uate)?|university grad(?:uate)?|early career|entry level)\b", re.I),
]


class AdapterError(Exception):
    """Raised by an adapter when a source cannot be fetched at all (network
    error, non-2xx, block page). run_source.py catches this and records a
    failed/blocked source-result rather than emitting guessed data."""


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
    )
    return session


def strip_html(value: Any) -> str:
    if value is None:
        return ""
    text = BeautifulSoup(html.unescape(str(value)), "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def extract_experience(text: str) -> str | None:
    matches: list[str] = []
    for pattern in EXPERIENCE_PATTERNS:
        matches.extend(match.group(0) for match in pattern.finditer(text))
    if not matches:
        return None
    unique = list(dict.fromkeys(matches))
    return "; ".join(unique[:5])


def get_json(session: requests.Session, url: str, timeout: int, **kwargs: Any) -> Any:
    try:
        response = session.get(url, timeout=timeout, **kwargs)
    except requests.RequestException as exc:  # noqa: BLE001
        raise AdapterError(f"GET {url} failed: {type(exc).__name__}: {exc}") from exc
    if response.status_code in (401, 403):
        raise AdapterError(f"GET {url} returned {response.status_code} (blocked/authentication required)")
    if response.status_code == 429:
        raise AdapterError(f"GET {url} returned 429 (rate limited)")
    if response.status_code >= 400:
        raise AdapterError(f"GET {url} returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:  # noqa: BLE001
        raise AdapterError(f"GET {url} did not return valid JSON: {exc}") from exc


def post_json(session: requests.Session, url: str, timeout: int, **kwargs: Any) -> Any:
    try:
        response = session.post(url, timeout=timeout, **kwargs)
    except requests.RequestException as exc:  # noqa: BLE001
        raise AdapterError(f"POST {url} failed: {type(exc).__name__}: {exc}") from exc
    if response.status_code in (401, 403):
        raise AdapterError(f"POST {url} returned {response.status_code} (blocked/authentication required)")
    if response.status_code == 429:
        raise AdapterError(f"POST {url} returned 429 (rate limited)")
    if response.status_code >= 400:
        raise AdapterError(f"POST {url} returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:  # noqa: BLE001
        raise AdapterError(f"POST {url} did not return valid JSON: {exc}") from exc


def get_html(session: requests.Session, url: str, timeout: int, **kwargs: Any) -> str:
    """GET a page and return its raw HTML text -- for platforms with no JSON
    API but whose job data is baked directly into plain server-rendered HTML
    (confirmed via a cookie-less fetch during onboarding, never a JS-rendered
    SPA shell -- that distinction matters and must be verified per-platform
    before this helper is used, since a JS-only page returns an empty shell
    here with no error to signal it)."""
    try:
        response = session.get(url, timeout=timeout, **kwargs)
    except requests.RequestException as exc:  # noqa: BLE001
        raise AdapterError(f"GET {url} failed: {type(exc).__name__}: {exc}") from exc
    if response.status_code in (401, 403):
        raise AdapterError(f"GET {url} returned {response.status_code} (blocked/authentication required)")
    if response.status_code == 429:
        raise AdapterError(f"GET {url} returned 429 (rate limited)")
    if response.status_code >= 400:
        raise AdapterError(f"GET {url} returned HTTP {response.status_code}")
    return response.text


_CHROME_TAGS = ("script", "style", "nav", "header", "footer", "noscript", "svg")


def extract_text(html_doc: str, selector: str | None = None) -> str:
    """Parse a full HTML document and return cleaned visible text -- from a
    specific CSS selector's element when given and found, otherwise the
    whole document with script/style/nav/header/footer chrome stripped.
    Used for detail-page description extraction on platforms with no JSON
    detail payload, where reverse-engineering an exact content selector for
    every company is unnecessary: the classifier only needs the real
    qualifying words in `full_description_text`, not pixel-perfect markup."""
    soup = BeautifulSoup(html_doc, "html.parser")
    target = soup.select_one(selector) if selector else None
    if target is None:
        target = soup.body or soup
        for tag in target.find_all(_CHROME_TAGS):
            tag.decompose()
    return re.sub(r"\s+", " ", target.get_text(" ")).strip()


_WORD_BOUNDARY_CACHE: dict[str, re.Pattern[str]] = {}


def keyword_matches(text: str, keywords: list[str]) -> str | None:
    """Return the first configured keyword that appears (whole word/phrase,
    case-insensitive) in `text`, or None. Used by platforms whose listing
    payload already contains the full job (title/description) so keyword
    scoping happens client-side instead of via a server search parameter."""
    lowered = text or ""
    for keyword in keywords:
        pattern = _WORD_BOUNDARY_CACHE.get(keyword)
        if pattern is None:
            pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z0-9])", re.I)
            _WORD_BOUNDARY_CACHE[keyword] = pattern
        if pattern.search(lowered):
            return keyword
    return None
