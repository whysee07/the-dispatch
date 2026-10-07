"""
AI INSIDER DAILY — ai_digest.py
================================
Pulls AI news from RSS feeds + GitHub + HN, generates a polished digest via
Google Gemini, renders it as an HTML email, and sends it via Gmail SMTP.

SETUP
-----
1. Copy .env.example to .env and fill in your credentials:
      GEMINI_API_KEY      — Google AI Studio key (aistudio.google.com)
      GMAIL_ADDRESS       — sender Gmail address
      GMAIL_APP_PASSWORD  — 16-char Gmail App Password (not your login password)
      RECIPIENT_EMAIL     — where to deliver the digest

2. Install dependencies:
      pip install -r requirements.txt

3. Run:
      python ai_digest.py
"""

# ── Standard library ──────────────────────────────────────────────────────────
import os
import json
import time
import logging
import smtplib
import textwrap
from pathlib import Path
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── Third-party ───────────────────────────────────────────────────────────────
import feedparser
import requests
from dotenv import load_dotenv
import google.generativeai as genai

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()

GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY")
GMAIL_ADDRESS     = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL   = os.getenv("RECIPIENT_EMAIL")

GEMINI_MODEL      = "gemini-2.5-pro"           # fall back to gemini-2.5-flash if quota hit
MAX_CONTENT_CHARS = 80_000                     # cap sent to Gemini
LOOKBACK_HOURS    = 24
REQUEST_TIMEOUT   = 15                         # seconds per HTTP/feed request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# RSS FEED CATALOG
# ─────────────────────────────────────────────────────────────────────────────
FEEDS = {
    "AI Model & Tool Updates": [
        ("OpenAI Blog",          "https://openai.com/blog/rss.xml"),
        ("Google DeepMind",      "https://deepmind.google/blog/rss.xml"),
        ("Anthropic News",       "https://www.anthropic.com/news/rss"),
        ("Google AI Blog",       "https://blog.google/technology/ai/rss"),
        ("Meta AI Blog",         "https://ai.meta.com/blog/feed"),
    ],
    "AI Industry & Funding": [
        ("VentureBeat AI",       "https://venturebeat.com/category/ai/feed"),
        ("TechCrunch AI",        "https://techcrunch.com/category/artificial-intelligence/feed"),
        ("The Verge AI",         "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml"),
        ("MIT Tech Review AI",   "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
        ("Wired AI",             "https://www.wired.com/feed/tag/ai/latest/rss"),
    ],
    "AI Research & Engineering": [
        ("Hugging Face Blog",    "https://huggingface.co/blog/feed.xml"),
        ("Papers With Code",     "https://paperswithcode.com/rss.xml"),
        ("Towards Data Science", "https://towardsdatascience.com/feed"),
        ("The Gradient",         "https://thegradient.pub/rss"),
        ("AI Alignment Forum",   "https://www.alignmentforum.org/feed.xml"),
    ],
    "AI Tools, Agents & Startups": [
        ("Ben's Bites",          "https://www.bensbites.co/feed"),
        ("The Rundown AI",       "https://www.therundown.ai/feed"),
        ("Every.to",             "https://every.to/feed"),
        ("Lenny's Newsletter",   "https://www.lennysnewsletter.com/feed"),
        ("TLDR AI",              "https://tldr.tech/ai/rss"),
    ],
    "AI Policy, Safety & Ethics": [
        ("Future of Life Inst.", "https://futureoflife.org/feed"),
        ("AI Now Institute",     "https://ainowinstitute.org/feed.xml"),
        ("Import AI",            "https://jack-clark.net/feed"),
    ],
    "Trending GitHub Repos & Open Source": [
        ("GitHub Trending",      "https://github-rss.alefranzoni.com/?lang=all&since=daily"),
    ],
}

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _cutoff_ts() -> float:
    """Unix timestamp for LOOKBACK_HOURS ago."""
    return (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).timestamp()


def _entry_ts(entry) -> float:
    """Best-effort published timestamp from a feedparser entry."""
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return time.mktime(t)
            except Exception:
                pass
    return time.time()   # assume recent if no date found


def _strip(text: str, max_chars: int = 500) -> str:
    """Remove excess whitespace and truncate."""
    import re
    text = re.sub(r"<[^>]+>", " ", text)          # strip HTML tags
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars] + ("…" if len(text) > max_chars else "")


