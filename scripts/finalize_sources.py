#!/usr/bin/env python3
"""Raw-source-file finalization: a checksummed manifest gate between source
collection and source-history commit/classification.

Root cause this hardens against (run 20260812T152540Z): source history was
committed while NVIDIA's raw file was still being rewritten with corrected
data mid-run, forcing commit/summarize/classify/dedup/report to be rerun by
hand. There was no checkpoint that could detect "this raw file changed after
we thought collection was done."

Contract (see CLAUDE.md "3. RAW-FILE FINALIZATION / PIPELINE CONSISTENCY"):
  1. `manifest` -- assert every configured source's raw file exists with a
     terminal `status` (success/partial/blocked/failed), then compute and
     persist a SHA-256 checksum, record count, status, and completion
     timestamp for each into runs/<RUN_ID>/source_manifest.json. Refuses to
     write a manifest (nonzero exit) if any source is missing or has a
     non-terminal/invalid status -- collection is not actually finished yet.
  2. `verify` -- recompute checksums for every file listed in the manifest
     and compare. Read-only; reports {"ok": true} or {"ok": false, changed:
     [...], missing: [...]}. Cheap enough to call at every checkpoint
     (before commit, before classification, after reporting).
  3. `refresh` -- recompute manifest entries for specifically the
     changed/missing slugs (leaves every other entry untouched), and appends
     a recovery-log entry to the manifest recording what changed and when.
     Idempotent: refreshing when nothing changed is a no-op beyond updating
     nothing.

The manifest never blocks re-running verify/refresh/manifest itself --
"idempotent" is a property of these operations, not of source_history.py's
commit (which is independently idempotent/safe to rerun; see its own
docstring), so the orchestrator can safely call `commit` again after a
refresh without special-casing anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TERMINAL_STATUSES = {"success", "partial", "blocked", "failed"}
MANIFEST_FILENAME = "source_manifest.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def list_configured_slugs(config_path: Path) -> list[str]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    return [s["slug"] for s in config.get("sources", []) if isinstance(s, dict) and "slug" in s]


def manifest_path(run_dir: Path) -> Path:
    return run_dir / MANIFEST_FILENAME


def load_manifest(run_dir: Path) -> dict[str, Any]:
    path = manifest_path(run_dir)
    if not path.exists():
        raise FileNotFoundError(f"No source manifest at {path} -- run 'finalize_sources.py manifest' first")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    manifest_path(run_dir).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _entry_for(raw_path: Path) -> dict[str, Any]:
    data = json.loads(raw_path.read_text(encoding="utf-8"))
    status = data.get("status")
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"{raw_path}: status {status!r} is not a terminal status {sorted(TERMINAL_STATUSES)}")
    postings = data.get("postings")
    record_count = len(postings) if isinstance(postings, list) else 0
    return {
        "status": status,
        "record_count": record_count,
        "checksum_sha256": sha256_of(raw_path),
        "completion_timestamp": data.get("fetched_at") or utc_now(),
    }


def build_manifest(run_dir: Path, config_path: Path) -> dict[str, Any]:
    """Build a fresh manifest. Raises with a specific message if any
    configured source is missing its raw file or has a non-terminal/invalid
    status -- collection is not actually finished yet."""
    raw_dir = run_dir / "raw"
    slugs = list_configured_slugs(config_path)
    missing: list[str] = []
    invalid: list[str] = []
    sources: dict[str, Any] = {}

    for slug in slugs:
        raw_path = raw_dir / f"{slug}.json"
        if not raw_path.exists():
            missing.append(slug)
            continue
        try:
            sources[slug] = _entry_for(raw_path)
        except Exception as exc:  # noqa: BLE001
            invalid.append(f"{slug}: {exc}")

    if missing or invalid:
        problems = []
        if missing:
            problems.append(f"missing raw file for source(s): {sorted(missing)}")
        if invalid:
            problems.append(f"invalid/non-terminal raw file(s): {invalid}")
        raise RuntimeError(
            "Cannot finalize -- not every configured source is in a terminal state yet. "
            + "; ".join(problems)
            + ". Run scripts/ensure_expected_sources.py first, or dispatch the missing source's agent again."
        )

    return {
        "run_id": run_dir.name,
        "created_at": utc_now(),
        "sources": sources,
        "recovery_log": [],
    }


def cmd_manifest(run_dir: Path, config_path: Path) -> dict[str, Any]:
    manifest = build_manifest(run_dir, config_path)
    _write_manifest(run_dir, manifest)
    return {"ok": True, "sources_finalized": len(manifest["sources"]), "manifest": str(manifest_path(run_dir))}


def cmd_verify(run_dir: Path) -> dict[str, Any]:
    manifest = load_manifest(run_dir)
    raw_dir = run_dir / "raw"
    changed: list[dict[str, Any]] = []
    missing: list[str] = []

    for slug, entry in manifest.get("sources", {}).items():
        raw_path = raw_dir / f"{slug}.json"
        if not raw_path.exists():
            missing.append(slug)
            continue
        current_checksum = sha256_of(raw_path)
        if current_checksum != entry.get("checksum_sha256"):
            changed.append({"slug": slug, "old_checksum": entry.get("checksum_sha256"), "new_checksum": current_checksum})

    ok = not changed and not missing
    return {"ok": ok, "changed": changed, "missing": missing}


def cmd_refresh(run_dir: Path, config_path: Path, note: str | None) -> dict[str, Any]:
    """Recompute manifest entries for slugs verify reports as changed or
    missing, and record a recovery-log entry. Leaves every unaffected slug's
    entry untouched (no unnecessary rewrite / no false "changed" noise)."""
    manifest = load_manifest(run_dir)
    verify_result = cmd_verify(run_dir)
    affected_slugs = [c["slug"] for c in verify_result["changed"]] + verify_result["missing"]

    if not affected_slugs:
        return {"ok": True, "refreshed": [], "message": "no changes detected -- nothing to refresh"}

    raw_dir = run_dir / "raw"
    timestamp = utc_now()
    refreshed: list[str] = []
    for slug in affected_slugs:
        raw_path = raw_dir / f"{slug}.json"
        old_entry = manifest["sources"].get(slug, {})
        if not raw_path.exists():
            # Still missing -- cannot refresh yet; leave the stale entry in
            # place (verify will keep reporting it as missing) and record
            # the observation rather than silently dropping it.
            manifest.setdefault("recovery_log", []).append(
                {
                    "detected_at": timestamp,
                    "slug": slug,
                    "event": "raw_file_still_missing",
                    "note": note,
                }
            )
            continue
        new_entry = _entry_for(raw_path)
        manifest["sources"][slug] = new_entry
        manifest.setdefault("recovery_log", []).append(
            {
                "detected_at": timestamp,
                "slug": slug,
                "event": "raw_file_changed_after_finalization",
                "old_checksum": old_entry.get("checksum_sha256"),
                "new_checksum": new_entry["checksum_sha256"],
                "old_record_count": old_entry.get("record_count"),
                "new_record_count": new_entry["record_count"],
                "note": note,
            }
        )
        refreshed.append(slug)

    _write_manifest(run_dir, manifest)
    return {"ok": True, "refreshed": refreshed, "manifest": str(manifest_path(run_dir))}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/sources.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p_manifest = sub.add_parser("manifest", help="Finalize: assert terminal state and write the checksum manifest")
    p_manifest.add_argument("--run-dir", required=True)

    p_verify = sub.add_parser("verify", help="Read-only: check raw files still match the manifest's checksums")
    p_verify.add_argument("--run-dir", required=True)

    p_refresh = sub.add_parser("refresh", help="Recompute manifest entries for changed/missing sources; log recovery")
    p_refresh.add_argument("--run-dir", required=True)
    p_refresh.add_argument("--note", default=None, help="Human-readable reason, recorded in the recovery log")

    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    config_path = Path(args.config)

    if args.command == "manifest":
        try:
            result = cmd_manifest(run_dir, config_path)
        except RuntimeError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            return 2
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "verify":
        result = cmd_verify(run_dir)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    if args.command == "refresh":
        result = cmd_refresh(run_dir, config_path, args.note)
        print(json.dumps(result, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
