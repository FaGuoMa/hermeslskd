# hermeslskd

Hermes skill for downloading music from Soulseek. Give it a Spotify link, it finds the best available FLAC or high-quality MP3 and queues it in your [slskd](https://github.com/slskd/slskd) instance.

## How it works

The skill runs inside [Hermes](https://github.com/lucidhq/hermes), a local AI agent framework. When you invoke `/slskd <spotify_url>`, Hermes:

1. Resolves track metadata (title, artist) from the Spotify URL
2. Searches Soulseek via the slskd API
3. Picks the best quality match and enqueues it for download

The search strategy is designed around Soulseek's bare-bones search engine:

- **Title-only query** — shorter queries reach more peers than "Artist - Title"
- **Artist as soft validator** — results with the artist in the file path are ranked higher, but not excluded
- **Two attempts max** — if the title search finds nothing, retries with "artist title"; never spams
- **Quality floor** — only FLAC or MP3 ≥ 320 kbps are accepted; everything else is silently rejected
- **Peer reputation** — users you've downloaded from before are preferred as tiebreakers

## Setup

### Prerequisites

- [Hermes](https://github.com/lucidhq/hermes) installed and running
- [slskd](https://github.com/slskd/slskd) running and accessible on your network
- Python dependencies in the Hermes venv:

```bash
uv pip install slskd-api spotipy \
  --python ~/.hermes/hermes-agent/venv/bin/python3
```

### Configuration

**1. Register this repo as a Hermes external skill directory** in `~/.hermes/config.yaml`:

```yaml
skills:
  external_dirs:
    - /path/to/hermeslskd
```

**2. Add credentials** to `~/.hermes/.env`:

```env
SLSKD_API_KEY=your_slskd_api_key
SPOTIFY_CLIENT_ID=your_spotify_client_id
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
```

**3. Set your slskd host** — the skill defaults to `192.168.1.110:5030`. Override in the Hermes skill config or via environment variables `SLSKD_HOST` / `SLSKD_PORT`.

## Usage

### Download a track

```
/slskd https://open.spotify.com/track/4u7EnebtmKWzUH433cf5Qv
```

Hermes resolves the metadata, searches, and replies:

```
Queued: Bohemian Rhapsody.flac [FLAC] from somepeer (42.3 MB)
```

### Retry failed downloads

```
/slskd retry
```

Finds all `Errored`, `TimedOut`, and `Rejected` transfers in your slskd queue, runs a fresh search for each, enqueues the best match, and removes the failed entry on success.

Use `--dry-run` to preview without downloading:

```
/slskd retry --dry-run
```

## Files

```
slskd/
├── SKILL.md                  # Hermes skill definition and instructions
└── scripts/
    ├── spotify_info.py       # Resolves Spotify URL → title, artist, duration
    ├── slskd_download.py     # Searches slskd and enqueues the best match
    ├── slskd_retry.py        # Retries all failed transfers
    └── peers.py              # Tracks download history per Soulseek user
```

## Peer tracking

Every successful download is recorded in `~/.hermes/slskd_peers.json`. Known peers get a slight ranking boost as a tiebreaker — if two results are otherwise equal, the one from a user you've downloaded from before wins.