def _entry_text(entry) -> str:
    """Extract best available body text from a feedparser entry."""
    for attr in ("summary", "content"):
        val = getattr(entry, attr, None)
        if val:
            if isinstance(val, list):
                val = val[0].get("value", "")
            return _strip(str(val))
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# 1. RSS FETCHER
# ─────────────────────────────────────────────────────────────────────────────

def fetch_rss_articles() -> list[dict]:
    """
    Iterate all feeds, return articles published in the last LOOKBACK_HOURS.
    Each dict: {category, source, title, url, summary, published_ts}
    """
    cutoff = _cutoff_ts()
    articles: list[dict] = []

    for category, feed_list in FEEDS.items():
        for source_name, url in feed_list:
            try:
                log.info("Fetching %-30s %s", source_name, url)
                feed = feedparser.parse(
                    url,
                    agent="Mozilla/5.0 (compatible; AIDigestBot/1.0)",
                    request_headers={"Accept": "application/rss+xml, application/xml, text/xml"},
                )
                if feed.bozo and not feed.entries:
                    log.warning("  ↳ bozo feed (parse error), skipping: %s", feed.bozo_exception)
                    continue

                fresh = [e for e in feed.entries if _entry_ts(e) >= cutoff]
                log.info("  ↳ %d fresh / %d total entries", len(fresh), len(feed.entries))

                for entry in fresh:
                    articles.append({
                        "category":     category,
                        "source":       source_name,
                        "title":        getattr(entry, "title", "(no title)"),
                        "url":          getattr(entry, "link", ""),
                        "summary":      _entry_text(entry),
                        "published_ts": _entry_ts(entry),
                    })

            except Exception as exc:
                log.error("  ↳ Failed to fetch %s — %s", source_name, exc)

    # Sort newest first
    articles.sort(key=lambda a: a["published_ts"], reverse=True)
    log.info("Total fresh RSS articles: %d", len(articles))
    return articles


# ─────────────────────────────────────────────────────────────────────────────
# 2. SUPPLEMENTAL DATA
# ─────────────────────────────────────────────────────────────────────────────

def fetch_github_trending() -> list[dict]:
    """Top 5 AI/LLM repos by stars via GitHub Search API (no auth required)."""
    url = (
        "https://api.github.com/search/repositories"
        "?q=topic:ai+topic:llm&sort=stars&order=desc&per_page=5"
    )
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                            headers={"Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28"})
        resp.raise_for_status()
        items = resp.json().get("items", [])
        return [
            {
                "name":        r["full_name"],
                "description": r.get("description") or "",
                "stars":       r["stargazers_count"],
                "url":         r["html_url"],
                "language":    r.get("language") or "N/A",
            }
            for r in items
        ]
    except Exception as exc:
        log.error("GitHub trending fetch failed: %s", exc)
        return []


def fetch_hn_ai_posts() -> list[dict]:
    """Top AI-related HN stories posted in the last 24 hours."""
    cutoff = int(_cutoff_ts())
    queries = ["LLM AI", "GPT", "Claude AI", "Gemini AI"]
    seen_ids: set = set()
    posts: list[dict] = []

    for q in queries:
        url = (
            f"https://hn.algolia.com/api/v1/search"
            f"?query={requests.utils.quote(q)}"
            f"&tags=story"
            f"&numericFilters=created_at_i>{cutoff}"
            f"&hitsPerPage=10"
        )
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            for hit in resp.json().get("hits", []):
                if hit["objectID"] not in seen_ids:
                    seen_ids.add(hit["objectID"])
                    posts.append({
                        "title":   hit.get("title", ""),
                        "url":     hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}",
                        "points":  hit.get("points", 0),
                        "comments":hit.get("num_comments", 0),
                        "hn_url":  f"https://news.ycombinator.com/item?id={hit['objectID']}",
                    })
        except Exception as exc:
            log.error("HN fetch failed for query '%s': %s", q, exc)

    posts.sort(key=lambda p: p["points"], reverse=True)
    log.info("HN posts collected: %d", len(posts))
    return posts[:15]   # cap to top 15


