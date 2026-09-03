#!/usr/bin/env python3
"""Persistent raw-source posting history for the incremental two-stage scraping workflow.

This is the deterministic engine behind `state/seen_source_jobs.json`. Site
scraper agents use it to discover which listing identities are genuinely
unseen (so they only open detail pages for those), and the orchestrator uses
it to commit a run's results into permanent history *after* every source's
raw result file has been written and validated.

Schema of state/seen_source_jobs.json
--------------------------------------
{
  "schema_version": 1,
  "created_at": "<ISO8601>",
  "updated_at": "<ISO8601>",
  "sources": {
    "<slug>": {
      "<stable identity>": {
        "identity": "<stable identity string>",
        "company": "...",
        "job_id": "..." | null,
        "normalized_title": "...",
        "location": "..." | null,
        "job_url": "...",
        "posting_date": "..." | null,
        "first_seen_at": "<ISO8601>",
        "last_seen_at": "<ISO8601>",
        "processing_status": "success" | "failed" | "blocked" | "baseline_existing",
        "is_repost": true,                 # present only once a repost has been detected
        "original_first_seen_at": "...",   # present only once a repost has been detected
        "repost_detected_at": "..."        # present only once a repost has been detected
      },
      ...
    },
    ...
  }
}

Stable identity
---------------
company + job_id when job_id exists; otherwise
company + normalized title + location + direct job URL.
This is intentionally more specific in its fallback than the qualifying-job
identity used by state/seen_jobs.json (which does not include the URL),
because raw source history needs to avoid collisions among generically
titled postings before any eligibility filtering has happened.

processing_status values
-------------------------
"success" / "failed" / "blocked" describe the outcome of a real Stage-B
detail-fetch attempt. "baseline_existing" is different: it marks an identity
that was seeded from a Stage-A-only inventory scan (see
`commit_baseline_inventory` / the `seed-baseline` CLI command) during initial
production baseline creation -- its detail page was deliberately never
fetched, no classification ever ran on it, and it must never be treated as a
newly discovered posting.

Repost rule
-----------
A posting with an *already-known* job ID is only treated as newly posted
(eligible for a fresh detail fetch / reprocessing) when its posting_date is
strictly newer than the stored posting_date AND the scraper has set
`repost_signal: true` on the raw posting (meaning the source site itself
clearly indicated a repost/renewal, not merely a scraper-side date-format
change). Description edits, location-formatting changes, or title
punctuation changes never trigger this.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dateutil import parser as date_parser

STATE_SCHEMA_VERSION = 1

# "success"/"failed"/"blocked" describe a real Stage-B detail-fetch attempt.
# "baseline_existing" marks an identity seeded from a Stage-A-only baseline
# scan, whose detail page was deliberately never opened.
PROCESSING_STATUSES = {"success", "failed", "blocked", "baseline_existing"}

# A TERMINAL exclusion status: the source worker reached a definitive,
# deterministic disposition for this identity WITHOUT it ever becoming an
# included/classified posting -- so it should still be permanently recorded
# in history (via commit_processed(), same as any other posting), never
# left out of `postings[]` just because it isn't a full Stage-B success.
# Originally introduced by scripts/run_source.py for two Stage-A-only cases
# (excluded_non_us, excluded_title_reject); excluded_out_of_scope extends the
# same contract to a source-specific required_scope judgment (e.g. Google
# Cloud's "is this posting actually Cloud-scoped, not just Google-general")
# that may need a quick look at the fetched content to decide, but is still
# a genuine terminal disposition once reached -- see build_exclusion_stub().
# NEVER use one of these for an identity that was merely deferred, time-
# budget-cut, or left ambiguous -- those must stay OUT of `postings[]`
# entirely so diff_inventory() keeps offering them up for retry.
TERMINAL_EXCLUSION_STATUSES = {"excluded_non_us", "excluded_title_reject", "excluded_out_of_scope"}


def build_exclusion_stub(
    item: dict[str, Any], processing_status: str, exclusion_reason: str | None = None
) -> dict[str, Any]:
    """Build a lightweight, terminal-disposition stub record for an unseen
    Stage-A inventory item (or an item whose disposition needed a quick
    look at fetched content, e.g. a required_scope check) that has been
    definitively, deterministically excluded -- WITHOUT ever needing (or
    getting) a full Stage-B detail fetch for classification purposes. This
    mirrors scripts/run_source.py's own excluded_non_us/excluded_title_reject
    stub convention exactly (same null detail fields, same shape), extended
    so any source worker -- not only the shared run_source.py/adapters
    driver -- can get a confirmed exclusion permanently recorded into
    state/seen_source_jobs.json via commit_processed(). Without this, an
    excluded identity that never appears in `postings[]` is invisible to
    commit_processed() and resurfaces as "unseen" in every future run,
    forever (see CLAUDE.md's incremental-workflow contract).

    `item` is the Stage-A inventory record (company, job_title, job_id,
    location, posting_date, job_url, source_keyword) -- every field is
    carried through unchanged; only the detail-only fields are nulled out
    and `processing_status` (plus an optional `exclusion_reason`) is set.

    Only call this for a genuinely TERMINAL disposition -- see
    TERMINAL_EXCLUSION_STATUSES. Never call this for an identity that was
    merely deferred, time-budget-cut, or left ambiguous; leave those out of
    `postings[]` entirely so they remain retryable on the next run.
    """
    if processing_status not in TERMINAL_EXCLUSION_STATUSES:
        raise ValueError(
            f"processing_status must be one of {sorted(TERMINAL_EXCLUSION_STATUSES)} for an exclusion stub, "
            f"got {processing_status!r} -- a non-terminal disposition must not be stubbed at all"
        )
    stub = {k: v for k, v in item.items() if not str(k).startswith("_")}
    stub.update(
        {
            "experience_level_text": None,
            "team_department": None,
            "short_description": None,
            "full_description_text": None,
            "processing_status": processing_status,
        }
    )
    if exclusion_reason:
        stub["stage_a_exclusion_reason"] = exclusion_reason
    return stub


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def stable_identity(posting: dict[str, Any]) -> str:
    company = normalize(posting.get("company"))
    job_id = normalize(posting.get("job_id"))
    if job_id:
        return f"{company}|id|{job_id}"
    return "|".join(
        [
            company,
            "fallback",
            normalize(posting.get("job_title")),
            normalize(posting.get("location")),
            normalize(posting.get("job_url")),
        ]
    )


def load_history(path: Path) -> dict[str, Any]:
    if not path.exists():
        timestamp = utc_now()
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "created_at": timestamp,
            "updated_at": timestamp,
            "sources": {},
        }
    data = load_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        raise ValueError(f"Refusing to use invalid source-history file: {path}")
    if data.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ValueError(f"Unsupported source-history schema in {path}")
    return data


def parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = date_parser.parse(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def is_strictly_newer(new_date: Any, old_date: Any) -> bool:
    new_parsed = parse_date(new_date)
    old_parsed = parse_date(old_date)
    if new_parsed is None or old_parsed is None:
        return False
    return new_parsed > old_parsed


def is_repost(existing_entry: dict[str, Any], candidate_posting: dict[str, Any]) -> bool:
    """A same-identity posting is a repost only when the source explicitly
    signals a repost/renewal AND the posting date is strictly newer than what
    is already on file. Neither condition alone is sufficient."""
    if not candidate_posting.get("repost_signal"):
        return False
    return is_strictly_newer(candidate_posting.get("posting_date"), existing_entry.get("posting_date"))


def diff_inventory(
    history: dict[str, Any], slug: str, inventory: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a Stage-A lightweight inventory into (unseen, previously_processed).

    A previously-processed item whose posting is a clearly-signaled repost is
    treated as unseen (eligible for a fresh detail fetch). This function is
    read-only: it never mutates `history`.
    """
    source_history = history.get("sources", {}).get(slug, {})
    unseen: list[dict[str, Any]] = []
    previously_processed: list[dict[str, Any]] = []
    for item in inventory:
        identity = stable_identity(item)
        existing = source_history.get(identity)
        if existing is None:
            unseen.append(item)
        elif is_repost(existing, item):
            unseen.append(item)
        else:
            previously_processed.append(item)
    return unseen, previously_processed


def touch_inventory(history: dict[str, Any], slug: str, inventory: list[dict[str, Any]], timestamp: str) -> int:
    """Bump last_seen_at for inventory identities already on file, proving
    they are still live on the source site. Never adds new identities and
    never removes any (a posting missing from this run's inventory is left
    untouched, not deleted)."""
    source_history = history.setdefault("sources", {}).setdefault(slug, {})
    touched = 0
    for item in inventory:
        identity = stable_identity(item)
        entry = source_history.get(identity)
        if entry is not None:
            entry["last_seen_at"] = timestamp
            touched += 1
    return touched


def load_baseline_status(path: Path) -> dict[str, Any]:
    """Load state/source_baseline_status.json. Returns {} (meaning every
    source behaves as "full") when the file doesn't exist, so projects
    that haven't adopted baseline-completeness tracking are unaffected."""
    if not path.exists():
        return {}
    data = load_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"Refusing to use invalid baseline-status file: {path}")
    return data.get("sources", data) if "sources" in data else data


