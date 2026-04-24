#!/usr/bin/env python3
"""
LinkedIn Feed Patrol — scrapes the feed via Chrome CDP, classifies posts,
and outputs JSON for Hermes to format and send.

Exits 0 with JSON on stdout. Exits 1 with {"error": "..."} on failure.
"""

import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CACHE_FILE = Path.home() / ".hermes" / "cache" / "linkedin_feed_latest.json"
PLAYBOOK_FILE = Path.home() / "Obsidian/max_second_brain/linkedin-brand-playbook.md"

# ---------------------------------------------------------------------------
# Browser scraping
# ---------------------------------------------------------------------------

def _human_delay(lo=0.8, hi=2.2):
    time.sleep(random.uniform(lo, hi))


_EXTRACT_JS = """
    () => {
        const h2s = Array.from(document.querySelectorAll('h2'))
                      .filter(h => h.textContent.trim() === 'Feed post');
        const activityPat = /likes this|commented|follows this|reposts this|shared this/;
        const timePat = /^(\\d+[hdwm]o?)\\s*[•·]?\\s*$/;
        const footerPat = /^(Like|Comment|Repost|Send)$/;

        return h2s.map(h2 => {
            const container = h2.closest('[componentkey]');
            if (!container) return null;

            const text = container.innerText || '';
            const isPromoted = text.includes('\\nPromoted\\n');
            const lines = text.split('\\n').map(l => l.trim()).filter(l => l);

            let headerEndIdx = 1;
            for (let i = 1; i < Math.min(lines.length, 5); i++) {
                if (activityPat.test(lines[i])) { headerEndIdx = i + 1; break; }
            }
            const authorName = lines[headerEndIdx] || '';

            const allLinks = Array.from(container.querySelectorAll('a[href*="/in/"], a[href*="/company/"]'));
            let authorLink = allLinks.find(a => {
                const t = a.innerText.trim().split('\\n')[0].trim();
                return t === authorName || (authorName.length > 5 && t.startsWith(authorName.slice(0, 20)));
            });
            if (!authorLink) {
                const withText = allLinks.filter(a => a.innerText.trim().length > 1);
                authorLink = withText[headerEndIdx > 1 ? 1 : 0];
            }

            const author = authorLink?.innerText?.trim()?.split('\\n')[0]?.trim() || authorName;
            const authorUrl = authorLink?.href || '';

            let contentStart = -1, contentEnd = lines.length;
            for (let i = 0; i < lines.length; i++) {
                if (timePat.test(lines[i])) {
                    contentStart = i + 1;
                    while (contentStart < lines.length &&
                           ['Follow', 'Following', 'Pending'].includes(lines[contentStart])) {
                        contentStart++;
                    }
                    break;
                }
            }
            if (contentStart === -1) return null;

            for (let i = contentStart; i < lines.length; i++) {
                if (footerPat.test(lines[i])) {
                    contentEnd = i;
                    while (contentEnd > contentStart &&
                           /reacted|reaction|comment|repost|^\\d/.test(lines[contentEnd-1])) {
                        contentEnd--;
                    }
                    break;
                }
            }

            const content = lines.slice(contentStart, contentEnd)
                .filter(l => l !== '… more' && l !== '…more')
                .join('\\n').trim();

            const likesMatch = text.match(/([\\d,]+)\\s+(?:reaction|like)/i);
            const commentsMatch = text.match(/(\\d+)\\s+comment/i);

            return {
                author: author.slice(0, 100),
                author_url: authorUrl,
                content,
                is_promoted: isPromoted,
                likes: likesMatch ? parseInt(likesMatch[1].replace(/,/g, '')) : 0,
                comments: commentsMatch ? parseInt(commentsMatch[1]) : 0,
            };
        }).filter(p => p && p.content && p.content.length > 30 && !p.is_promoted);
    }
"""


def scrape_linkedin_feed() -> list[dict] | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _err("playwright not installed — run: pip install playwright")
        return None

    try:
        import requests
        requests.get("http://localhost:9222/json/version", timeout=3).raise_for_status()
    except Exception as e:
        _err(f"Chrome CDP not reachable: {e}")
        return None

    posts: list[dict] = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
        except Exception as e:
            _err(f"Could not connect to Chrome: {e}")
            return None

        ctx = browser.contexts[0] if browser.contexts else browser.new_context()

        feed_page = None
        for pg in ctx.pages:
            if "linkedin.com/feed" in pg.url:
                feed_page = pg
                break

        try:
            if feed_page:
                feed_page.bring_to_front()
                _human_delay(0.5, 1)
            else:
                feed_page = ctx.new_page()
                feed_page.goto("https://www.linkedin.com/feed/",
                               wait_until="domcontentloaded", timeout=30000)
                _human_delay(8, 11)

            if "login" in feed_page.url or "authwall" in feed_page.url:
                _err("Not logged in to LinkedIn — cookies may have expired")
                return None

            for _ in range(3):
                feed_page.keyboard.press("End")
                _human_delay(1.5, 2.5)

            raw = feed_page.evaluate(_EXTRACT_JS)

            for i, item in enumerate(raw[:15]):
                posts.append({
                    "id": f"post_{i+1}",
                    "author": item.get("author", "Unknown"),
                    "author_url": item.get("author_url", ""),
                    "content": item.get("content", ""),
                    "url": "",
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                    "likes": item.get("likes", 0),
                    "comments": item.get("comments", 0),
                    "shares": 0,
                })

        except Exception as e:
            _err(f"Scraping error: {e}")
            return None

    return posts if posts else None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

