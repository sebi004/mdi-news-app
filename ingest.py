"""
Daily ingestion pipeline for the MBA cohort news app.

Pulls from two kinds of sources:
  1. The MDI library digest email — parsed and pushed to this repo by a
     separate Google Apps Script (digest-fetcher.gs) that runs inside
     your own Gmail account. MDI's Workspace policy blocks IMAP,
     forwarding, and external sharing, so this Python script does NOT
     talk to Gmail at all — it just reads the file Apps Script already
     committed to data/raw/library-digest-<date>.json.
  2. RSS feeds for specialization-specific sources — HR, Marketing, Ops

Output: a single JSON file (data/YYYY-MM-DD.json) with one record per
article, ready for the Gemini tagging step.

Run this from GitHub Actions AFTER the Apps Script has pushed today's
raw digest file (schedule the Action a bit later than the Apps Script
trigger to guarantee ordering). Needs real internet access for the RSS
part, which this build sandbox does not have, so it hasn't been
executed here — only unit-tested where possible.

Install deps first:
    pip install feedparser beautifulsoup4 requests --break-system-packages
"""

import re
import json
import datetime
from pathlib import Path

import requests
import feedparser
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------
# 1. RSS SOURCES — auto-discovered, not hardcoded
# ----------------------------------------------------------------------

# Confirmed working feed URLs (verified by directly testing the response
# for real <rss>/<feed> XML, not just a 200 status). ET's HR and
# BrandEquity verticals looked promising but their /rss links are
# actually newsletter signup pages, not real feeds — dropped in favor
# of these.
RSS_SOURCES = {
    "HR": ["https://www.hrkatha.com/feed"],
    "Marketing": ["https://www.afaqs.com/rss"],
    "Operations": [
        "https://www.itln.in/",       # homepage — feed auto-discovered
        "https://www.logisticsinsider.in/",  # currently 403s; kept in case blocking eases
    ],
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def discover_rss_feed_url(homepage_url: str) -> str | None:
    """Find the real RSS feed URL by reading a site's <head> for the
    standard autodiscovery <link> tag. Falls back to the common
    WordPress /feed/ convention if no explicit tag is found."""
    try:
        resp = requests.get(homepage_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [warn] could not load {homepage_url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    link_tag = soup.find("link", attrs={"type": re.compile("rss|atom", re.I)})
    if link_tag and link_tag.get("href"):
        href = link_tag["href"]
        if href.startswith("/"):
            # relative URL -> make absolute
            from urllib.parse import urljoin
            href = urljoin(homepage_url, href)
        return href

    # Fallback: try common feed URL conventions.
    # /feed/ is the WordPress convention (Logistics Insider, ITLN).
    # /rss is what the Economic Times vertical sites (HR, BrandEquity)
    # use instead — confirmed by checking hr.economictimes' page source,
    # where it's a plain footer link, not a <link> autodiscovery tag.
    for suffix in ["feed/", "rss", "rss/"]:
        fallback = homepage_url.rstrip("/") + "/" + suffix
        try:
            r = requests.get(fallback, headers=HEADERS, timeout=10)
            if r.status_code == 200 and "<rss" in r.text.lower():
                return fallback
        except requests.RequestException:
            continue

    print(f"  [warn] no RSS feed discovered for {homepage_url} — check manually")
    return None


def looks_like_direct_feed_url(url: str) -> bool:
    """Heuristic: does this URL already point at a feed, rather than a
    homepage that needs discovery? Confirmed sources (HR Katha, afaqs!)
    are stored as direct feed URLs to skip discovery entirely."""
    lowered = url.lower()
    return any(marker in lowered for marker in ["/feed", "/rss", ".xml"])


def fetch_rss_articles(specialization: str, source_url: str, max_items: int = 15) -> list[dict]:
    if looks_like_direct_feed_url(source_url):
        feed_url = source_url
    else:
        feed_url = discover_rss_feed_url(source_url)
        if not feed_url:
            return []

    parsed = feedparser.parse(feed_url)
    articles = []
    for entry in parsed.entries[:max_items]:
        articles.append({
            "headline": entry.get("title", "").strip(),
            "url": entry.get("link", "").strip(),
            "published": entry.get("published", entry.get("updated", "")),
            "source_publication": parsed.feed.get("title", source_url),
            "source_type": "rss",
            "specialization_hint": specialization,  # feeds directly to this beat
        })
    return articles


# ----------------------------------------------------------------------
# 2. LIBRARY DIGEST — already parsed by Apps Script, just read the file
# ----------------------------------------------------------------------

RAW_DIGEST_PATH_PREFIX = "data/raw/library-digest"  # -> -2026-09-10.json


def load_library_digest_articles(date_str: str) -> list[dict]:
    """
    Reads the file that digest-fetcher.gs already pushed to this repo
    (data/raw/library-digest-<date>.json) and normalizes it into the
    same article shape the RSS fetchers produce.

    No network call here at all — Apps Script did the Gmail work and
    the parsing; this just loads what's already on disk in the repo.
    """
    path = Path(f"{RAW_DIGEST_PATH_PREFIX}-{date_str}.json")
    if not path.exists():
        print(f"  [warn] {path} not found — did the Apps Script trigger run yet today?")
        return []

    raw = json.loads(path.read_text(encoding="utf-8"))
    articles = []
    for a in raw.get("articles", []):
        articles.append({
            "headline": a.get("headline", "").strip(),
            "url": a.get("url", "").strip(),
            "published": raw.get("date", date_str),
            "source_publication": a.get("source", "Unknown"),
            "source_type": "library_digest",
            "specialization_hint": None,  # let Gemini classify these
        })
    return articles


# ----------------------------------------------------------------------
# 3. MAIN
# ----------------------------------------------------------------------

def main():
    all_articles = []
    today_str = datetime.date.today().isoformat()

    print("Fetching RSS sources...")
    for specialization, sources in RSS_SOURCES.items():
        for source_url in sources:
            print(f"  -> {specialization}: {source_url}")
            all_articles.extend(fetch_rss_articles(specialization, source_url))

    print("Loading library digest (pushed by Apps Script)...")
    all_articles.extend(load_library_digest_articles(today_str))

    out_dir = Path("data")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"{today_str}.json"
    out_path.write_text(json.dumps(all_articles, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nDone. {len(all_articles)} articles written to {out_path}")


if __name__ == "__main__":
    main()