NOTIFICATION_MODES = {"full", "date_verified_only", "disabled"}


def resolve_notification_mode(baseline_entry: dict[str, Any] | None) -> str:
    """Resolve a source's notification policy, preferring the current
    `notification_mode` field but reading older records for backward
    compatibility. Only `notification_mode` is ever written going forward
    (see state/source_baseline_status.json); the fallbacks below exist purely
    to keep old data usable.

    Precedence: explicit notification_mode -> legacy notifications_enabled
    boolean (true=full, false=disabled) -> legacy baseline_status
    (complete=full, partial=date_verified_only, failed=disabled) -> "full"
    when there's no baseline-status entry at all (unchanged prior behavior
    for sources that have never used this tracking)."""
    if not baseline_entry:
        return "full"
    mode = baseline_entry.get("notification_mode")
    if mode in NOTIFICATION_MODES:
        return mode
    if "notifications_enabled" in baseline_entry:
        return "full" if baseline_entry["notifications_enabled"] else "disabled"
    status = baseline_entry.get("baseline_status")
    if status == "complete":
        return "full"
    if status == "partial":
        return "date_verified_only"
    if status == "failed":
        return "disabled"
    return "full"


def classify_discovery_type(
    baseline_entry: dict[str, Any] | None,
    posting: dict[str, Any],
    existing_history_entry: dict[str, Any] | None,
) -> str:
    """Decide whether an unseen posting is a genuinely `new_posting` (may
    trigger a notification, subject to the source's notification_mode) or
    `baseline_catchup` (a pre-existing posting this source's incomplete
    baseline scan simply hadn't reached yet -- must never notify).

    notification_mode: "full" -- any unseen identity (new job ID or an
    explicitly-signaled repost) is new_posting. Matches a source whose
    inventory is fully seeded, so nothing further needs to be verified.

    notification_mode: "disabled" -- always baseline_catchup. The posting is
    still fetched/classified/committed to history as normal; it just never
    reaches notification output.

    notification_mode: "date_verified_only" -- new_posting only when at
    least one of these holds, using this source's baseline_started_at as the
    reference point:
      (a) the posting's own posting_date is reliably (strictly) newer;
      (b) it's a same-identity posting explicitly flagged as reposted
          (repost_signal) with a date strictly newer;
      (c) the posting carries an explicit `confirmed_absent_at_checkpoint`
          timestamp (set by incremental catch-up tooling when it has
          positively verified, via a real checkpoint comparison, that this
          identity did not exist at that checkpoint) that is itself strictly
          newer than baseline_started_at.
    Missing, ambiguous, malformed, or unchanged dates fall through to
    "baseline_catchup" -- this is the conservative default, so postings
    merely being backfilled while completing missing pages/keywords never
    notify.
    """
    mode = resolve_notification_mode(baseline_entry)
    if mode == "full":
        return "new_posting"
    if mode == "disabled":
        return "baseline_catchup"

    # date_verified_only
    started_at = (baseline_entry or {}).get("baseline_started_at")
    posting_date = posting.get("posting_date")

    if started_at and is_strictly_newer(posting_date, started_at):
        return "new_posting"  # (a)

    if (
        existing_history_entry is not None
        and posting.get("repost_signal")
        and started_at
        and is_strictly_newer(posting_date, started_at)
    ):
        return "new_posting"  # (b)

    checkpoint = posting.get("confirmed_absent_at_checkpoint")
    if checkpoint and started_at and is_strictly_newer(checkpoint, started_at):
        return "new_posting"  # (c)

    return "baseline_catchup"


