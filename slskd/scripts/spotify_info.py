#!/usr/bin/env python3
"""
Extract artist and title from a Spotify track URL using only public endpoints.
No API credentials required.

Approach:
  1. Parse track ID from the URL.
  2. Fetch the Spotify embed page (/embed/track/<id>) which contains a
     __NEXT_DATA__ JSON block with full track metadata (title, artists, etc.).
  3. Fall back to the oEmbed API for the title if the embed page fails.

Usage:
    python spotify_info.py <spotify_track_url>

Output (JSON to stdout):
    {"artist": "Queen", "title": "Bohemian Rhapsody", "query": "Queen - Bohemian Rhapsody"}

Exit code 0 on success, 1 on failure (with {"error": "..."}).
"""
import json
import re
import sys

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def get_track_info(url: str) -> dict:
    # Normalise: strip query params / fragments
    url = re.split(r"[?#]", url)[0].rstrip("/")

    track_id_match = re.search(r"/track/([A-Za-z0-9]+)", url)
    if not track_id_match:
        raise ValueError(f"Not a Spotify track URL: {url}")
    track_id = track_id_match.group(1)

    title = ""
    artist = ""
    duration_s = None

    # -- Primary: embed page __NEXT_DATA__ JSON (no auth required) --
    try:
        embed_url = f"https://open.spotify.com/embed/track/{track_id}"
        r = requests.get(embed_url, headers=HEADERS, timeout=10)
        r.raise_for_status()

        # Spotify's embed page is a Next.js app that inlines all track data
        nd_match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
            r.text,
            re.DOTALL,
        )
        if nd_match:
            data = json.loads(nd_match.group(1))
            entity = (
                data.get("props", {})
                    .get("pageProps", {})
                    .get("state", {})
                    .get("data", {})
                    .get("entity", {})
            )
            title = entity.get("title") or entity.get("name", "")
            artists = entity.get("artists", [])
            if artists:
                artist = ", ".join(a["name"] for a in artists if a.get("name"))
            # Duration in milliseconds → seconds
            dur_ms = entity.get("duration")
            if dur_ms:
                duration_s = int(dur_ms) // 1000
    except Exception:
        pass

    # -- Fallback: oEmbed gives at least the track title --
    if not title:
        try:
            oembed_url = (
                f"https://open.spotify.com/oembed"
                f"?url=https://open.spotify.com/track/{track_id}"
            )
            r = requests.get(oembed_url, headers=HEADERS, timeout=10)
            r.raise_for_status()
            title = r.json().get("title", "").strip()
        except Exception:
            pass

    if not title:
        raise RuntimeError("Could not extract track title from Spotify URL")

    query = f"{artist} - {title}" if artist else title
    result = {"artist": artist, "title": title, "query": query}
    if duration_s is not None:
        result["duration_s"] = duration_s
    return result


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: spotify_info.py <spotify_track_url>"}))
        sys.exit(1)

    try:
        info = get_track_info(sys.argv[1])
        print(json.dumps(info))
        sys.exit(0)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
