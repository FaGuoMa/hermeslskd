"""
Lightweight peer reputation tracker.

Stores download history per Soulseek username in ~/.hermes/slskd_peers.json:
  {
    "username": [
      {"file": "...", "format": "flac", "size_mb": 42.3, "ts": "2026-04-18T17:00:00"}
    ]
  }

Usage:
  from peers import record_download, is_known_peer, download_count
"""
import json
import logging
import os
from datetime import datetime, timezone

PEERS_FILE = os.path.expanduser("~/.hermes/slskd_peers.json")
log = logging.getLogger("slskd.peers")


def _load() -> dict:
    try:
        with open(PEERS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("Could not read peers file: %s", e)
        return {}


def _save(data: dict) -> None:
    try:
        with open(PEERS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning("Could not write peers file: %s", e)


def record_download(username: str, file: str, fmt: str, size_mb: float | None) -> None:
    data = _load()
    entry = {
        "file": file,
        "format": fmt,
        "size_mb": size_mb,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
    }
    data.setdefault(username, []).append(entry)
    _save(data)
    log.debug("recorded download from %s (total: %d)", username, len(data[username]))


def download_count(username: str) -> int:
    """Return how many times we've successfully downloaded from this user."""
    return len(_load().get(username, []))


def is_known_peer(username: str) -> bool:
    return download_count(username) > 0
