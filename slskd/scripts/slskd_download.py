#!/usr/bin/env python3
"""
Search slskd for a track and download the best available quality file.

Quality floor: FLAC (lossless, any bitrate) or MP3 at 320 kbps minimum.
Ranking: FLAC > MP3-320; open upload slots preferred; larger file as tiebreaker.

Usage:
    python slskd_download.py "Artist - Track Title"

Environment variables:
    SLSKD_API_KEY   (required)
    SLSKD_HOST      (default: 192.168.1.110)
    SLSKD_PORT      (default: 5030)

Output (JSON to stdout):
    Success: {"success": true, "file": "...", "format": "flac|mp3", "bitrate": N,
              "user": "...", "size_mb": N}
    No match: {"success": false, "reason": "no_quality_match",
               "best_found": "mp3@128" or "none"}
    Error:    {"success": false, "reason": "error", "error": "..."}

Exit code 0 on success or no-match, 1 on error.
"""
import json
import os
import sys
import time

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

# ---------------------------------------------------------------------------
# Config (from environment)
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("SLSKD_API_KEY", "")
_HOST = os.environ.get("SLSKD_HOST", "192.168.1.110")
_PORT = os.environ.get("SLSKD_PORT", "5030")
# SlskdClient expects a full URL as host
HOST = f"http://{_HOST}:{_PORT}" if not _HOST.startswith("http") else _HOST

SEARCH_TIMEOUT_MS = 15000   # slskd-side search timeout
POLL_INTERVAL_S = 2         # seconds between isComplete polls
MAX_POLL_S = 25             # give up after this many seconds
RESPONSE_LIMIT = 200        # max peers to collect

# ---------------------------------------------------------------------------
# Quality helpers
# ---------------------------------------------------------------------------

def _normalise_ext(f: dict) -> str:
    """Return lowercased extension with leading dot, e.g. '.flac'."""
    ext = f.get("extension", "")
    if ext:
        ext = ext.lower()
        return ext if ext.startswith(".") else f".{ext}"
    filename = f.get("filename", "")
    return os.path.splitext(filename.lower())[1]


def _is_accepted(f: dict) -> bool:
    """Return True if this file meets the quality floor."""
    ext = _normalise_ext(f)
    if ext == ".flac":
        return True
    if ext == ".mp3":
        bitrate = f.get("bitRate")
        return bitrate is not None and int(bitrate) >= 320
    return False


def _rank_key(c: dict) -> tuple:
    """Lower tuple = higher priority (FLAC, open slot, bigger file)."""
    return (
        0 if c["format"] == "flac" else 1,
        0 if c["slot_open"] else 1,
        -(c["size"] or 0),
    )


def _best_found_info(responses: list) -> str:
    """Summarise the best quality seen when nothing passes the floor."""
    best_ext = None
    best_br = None
    audio_exts = {".flac", ".mp3", ".ogg", ".wma", ".aac", ".m4a"}
    for resp in responses:
        for f in resp.get("files", []):
            ext = _normalise_ext(f)
            if ext not in audio_exts:
                continue
            br = f.get("bitRate")
            if best_ext is None:
                best_ext, best_br = ext, br
            elif ext == ".flac" and best_ext != ".flac":
                best_ext, best_br = ext, br
            elif best_br is None or (br and int(br) > int(best_br or 0)):
                best_br = br
    if best_ext is None:
        return "none"
    br_str = f"@{best_br}" if best_br else ""
    return f"{best_ext.lstrip('.')}{br_str}"


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def run(query: str) -> dict:
    if not API_KEY:
        return {
            "success": False, "reason": "error",
            "error": "SLSKD_API_KEY is not set. Add it to ~/.hermes/.env",
        }

    try:
        client = SlskdClient(host=HOST, api_key=API_KEY, url_base="/")
    except Exception as e:
        return {"success": False, "reason": "error", "error": f"Connection failed: {e}"}

    # ---- Initiate search ----
    try:
        search = client.searches.search_text(
            query,
            searchTimeout=SEARCH_TIMEOUT_MS,
            responseLimit=RESPONSE_LIMIT,
        )
        search_id = search.get("id")
        if not search_id:
            return {
                "success": False, "reason": "error",
                "error": f"Could not obtain search ID. Response: {search}",
            }
    except Exception as e:
        return {"success": False, "reason": "error", "error": f"Search initiation failed: {e}"}

    # ---- Poll until isComplete ----
    deadline = time.time() + MAX_POLL_S
    while time.time() < deadline:
        try:
            state = client.searches.state(search_id)
            if state.get("isComplete"):
                break
        except Exception:
            pass
        time.sleep(POLL_INTERVAL_S)

    # ---- Collect responses ----
    try:
        responses = client.searches.search_responses(search_id)
    except Exception as e:
        return {"success": False, "reason": "error", "error": f"Could not fetch responses: {e}"}

    if not isinstance(responses, list):
        responses = []

    # ---- Filter & rank candidates ----
    candidates = []
    for resp in responses:
        username = resp.get("username", "")
        slot_open = bool(resp.get("hasFreeUploadSlot", False))
        for f in resp.get("files", []):
            if not _is_accepted(f):
                continue
            ext = _normalise_ext(f)
            candidates.append({
                "username": username,
                "filename": f.get("filename", ""),
                "size": f.get("size") or 0,
                "bitrate": f.get("bitRate"),
                "format": "flac" if ext == ".flac" else "mp3",
                "slot_open": slot_open,
                "_raw": f,   # kept for enqueue()
            })

    if not candidates:
        return {
            "success": False,
            "reason": "no_quality_match",
            "query": query,
            "best_found": _best_found_info(responses),
        }

    candidates.sort(key=_rank_key)
    best = candidates[0]

    # ---- Enqueue download ----
    try:
        client.transfers.enqueue(best["username"], [best["_raw"]])
    except Exception as e:
        return {
            "success": False, "reason": "error",
            "error": f"Enqueue failed ({best['username']} / {best['filename']}): {e}",
        }

    # Clean up search from slskd UI
    try:
        client.searches.delete(search_id)
    except Exception:
        pass

    size_mb = round(best["size"] / (1024 * 1024), 1) if best["size"] else None
    return {
        "success": True,
        "file": os.path.basename(best["filename"]),
        "full_path": best["filename"],
        "format": best["format"],
        "bitrate": best["bitrate"],
        "user": best["username"],
        "size_mb": size_mb,
        "slot_open": best["slot_open"],
    }


def main():
    if len(sys.argv) < 2:
        print(json.dumps({
            "success": False, "reason": "error",
            "error": 'Usage: slskd_download.py "Artist - Track Title"',
        }))
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    result = run(query)
    print(json.dumps(result))
    sys.exit(0 if result.get("success") or result.get("reason") == "no_quality_match" else 1)


if __name__ == "__main__":
    main()
