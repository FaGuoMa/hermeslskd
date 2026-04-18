---
name: slskd
description: Download a Spotify track from Soulseek via slskd — resolves track metadata, searches for FLAC or MP3 320kbps, and queues the best match for download
version: 1.1.0
author: max
platforms: [linux, telegram]
metadata:
  hermes:
    tags: [music, download, soulseek, spotify, flac]
    config:
      - key: slskd.host
        description: slskd server hostname or IP address
        default: "192.168.1.110"
        prompt: "slskd server host?"
      - key: slskd.port
        description: slskd server port
        default: "5030"
        prompt: "slskd server port?"
---

# slskd Spotify Downloader

Download music from Soulseek when given a Spotify track link. Searches for high-quality files (FLAC or MP3 ≥ 320 kbps) and queues the best match in your slskd instance.

## How the search works

Soulseek search is bare-bones: no boolean operators, no filetype filters, extremely sensitive to exact phrasing.  The script compensates with this strategy:

- **Search uses the track title only** (never "Artist - Title") — a shorter, cleaner query hits more peers
- **Artist name is a soft validator** — results whose file path contains the artist name are ranked above those that don't, but are not excluded (so a FLAC without the artist in the path beats an MP3 that has it)
- **Accents and special characters are stripped** before the query is sent ("Björk" → "Bjork", "Ñoño" → "Nono")
- **Parenthetical version info is dropped** from the search query ("Bohemian Rhapsody (Remastered 2011)" → "Bohemian Rhapsody")
- **Two attempts maximum**: if the title-only search returns zero qualifying results, the script waits 8 seconds (rate-limit) and retries with "artist title"; it never fires more than two searches

## Setup

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

# Locate the skill directory (works whether installed in default or external_dirs)
SKILL_DIR="$(python3 -c "
import os, sys, yaml
home = os.environ.get('HERMES_HOME', os.path.expanduser('~/.hermes'))
cfg_path = os.path.join(home, 'config.yaml')
search_bases = [home]
if os.path.isfile(cfg_path):
    cfg = yaml.safe_load(open(cfg_path)) or {}
    for d in (cfg.get('skills') or {}).get('external_dirs') or []:
        search_bases.append(os.path.expanduser(d))
for base in search_bases:
    for candidate in [os.path.join(base, 'skills', 'slskd'), os.path.join(base, 'slskd')]:
        if os.path.isfile(os.path.join(candidate, 'SKILL.md')):
            print(candidate); sys.exit(0)
print('')
")"

PYTHON_BIN="$HERMES_HOME/hermes-agent/venv/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

SPOTIFY_SCRIPT="$SKILL_DIR/scripts/spotify_info.py"
SLSKD_SCRIPT="$SKILL_DIR/scripts/slskd_download.py"
SLSKD_RETRY_SCRIPT="$SKILL_DIR/scripts/slskd_retry.py"
```

## Workflow

When the user invokes `/slskd <spotify_url>`:

### Step 1 — Extract track metadata

```bash
TRACK_INFO=$("$PYTHON_BIN" "$SPOTIFY_SCRIPT" "<spotify_url>")
echo "$TRACK_INFO"
```

Parse the JSON. Extract:
- `title` — track title (may include version suffix like "Remastered 2011" — the script handles stripping it)
- `artist` — artist name (used as a soft validator, not a search term)

If the script exits non-zero or returns `{"error": "..."}`:
- Try to extract a plain title from the URL itself
- Proceed with just the title (no artist validation)

### Step 2 — Search and download

```bash
SLSKD_HOST="<slskd.host>" SLSKD_PORT="<slskd.port>" \
  "$PYTHON_BIN" "$SLSKD_SCRIPT" \
    --title "<title>" \
    --artist "<artist>"
```

Replace `<slskd.host>` and `<slskd.port>` with the values injected from skill config.
Pass the raw title and artist exactly as returned by `spotify_info.py` — the download script handles all cleaning internally.

If artist is unknown or empty, omit `--artist` entirely.

### Step 3 — Report result to user

Parse the JSON output:

**Success (`"success": true`):**
```
Queued: <file> [<FORMAT> / <bitrate> kbps] from <user> (<size_mb> MB)
```
- If `known_peer` is true: add "(known peer, <peer_download_count> previous downloads)"
- If `artist_validated` is false: add a note — "⚠ artist not confirmed in filename"
- If `attempt` is 2: add a note — "found on retry search"

**No quality match (`"reason": "no_quality_match"`):**
```
No FLAC or MP3 320+ found for "<title>". Best available was <best_found>. Not downloading.
```

**Error (`"reason": "error"`):**
```
Download failed: <error>
```

## Retry failed downloads

When the user invokes `/slskd retry` (optionally with `--dry-run`):

```bash
SLSKD_HOST="<slskd.host>" SLSKD_PORT="<slskd.port>" \
  "$PYTHON_BIN" "$SKILL_DIR/scripts/slskd_retry.py"
```

Add `--dry-run` if the user asked to preview without downloading.

The script:
1. Fetches all transfers in `Errored` or `TimedOut` state
2. Parses title and artist from each file's path
3. Runs a fresh search for each (same quality floor: FLAC or MP3 ≥ 320 kbps)
4. On success: queues the new file and removes the old failed entry
5. On failure: leaves the failed entry in place

**Report format:**

For each result:
- `success` → `Queued: <new_file> [FORMAT] from <user> — replaced failed <original_file>`
- `no_match` → `No quality match for "<title>" (<reason>). Failed entry kept.`
- `error` → `Error retrying "<title>": <reason>. Failed entry kept.`

End with a summary line: `Retry complete: N succeeded, N failed, N no match (of N total).`

If `total_failed` is 0: reply "No failed downloads found."

## Example

User: `/slskd https://open.spotify.com/track/4u7EnebtmKWzUH433cf5Qv`

```bash
# Step 1
"$PYTHON_BIN" "$SPOTIFY_SCRIPT" "https://open.spotify.com/track/4u7EnebtmKWzUH433cf5Qv"
# → {"artist": "Queen", "title": "Bohemian Rhapsody - Remastered 2011", "query": "Queen - Bohemian Rhapsody - Remastered 2011"}

# Step 2 — pass title and artist separately
SLSKD_HOST="192.168.1.110" SLSKD_PORT="5030" \
  "$PYTHON_BIN" "$SLSKD_SCRIPT" \
    --title "Bohemian Rhapsody - Remastered 2011" \
    --artist "Queen"
# Script searches for "Bohemian Rhapsody" (stripped), validates against "Queen" in path
# → {"success": true, "file": "01 - Bohemian Rhapsody.flac", "format": "flac",
#    "bitrate": null, "user": "somepeer", "size_mb": 42.3,
#    "artist_validated": true, "attempt": 1}
```

Reply: "Queued: 01 - Bohemian Rhapsody.flac [FLAC] from somepeer (42.3 MB)"

## Rules

1. **Pass title and artist as separate arguments** — never combine them into one string before passing to the script; the script decides how to use each.
2. **Quality floor is strict** — MP3 < 320 kbps and non-FLAC/MP3 formats are silently rejected by the script.
3. **The download is queued, not instant** — say "queued" not "downloaded".
4. **Two searches max** — the script already enforces this; do not call it in a loop.
5. **Three enqueue attempts max, 5s apart** — rapid enqueue retries trigger Soulseek's anti-spam protection and cause disconnection.
6. **If `SLSKD_API_KEY` is missing** — report: "SLSKD_API_KEY is not set in ~/.hermes/.env"
7. **If Spotify metadata extraction fails** — still attempt the download using just the title.
