#!/usr/bin/env python3
"""
Search slskd for a track and download the best available quality file.

Soulseek search is basic — no boolean operators, no file-type filters, very
sensitive to exact phrasing.  Strategy:

  Attempt 1 – search with the cleaned track TITLE only.
              Artist name is used only as a post-filter ranking signal
              (preferred, not required), so a FLAC of "Bohemian Rhapsody"
              by anyone beats an MP3-192 tagged Queen.

  Attempt 2 – if attempt 1 yields zero qualifying results, wait
              BETWEEN_SEARCH_DELAY_S seconds (rate-limit) and retry with
              "artist title" combined.  Two searches max — never spam.

Quality floor: FLAC (any bitrate) OR MP3 ≥ 320 kbps.  Anything below is
rejected silently.

Usage:
    python slskd_download.py --title "Bohemian Rhapsody - Remastered 2011" \\
                              --artist "Queen"

    # artist is optional; without it quality-only ranking is used
    python slskd_download.py --title "Smells Like Teen Spirit"

Environment variables:
    SLSKD_API_KEY   (required)
    SLSKD_HOST      (default: 192.168.1.110)
    SLSKD_PORT      (default: 5030)

Exit 0 on success or clean no-match, 1 on hard error.

JSON output on stdout:
    success  → {"success": true, "file": "...", "format": "flac|mp3",
                 "bitrate": N, "user": "...", "size_mb": N,
                 "artist_validated": true|false, "attempt": 1|2}
    no match → {"success": false, "reason": "no_quality_match",
                 "query_used": "...", "best_found": "mp3@128|none"}
    error    → {"success": false, "reason": "error", "error": "..."}
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata

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
# Config
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("SLSKD_API_KEY", "")
_HOST = os.environ.get("SLSKD_HOST", "192.168.1.110")
_PORT = os.environ.get("SLSKD_PORT", "5030")
HOST = f"http://{_HOST}:{_PORT}" if not _HOST.startswith("http") else _HOST

SEARCH_TIMEOUT_MS = 15000       # slskd-side timeout per search
POLL_INTERVAL_S = 2             # polling cadence while waiting for results
MAX_POLL_S = 25                 # give up polling after this
RESPONSE_LIMIT = 200            # max peers per search
BETWEEN_SEARCH_DELAY_S = 8      # pause between attempt 1 and attempt 2

# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

def _strip_accents(text: str) -> str:
    """Decompose accented characters and drop combining marks.
    e.g.  "Björk" → "Bjork",  "Cœur" → "Coeur"
    """
    nfd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def clean_for_search(text: str) -> str:
    """Return a Soulseek-friendly search token.

    Steps:
      1. Strip accents (NFD decomposition)
      2. Drop parenthetical / bracketed version info:
         "Something (Remastered 2011)" → "Something"
         "Something [Live]" → "Something"
      3. Drop bare dash-separated version suffixes:
         "Bohemian Rhapsody - Remastered 2011" → "Bohemian Rhapsody"
         "Track - Radio Edit" → "Track"
         (Only strips known version keywords to avoid eating real titles)
      4. Replace non-alphanumeric chars (except spaces) with a space
      5. Collapse whitespace
    """
    VERSION_KEYWORDS = (
        r"remaster(?:ed)?(?:\s+\d{4})?",
        r"re-?master(?:ed)?(?:\s+\d{4})?",
        r"live(?:\s+version)?",
        r"acoustic(?:\s+version)?",
        r"radio\s+edit",
        r"single\s+version",
        r"album\s+version",
        r"extended\s+(?:version|mix)",
        r"original\s+(?:version|mix)",
        r"deluxe(?:\s+edition)?",
        r"\d{4}\s+remaster",
        r"feat\.?.*",
        r"ft\.?.*",
    )
    text = _strip_accents(text)
    # Remove bracketed/parenthetical content
    text = re.sub(r"\s*[\(\[][^\)\]]{0,60}[\)\]]", "", text)
    # Remove bare " - <version keyword>" suffixes
    version_pat = "|".join(VERSION_KEYWORDS)
    text = re.sub(rf"\s*-\s*(?:{version_pat})\s*$", "", text, flags=re.IGNORECASE)
    # Replace anything that's not a word char or space with a space
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Artist validation (soft heuristic, not a hard filter)
# ---------------------------------------------------------------------------

def _primary_artist(artist: str) -> str:
    """Return the main artist, dropping 'feat.' collaborators."""
    return re.split(r"\s*(?:feat\.?|ft\.?|&|,)\s*", artist, maxsplit=1,
                    flags=re.IGNORECASE)[0].strip()


def artist_score(filepath: str, artist: str) -> int:
    """Return 1 if the artist seems to appear in the file path, else 0.

    Uses the cleaned primary artist name, checked as a substring and as a
    bag-of-significant-words against the full file path (which typically
    includes artist/album directory names).
    """
    if not artist:
        return 0
    path_n = _strip_accents(filepath.lower())
    primary = _primary_artist(artist)
    artist_n = _strip_accents(primary.lower())
    # Direct substring
    if artist_n in path_n:
        return 1
    # All significant words (> 2 chars) present individually
    words = [w for w in re.split(r"[\s\-_]+", artist_n) if len(w) > 2]
    if words and all(w in path_n for w in words):
        return 1
    return 0


# ---------------------------------------------------------------------------
# Quality helpers
# ---------------------------------------------------------------------------

def _normalise_ext(f: dict) -> str:
    ext = f.get("extension", "")
    if ext:
        ext = ext.lower()
        return ext if ext.startswith(".") else f".{ext}"
    return os.path.splitext(f.get("filename", "").lower())[1]


def _is_accepted(f: dict) -> bool:
    ext = _normalise_ext(f)
    if ext == ".flac":
        return True
    if ext == ".mp3":
        br = f.get("bitRate")
        return br is not None and int(br) >= 320
    return False


def _rank_key(c: dict) -> tuple:
    """
    Priority (lowest tuple = best):
      1. artist validated
      2. FLAC before MP3
      3. upload slot open
      4. larger file
    """
    return (
        0 if c["artist_ok"] else 1,
        0 if c["format"] == "flac" else 1,
        0 if c["slot_open"] else 1,
        -(c["size"] or 0),
    )


def _best_found_info(responses: list) -> str:
    audio = {".flac", ".mp3", ".ogg", ".wma", ".aac", ".m4a"}
    best_ext, best_br = None, None
    for resp in responses:
        for f in resp.get("files", []):
            ext = _normalise_ext(f)
            if ext not in audio:
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
    return f"{best_ext.lstrip('.')}" + (f"@{best_br}" if best_br else "")


# ---------------------------------------------------------------------------
# Single search + filter pass
# ---------------------------------------------------------------------------

def _do_search(client, query: str, artist: str) -> tuple[list, list]:
    """Run one search, return (candidates, raw_responses).

    Raises on hard errors so the caller can decide whether to retry.
    """
    search = client.searches.search_text(
        query,
        searchTimeout=SEARCH_TIMEOUT_MS,
        responseLimit=RESPONSE_LIMIT,
    )
    search_id = search.get("id")
    if not search_id:
        raise RuntimeError(f"No search ID in response: {search}")

    # Poll until done
    deadline = time.time() + MAX_POLL_S
    while time.time() < deadline:
        try:
            if client.searches.state(search_id).get("isComplete"):
                break
        except Exception:
            pass
        time.sleep(POLL_INTERVAL_S)

    try:
        responses = client.searches.search_responses(search_id)
    except Exception as e:
        raise RuntimeError(f"Could not fetch responses: {e}")

    if not isinstance(responses, list):
        responses = []

    # Clean up from slskd UI
    try:
        client.searches.delete(search_id)
    except Exception:
        pass

    candidates = []
    for resp in responses:
        username = resp.get("username", "")
        slot_open = bool(resp.get("hasFreeUploadSlot", False))
        for f in resp.get("files", []):
            if not _is_accepted(f):
                continue
            ext = _normalise_ext(f)
            filename = f.get("filename", "")
            candidates.append({
                "username": username,
                "filename": filename,
                "size": f.get("size") or 0,
                "bitrate": f.get("bitRate"),
                "format": "flac" if ext == ".flac" else "mp3",
                "slot_open": slot_open,
                "artist_ok": bool(artist_score(filename, artist)),
                "_raw": f,
            })

    return candidates, responses


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(title: str, artist: str = "") -> dict:
    if not API_KEY:
        return {
            "success": False, "reason": "error",
            "error": "SLSKD_API_KEY is not set. Add it to ~/.hermes/.env",
        }

    try:
        client = SlskdClient(host=HOST, api_key=API_KEY, url_base="/")
    except Exception as e:
        return {"success": False, "reason": "error", "error": f"Connection failed: {e}"}

    clean_title = clean_for_search(title)
    clean_artist = clean_for_search(artist) if artist else ""

    # ---- Attempt 1: title only ----
    query1 = clean_title
    candidates, responses = [], []
    try:
        candidates, responses = _do_search(client, query1, artist)
    except Exception as e:
        return {"success": False, "reason": "error", "error": str(e)}

    attempt = 1

    # ---- Attempt 2: artist + title (only if attempt 1 found nothing) ----
    if not candidates and clean_artist:
        time.sleep(BETWEEN_SEARCH_DELAY_S)
        query2 = f"{clean_artist} {clean_title}"
        try:
            candidates, responses = _do_search(client, query2, artist)
            attempt = 2
        except Exception as e:
            # Don't mask attempt-1 "nothing found" behind a retry error
            pass

    if not candidates:
        return {
            "success": False,
            "reason": "no_quality_match",
            "query_used": query1 if attempt == 1 else f"{clean_artist} {clean_title}",
            "best_found": _best_found_info(responses),
        }

    candidates.sort(key=_rank_key)
    best = candidates[0]

    try:
        client.transfers.enqueue(best["username"], [best["_raw"]])
    except Exception as e:
        return {
            "success": False, "reason": "error",
            "error": f"Enqueue failed ({best['username']} / {best['filename']}): {e}",
        }

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
        "artist_validated": best["artist_ok"],
        "attempt": attempt,
    }


def main():
    parser = argparse.ArgumentParser(description="Download a track from Soulseek via slskd")
    parser.add_argument("--title", required=True, help="Track title (from Spotify metadata)")
    parser.add_argument("--artist", default="", help="Artist name (used as post-filter, not search term)")
    args = parser.parse_args()

    result = run(title=args.title, artist=args.artist)
    print(json.dumps(result))
    sys.exit(0 if result.get("success") or result.get("reason") == "no_quality_match" else 1)


if __name__ == "__main__":
    main()