def commit_processed(
    history: dict[str, Any],
    slug: str,
    processed_postings: list[dict[str, Any]],
    timestamp: str,
    baseline_status: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Record postings whose detail fetch was attempted this run (status
    success/failed/blocked) into permanent history. Mutates `history` and
    also mutates each posting dict in place to add repost fields when a
    repost was detected, and a `discovery_type` field (new_posting vs
    baseline_catchup) computed against `baseline_status` (this source's entry
    from state/source_baseline_status.json), so both annotations flow through
    to the raw source-result file and downstream classification/notification."""
    source_history = history.setdefault("sources", {}).setdefault(slug, {})
    baseline_entry = (baseline_status or {}).get(slug)
    newly_added: list[str] = []
    reposted: list[str] = []
    baseline_catchup: list[str] = []

    for posting in processed_postings:
        identity = stable_identity(posting)
        status = str(posting.get("processing_status") or "success")
        existing = source_history.get(identity)

        discovery_type = classify_discovery_type(baseline_entry, posting, existing)
        posting["discovery_type"] = discovery_type
        if discovery_type == "baseline_catchup":
            baseline_catchup.append(identity)

        if existing is not None and is_repost(existing, posting):
            posting["is_repost"] = True
            posting["original_first_seen_at"] = existing.get("first_seen_at")
            posting["repost_detected_at"] = timestamp
            reposted.append(identity)
            source_history[identity] = {
                "identity": identity,
                "company": posting.get("company"),
                "job_id": posting.get("job_id"),
                "normalized_title": normalize(posting.get("job_title")),
                "location": posting.get("location"),
                "job_url": posting.get("job_url"),
                "posting_date": posting.get("posting_date"),
                "first_seen_at": existing.get("first_seen_at"),
                "last_seen_at": timestamp,
                "processing_status": status,
                "discovery_type": discovery_type,
                "is_repost": True,
                "original_first_seen_at": existing.get("first_seen_at"),
                "repost_detected_at": timestamp,
            }
            continue

        if existing is None:
            newly_added.append(identity)
            first_seen_at = timestamp
        else:
            first_seen_at = existing.get("first_seen_at") or timestamp

        source_history[identity] = {
            "identity": identity,
            "company": posting.get("company"),
            "job_id": posting.get("job_id"),
            "normalized_title": normalize(posting.get("job_title")),
            "location": posting.get("location"),
            "job_url": posting.get("job_url"),
            "posting_date": posting.get("posting_date"),
            "first_seen_at": first_seen_at,
            "last_seen_at": timestamp,
            "processing_status": status,
            "discovery_type": discovery_type,
        }

    return {"newly_added": newly_added, "reposted": reposted, "baseline_catchup": baseline_catchup}


def commit_baseline_inventory(
    history: dict[str, Any],
    slug: str,
    inventory: list[dict[str, Any]],
    timestamp: str,
    status: str = "baseline_existing",
) -> dict[str, Any]:
    """Seed history with pre-existing listings from a Stage-A-only baseline
    scan. Every newly recorded identity gets `processing_status=status`
    (default "baseline_existing") -- its detail page was never opened, it was
    never classified, and it must never be reported as newly discovered.

    Never overwrites an identity that already has a real history entry (from
    a normal commit_processed run); a baseline seed only fills in gaps, it
    never clobbers genuine processing history. Duplicate identities within
    the supplied inventory itself (e.g. the same job_id captured twice by
    overlapping keyword searches) are counted, not double-recorded.
    """
    if status not in PROCESSING_STATUSES:
        raise ValueError(f"Unsupported processing_status for baseline seeding: {status!r}")

    source_history = history.setdefault("sources", {}).setdefault(slug, {})
    newly_added: list[str] = []
    already_known: list[str] = []
    seen_this_call: set[str] = set()
    duplicate_identity_count = 0

    for item in inventory:
        identity = stable_identity(item)
        if identity in seen_this_call:
            duplicate_identity_count += 1
            continue
        seen_this_call.add(identity)

        existing = source_history.get(identity)
        if existing is not None:
            already_known.append(identity)
            existing["last_seen_at"] = timestamp
            continue

        source_history[identity] = {
            "identity": identity,
            "company": item.get("company"),
            "job_id": item.get("job_id"),
            "normalized_title": normalize(item.get("job_title")),
            "location": item.get("location"),
            "job_url": item.get("job_url"),
            "posting_date": item.get("posting_date"),
            "first_seen_at": timestamp,
            "last_seen_at": timestamp,
            "processing_status": status,
            "discovery_type": status,
        }
        newly_added.append(identity)

    return {
        "newly_added": newly_added,
        "already_known": already_known,
        "duplicate_identity_count": duplicate_identity_count,
    }


def commit_source_result(
    history: dict[str, Any],
    slug: str,
    source_result: dict[str, Any],
    timestamp: str,
    baseline_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Commit one source's raw result (inventory + processed postings) into
    history. Does not fail the whole commit when a source's own status is
    blocked/failed/partial -- its prior history is simply left untouched
    beyond touch_inventory for whatever inventory it did manage to collect."""
    inventory = source_result.get("inventory") or []
    postings = source_result.get("postings") or []
    touched = touch_inventory(history, slug, inventory, timestamp)
    result = commit_processed(history, slug, postings, timestamp, baseline_status=baseline_status)
    result["inventory_touched"] = touched
    return result


def commit_run(history_path: Path, run_dir: Path, baseline_status_path: Path | None = None) -> dict[str, Any]:
    history = load_history(history_path)
    baseline_status = load_baseline_status(baseline_status_path) if baseline_status_path else {}
    timestamp = utc_now()
    raw_dir = run_dir / "raw"
    summary: dict[str, Any] = {}

    for path in sorted(raw_dir.glob("*.json")):
        slug = path.stem
        try:
            source_result = load_json(path)
        except Exception as exc:  # noqa: BLE001
            summary[slug] = {"error": f"Invalid JSON: {type(exc).__name__}: {exc}"}
            continue
        if not isinstance(source_result, dict):
            summary[slug] = {"error": "source result is not a JSON object"}
            continue
        result = commit_source_result(history, slug, source_result, timestamp, baseline_status=baseline_status)
        # Repost/discovery_type annotations were written onto the in-memory
        # postings; persist them back onto the raw file so classification and
        # the notification layer see the same data.
        if result.get("reposted") or result.get("baseline_catchup"):
            atomic_write_json(path, source_result)
        summary[slug] = result

    history["updated_at"] = timestamp
    atomic_write_json(history_path, history)
    return {"timestamp": timestamp, "sources": summary}


def summarize_run(run_dir: Path) -> dict[str, Any]:
    """Aggregate the two-stage counters across every source in a run, for the
    orchestrator's early-exit decision and reporting."""
    raw_dir = run_dir / "raw"
    totals = {
        "sources_attempted": 0,
        "listing_records_scanned": 0,
        "unseen_postings_discovered": 0,
        "detail_pages_fetched": 0,
        "previously_processed_count": 0,
    }
    per_source: dict[str, Any] = {}
    for path in sorted(raw_dir.glob("*.json")):
        slug = path.stem
        try:
            data = load_json(path)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        totals["sources_attempted"] += 1
        inv = int(data.get("inventory_count") or len(data.get("inventory") or []))
        unseen = int(data.get("unseen_inventory_count") or len(data.get("postings") or []))
        detail = int(data.get("detail_fetch_count") or len(data.get("postings") or []))
        prev = int(data.get("previously_processed_count") or max(0, inv - unseen))
        totals["listing_records_scanned"] += inv
        totals["unseen_postings_discovered"] += unseen
        totals["detail_pages_fetched"] += detail
        totals["previously_processed_count"] += prev
        per_source[slug] = {
            "status": data.get("status"),
            "inventory_count": inv,
            "unseen_inventory_count": unseen,
            "previously_processed_count": prev,
            "detail_fetch_count": detail,
        }
    totals["per_source"] = per_source
    return totals


def list_configured_sources(config_path: Path) -> list[dict[str, Any]]:
    """Read config/sources.json and return each source's slug/agent/company.

    Deliberately has no hardcoded count or source list: adding a new entry to
    config/sources.json is picked up automatically by every caller of this
    function without any code change.
    """
    config = json.loads(config_path.read_text(encoding="utf-8"))
    sources = config.get("sources", [])
    return [
        {"slug": s["slug"], "agent": s.get("agent"), "company": s.get("company")}
        for s in sources
        if isinstance(s, dict) and "slug" in s
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history-file", default="state/seen_source_jobs.json")
    sub = parser.add_subparsers(dest="command", required=True)

    diff_parser = sub.add_parser("diff-inventory", help="Split a lightweight inventory into unseen vs previously-processed")
    diff_parser.add_argument("--slug", required=True)
    diff_parser.add_argument("--inventory-file", required=True)
    diff_parser.add_argument("--out-unseen", required=True)

    commit_parser = sub.add_parser("commit", help="Commit an entire run's raw source results into permanent history")
    commit_parser.add_argument("--run-dir", required=True)
    commit_parser.add_argument("--baseline-status-file", default="state/source_baseline_status.json")

    summarize_parser = sub.add_parser("summarize", help="Aggregate two-stage counters across a run for reporting")
    summarize_parser.add_argument("--run-dir", required=True)

    seed_parser = sub.add_parser(
        "seed-baseline",
        help="Seed history with pre-existing listings from a Stage-A-only baseline scan (no detail fetch, no classification)",
    )
    seed_parser.add_argument("--slug", required=True)
    seed_parser.add_argument("--inventory-file", required=True)
    seed_parser.add_argument("--status", default="baseline_existing")

    args = parser.parse_args()
    history_path = Path(args.history_file)

    if args.command == "diff-inventory":
        history = load_history(history_path)
        inventory = load_json(Path(args.inventory_file))
        if not isinstance(inventory, list):
            raise ValueError("--inventory-file must contain a JSON array")
        unseen, previously_processed = diff_inventory(history, args.slug, inventory)
        Path(args.out_unseen).write_text(json.dumps(unseen, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "inventory_count": len(inventory),
                    "unseen_inventory_count": len(unseen),
                    "previously_processed_count": len(previously_processed),
                    "unseen_file": args.out_unseen,
                },
                indent=2,
            )
        )
        return 0

    if args.command == "commit":
        result = commit_run(history_path, Path(args.run_dir), baseline_status_path=Path(args.baseline_status_file))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "summarize":
        result = summarize_run(Path(args.run_dir))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "seed-baseline":
        history = load_history(history_path)
        inventory = load_json(Path(args.inventory_file))
        if not isinstance(inventory, list):
            raise ValueError("--inventory-file must contain a JSON array")
        timestamp = utc_now()
        result = commit_baseline_inventory(history, args.slug, inventory, timestamp, status=args.status)
        history["updated_at"] = timestamp
        atomic_write_json(history_path, history)
        result["timestamp"] = timestamp
        result["inventory_count"] = len(inventory)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
