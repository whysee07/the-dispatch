import os
import json
import time
import smtplib
import feedparser
from pathlib import Path
from google import genai
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

FEEDS = {
    "finance": [
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
        "https://finance.yahoo.com/news/rssindex",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://www.theguardian.com/uk/business/rss",
        "https://api.axios.com/feed/",
    ],
    "geopolitics": [
        "https://feeds.npr.org/1004/rss.xml",
        "https://foreignpolicy.com/feed/",
        "https://rss.dw.com/rdf/rss-en-all",
    ],
    "tech": [
        "https://hnrss.org/frontpage",
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://www.wired.com/feed/rss",
    ],
    "creator_economy": [
        "https://digiday.com/feed/",
        "https://www.socialmediatoday.com/rss/",
    ],
}

MAX_ARTICLES_PER_FEED = 5
LOOKBACK_HOURS = 48          # drop anything older than this so no stale news leaks in
_CUTOFF = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)


def _entry_dt(entry):
    """Best-effort published/updated datetime for an RSS entry, or None if absent."""
    for attr in ("published_parsed", "updated_parsed"):
        t = entry.get(attr)
        if t:
            try:
                return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def fetch_articles(feeds: dict) -> dict:
    import re
    all_articles = {}
    dropped_old = dropped_undated = 0
    for category, urls in feeds.items():
        articles = []
        for url in urls:
            try:
                feed = feedparser.parse(url)
                source = feed.feed.get("title", url)
                # Newest first, so the freshest items win the per-feed cap
                entries = sorted(
                    feed.entries,
                    key=lambda e: (_entry_dt(e) or _CUTOFF),
                    reverse=True,
                )
                kept = 0
                for entry in entries:
                    if kept >= MAX_ARTICLES_PER_FEED:
                        break
                    dt = _entry_dt(entry)
                    # Drop stale items. Undated entries are also dropped: feeds that
                    # omit dates are the ones that surface evergreen/old stories.
                    if dt is None:
                        dropped_undated += 1
                        continue
                    if dt < _CUTOFF:
                        dropped_old += 1
                        continue
                    title = entry.get("title", "").strip()
                    summary = entry.get("summary", entry.get("description", "")).strip()
                    summary = re.sub(r"<[^>]+>", "", summary)[:300]
                    if title:
                        day = dt.strftime("%b %-d")
                        articles.append(f"[{source}, {day}] {title}: {summary}")
                        kept += 1
            except Exception as e:
                print(f"  Warning: could not fetch {url}: {e}")
        all_articles[category] = articles
    print(f"  Date filter (last {LOOKBACK_HOURS}h): dropped {dropped_old} old, "
          f"{dropped_undated} undated.")
    return all_articles


def build_prompt(articles: dict) -> str:
    sections = {
        "finance": ("Money Talk", articles.get("finance", [])),
        "geopolitics": ("World Lore", articles.get("geopolitics", [])),
        "tech": ("Tech Tea", articles.get("tech", [])),
        "creator_economy": ("Creator Szn", articles.get("creator_economy", [])),
    }

    article_block = ""
    for key, (label, items) in sections.items():
        article_block += f"\n## {label}\n"
        for item in items:
            article_block += f"- {item}\n"

    today_str = datetime.now(timezone.utc).strftime("%A, %B %-d, %Y")
    prompt = f"""You are a sharp, witty friend who actually reads the news — think a cross between a finance bro, a foreign correspondent, a tech nerd, and a culture vulture. You write in a punchy, conversational tone with dry humor and the occasional hot take. No fluff, no filler.

Today is {today_str}. Only write about events in the articles below — they are all from the last couple of days. Do NOT bring in older news from memory, and do NOT reference events you can't tie to one of these articles.

Write a daily news digest with exactly these five sections:

1. **Money Talk** — Finance & markets. What's moving money, who's winning, who's getting cooked.
2. **World Lore** — Geopolitics & global news. Keep it sharp, not doom-scrolly.
3. **Tech Tea** — Tech news. Hype, drama, genuinely interesting stuff.
4. **Creator Szn** — Creator economy, social media, digital culture.
5. **Speed Round** — 5–7 punchy one-liners covering anything from any category. Like a lightning round of today's news.

FORMAT — read carefully:
- Sections 1–4 each have 3–5 items. Write EACH item as: a bold one-line headline, then a line break, then 1–3 sentences of detail.
  Example:
  **Oil spikes on Hormuz tension.**
  Crude jumped 4% after fresh tanker attacks in the strait. Asia imports the most through it, so watch shipping and insurance costs.
- The bold headline must stand on its own as a scannable one-liner. The detail is the deep-dive.
- Start each item's headline line with "- " (a dash) so items are clearly separated.
- Speed Round is different: 5–7 one-sentence zingers, each on its own "- " line, NO bold headline, NO detail.
- Write like you're texting a smart friend, not filing a report. Add your own color and hot takes.
- Use the plain section titles exactly as above (Money Talk, World Lore, Tech Tea, Creator Szn, Speed Round). No numbering, no markdown # headers.

Here are today's articles:
{article_block}

Write the digest now:"""
    return prompt