# ─────────────────────────────────────────────────────────────────────────────
# 3. GEMINI DIGEST GENERATION
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PERSONA = (
    "You are an elite AI insider — part product strategist, part creative director, "
    "part tech journalist. You have your finger on the pulse of every meaningful AI "
    "development. You write with clarity, wit, and a bias toward action. You don't just "
    "report what happened — you explain what it means and what a smart person should do "
    "about it."
)

DIGEST_PROMPT_TEMPLATE = """\
Here are today's AI articles and signals. Write a polished daily digest with these exact sections:

---

**🔥 THE BIG STORY**
The single most important AI development today in 3–4 sentences. What happened, why it matters, and the one implication most people will miss.

---

**🛠️ TOOL DROPS & UPDATES**
What's new or updated in ChatGPT, Gemini, Claude, Midjourney, Runway, ElevenLabs, Cursor, Perplexity, Suno, and other major AI tools. For each update: tool name, what changed, and a one-line 'who cares and why'.

---

**🎯 USE CASE SPOTLIGHT — BY ROLE**

For each of the following roles, identify 1–2 specific, practical ways today's AI news or tools can be used RIGHT NOW. Be concrete. No fluff.

- **📣 Product Marketer** — campaigns, positioning, copy, audience insights, brand voice, competitor analysis
- **📋 Product Manager** — PRDs, user research synthesis, roadmap prioritization, stakeholder updates, A/B test design
- **🎨 Designer (UI/UX)** — Figma plugins, generative UI, design system automation, user flow ideation, accessibility tools
- **📸 Photographer** — AI editing, background removal, style transfer, client delivery automation, prompt-to-shot
- **🎬 Videographer & Filmmaker** — B-roll generation, script-to-storyboard, AI color grading, voiceover synthesis, scene planning
- **📱 App Developer** — new APIs, code gen updates, AI SDK releases, agentic frameworks, deployment shortcuts

---

**🔬 FROM THE LABS**
The most interesting AI research paper or benchmark result from today, explained in plain English. Include: what they built, what it beat, and why it matters for real-world products in 6–12 months.

---

**💸 MONEY & MOVES**
Funding rounds, acquisitions, partnerships, or strategic pivots in AI today. Include company, amount/deal, and a one-line 'what this signals about where AI money is flowing.'

---

**🤖 OPEN SOURCE WATCH**
Top 2–3 trending AI GitHub repos right now. For each: repo name, what it does, star count, and a sentence on who should care.

---

**🗣️ HACKER NEWS PULSE**
The 3 most upvoted AI-related posts on HN today. For each: title, comment thread vibe in one sentence, and the sharpest take from the top comments.

---

**⚡ SPEED ROUND**
8–10 rapid-fire one-liners — the AI news you need to know but don't need to dwell on. Sharp, punchy, no fluff.

---

**🎮 TODAY'S DAILY CHALLENGE — TRY THIS IN 15 MINUTES**
One specific, hands-on thing I can do TODAY to experience a new AI capability firsthand. Include:
- The tool to use (with URL)
- The exact prompt or workflow to try
- What I'm testing for / what to look for
- Why this skill/tool matters right now

Make the challenge achievable in one sitting but genuinely illuminating — something that builds muscle memory with cutting-edge AI, not just reading about it.

---

**🧠 EDGE INSIGHT**
One non-obvious pattern, emerging trend, or second-order implication from today's AI landscape that a top-1% AI practitioner would notice but most people would miss. Think: what does today's news mean for how we'll be working 90 days from now?

---

=== TODAY'S DATA ===
{data}
"""


