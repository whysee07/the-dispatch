#!/usr/bin/env python3
"""Culture · Photography — photo of the day + a gallery + the day's news photo.

License-safe by design: images come from sources that permit display with
attribution, and every image is credited.
  - Photo of the day + gallery: Wikimedia "Picture of the day" (Commons, CC /
    public-domain, credited). No key.
  - News photo: NASA APOD (public domain), used as a stand-in until a licensed
    news-wire source (Reuters/AP) is wired in — see NOTE below. Optional key
    NASA_API_KEY (defaults to DEMO_KEY).

Everything degrades gracefully: any source that fails is simply omitted and its
section hides on the site.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone, timedelta

import requests

from _common import write_culture

UA = {"User-Agent": "TheKhatTing/1.0 (weekend culture section; github.com/whysee07/the-dispatch)"}
WIKI = "https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{y}/{m:02d}/{d:02d}"


def _strip(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "").strip()


def _potd_for(dt: datetime) -> dict | None:
    """Wikimedia Picture of the day for a given date -> our photo shape."""
    try:
        r = requests.get(WIKI.format(y=dt.year, m=dt.month, d=dt.day), headers=UA, timeout=20)
        r.raise_for_status()
        img = r.json().get("image") or {}
    except Exception as e:  # noqa: BLE001
        print(f"photo: wiki POTD {dt.date()} failed ({e})")
        return None
    if not img:
        return None
    src = (img.get("thumbnail") or {}).get("source") or (img.get("image") or {}).get("source", "")
    artist = _strip((img.get("artist") or {}).get("text", ""))
    lic = (img.get("license") or {}).get("type", "")
    title = (img.get("title") or "").replace("File:", "").rsplit(".", 1)[0].replace("_", " ")
    return {
        "title": title or "Picture of the day",
        "caption": _strip((img.get("description") or {}).get("text", ""))[:220],
        "credit": " · ".join(p for p in [artist, "Wikimedia Commons", lic] if p),
        "image": src,
    }


def _news_photo() -> dict | None:
    # NOTE: APOD is a public-domain stand-in. Swap for a licensed news-wire
    # "pictures of the day" source (Reuters/AP) when one is available.
    try:
        r = requests.get("https://api.nasa.gov/planetary/apod",
                         params={"api_key": os.getenv("NASA_API_KEY", "DEMO_KEY")}, timeout=20)
        r.raise_for_status()
        j = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"photo: APOD failed ({e})")
        return None
    if j.get("media_type") != "image":
        return None
    return {
        "title": j.get("title", "Image of the day"),
        "caption": (j.get("explanation", "")[:220]),
        "credit": f"NASA/APOD · {j.get('copyright', 'Public domain')}",
        "image": j.get("hdurl") or j.get("url", ""),
    }


def main() -> None:
    now = datetime.now(timezone.utc)
    potd = _potd_for(now) or _potd_for(now - timedelta(days=1))

    gallery: list[dict] = []
    for back in range(1, 10):
        if len(gallery) >= 6:
            break
        p = _potd_for(now - timedelta(days=back))
        if p and p.get("image"):
            gallery.append({"caption": p["title"], "image": p["image"]})

    data: dict = {}
    if potd:
        data["potd"] = potd
    news = _news_photo()
    if news:
        data["news_photo"] = news
    if gallery:
        data["gallery"] = gallery

    write_culture("photo", data)


if __name__ == "__main__":
    main()