def call_gemini(prompt: str) -> str:
    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    return response.text


def parse_digest_to_html(raw_text: str) -> str:
    """Convert Gemini's plain-text digest into structured HTML blocks."""
    import re

    section_config = {
        "Money Talk": {"color": "#10b981", "emoji": "💰", "bg": "#064e3b"},
        "World Lore": {"color": "#f59e0b", "emoji": "🌍", "bg": "#451a03"},
        "Tech Tea": {"color": "#3b82f6", "emoji": "💻", "bg": "#1e3a5f"},
        "Creator Szn": {"color": "#a855f7", "emoji": "🎬", "bg": "#3b0764"},
        "Speed Round": {"color": "#f43f5e", "emoji": "⚡", "bg": "#4c0519"},
    }

    section_order = list(section_config.keys())

    # Split text into sections
    pattern = r"(?m)^(" + "|".join(re.escape(s) for s in section_order) + r")\s*[\n:]"
    parts = re.split(pattern, raw_text)

    sections = {}
    i = 1
    while i < len(parts) - 1:
        title = parts[i].strip()
        content = parts[i + 1].strip() if i + 1 < len(parts) else ""
        sections[title] = content
        i += 2

    html_sections = ""
    for title in section_order:
        content = sections.get(title, "")
        if not content:
            continue
        cfg = section_config[title]

        # Convert bullet lines to <li> items
        lines = content.split("\n")
        body_html = ""
        in_list = False
        for line in lines:
            line = line.strip()
            if not line:
                if in_list:
                    body_html += "</ul>"
                    in_list = False
                body_html += "<br>"
                continue
            if line.startswith(("- ", "• ", "* ")):
                if not in_list:
                    body_html += '<ul style="margin:8px 0 8px 20px;padding:0;">'
                    in_list = True
                body_html += f'<li style="margin-bottom:6px;">{line[2:].strip()}</li>'
            else:
                if in_list:
                    body_html += "</ul>"
                    in_list = False
                body_html += f'<p style="margin:8px 0;">{line}</p>'
        if in_list:
            body_html += "</ul>"

        html_sections += f"""
        <div style="background:{cfg['bg']};border-left:4px solid {cfg['color']};border-radius:8px;padding:20px 24px;margin-bottom:20px;">
          <h2 style="color:{cfg['color']};margin:0 0 12px 0;font-size:18px;letter-spacing:0.5px;">
            {cfg['emoji']} {title}
          </h2>
          <div style="color:#e2e8f0;font-size:15px;line-height:1.7;">
            {body_html}
          </div>
        </div>
        """

    return html_sections


def build_html_email(digest_text: str) -> str:
    today = datetime.now().strftime("%A, %B %-d, %Y")
    sections_html = parse_digest_to_html(digest_text)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Your Daily Digest</title>
</head>
<body style="margin:0;padding:0;background:#0f172a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0f172a;padding:32px 16px;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

          <!-- Header -->
          <tr>
            <td style="background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);border-radius:12px 12px 0 0;padding:32px 32px 24px;border-bottom:1px solid #334155;">
              <p style="color:#64748b;font-size:12px;letter-spacing:2px;text-transform:uppercase;margin:0 0 8px 0;">Daily Intelligence Brief</p>
              <h1 style="color:#f8fafc;font-size:28px;font-weight:700;margin:0 0 4px 0;">Ground News Digest</h1>
              <p style="color:#94a3b8;font-size:14px;margin:0;">{today}</p>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="background:#1e293b;padding:24px 32px;">
              {sections_html}
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background:#0f172a;border-radius:0 0 12px 12px;padding:20px 32px;text-align:center;border-top:1px solid #1e293b;">
              <p style="color:#475569;font-size:12px;margin:0;">
                Generated by your personal digest bot &nbsp;·&nbsp; {today}
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
    return html


def send_email(html_body: str):
    today = datetime.now().strftime("%b %-d")
    subject = f"Your Daily Digest — {today}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL

    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())


def write_web_json(feed: str, feed_name: str, raw_text: str) -> None:
    """Persist the raw digest for the website build. Repo layout: feeds/<feed>/digest.py -> repo root is parents[2]."""
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
    print(f"  Web JSON written -> {out}")


def main():
    print("Fetching articles...")
    articles = fetch_articles(FEEDS)
    total = sum(len(v) for v in articles.values())
    print(f"  Fetched {total} articles across {len(articles)} categories.")

    print("Building prompt and calling Gemini...")
    prompt = build_prompt(articles)
    digest_text = call_gemini(prompt)
    print("  Gemini response received.")

    # Persist for the website build (runs regardless of email setting)
    write_web_json("brief", "Daily Brief", digest_text)

    # Email for this feed is OFF by default (web-only). Set SEND_EMAIL=true to re-enable.
    if os.getenv("SEND_EMAIL", "false").lower() == "true":
        print("Rendering HTML email...")
        html = build_html_email(digest_text)
        print("Sending email...")
        send_email(html)
        print(f"  Digest sent to {RECIPIENT_EMAIL}.")
    else:
        print("  Email disabled (SEND_EMAIL != true) — web-only feed.")


if __name__ == "__main__":
    main()
