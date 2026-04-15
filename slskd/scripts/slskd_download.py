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
import logging
import os
import re
import sys
import time
import unicodedata

logging.basicConfig(
    stream=sys.stderr,
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("slskd")

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
BETWEEN_ENQUEUE_DELAY_S = 5     # pause between enqueue retries (Soulseek anti-spam)

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


def title_score(filepath: str, title: str) -> int:
    """Return 1 if the cleaned title appears in the filename (not the full path).

    Uses only the last path component so we don't match directory names from
    albums/compilations that share the title (e.g. the 'Bohemian Rhapsody'
    movie soundtrack directory).
    """
    if not title:
        return 0
    # Only check the filename, not the full directory path
    basename = filepath.replace("\\", "/").split("/")[-1]
    base_n = _strip_accents(basename.lower())
    title_n = _strip_accents(clean_for_search(title).lower())
    if not title_n:
        return 0
    # Direct substring
    if title_n in base_n:
        return 1
    # All significant words (> 2 chars) present in the filename
    words = [w for w in re.split(r"[\s\-_]+", title_n) if len(w) > 2]
    if words and all(w in base_n for w in words):
        return 1
    return 0


# Keywords that signal a non-standard/variant recording — penalised in ranking.
# Remastered/deluxe/anniversary are NOT listed: those are still the studio take.
_VARIANT_RE = re.compile(
    r"\b("
    r"live|concert|tour|bootleg"
    r"|acoustic|unplugged|stripped"
    r"|piano|orchestral|instrumental|a[ _-]?cappella|cappella"
    r"|demo|rehearsal|outtake|alternate|alternative"
    r"|remix|remixed|rmx|edit|radio[ _-]?edit|single[ _-]?edit"
    r"|cover|tribute|karaoke|backing[ _-]?track"
    r"|medley|suite|reprise|excerpt|intro|outro|snippet"
    r")\b",
    re.IGNORECASE,
)


def variant_penalty(filename: str, title: str) -> int:
    """Return 1 if the filename contains variant keywords outside the title itself.

    Strategy: strip the cleaned title words from the basename, then check whether
    any variant keyword remains in what's left.  This avoids penalising a track
    called 'Live and Let Die' while still catching 'Bohemian Rhapsody (Live)'.
    """
    basename = filename.replace("\\", "/").split("/")[-1]
    base_n = _strip_accents(re.sub(r"[^\w\s]", " ", basename.lower()))
    title_n = _strip_accents(clean_for_search(title).lower())
    # Blank out title words in basename so we only inspect the suffix/extra parts
    for word in re.split(r"\s+", title_n):
        if len(word) > 2:
            base_n = base_n.replace(word, " ")
    return 1 if _VARIANT_RE.search(base_n) else 0


def _dur_tier(length_s, expected_s, tolerance_s=45) -> int:
    """Return 0 if within tolerance of expected duration, 1 otherwise.

    If expected_s is None (unknown), always returns 0 (no penalty).
    If length_s is None (not reported by peer), returns 0 (benefit of the doubt).
    """
    if expected_s is None or length_s is None:
        return 0
    return 0 if abs(int(length_s) - int(expected_s)) <= tolerance_s else 1


def _size_tier(size: int, fmt: str) -> int:
    """Classify file size into 0=normal, 1=oversized, 2=too-small.

    Extremes are deprioritised: a 3 MB 'FLAC' is likely a short jingle,
    a 900 MB FLAC is likely a 24-bit hi-res or full album rip.
    Normal range per format:
      FLAC  : 8 MB – 300 MB
      MP3   : 3 MB – 60 MB
    """
    mb = size / (1024 * 1024) if size else 0
    if fmt == "flac":
        if mb < 8:   return 2
        if mb > 300: return 1
    else:
        if mb < 3:   return 2
        if mb > 60:  return 1
    return 0


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
      1. title found in filename  — strongest signal we have the right track
      2. duration within ±45s of expected (if known) — filters live/partial versions
      3. artist found in path     — confirms it's the right artist
      4. size tier                — 0=normal, 1=oversized, 2=too-small
      5. FLAC before MP3
      6. upload slot open
      7. size within tier         — prefer smaller within the same tier (avoid huge rips)
    """
    return (
        0 if c["title_ok"] else 1,   # 1. title in filename
        c["dur_tier"],               # 2. duration within ±45s
        c["variant"],                # 3. no live/piano/demo/etc. in filename
        0 if c["artist_ok"] else 1, # 4. artist in path
        c["size_tier"],              # 5. reasonable file size
        0 if c["format"] == "flac" else 1,  # 6. FLAC > MP3
        0 if c["slot_open"] else 1, # 7. slot open
        c["size"] or 0,              # 8. smaller within tier
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

def _do_search(client, query: str, title: str, artist: str, expected_duration_s=None) -> tuple[list, list]:
    """Run one search, return (candidates, raw_responses).

    Raises on hard errors so the caller can decide whether to retry.
    """
    log.debug("search query=%r title=%r artist=%r", query, title, artist)
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

    log.debug("search complete: %d peer responses", len(responses))

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
            size = f.get("size") or 0
            length = f.get("length")   # seconds, may be None
            fmt = "flac" if ext == ".flac" else "mp3"
            candidates.append({
                "username": username,
                "filename": filename,
                "size": size,
                "bitrate": f.get("bitRate"),
                "length": length,
                "format": fmt,
                "slot_open": slot_open,
                "title_ok": bool(title_score(filename, title)),
                "artist_ok": bool(artist_score(filename, artist)),
                "variant": variant_penalty(filename, title),
                "size_tier": _size_tier(size, fmt),
                "dur_tier": _dur_tier(length, expected_duration_s),
                "_raw": f,
            })

    log.debug("candidates after quality filter: %d", len(candidates))
    return candidates, responses


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(title: str, artist: str = "", duration_s: int = None) -> dict:
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
    expected_duration_s = duration_s  # may be None
    log.debug("run title=%r artist=%r clean_title=%r clean_artist=%r duration_s=%s",
              title, artist, clean_title, clean_artist, duration_s)

    # ---- Attempt 1: title only ----
    query1 = clean_title
    candidates, responses = [], []
    try:
        candidates, responses = _do_search(client, query1, title, artist, expected_duration_s)
    except Exception as e:
        msg = str(e)
        if "409" in msg:
            return {"success": False, "reason": "error",
                    "error": "slskd search rate limit hit — too many searches in a short window, try again in a minute"}
        return {"success": False, "reason": "error", "error": msg}

    attempt = 1

    # ---- Attempt 2: artist + title (only if attempt 1 found nothing) ----
    if not candidates and clean_artist:
        time.sleep(BETWEEN_SEARCH_DELAY_S)
        query2 = f"{clean_artist} {clean_title}"
        try:
            candidates, responses = _do_search(client, query2, title, artist, expected_duration_s)
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
    for i, c in enumerate(candidates[:5]):
        log.debug("  #%d %s [%s] title_ok=%s artist_ok=%s variant=%s rank=%s",
                  i+1, c["filename"], c["format"],
                  c["title_ok"], c["artist_ok"], c["variant"], _rank_key(c))

    # Try artist-validated candidates first, then fall back to the full list
    artist_ok = [c for c in candidates if c["artist_ok"]]
    rest = [c for c in candidates if not c["artist_ok"]]
    pool = artist_ok + rest

    MAX_ENQUEUE_TRIES = 3           # keep low — rapid retries can trigger Soulseek anti-spam
    best = None
    last_enqueue_error = ""
    enqueue_tries = 0
    failed_users: set[str] = set()   # skip repeat users after any failure
    for candidate in pool:
        if enqueue_tries >= MAX_ENQUEUE_TRIES:
            log.debug("reached max enqueue attempts (%d)", MAX_ENQUEUE_TRIES)
            break

        username = candidate["username"]
        if username in failed_users:
            log.debug("skipping already-failed user %s", username)
            continue

        if enqueue_tries > 0:
            log.debug("waiting %ds before next enqueue attempt", BETWEEN_ENQUEUE_DELAY_S)
            time.sleep(BETWEEN_ENQUEUE_DELAY_S)

        enqueue_tries += 1
        log.debug("trying enqueue [%d/%d]: %s / %s",
                  enqueue_tries, MAX_ENQUEUE_TRIES, username, candidate["filename"])
        payload = {"filename": candidate["filename"], "size": candidate["size"]}
        try:
            ok = client.transfers.enqueue(username, [payload])
        except Exception as e:
            last_enqueue_error = str(e)
            log.warning("enqueue exception for %s / %s: %s", username, candidate["filename"], e)
            failed_users.add(username)
            continue
        if not ok:
            last_enqueue_error = f"HTTP error for {username}"
            log.warning("enqueue rejected for %s / %s", username, candidate["filename"])
            failed_users.add(username)
            continue
        best = candidate
        log.debug("enqueue ok: %s / %s", username, candidate["filename"])
        break

    if best is None:
        return {
            "success": False, "reason": "error",
            "error": f"All enqueue attempts failed. Last error: {last_enqueue_error}",
        }

    # Soulseek paths use Windows backslashes; os.path.basename only splits on /
    full_path = best["filename"]
    basename = full_path.replace("\\", "/").split("/")[-1]

    size_mb = round(best["size"] / (1024 * 1024), 1) if best["size"] else None
    return {
        "success": True,
        "file": basename,
        "full_path": full_path,
        "format": best["format"],
        "bitrate": best["bitrate"],
        "user": best["username"],
        "size_mb": size_mb,
        "slot_open": best["slot_open"],
        "length_s": best["length"],
        "title_validated": best["title_ok"],
        "artist_validated": best["artist_ok"],
        "attempt": attempt,
    }


def main():
    parser = argparse.ArgumentParser(description="Download a track from Soulseek via slskd")
    parser.add_argument("--title", required=True, help="Track title (from Spotify metadata)")
    parser.add_argument("--artist", default="", help="Artist name (used as post-filter, not search term)")
    parser.add_argument("--duration_s", type=int, default=None, help="Expected track duration in seconds (from Spotify)")
    args = parser.parse_args()

    result = run(title=args.title, artist=args.artist, duration_s=args.duration_s)
    print(json.dumps(result))
    sys.exit(0 if result.get("success") or result.get("reason") == "no_quality_match" else 1)


if __name__ == "__main__":
    main()
