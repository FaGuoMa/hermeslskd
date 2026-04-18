#!/usr/bin/env python3
"""
Retry failed slskd downloads: lists Errored/TimedOut transfers, attempts
a fresh search for each, and removes the failed entry on success.

Usage:
    python slskd_retry.py [--dry-run]

Environment variables:
    SLSKD_API_KEY   (required)
    SLSKD_HOST      (default: localhost)
    SLSKD_PORT      (default: 5030)

JSON output on stdout:
    {
      "total_failed": N,
      "succeeded": N,
      "failed": N,
      "results": [
        {"original_file": "...", "original_user": "...", "original_state": "...",
         "parsed_title": "...", "parsed_artist": "...",
         "status": "success|no_match|error|dry_run",
         "new_file": "...",        // on success
         "new_user": "...",        // on success
         "format": "flac|mp3",    // on success
         "original_removed": true, // on success
         "reason": "..."}         // on failure
      ]
    }
"""
import argparse
import json
import logging
import os
import re
import sys
import time

logging.basicConfig(
    stream=sys.stderr,
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("slskd_retry")

sys.path.insert(0, os.path.dirname(__file__))

try:
    from slskd_api import SlskdClient
except ImportError:
    print(json.dumps({
        "success": False, "reason": "error",
        "error": (
            "slskd-api not installed. Run: "
            "uv pip install slskd-api --python ~/.hermes/hermes-agent/venv/bin/python3"
        ),
    }))
    sys.exit(1)

from slskd_download import run as download_run, HOST, API_KEY, BETWEEN_SEARCH_DELAY_S

# Secondary states (after "Completed, ") worth retrying
FAILED_STATES = {"Errored", "TimedOut", "Rejected"}


def parse_title_artist(filepath: str) -> tuple[str, str]:
    """Extract best-guess title and artist from a Soulseek file path.

    Typical path: C:\\Users\\peer\\Music\\Queen\\A Night at the Opera\\05 - Bohemian Rhapsody.flac
    → title="Bohemian Rhapsody", artist="Queen"
    """
    parts = [p for p in filepath.replace("\\", "/").split("/") if p]

    filename = parts[-1] if parts else filepath
    title, _ = os.path.splitext(filename)
    # Strip leading track number: "05 - ", "5. ", "01 ", "1-"
    title = re.sub(r"^\d+[\s.\-_]+", "", title).strip()

    # Artist: 2 levels up from filename (structure: …/Artist/Album/file)
    artist = ""
    if len(parts) >= 3:
        candidate = parts[-3]
    elif len(parts) >= 2:
        candidate = parts[-2]
    else:
        candidate = ""

    # Skip obviously non-artist path components
    _SKIP = {"music", "downloads", "users", "home", "documents", "muziek",
              "mp3", "flac", "audio", "media", "shared", "public"}
    if candidate and not re.match(r"^[A-Z]:$", candidate) and candidate.lower() not in _SKIP:
        artist = candidate

    return title, artist


def get_failed_downloads(client: SlskdClient) -> list[dict]:
    """Return list of {username, id, filename, state} for failed downloads."""
    failed = []
    try:
        all_downloads = client.transfers.get_all_downloads()
    except Exception as e:
        log.error("Failed to fetch downloads: %s", e)
        return []

    for user_group in (all_downloads or []):
        username = user_group.get("username", "")
        for directory in user_group.get("directories", []):
            for f in directory.get("files", []):
                state = f.get("state", "") or ""
                # slskd state is "Completed, <secondary>" — check secondary part
                state_key = state.split(",")[-1].strip()
                if state_key in FAILED_STATES:
                    failed.append({
                        "username": username,
                        "id": f.get("id", ""),
                        "filename": f.get("filename", ""),
                        "state": state,
                    })

    return failed


def remove_failed(client: SlskdClient, username: str, file_id: str) -> bool:
    """Remove a failed download entry from slskd."""
    try:
        client.transfers.cancel_download(username=username, id=file_id, remove=True)
        return True
    except Exception as e:
        log.warning("Failed to remove %s / %s: %s", username, file_id, e)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Retry failed slskd downloads with a fresh search"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List failed downloads without actually retrying them",
    )
    args = parser.parse_args()

    if not API_KEY:
        print(json.dumps({
            "success": False, "reason": "error",
            "error": "SLSKD_API_KEY is not set. Add it to ~/.hermes/.env",
        }))
        sys.exit(1)

    try:
        client = SlskdClient(host=HOST, api_key=API_KEY, url_base="/")
    except Exception as e:
        print(json.dumps({"success": False, "reason": "error",
                          "error": f"Connection failed: {e}"}))
        sys.exit(1)

    failed = get_failed_downloads(client)
    log.info("Found %d failed download(s) in history", len(failed))

    # Deduplicate: keep only the most recent failure per basename so we don't
    # retry the same track N times when it has failed repeatedly in history.
    seen_basenames: set[str] = set()
    deduped = []
    for entry in failed:
        basename = entry["filename"].replace("\\", "/").split("/")[-1].lower()
        if basename not in seen_basenames:
            seen_basenames.add(basename)
            deduped.append(entry)
    if len(deduped) < len(failed):
        log.info("Deduplicated to %d unique track(s)", len(deduped))
    failed = deduped

    if not failed:
        print(json.dumps({"total_failed": 0, "succeeded": 0, "failed": 0, "results": []}))
        sys.exit(0)

    results = []
    succeeded = 0
    failed_count = 0

    for i, entry in enumerate(failed):
        title, artist = parse_title_artist(entry["filename"])
        basename = entry["filename"].replace("\\", "/").split("/")[-1]
        log.info("[%d/%d] %s  (title=%r  artist=%r)", i + 1, len(failed), basename, title, artist)

        result_entry = {
            "original_file": basename,
            "original_user": entry["username"],
            "original_state": entry["state"],
            "parsed_title": title,
            "parsed_artist": artist,
        }

        if args.dry_run:
            result_entry["status"] = "dry_run"
            results.append(result_entry)
            continue

        if i > 0:
            log.debug("Waiting %ds before next search (rate-limit)", BETWEEN_SEARCH_DELAY_S)
            time.sleep(BETWEEN_SEARCH_DELAY_S)

        dl = download_run(title=title, artist=artist)

        if dl.get("success"):
            succeeded += 1
            removed = remove_failed(client, entry["username"], entry["id"])
            result_entry.update({
                "status": "success",
                "new_file": dl.get("file"),
                "new_user": dl.get("user"),
                "format": dl.get("format"),
                "original_removed": removed,
            })
            log.info("  → queued %s [%s] — original %s",
                     dl.get("file"), dl.get("format"),
                     "removed" if removed else "NOT removed")
        elif dl.get("reason") == "no_quality_match":
            failed_count += 1
            result_entry.update({
                "status": "no_match",
                "reason": f"No FLAC/MP3-320 found (best: {dl.get('best_found', 'none')})",
            })
            log.info("  → no quality match (best: %s)", dl.get("best_found"))
        else:
            failed_count += 1
            result_entry.update({
                "status": "error",
                "reason": dl.get("error", "unknown error"),
            })
            log.info("  → error: %s", dl.get("error"))

        results.append(result_entry)

    print(json.dumps({
        "total_failed": len(failed),
        "succeeded": succeeded,
        "failed": failed_count,
        "results": results,
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
