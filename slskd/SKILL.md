---
name: slskd
description: Download a Spotify track from Soulseek via slskd — resolves track metadata, searches for FLAC or MP3 320kbps, and queues the best match for download
version: 1.0.0
author: max
platforms: [linux]
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

## Setup

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SKILL_DIR="$(python3 -c "
import os, sys
home = os.environ.get('HERMES_HOME', os.path.expanduser('~/.hermes'))
# Check external_dirs first, then default skills path
for base in [home]:
    for candidate in [
        os.path.join(base, 'skills', 'slskd'),
        os.path.join(base, '..', 'hermeslskd', 'slskd'),
    ]:
        if os.path.isfile(os.path.join(candidate, 'SKILL.md')):
            print(candidate)
            sys.exit(0)
# Fallback: search external_dirs from config
import yaml
cfg = os.path.join(home, 'config.yaml')
if os.path.isfile(cfg):
    data = yaml.safe_load(open(cfg))
    for d in (data.get('skills', {}).get('external_dirs') or []):
        candidate = os.path.join(os.path.expanduser(d), 'slskd')
        if os.path.isfile(os.path.join(candidate, 'SKILL.md')):
            print(candidate)
            sys.exit(0)
print('')
")"

PYTHON_BIN="${HERMES_HOME}/hermes-agent/venv/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

SPOTIFY_SCRIPT="$SKILL_DIR/scripts/spotify_info.py"
SLSKD_SCRIPT="$SKILL_DIR/scripts/slskd_download.py"
```

## Workflow

When the user invokes `/slskd <spotify_url>`, follow these steps in order:

### Step 1 — Extract track metadata

```bash
TRACK_INFO=$("$PYTHON_BIN" "$SPOTIFY_SCRIPT" "<spotify_url>")
echo "$TRACK_INFO"
```

Parse the JSON output:
- `query` — the search string to use (e.g. `"Queen - Bohemian Rhapsody"`)
- `artist` — artist name (may be empty if extraction failed)
- `title` — track title

If the script fails (exit code 1 or `error` key present), report the error and **still attempt the download using the raw title** extracted manually from the URL if possible, or ask the user to provide the artist and title.

### Step 2 — Search and download

```bash
SLSKD_HOST="<slskd.host>" SLSKD_PORT="<slskd.port>" \
  "$PYTHON_BIN" "$SLSKD_SCRIPT" "<query>"
```

Replace `<slskd.host>` and `<slskd.port>` with the values from the skill config above.

### Step 3 — Report result

Parse the JSON output and reply to the user:

**Success:**
```
Queued: <file> [FLAC / MP3 320] from <user> (<size_mb> MB)
```

**No quality match:**
```
No FLAC or MP3 320+ found for "<query>".
Best available was <best_found> — below quality floor. Not downloading.
```

**Error:**
```
Download failed: <error message>
```

## Example

User: `/slskd https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT`

1. Run `spotify_info.py` → `{"artist": "Queen", "title": "Bohemian Rhapsody", "query": "Queen - Bohemian Rhapsody"}`
2. Run `slskd_download.py "Queen - Bohemian Rhapsody"` → `{"success": true, "file": "Queen - Bohemian Rhapsody.flac", "format": "flac", "user": "somepeer", "size_mb": 42.3}`
3. Reply: "Queued: Queen - Bohemian Rhapsody.flac [FLAC] from somepeer (42.3 MB)"

## Rules

1. **Quality floor is strict** — never download MP3 below 320 kbps or non-FLAC/MP3 formats (no `.ogg`, `.wma`, `.aac`, `.m4a`). The script enforces this but do not override it.
2. **The download is queued, not instant** — always say "queued" not "downloaded" in the success message.
3. **If `SLSKD_API_KEY` is missing** — report: "SLSKD_API_KEY is not set in ~/.hermes/.env. Please add it and try again."
4. **If Spotify metadata extraction fails** — still attempt the download using the title extracted from the URL (e.g. from the page title), or ask the user for the search query.
5. **Do not modify the scripts** — if something is broken, report it so the user can fix it.