def _build_data_payload(
    articles: list[dict],
    github_repos: list[dict],
    hn_posts: list[dict],
) -> str:
    """
    Serialize all collected data into a text block for Gemini.
    Caps at MAX_CONTENT_CHARS, prioritising recency then source authority.
    """
    lines: list[str] = []

    # ── RSS articles ──────────────────────────────────────────────────────────
    lines.append("## RSS ARTICLES (newest first)\n")
    for a in articles:
        ts = datetime.fromtimestamp(a["published_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(
            f"[{a['category']} | {a['source']} | {ts}]\n"
            f"TITLE: {a['title']}\n"
            f"URL: {a['url']}\n"
            f"SUMMARY: {a['summary']}\n"
        )

    # ── GitHub trending ───────────────────────────────────────────────────────
    lines.append("\n## GITHUB TRENDING AI REPOS\n")
    for r in github_repos:
        lines.append(
            f"REPO: {r['name']} ({r['stars']:,} stars, {r['language']})\n"
            f"DESC: {r['description']}\n"
            f"URL: {r['url']}\n"
        )

    # ── Hacker News ───────────────────────────────────────────────────────────
    lines.append("\n## HACKER NEWS — TOP AI POSTS (LAST 24H)\n")
    for p in hn_posts:
        lines.append(
            f"TITLE: {p['title']}\n"
            f"POINTS: {p['points']}  COMMENTS: {p['comments']}\n"
            f"URL: {p['url']}\n"
            f"HN THREAD: {p['hn_url']}\n"
        )

    full = "\n".join(lines)
    if len(full) > MAX_CONTENT_CHARS:
        log.warning(
            "Payload too large (%d chars) — truncating to %d chars",
            len(full), MAX_CONTENT_CHARS,
        )
        full = full[:MAX_CONTENT_CHARS] + "\n\n[... truncated to fit context window ...]"

    return full


def generate_digest(
    articles: list[dict],
    github_repos: list[dict],
    hn_posts: list[dict],
) -> str:
    """Call Gemini and return the markdown digest text."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not set in .env")

    genai.configure(api_key=GEMINI_API_KEY)

    data_payload = _build_data_payload(articles, github_repos, hn_posts)
    prompt = DIGEST_PROMPT_TEMPLATE.format(data=data_payload)

    log.info("Sending %d chars to Gemini (%s)…", len(prompt), GEMINI_MODEL)

    # Try primary model, fall back to flash on quota errors
    for model_name in (GEMINI_MODEL, "gemini-2.5-flash"):
        try:
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=SYSTEM_PERSONA,
            )
            response = model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=0.7,
                    max_output_tokens=8192,
                ),
            )
            log.info("Gemini responded with %d chars (model: %s)", len(response.text), model_name)
            return response.text
        except Exception as exc:
            log.error("Gemini call failed with %s: %s", model_name, exc)
            if model_name == "gemini-1.5-flash-latest":
                raise   # both models failed

    return ""   # unreachable


# ─────────────────────────────────────────────────────────────────────────────
# 4. HTML EMAIL RENDERER
# ─────────────────────────────────────────────────────────────────────────────

# Maps markdown section header emoji → CSS accent color
SECTION_COLORS = {
    "🔥": "#F59E0B",   # amber — Big Story
    "🛠️": "#3B82F6",   # electric blue — Tool Drops
    "🎯": "#8B5CF6",   # violet — Use Case Spotlight
    "📣": "#EC4899",   # pink — Product Marketer
    "📋": "#06B6D4",   # cyan — PM
    "🎨": "#14B8A6",   # teal — Designer
    "📸": "#F97316",   # orange — Photographer
    "🎬": "#A855F7",   # purple — Filmmaker
    "📱": "#22C55E",   # green — App Dev
    "🔬": "#10B981",   # emerald green — Labs
    "💸": "#34D399",   # emerald — Money
    "🤖": "#F97316",   # orange — Open Source
    "🗣️": "#F87171",   # coral/red — HN Pulse
    "⚡": "#E5E7EB",   # light — Speed Round
    "🎮": "#FBBF24",   # gold — Daily Challenge
    "🧠": "#6D28D9",   # indigo — Edge Insight
}

# Section emojis that mark a new card boundary
SECTION_EMOJIS = {"🔥", "🛠", "🎯", "🔬", "💸", "🤖", "🗣", "⚡", "🎮", "🧠"}

def _extract_section_header(line: str):
    """
    Detect a section header line regardless of how Gemini formatted it.
    Handles all these patterns:
      **🔥 THE BIG STORY**          — full line bold
      🔥 **THE BIG STORY**          — emoji then bold
      ## 🔥 THE BIG STORY           — markdown heading
      ### 🔥 THE BIG STORY          — markdown heading
      🔥 THE BIG STORY              — plain emoji line (short)
    Returns the clean header text if matched, else None.
    """
    import re
    s = line.strip()
    if not s:
        return None

    # Strip leading #'s (markdown headings)
    s = re.sub(r"^#{1,4}\s+", "", s)
    # Strip surrounding ** if the whole line is bold
    s = re.sub(r"^\*\*(.+)\*\*$", r"\1", s).strip()
    # Strip leading ** if emoji comes first then bold: already handled above
    # Strip any remaining ** around the text portion
    s = re.sub(r"\*\*", "", s).strip()

    # Now check if the line starts with one of our known section emojis
    # (emoji can be 1-2 chars wide; check first 2 unicode chars)
    for emoji in SECTION_EMOJIS:
        if s.startswith(emoji):
            return s   # return clean header text

    return None


def _md_to_html_sections(markdown_text: str) -> str:
    """
    Convert Gemini's markdown output into styled HTML cards.
    Robust to all header formats Gemini may produce.
    """
    import re

    def escape(t: str) -> str:
        return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    lines = markdown_text.split("\n")
    html_parts: list[str] = []

    # Split into (header, body_lines) sections first
    sections: list[tuple] = []   # (header_text | None, [body_lines])
    current_header = None
    current_body: list[str] = []

    for raw in lines:
        stripped = raw.strip()
        if stripped == "---":
            continue

        header = _extract_section_header(raw)
        if header:
            # Save previous section
            if current_header is not None or current_body:
                sections.append((current_header, current_body))
            current_header = header
            current_body = []
        else:
            current_body.append(raw)

    # Flush last section
    if current_header is not None or current_body:
        sections.append((current_header, current_body))

    # Render each section as a card
    for header, body_lines in sections:
        if header is None:
            # Preamble text (before first section header)
            body_html = _render_body(body_lines)
            if body_html.strip():
                html_parts.append(f'<div class="card" style="border-left:6px solid #4F46E5;">{body_html}</div>')
            continue

        # Determine emoji and color — strip variation selectors (U+FE0F) before lookup
        emoji = ""
        for ch in header:
            if ch in SECTION_EMOJIS:
                emoji = ch
                break
        # Try exact match first, then with variation selector stripped
        color = SECTION_COLORS.get(emoji) or SECTION_COLORS.get(emoji + "\uFE0F", "#4F46E5")

        # Card class
        if emoji == "🎮":
            card_class = 'class="card challenge-card"'
        elif emoji == "🧠":
            card_class = 'class="card edge-card"'
        else:
            card_class = 'class="card"'

        body_html = _render_body(body_lines)
        html_parts.append(
            f'<div {card_class} style="border-left:6px solid {color};">'
            f'<h2 class="section-title" style="color:{color};">{escape(header)}</h2>'
            f'{body_html}'
            f'</div>'
        )

    return "\n".join(html_parts)


def _render_body(lines: list[str]) -> str:
    """Render body lines (paragraphs, bullets, sub-bullets) as HTML."""
    import re

    def inline_format(t: str) -> str:
        def escape(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        t = escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"\*(.+?)\*",     r"<em>\1</em>", t)
        t = re.sub(r"`(.+?)`",       r"<code style='background:#1e2130;padding:1px 5px;border-radius:3px;font-family:monospace;font-size:0.9em;'>\1</code>", t)
        # linkify bare URLs
        t = re.sub(
            r"(https?://[^\s<\"']+)",
            r'<a href="\1" style="color:#60A5FA;text-decoration:none;">\1</a>',
            t,
        )
        return t

    html: list[str] = []
    in_ul = False

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            if in_ul:
                html.append("</ul>")
                in_ul = False
            continue

        # Sub-bullets: "  - " or "    - "
        if re.match(r"^\s{2,}-\s+", raw):
            if not in_ul:
                html.append('<ul class="sub-list">')
                in_ul = True
            content = re.sub(r"^\s+-\s+", "", raw)
            html.append(f'<li style="color:#E6EDF3;">{inline_format(content)}</li>')
            continue

        # Top-level bullets: "- "
        if stripped.startswith("- "):
            if not in_ul:
                html.append('<ul class="main-list">')
                in_ul = True
            content = stripped[2:]
            html.append(f'<li style="color:#E6EDF3;">{inline_format(content)}</li>')
            continue

        # Role sub-headers inside Use Case section: "- **📣 ...**"
        m = re.match(r"^-\s+\*\*(.+?)\*\*(.*)$", stripped)
        if m:
            if in_ul:
                html.append("</ul>")
                in_ul = False
            role_label = m.group(1)
            rest       = m.group(2).strip(" —–")
            emoji      = role_label[0] if role_label else ""
            badge_color = SECTION_COLORS.get(emoji, "#6B7280")
            html.append(
                f'<div class="role-block">'
                f'<span class="role-badge" style="background:{badge_color}20;color:{badge_color};border:1px solid {badge_color}40;">'
                f'{role_label}</span>'
                + (f"<p style='margin:6px 0 0;color:#E6EDF3;'>{inline_format(rest)}</p>" if rest else "")
                + "</div>"
            )
            continue

        # Normal numbered lines: "1. "
        if re.match(r"^\d+\.\s+", stripped):
            if in_ul:
                html.append("</ul>")
                in_ul = False
            content = re.sub(r"^\d+\.\s+", "", stripped)
            html.append(f'<p style="margin:4px 0;color:#E6EDF3;">• {inline_format(content)}</p>')
            continue

        # Plain paragraph
        if in_ul:
            html.append("</ul>")
            in_ul = False
        html.append(f'<p style="color:#E6EDF3;">{inline_format(stripped)}</p>')

    if in_ul:
        html.append("</ul>")

    return "\n".join(html)


def render_html_email(digest_markdown: str, generated_at: datetime) -> str:
    """Wrap the digest in a full, styled HTML email document."""

    date_str     = generated_at.strftime("%A, %B %-d, %Y")
    ts_str       = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    body_html    = _md_to_html_sections(digest_markdown)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Insider Daily — {date_str}</title>
<style>
  /* ── Reset & base ── */
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    background: #0D1117;
    color: #C9D1D9;
    line-height: 1.7;
    -webkit-font-smoothing: antialiased;
  }}

  /* ── Outer wrapper ── */
  .wrapper {{
    max-width: 680px;
    margin: 0 auto;
    padding: 24px 16px 48px;
  }}

  /* ── Header ── */
  .header {{
    background: linear-gradient(135deg, #0A1628 0%, #1a1a2e 40%, #16213e 70%, #2d2d44 100%);
    border-radius: 16px 16px 0 0;
    padding: 40px 32px 32px;
    text-align: center;
    border-bottom: 2px solid #30363D;
  }}
  .header-title {{
    font-size: 2rem;
    font-weight: 800;
    color: #F0F6FC;
    letter-spacing: -0.5px;
    margin-bottom: 6px;
  }}
  .header-subtitle {{
    color: #8B949E;
    font-size: 0.9rem;
    text-transform: uppercase;
    letter-spacing: 2px;
  }}

  /* ── Cards ── */
  .card {{
    background: #161B22;
    border: 1px solid #30363D;
    border-radius: 10px;
    padding: 24px 28px;
    margin: 16px 0;
  }}
  .challenge-card {{
    background: linear-gradient(135deg, #1a1200 0%, #1a1500 100%);
    border-color: #FBBF24;
  }}
  .edge-card {{
    background: linear-gradient(135deg, #0f0a1e 0%, #1a1030 100%);
    border-color: #6D28D9;
    font-style: italic;
  }}

  /* ── Section title ── */
  .section-title {{
    font-size: 1.15rem;
    font-weight: 700;
    margin-bottom: 14px;
    padding-bottom: 10px;
    border-bottom: 1px solid #30363D;
  }}

  /* ── Paragraphs & lists ── */
  p {{ margin: 8px 0; font-size: 0.95rem; }}
  .main-list {{ list-style: none; padding: 0; }}
  .main-list li {{
    padding: 6px 0 6px 18px;
    border-bottom: 1px solid #21262D;
    font-size: 0.93rem;
    position: relative;
  }}
  .main-list li::before {{ content: "›"; position: absolute; left: 0; color: #58A6FF; font-weight: bold; }}
  .main-list li:last-child {{ border-bottom: none; }}
  .sub-list {{ list-style: none; padding: 0 0 0 16px; }}
  .sub-list li {{ padding: 3px 0; font-size: 0.88rem; color: #8B949E; }}

  /* ── Role badges ── */
  .role-block {{ margin: 12px 0; }}
  .role-badge {{
    display: inline-block;
    padding: 3px 10px;
    border-radius: 20px;
    font-size: 0.8rem;
    font-weight: 600;
    margin-bottom: 4px;
  }}

  /* ── Footer ── */
  .footer {{
    text-align: center;
    color: #484F58;
    font-size: 0.78rem;
    margin-top: 32px;
    padding: 20px;
    border-top: 1px solid #21262D;
  }}
  .footer strong {{ color: #6E7681; }}

  /* ── Responsive ── */
  @media (max-width: 480px) {{
    .header {{ padding: 28px 20px 24px; }}
    .header-title {{ font-size: 1.5rem; }}
    .card {{ padding: 18px 16px; }}
  }}
</style>
</head>
<body>
<div class="wrapper" style="color:#E6EDF3;">

  <!-- HEADER -->
  <div class="header">
    <div class="header-title">🤖 AI INSIDER DAILY</div>
    <div class="header-subtitle">{date_str}</div>
  </div>

  <!-- DIGEST BODY -->
  {body_html}

  <!-- FOOTER -->
  <div class="footer">
    <strong>Built for staying in the top 1%</strong><br>
    Generated at {ts_str} · Powered by Google Gemini &amp; 20+ AI sources
  </div>

</div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# 5. GMAIL SENDER
# ─────────────────────────────────────────────────────────────────────────────

def send_email(html_content: str, subject: str) -> None:
    """Send the HTML digest via Gmail SMTP over TLS (port 587)."""
    if not all([GMAIL_ADDRESS, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]):
        raise ValueError(
            "Missing one or more email env vars: GMAIL_ADDRESS, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL"
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"AI Insider Daily <{GMAIL_ADDRESS}>"
    msg["To"]      = RECIPIENT_EMAIL

    # Plain-text fallback (strip tags crudely)
    import re
    plain = re.sub(r"<[^>]+>", "", html_content)
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    log.info("Connecting to Gmail SMTP (smtp.gmail.com:587)…")
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())
    log.info("Email sent to %s", RECIPIENT_EMAIL)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def write_web_json(feed: str, feed_name: str, raw_text: str) -> None:
    """Persist the raw digest for the website build (feeds/<feed>/ -> repo root is parents[2])."""
    root = Path(__file__).resolve().parents[2]
    today = datetime.now(timezone.utc).date().isoformat()
    out = root / "data" / feed / f"{today}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "feed": feed,
                "feed_name": feed_name,
                "date": today,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "raw_text": raw_text,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log.info("Web JSON written -> %s", out)


def main() -> None:
    start = time.time()
    now   = datetime.now(timezone.utc)

    log.info("=" * 60)
    log.info("AI INSIDER DAILY — %s", now.strftime("%Y-%m-%d %H:%M UTC"))
    log.info("=" * 60)

    # 1. Collect RSS articles
    log.info("\n[1/5] Fetching RSS feeds…")
    articles = fetch_rss_articles()

    # 2. Supplemental data
    log.info("\n[2/5] Fetching GitHub trending repos…")
    github_repos = fetch_github_trending()

    log.info("\n[2/5] Fetching Hacker News AI posts…")
    hn_posts = fetch_hn_ai_posts()

    # 3. Gemini digest
    log.info("\n[3/5] Generating digest with Gemini…")
    digest_md = generate_digest(articles, github_repos, hn_posts)

    # Persist for the website build (runs regardless of email setting)
    write_web_json("ai", "AI Insider", digest_md)

    # 4. Render HTML
    log.info("\n[4/5] Rendering HTML email…")
    html = render_html_email(digest_md, now)

    # 5. Send — email stays ON for this feed by default. Set SEND_EMAIL=false to disable.
    if os.getenv("SEND_EMAIL", "true").lower() == "true":
        log.info("\n[5/5] Sending email…")
        subject = f"🤖 AI Insider Daily — {now.strftime('%B %-d, %Y')}"
        send_email(html, subject)
    else:
        log.info("\n[5/5] Email disabled (SEND_EMAIL=false) — skipping send.")

    elapsed = time.time() - start
    log.info("\nDone in %.1f seconds.", elapsed)


if __name__ == "__main__":
    main()
