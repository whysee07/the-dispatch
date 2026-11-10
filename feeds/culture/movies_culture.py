#!/usr/bin/env python3
"""Culture · Movies — what to watch tonight + releases & reviews + critic's pick.

Backbone is TMDB (free API). Gemini adds the one-line "why"/take/blurb with
personality. No theatrical showtimes (parked). World ~60% / India ~40%.

Env:
  TMDB_API_KEY   required for real data (https://www.themoviedb.org/settings/api).
                 Without it, writes a minimal file (services only) and the page
                 shows "being compiled".
  GEMINI_API_KEY optional — used only to write the blurbs. Falls back to the
                 TMDB overview text when absent.
  CULTURE_MODEL  model override (default gemini-2.5-flash).
"""
from __future__ import annotations

import os

import requests
from dotenv import load_dotenv

from _common import write_culture, extract_json

load_dotenv()
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("CULTURE_MODEL", "gemini-2.5-flash")

BASE = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"
SERVICES = ["Netflix", "Prime Video", "Max", "Apple TV+", "JioHotstar", "MUBI"]


def tmdb(path: str, **params) -> dict:
    params["api_key"] = TMDB_API_KEY
    r = requests.get(BASE + path, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _geo(movie: dict) -> str:
    return "india" if movie.get("original_language") == "hi" else "world"


def _providers(movie_id: int, region: str = "US") -> list[str]:
    try:
        res = tmdb(f"/movie/{movie_id}/watch/providers").get("results", {})
        flat = res.get(region, {}).get("flatrate", []) or res.get("IN", {}).get("flatrate", [])
        return [p["provider_name"] for p in flat][:3]
    except Exception:  # noqa: BLE001
        return []


def _meta(movie: dict, detail: dict) -> str:
    year = (movie.get("release_date") or "")[:4]
    genre = (detail.get("genres") or [{}])[0].get("name", "")
    runtime = detail.get("runtime") or 0
    rt = f"{runtime // 60}h {runtime % 60}m" if runtime else ""
    star = f"★ {movie.get('vote_average', 0):.1f}" if movie.get("vote_average") else ""
    return " · ".join(p for p in [year, genre, rt, star] if p)


def _blurbs(payload: str) -> dict:
    """One Gemini call to write personality copy; returns {} on any failure."""
    if not GEMINI_API_KEY:
        return {}
    prompt = (
        "You write a sharp weekend movie column. For the JSON of films below, return STRICT JSON only "
        '(no code fences) {"tonight_why": "...", "takes": {"<title>": "<one-line review>", ...}, '
        '"critic_blurb": "..."} — "tonight_why" is 1-2 vivid sentences on the tonight pick; each take is one '
        "witty sentence; critic_blurb is 1-2 sentences making the case for the critic pick. Films:\n" + payload
    )
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=MODEL, contents=prompt,
            config={"response_mime_type": "application/json", "temperature": 0.7},
        )
        return extract_json(resp.text or "")
    except Exception as e:  # noqa: BLE001
        print(f"movies: gemini failed ({e})")
        return {}


def main() -> None:
    if not TMDB_API_KEY:
        print("movies: no TMDB_API_KEY; writing services-only fallback")
        write_culture("movies", {"services": SERVICES})
        return

    try:
        trending = tmdb("/trending/movie/week").get("results", [])[:8]
    except Exception as e:  # noqa: BLE001
        print(f"movies: TMDB failed ({e}); writing fallback")
        write_culture("movies", {"services": SERVICES})
        return

    details = {m["id"]: tmdb(f"/movie/{m['id']}") for m in trending[:6]}
    ranked = sorted(trending, key=lambda m: m.get("vote_average", 0), reverse=True)

    tonight = trending[0]
    critic = ranked[0]
    releases = [m for m in trending if m["id"] != tonight["id"]][:3]

    # personality copy
    payload_lines = [f"- {m['title']}: {m.get('overview','')[:240]}" for m in trending[:6]]
    copy = _blurbs("\n".join(payload_lines))
    takes = copy.get("takes", {}) if isinstance(copy.get("takes"), dict) else {}

    def take_for(m):
        return takes.get(m["title"]) or (m.get("overview", "")[:160].rsplit(" ", 1)[0] + "…")

    data = {
        "tonight": {
            "title": tonight["title"],
            "meta": _meta(tonight, details.get(tonight["id"], {})),
            "why": copy.get("tonight_why") or (tonight.get("overview", "")[:220]),
            "where": _providers(tonight["id"]),
            "geo": _geo(tonight),
            "poster": (IMG + tonight["poster_path"]) if tonight.get("poster_path") else "",
        },
        "releases": [
            {"title": m["title"], "take": take_for(m),
             "score": round(m.get("vote_average", 0) * 10), "geo": _geo(m)}
            for m in releases
        ],
        "critic": {
            "title": critic["title"],
            "blurb": copy.get("critic_blurb") or (critic.get("overview", "")[:220]),
            "where": "",
        },
        "services": SERVICES,
    }
    write_culture("movies", data)


if __name__ == "__main__":
    main()