PILLARS = {
    'institutional_crypto': {
        'name': 'Market Structure & Institutional Crypto', 'emoji': '🏛️',
        'keywords': ['institutional', 'infrastructure', 'settlement', 'custody', 'finality',
                     'regulatory', 'corridors', 'clearance', 'custodial'],
    },
    'trading_ops': {
        'name': 'Trading Operations at Scale', 'emoji': '📊',
        'keywords': ['execution', 'trading', 'desk', 'systematic', 'monitoring', 'liquidity',
                     'microstructure', 'market making', 'latency', 'flow'],
    },
    'ai_native_work': {
        'name': 'AI-Native Operations', 'emoji': '🤖',
        'keywords': ['ai', 'token economy', 'spec', 'automation', 'cognitive', 'blast radius',
                     'code', 'operational', 'llm', 'agent', 'claude', 'gpt'],
    },
    'cross_domain': {
        'name': 'Cross-Domain Insights', 'emoji': '💡',
        'keywords': ['cognition', 'decision', 'risk', 'process', 'mindset', 'framework',
                     'mental model', 'first principles'],
    },
}


def classify_posts(posts: list) -> list:
    for post in posts:
        content_lower = post['content'].lower()
        best_pillar, best_score = None, 0
        for key, info in PILLARS.items():
            score = sum(1 for kw in info['keywords'] if kw in content_lower)
            if score > best_score:
                best_score, best_pillar = score, key
        post['pillar_classification'] = best_pillar or 'uncategorized'
        post['pillar_confidence'] = min(0.95, 0.5 + best_score * 0.15) if best_pillar else 0.3
        post['pillar_name'] = PILLARS[best_pillar]['name'] if best_pillar else 'Uncategorized'
        post['pillar_emoji'] = PILLARS[best_pillar]['emoji'] if best_pillar else '❓'
    return posts


# ---------------------------------------------------------------------------
# Comment generation
# ---------------------------------------------------------------------------

def generate_comments(posts: list) -> list:
    api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("BEDROCK_API_KEY")
    if api_key:
        try:
            return _generate_comments_llm(posts)
        except Exception as e:
            print(f"LLM comment generation failed: {e} — using heuristics", file=sys.stderr)
    for post in posts:
        post['comment_suggestion'] = (
            f"Interesting point on {post['content'].split('.')[0][:60]}. "
            f"What's your read on the operational implications at scale?"
        )
    return posts


def _generate_comments_llm(posts: list) -> list:
    import anthropic
    playbook = PLAYBOOK_FILE.read_text()[:2000] if PLAYBOOK_FILE.exists() else ""
    client = anthropic.Anthropic()
    for post in posts:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=(
                "You are Maxime Fages — direct, precise, opinion-first voice. "
                "Write a 2-3 sentence LinkedIn comment that adds value, asks a sharp question, "
                "or offers a concrete counterpoint. No fluff, no compliments.\n\n"
                f"Brand playbook excerpt:\n{playbook}"
            ),
            messages=[{"role": "user", "content": f"Post by {post['author']}:\n\n{post['content']}"}],
        )
        post['comment_suggestion'] = msg.content[0].text.strip()
    return posts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _err(msg: str):
    print(msg, file=sys.stderr)


def save_cache(posts: list, demo_mode: bool):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(
        {"timestamp": datetime.now().isoformat(), "demo_mode": demo_mode,
         "posts_count": len(posts), "posts": posts},
        indent=2
    ))


# ---------------------------------------------------------------------------
# Main — outputs JSON to stdout
# ---------------------------------------------------------------------------

def main():
    posts = scrape_linkedin_feed()
    demo_mode = posts is None
    if demo_mode:
        posts = [
            {"id": "post_1", "author": "Alice Chen", "author_url": "https://linkedin.com/in/alice-chen",
             "content": "The real bottleneck in institutional crypto settlement isn't speed—it's finality.",
             "url": "", "timestamp": datetime.now(tz=timezone.utc).isoformat(),
             "likes": 142, "comments": 23, "shares": 8},
            {"id": "post_2", "author": "Bob Trading", "author_url": "https://linkedin.com/in/bob-trading",
             "content": "Execution latency under volatility: order-level monitoring costs more than the latency it prevents.",
             "url": "", "timestamp": datetime.now(tz=timezone.utc).isoformat(),
             "likes": 87, "comments": 14, "shares": 5},
        ]

    posts = classify_posts(posts)
    posts = generate_comments(posts)
    save_cache(posts, demo_mode)

    print(json.dumps({
        "demo_mode": demo_mode,
        "posts_count": len(posts),
        "posts": posts,
    }))


if __name__ == "__main__":
    main()
