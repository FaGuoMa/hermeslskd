---
name: linkedin
description: Patrol your LinkedIn feed — scrapes real posts from your logged-in Chrome session, classifies them against your content pillars, and suggests comments in your voice
version: 1.0.0
author: max
platforms: [linux, telegram]
metadata:
  hermes:
    tags: [linkedin, feed, social, content]
---

# LinkedIn Feed Patrol

Scrape your LinkedIn feed from the running Chrome session, classify posts against your content pillars, and generate on-brand comment suggestions.

## Setup

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

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
    for candidate in [os.path.join(base, 'skills', 'linkedin'), os.path.join(base, 'linkedin')]:
        if os.path.isfile(os.path.join(candidate, 'SKILL.md')):
            print(candidate); sys.exit(0)
print('')
")"

PYTHON_BIN="$HERMES_HOME/hermes-agent/venv/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

PATROL_SCRIPT="$SKILL_DIR/scripts/patrol.py"
```

**Prerequisites:**
- Chrome running with CDP at `localhost:9222` (via `chrome-hermes.service`)
- Logged in to LinkedIn in that Chrome profile
- `playwright` installed: `pip install playwright` (already done)

## Workflow

When the user invokes `/linkedin`:

### Step 1 — Run the patrol

```bash
"$PYTHON_BIN" "$PATROL_SCRIPT"
```

The script outputs JSON to stdout:

```json
{
  "demo_mode": false,
  "posts_count": 7,
  "posts": [
    {
      "id": "post_1",
      "author": "Julien Chaumond",
      "author_url": "https://www.linkedin.com/in/julienchaumond/",
      "content": "Qwen3.6 27B running inside of Pi coding agent...",
      "timestamp": "2026-04-24T13:00:00+00:00",
      "likes": 325,
      "comments": 22,
      "pillar_classification": "ai_native_work",
      "pillar_name": "AI-Native Operations",
      "pillar_emoji": "🤖",
      "pillar_confidence": 0.8,
      "comment_suggestion": "The inference efficiency gap is closing faster than anyone expected..."
    }
  ]
}
```

If `demo_mode` is true, Chrome wasn't reachable — note this clearly.

### Step 2 — Format and send the digest

Format each post as:

```
{pillar_emoji} **[N] {pillar_name}** ({confidence}%)
_{author}_
_{content preview, ~120 chars}_
💬 _{comment_suggestion}_
```

Group posts by pillar. Lead with:
```
📊 **LinkedIn Feed Patrol** — {date}
Found {N} posts:
```

End with:
```
_Saved: ~/.hermes/cache/linkedin_feed_latest.json_
```

If `demo_mode` is true, add _(demo — Chrome not reachable)_ after the title.

## Rules

1. **Never open a new browser window** — the script reuses the existing Chrome session.
2. **If posts_count is 0** — say "Feed loaded but no posts matched the extraction pattern. LinkedIn may have updated their DOM."
3. **Comment suggestions are drafts** — tell the user they can edit before posting.
4. **Classification is keyword-based** — low-confidence posts (<60%) may be miscategorised; mention this if all posts have the same pillar.

## Example

User: `/linkedin`

```bash
"$PYTHON_BIN" "$PATROL_SCRIPT"
# → JSON with 4–15 posts
```

Reply:
```
📊 **LinkedIn Feed Patrol** — 2026-04-24

Found 4 posts:

🤖 **[1] AI-Native Operations** (80%)
_Julien Chaumond_
_Qwen3.6 27B running inside of Pi coding agent via Llama.cpp on the MacBook Pro…_
💬 _The inference efficiency gap is closing faster than the incumbents want to admit. What's the actual blocker — energy, memory bandwidth, or just the UX tooling?_

...

_Saved: ~/.hermes/cache/linkedin_feed_latest.json_
```
