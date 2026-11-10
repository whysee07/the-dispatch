#!/usr/bin/env python3
"""Culture · Music — industry news + new-track recs + charts, by genre.

Weekly (Friday). Pulls music-press RSS, then has Gemini curate it into the
structured shape the site renders. World ~60% / India ~40%. No audio player —
that lives on the future "Author Recommends" site, not here.

Env: GEMINI_API_KEY (required for curation). CULTURE_MODEL overrides the model
(default gemini-2.5-flash — cheap; music is lower-stakes than the AI feed).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

import feedparser
from dotenv import load_dotenv

from _common import write_culture, extract_json

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("CULTURE_MODEL", "gemini-2.5-flash")

# ~60% world / 40% India sourcing.
FEEDS = {
    "world": [
        ("Pitchfork",      "https://pitchfork.com/rss/news/"),
        ("Stereogum",      "https://www.stereogum.com/feed/"),
        ("NME",            "https://www.nme.com/news/music/feed"),
        ("Rolling Stone",  "https://www.rollingstone.com/music/music-news/feed/"),
        ("Billboard",      "https://www.billboard.com/feed/"),
    ],
    "india": [
        ("Rolling Stone India", "https://rollingstoneindia.com/feed/"),
        ("Indian music news",   "https://news.google.com/rss/search?q=indian+indie+music+OR+bollywood+song+release+OR+desi+hip+hop&hl=en-IN&gl=IN&ceid=IN:en"),
    ],
}
LOOKBACK_HOURS = 8 * 24          # weekly section, so look back a little over a week
GENRES = ["Rock", "Indie", "Pop", "House", "Indie Indian", "Bollywood", "Desi hip-hop"]


def fetch_headlines() -> list[dict]:
    items: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    for geo, feeds in FEEDS.items():
        for name, url in feeds:
            try:
                parsed = feedparser.parse(url)
            except Exception as e:  # noqa: BLE001
                print(f"music: feed error {name}: {e}")
                continue
            for e in parsed.entries[:12]:
                pub = e.get("published_parsed") or e.get("updated_parsed")
                if pub:
                    dt = datetime(*pub[:6], tzinfo=timezone.utc)
                    if dt < cutoff:
                        continue
                items.append({
                    "title": e.get("title", "").strip(),
                    "source": name, "url": e.get("link", ""), "geo": geo,
                })
    return items


PROMPT = """You are the music editor of a sharp weekend culture section. Aim for roughly 60% world / 40% India.
From the headlines below, return STRICT JSON only (no prose, no code fences) with these keys:
- "news": exactly 3 objects {{"title","source","url","age"}} — the 3 most notable music-industry stories. Reuse the given source and url verbatim; set "age" like "2d".
- "tracks": 6 objects {{"title","artist","why","genre","geo"}} — fresh, real new-ish tracks worth hearing across these genres: {genres}. "why" is one punchy sentence. "geo" is "world" or "india". Skew ~60/40 world/India and spread the genres.
- "charts": {{"global": 5 objects {{"title","artist"}}, "india": 5 objects {{"title","artist"}}}} — current popular tracks.
- "genres": {genres}
Only output real artists/tracks you are confident exist. Headlines:
{head}
Return only the JSON object."""


def main() -> None:
    items = fetch_headlines()
    head = "\n".join(
        f"- [{i['geo']}] {i['title']} ({i['source']}) {i['url']}" for i in items[:80]
    ) or "(no headlines fetched — use your own knowledge of recent music)"

    data: dict = {"genres": GENRES}
    if GEMINI_API_KEY:
        try:
            from google import genai
            client = genai.Client(api_key=GEMINI_API_KEY, http_options={"api_version": "v1beta"})
            resp = client.models.generate_content(
                model=MODEL,
                contents=PROMPT.format(genres=GENRES, head=head),
                config={"response_mime_type": "application/json", "temperature": 0.7},
            )
            raw = (resp.text or "").strip()
            parsed = extract_json(raw)
            if parsed:
                data.update(parsed)
                data["genres"] = parsed.get("genres") or GENRES
            else:
                print(f"music: unparseable model output (len={len(raw)}): {raw[:200]!r}")
        except Exception as e:  # noqa: BLE001
            print(f"music: gemini failed ({e}); writing fallback")
    else:
        print("music: no GEMINI_API_KEY; writing genres-only fallback")

    write_culture("music", data)


if __name__ == "__main__":
    main()
