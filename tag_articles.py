"""
Tagging pipeline — takes the day's combined articles (output of ingest.py)
and enriches each with:
  - a 2-3 line original-wording summary (scraped from the full article,
    not just the headline, where scraping succeeds)
  - primary_specialization + secondary_specializations, from:
    Finance, Strategy, Information Management, HR, Marketing, Operations
    (skipped for RSS-sourced articles, which already know their beat —
    saves a Gemini call for those)
  - companies_mentioned: names only for now (sentiment/ticker extraction
    is reserved for the later stock-analyzer phase, per the project plan)

Uses gemini-3.6-flash — confirmed free of charge on the standard
tier per ai.google.dev/gemini-api/docs/pricing. (Earlier attempts:
gemini-2.5-flash-lite 404'd — not available on this account;
gemini-2.5-flash 404'd with an explicit "no longer available to
new users, use gemini-3.6-flash" message from the API itself.)

Run this AFTER ingest.py has produced data/<date>.json.

Install deps:
    pip install requests trafilatura --break-system-packages

Set your free Gemini API key (from aistudio.google.com/apikey) as an
environment variable before running:
    GEMINI_API_KEY = your key
"""

import os
import sys
import json
import time
import datetime
from pathlib import Path

import requests
import trafilatura

# Windows' console defaults to cp1252, which can't print ₹ and other
# non-ASCII characters found in real headlines — force UTF-8 output so
# print() doesn't crash mid-run (same root cause as the earlier
# ingest.py file-writing crash, different code path).
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

SPECIALIZATIONS = ["Finance", "Strategy", "Information Management", "HR", "Marketing", "Operations"]

SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


def scrape_article_text(url: str, max_chars: int = 4000, timeout: int = 10) -> str | None:
    """Best-effort full-text scrape. Returns None on failure (paywall,
    block, network error, or timeout) — caller falls back to
    headline-only tagging.

    Uses requests (with an explicit timeout) to fetch the raw HTML, then
    hands it to trafilatura for extraction — trafilatura.fetch_url() has
    no timeout of its own and can hang indefinitely on a slow or
    unresponsive site, which is what happened on the first real run."""
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=timeout)
        resp.raise_for_status()
        text = trafilatura.extract(resp.text)
        if not text:
            return None
        return text[:max_chars]
    except Exception:
        return None


def call_gemini(prompt: str, api_key: str, retries: int = 2) -> dict | None:
    """Calls Gemini with a prompt that demands JSON-only output, and
    parses the response. Returns None on failure after retries."""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                GEMINI_API_URL,
                params={"key": api_key},
                json=payload,
                timeout=30,
            )
            if resp.status_code == 429:
                # rate limited — brief backoff and retry
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            text_out = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text_out)
        except Exception as e:
            if attempt == retries:
                print(f"    [warn] Gemini call failed after retries: {e}")
                return None
            time.sleep(1)
    return None


def build_prompt_full_classification(headline: str, article_text: str | None) -> str:
    """For library-digest articles — full pipeline: summary +
    specialization tags + company mentions."""
    content = article_text or headline
    return f"""You are tagging a business news article for an MBA cohort news app.

Article headline: {headline}
Article content: {content}

Return ONLY a JSON object with this exact shape, no other text:
{{
  "summary": "2-3 sentence neutral summary in your own words, suitable for a quick-read news card",
  "primary_specialization": "one of: {', '.join(SPECIALIZATIONS)}",
  "secondary_specializations": ["0 to 2 additional relevant specializations from the same list, or empty array"],
  "companies_mentioned": ["list of company names mentioned, empty array if none"]
}}"""


def build_prompt_summary_only(headline: str, article_text: str | None) -> str:
    """For RSS-sourced articles — specialization is already known from
    the source, so just get the summary and company mentions."""
    content = article_text or headline
    return f"""You are summarizing a business news article for an MBA cohort news app.

Article headline: {headline}
Article content: {content}

Return ONLY a JSON object with this exact shape, no other text:
{{
  "summary": "2-3 sentence neutral summary in your own words, suitable for a quick-read news card",
  "companies_mentioned": ["list of company names mentioned, empty array if none"]
}}"""


def tag_article(article: dict, api_key: str) -> dict:
    """Enriches one article in place (returns a new dict) with summary,
    specialization tags, and company mentions."""
    article_text = scrape_article_text(article["url"])

    needs_classification = article.get("specialization_hint") is None

    if needs_classification:
        prompt = build_prompt_full_classification(article["headline"], article_text)
    else:
        prompt = build_prompt_summary_only(article["headline"], article_text)

    result = call_gemini(prompt, api_key)

    enriched = dict(article)
    if result:
        enriched["summary"] = result.get("summary", article["headline"])
        enriched["companies_mentioned"] = result.get("companies_mentioned", [])
        if needs_classification:
            enriched["primary_specialization"] = result.get("primary_specialization")
            enriched["secondary_specializations"] = result.get("secondary_specializations", [])
        else:
            enriched["primary_specialization"] = article["specialization_hint"]
            enriched["secondary_specializations"] = []
    else:
        # Gemini call failed entirely — fall back to headline as summary,
        # keep whatever specialization info we already had
        enriched["summary"] = article["headline"]
        enriched["companies_mentioned"] = []
        enriched["primary_specialization"] = article.get("specialization_hint")
        enriched["secondary_specializations"] = []

    return enriched


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: set GEMINI_API_KEY environment variable first.")
        return

    today_str = datetime.date.today().isoformat()
    input_path = Path(f"data/{today_str}.json")
    if not input_path.exists():
        print(f"ERROR: {input_path} not found — run ingest.py first.")
        return

    articles = json.loads(input_path.read_text(encoding="utf-8"))
    print(f"Tagging {len(articles)} articles...")

    tagged = []
    for i, article in enumerate(articles, 1):
        print(f"  [{i}/{len(articles)}] {article['headline'][:60]}...")
        tagged.append(tag_article(article, api_key))
        time.sleep(1)  # gentle pacing, well under free-tier rate limits

    out_dir = Path("data/tagged")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today_str}.json"
    out_path.write_text(json.dumps(tagged, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nDone. {len(tagged)} tagged articles written to {out_path}")


if __name__ == "__main__":
    main()
