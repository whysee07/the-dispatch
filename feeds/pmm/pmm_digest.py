#!/usr/bin/env python3
"""
pmm_digest.py
═══════════════════════════════════════════════════════════════════════════
Daily PMM/PM Insider Digest Generator

Fetches RSS articles from 30+ sources, scrapes PMM/PM jobs from 5 sources,
pulls community signals (Reddit, HN, Product Hunt, GitHub), generates a
sharp daily digest via Gemini 1.5 Pro, and sends it as a polished
Notion-inspired HTML email via Gmail SMTP.

Schedule (cron): 30 6 * * * /usr/bin/python3 /path/to/pmm_digest.py

Dependencies (pip install):
  feedparser requests beautifulsoup4 google-generativeai python-dotenv
  praw apify-client lxml pytz python-dateutil

.env variables required:
  GEMINI_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL,
  CRUNCHBASE_API_KEY, APIFY_API_KEY, REDDIT_CLIENT_ID,
  REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT, GITHUB_TOKEN
═══════════════════════════════════════════════════════════════════════════
"""

# ── IMPORTS ────────────────────────────────────────────────────────────────

import html as html_lib
import json
import logging
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser
from dotenv import load_dotenv

# ── ENVIRONMENT ────────────────────────────────────────────────────────────

load_dotenv()

GEMINI_API_KEY       = os.getenv("GEMINI_API_KEY")
GMAIL_ADDRESS        = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD   = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL      = os.getenv("RECIPIENT_EMAIL")
CRUNCHBASE_API_KEY   = os.getenv("CRUNCHBASE_API_KEY")
APIFY_API_KEY        = os.getenv("APIFY_API_KEY")
REDDIT_CLIENT_ID     = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT    = os.getenv("REDDIT_USER_AGENT", "pmm_digest_bot/1.0")
GITHUB_TOKEN         = os.getenv("GITHUB_TOKEN")

# ── LOGGING ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pmm_digest")

# ── CONSTANTS ──────────────────────────────────────────────────────────────

NOW_UTC          = datetime.now(timezone.utc)
CUTOFF_24H       = NOW_UTC - timedelta(hours=24)
CUTOFF_72H       = NOW_UTC - timedelta(hours=72)
MAX_GEMINI_CHARS = 80_000
REQUEST_TIMEOUT  = 15  # seconds per HTTP request

# Job title filters — all case-insensitive
TITLE_INCLUDE = ["product"]   # title must contain at least one of these
TITLE_EXCLUDE = [
    "director", "vp", "vice president", "head of", "principal",
    "chief", "c-", "group pmm lead", "manager of pmms",
]

# ── DATA CLASSES ───────────────────────────────────────────────────────────

@dataclass
class Article:
    title:     str
    url:       str
    published: datetime   # always timezone-aware UTC
    source:    str
    category:  str
    summary:   str = ""
    content:   str = ""

    @property
    def char_count(self) -> int:
        return len(self.title) + len(self.summary) + len(self.content)


@dataclass
class Job:
    title:          str
    company:        str
    location:       str
    posted:         datetime   # always timezone-aware UTC
    url:            str
    source:         str
    salary:         str = ""
    stage:          str = ""
    stage_status:   str = "UNKNOWN"   # "KEEP" or "SKIP"
    stage_reason:   str = ""
    yc_batch:       str = ""
    applicant_count: str = ""
    extra_sources:  list = field(default_factory=list)


@dataclass
class Signal:
    type:          str   # reddit | hn | producthunt | github
    title:         str
    url:           str
    score:         int = 0
    comment_count: int = 0
    summary:       str = ""
    subreddit:     str = ""
    top_comment:   str = ""
    tagline:       str = ""
    description:   str = ""
    stars:         int = 0
    language:      str = ""


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 — RSS FEEDS
# ═══════════════════════════════════════════════════════════════════════════

RSS_FEEDS: dict[str, list[tuple[str, str]]] = {
    # ── PMM thought leaders & communities ─────────────────────────────────
    # URLs verified working as of 2026-04. Re-run the RSS checker if feeds go dead.
    "PMM Thought Leaders & Communities": [
        # Forget the Funnel moved to Substack
        ("Forget the Funnel",          "https://forgetthefunnel.substack.com/feed"),
        ("Product Marketing Alliance", "https://productmarketingalliance.com/feed"),
        ("Pragmatic Institute",        "https://www.pragmaticinstitute.com/feed/"),
        # First Round Review: glossary/rss is the active feed path
        ("First Round Review",         "https://review.firstround.com/glossary/rss/"),
        # OpenView Partners feed is malformed XML — skip until fixed upstream
        # HubSpot Marketing Blog — high volume, reliably published
        ("HubSpot Marketing",          "https://blog.hubspot.com/marketing/rss.xml"),
        # Intercom — strong PLG/product-led content
        ("Intercom Blog",              "https://www.intercom.com/blog/rss"),
        # NOTE: Exit Five, CXL, a16z Marketing, and Reforge no longer publish
        # a public RSS feed as of 2026. Remove entries if still 404ing.
    ],
    # ── PM thought leaders ─────────────────────────────────────────────────
    "PM Thought Leaders": [
        ("SVPG",               "https://www.svpg.com/articles/rss"),
        ("Lenny's Newsletter", "https://www.lennysnewsletter.com/feed"),
        ("Teresa Torres",      "https://www.producttalk.org/feed"),
        ("Andrew Chen",        "https://andrewchen.com/feed"),
        # Product Plan blog — solid PM practitioner content
        ("ProductPlan Blog",   "https://www.productplan.com/blog/feed/"),
    ],
    # ── Competitive intel & positioning ───────────────────────────────────
    "Competitive Intel & Positioning": [
        ("Crayon Blog",       "https://www.crayon.co/blog/rss.xml"),
        ("Highspot Blog",     "https://www.highspot.com/blog/feed/"),
        ("Corporate Visions", "https://corporatevisions.com/feed/"),
        # Gong blog RSS not publicly available — removed
    ],
    # ── GTM, growth & pricing ─────────────────────────────────────────────
    "GTM, Growth & Pricing": [
        # Growth Unhinged (Kyle Poyar) is on Substack
        ("Growth Unhinged",   "https://growthunhinged.substack.com/feed"),
        # Dovetail changelog — frequent product/research updates
        ("Dovetail",          "https://dovetail.com/changelog/rss.xml"),
        # Gainsight RSS not publicly available — removed
    ],
    # ── Industry & tech news (for GTM market context) ─────────────────────
    "Industry & Tech News": [
        ("TechCrunch",            "https://techcrunch.com/feed"),
        ("The Verge",             "https://www.theverge.com/rss/index.xml"),
        ("VentureBeat",           "https://venturebeat.com/feed"),
        ("Wired",                 "https://www.wired.com/feed/rss"),
        ("MIT Technology Review", "https://www.technologyreview.com/feed"),
    ],
    # ── Product Hunt (new tools) ───────────────────────────────────────────
    "Product Hunt": [
        ("Product Hunt Daily",    "https://www.producthunt.com/feed"),
    ],
}

# Authority score per category — used when trimming articles to hit char cap.
# Higher = prioritized when trimming (kept longer).
FEED_AUTHORITY: dict[str, int] = {
    "PMM Thought Leaders & Communities": 5,
    "PM Thought Leaders":                5,
    "Competitive Intel & Positioning":   4,
    "GTM, Growth & Pricing":             4,
    "Product Hunt":                      3,
    "Industry & Tech News":              2,
}


def parse_published(entry) -> Optional[datetime]:
    """
    Extract a timezone-aware UTC datetime from a feedparser entry.
    Tries struct_time attributes first, then falls back to string parsing.
    """
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    for attr in ("published", "updated"):
        s = getattr(entry, attr, None)
        if s:
            try:
                dt = dateutil_parser.parse(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
    return None


def _strip_html(raw: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    try:
        text = BeautifulSoup(raw, "lxml").get_text(separator=" ")
    except Exception:
        text = BeautifulSoup(raw, "html.parser").get_text(separator=" ")
    return " ".join(text.split())


def fetch_feed(source_name: str, url: str, category: str) -> list[Article]:
    """
    Fetch one RSS feed, return articles published in the last 24 hours.

    Strategy: fetch raw bytes with requests first, then pass to feedparser.
    This sidesteps the strict XML parser that rejects slightly malformed feeds
    (e.g. unescaped ampersands, non-UTF-8 chars) — a very common real-world
    issue. feedparser in 'bozo' mode still yields entries from such feeds.
    """
    articles = []
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": REDDIT_USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        # Pass raw bytes — feedparser will detect encoding itself
        feed = feedparser.parse(resp.content)

        # bozo=True just means the feed is malformed XML; entries may still exist.
        # Only bail if there are genuinely zero entries.
        if not feed.entries:
            bozo_exc = getattr(feed, "bozo_exception", None)
            if bozo_exc:
                raise ValueError(f"No entries + parse error: {bozo_exc}")
            # Empty feed is fine — just 0 articles today
            log.info(f"[RSS] {source_name}: 0 articles in last 24h")
            return []

        for entry in feed.entries:
            published = parse_published(entry)
            if published is None or published < CUTOFF_24H:
                continue

            title   = getattr(entry, "title", "").strip()
            link    = getattr(entry, "link",  "").strip()
            summary = _strip_html(getattr(entry, "summary", ""))[:500]
            content = ""

            if hasattr(entry, "content") and entry.content:
                raw_content = entry.content[0].get("value", "")
                content = _strip_html(raw_content)[:2000]

            if title and link:
                articles.append(Article(
                    title=title, url=link, published=published,
                    source=source_name, category=category,
                    summary=summary, content=content,
                ))

        log.info(f"[RSS] {source_name}: {len(articles)} articles in last 24h")
    except Exception as e:
        log.warning(f"[RSS] FAILED {source_name} ({url}): {e}")

    return articles


def fetch_all_rss_feeds() -> list[Article]:
    """Fetch all configured RSS feeds, return flat list sorted newest-first."""
    all_articles: list[Article] = []
    for category, feeds in RSS_FEEDS.items():
        for source_name, url in feeds:
            all_articles.extend(fetch_feed(source_name, url, category))
    all_articles.sort(key=lambda a: a.published, reverse=True)
    return all_articles


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — JOB SCRAPING
# ═══════════════════════════════════════════════════════════════════════════

# ── SHARED HELPERS ─────────────────────────────────────────────────────────

def title_passes_filters(title: str) -> bool:
    """Return True if a job title passes include AND exclude filters."""
    t = title.lower()
    if not any(kw in t for kw in TITLE_INCLUDE):
        return False
    if any(kw in t for kw in TITLE_EXCLUDE):
        return False
    return True


def is_within_72h(posted: Optional[datetime]) -> bool:
    """Return True if the posted datetime falls within the last 72 hours."""
    if posted is None:
        return False
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return posted >= CUTOFF_72H


def parse_relative_date(text: str) -> Optional[datetime]:
    """
    Parse strings like '2 days ago', '3 hours ago', '1 week ago' into a
    timezone-aware UTC datetime. Falls back to dateutil for absolute dates.
    """
    if not text:
        return None
    text = text.lower().strip()
    patterns = [
        (r"(\d+)\s*minute", timedelta(minutes=1)),
        (r"(\d+)\s*hour",   timedelta(hours=1)),
        (r"(\d+)\s*day",    timedelta(days=1)),
        (r"(\d+)\s*week",   timedelta(weeks=1)),
        (r"(\d+)\s*month",  timedelta(days=30)),
    ]
    for pattern, unit in patterns:
        m = re.search(pattern, text)
        if m:
            return NOW_UTC - (int(m.group(1)) * unit)
    try:
        dt = dateutil_parser.parse(text, fuzzy=True)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# ── COMPANY CLASSIFIER ─────────────────────────────────────────────────────

fortune500_set: set[str] = set()
_company_cache: dict[str, tuple[str, str]] = {}

# Built-in fallback — top ~100 Fortune 500 + well-known tech companies.
# Replace this by providing a populated fortune500.txt file.
FORTUNE_500_FALLBACK = {
    "walmart", "amazon", "apple", "unitedhealth group", "berkshire hathaway",
    "cvs health", "exxon mobil", "alphabet", "mckesson", "costco wholesale",
    "cigna", "at&t", "microsoft", "cardinal health", "chevron", "home depot",
    "kroger", "ford motor", "verizon communications", "jpmorgan chase",
    "general motors", "centene", "meta platforms", "comcast", "phillips 66",
    "valero energy", "target", "dell technologies", "johnson & johnson",
    "humana", "fedex", "wells fargo", "citigroup", "bank of america",
    "boeing", "pfizer", "general electric", "lockheed martin", "goldman sachs",
    "morgan stanley", "ups", "caterpillar", "procter & gamble", "disney",
    "nike", "merck", "abbvie", "intel", "ibm", "abbott laboratories",
    "honeywell", "3m", "oracle", "hp", "american express", "visa",
    "mastercard", "netflix", "adobe", "nvidia", "qualcomm", "broadcom",
    "cisco", "pepsico", "coca-cola", "deere", "raytheon", "northrop grumman",
    "general dynamics", "l3harris",
    # Well-known tech/SaaS (large enough to treat as Fortune 500 tier)
    "servicenow", "workday", "zendesk", "twilio", "datadog", "snowflake",
    "atlassian", "palo alto networks", "crowdstrike", "hubspot", "shopify",
    "square", "stripe", "zoom", "docusign", "salesforce", "splunk",
}


def load_fortune500() -> set[str]:
    """
    Load Fortune 500 names from fortune500.txt (one per line).
    Falls back to the built-in FORTUNE_500_FALLBACK set if the file doesn't exist.
    """
    global fortune500_set
    txt_path = Path(__file__).parent / "fortune500.txt"
    if txt_path.exists():
        with open(txt_path) as f:
            fortune500_set = {line.strip().lower() for line in f if line.strip()}
        log.info(f"[Fortune500] Loaded {len(fortune500_set)} companies from fortune500.txt")
    else:
        fortune500_set = set(FORTUNE_500_FALLBACK)
        log.warning("[Fortune500] fortune500.txt not found — using built-in fallback list")
    return fortune500_set


def classify_company(company_name: str) -> tuple[str, str]:
    """
    Classify a company as KEEP or SKIP.

    Track 1: Check local Fortune 500 list — instant, no API call.
    Track 2: Crunchbase API v4 — keeps Series A through E startups.

    Returns:
        ("KEEP", reason_string) or ("SKIP", reason_string)

    Results are cached in _company_cache to avoid duplicate API calls.
    """
    name_lower = company_name.lower().strip()

    if name_lower in _company_cache:
        return _company_cache[name_lower]

    # Track 1 — Fortune 500
    if name_lower in fortune500_set:
        result: tuple[str, str] = ("KEEP", "Fortune 500")
        _company_cache[name_lower] = result
        return result

    # Track 2 — Crunchbase
    if not CRUNCHBASE_API_KEY:
        log.debug(f"[Crunchbase] No API key — defaulting KEEP for '{company_name}'")
        result = ("KEEP", "Unknown (no Crunchbase key)")
        _company_cache[name_lower] = result
        return result

    slug = re.sub(r"[^a-z0-9]+", "-", name_lower).strip("-")
    url  = f"https://api.crunchbase.com/api/v4/entities/organizations/{slug}"

    KEEP_STAGES = {"series_a", "series_b", "series_c", "series_d", "series_e"}

    try:
        resp = requests.get(
            url,
            params={"user_key": CRUNCHBASE_API_KEY, "field_ids": "last_funding_type,name"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            result = ("SKIP", "Not found on Crunchbase")
        elif resp.status_code == 200:
            data = resp.json().get("properties", {})
            stage = (data.get("last_funding_type") or "").lower().replace(" ", "_")
            if stage in KEEP_STAGES:
                result = ("KEEP", f"Startup ({stage.replace('_', ' ').title()})")
            elif stage:
                result = ("SKIP", stage)
            else:
                result = ("SKIP", "Unknown funding stage")
        else:
            log.warning(f"[Crunchbase] HTTP {resp.status_code} for '{company_name}'")
            result = ("KEEP", "Unknown (API error)")
    except Exception as e:
        log.warning(f"[Crunchbase] Exception for '{company_name}': {e}")
        result = ("KEEP", "Unknown (exception)")

    _company_cache[name_lower] = result
    return result


# ── SOURCE 1: WELLFOUND ────────────────────────────────────────────────────

def wellfound_jobs() -> list[Job]:
    """
    Scrape job listings from Wellfound (AngelList).

    NOTE: Wellfound uses heavy client-side rendering. This is a best-effort
    scrape using requests + BeautifulSoup. If you consistently get 0 results,
    swap to a headless browser (playwright / selenium) and target the same URL.
    The filter parameters in the URL still apply server-side for JS rendering.
    """
    jobs: list[Job] = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    url = (
        "https://wellfound.com/jobs"
        "?role=marketing&location=united-states"
        "&stage=series-a&stage=series-b&stage=series-c&stage=series-d&stage=series-e"
    )

    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(resp.text, "lxml")

        # Wellfound job cards — selectors may drift as they update their frontend.
        job_cards = (
            soup.select("[data-test='JobListing']") or
            soup.select("[class*='JobListing']") or
            soup.select("li[class*='job']") or
            soup.select("div[class*='job-card']")
        )

        for card in job_cards:
            try:
                title_el    = card.select_one("a[class*='title'], h2, h3, [class*='Title']")
                company_el  = card.select_one("[class*='company'], [class*='Company']")
                location_el = card.select_one("[class*='location'], [class*='Location']")
                salary_el   = card.select_one("[class*='salary'], [class*='compensation']")
                date_el     = card.select_one("time, [class*='date'], [class*='posted']")
                link_el     = card.select_one("a[href*='/jobs/']") or card.select_one("a")

                title    = title_el.get_text(strip=True)    if title_el    else ""
                company  = company_el.get_text(strip=True)  if company_el  else ""
                location = location_el.get_text(strip=True) if location_el else "United States"
                salary   = salary_el.get_text(strip=True)   if salary_el   else ""
                date_raw = (
                    (date_el.get("datetime") or date_el.get_text(strip=True))
                    if date_el else ""
                )
                job_url = urljoin("https://wellfound.com", link_el["href"]) if link_el else ""

                if not title or not company:
                    continue
                if not title_passes_filters(title):
                    continue

                posted = parse_relative_date(date_raw) or (NOW_UTC - timedelta(hours=12))
                if not is_within_72h(posted):
                    continue

                status, reason = classify_company(company)
                if status == "SKIP":
                    continue

                jobs.append(Job(
                    title=title, company=company, location=location,
                    salary=salary, posted=posted, url=job_url,
                    source="Wellfound", stage=reason,
                    stage_status=status, stage_reason=reason,
                ))
            except Exception as e:
                log.debug(f"[Wellfound] Card parse error: {e}")

        log.info(f"[Wellfound] {len(jobs)} jobs after filtering")
    except Exception as e:
        log.warning(f"[Wellfound] Scrape failed: {e}")

    return jobs


# ── SOURCE 2: YC WORK AT A STARTUP ────────────────────────────────────────

def yc_jobs() -> list[Job]:
    """
    Scrape job listings from YC Work at a Startup.

    Same JS-rendering caveat as Wellfound — swap to headless browser if
    you consistently get 0 results.
    """
    jobs: list[Job] = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    url = "https://www.workatastartup.com/jobs?role=marketing&location=US"

    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(resp.text, "lxml")

        job_cards = (
            soup.select(".job-name") or
            soup.select("[class*='job-listing']") or
            soup.select("li[class*='job']")
        )

        for card in job_cards:
            try:
                title_el    = card.select_one("a, h2, h3, [class*='title']")
                company_el  = card.select_one("[class*='company'], [class*='Company']")
                location_el = card.select_one("[class*='location'], [class*='remote']")
                batch_el    = card.select_one("[class*='batch'], [class*='yc']")
                date_el     = card.select_one("time, [class*='date'], [class*='posted']")
                link_el     = card.select_one("a[href*='/jobs/'], a[href*='/companies/']")

                title    = title_el.get_text(strip=True)    if title_el    else ""
                company  = company_el.get_text(strip=True)  if company_el  else ""
                location = location_el.get_text(strip=True) if location_el else "Remote-US"
                yc_batch = batch_el.get_text(strip=True)    if batch_el    else ""
                date_raw = (
                    (date_el.get("datetime") or date_el.get_text(strip=True))
                    if date_el else ""
                )
                job_url = (
                    urljoin("https://www.workatastartup.com", link_el["href"])
                    if link_el else ""
                )

                if not title or not company:
                    continue
                if not title_passes_filters(title):
                    continue

                posted = parse_relative_date(date_raw) or (NOW_UTC - timedelta(hours=12))
                if not is_within_72h(posted):
                    continue

                status, reason = classify_company(company)
                if status == "SKIP":
                    continue

                jobs.append(Job(
                    title=title, company=company, location=location,
                    posted=posted, url=job_url, source="YC Work at a Startup",
                    stage=reason, stage_status=status, stage_reason=reason,
                    yc_batch=yc_batch,
                ))
            except Exception as e:
                log.debug(f"[YC Jobs] Card parse error: {e}")

        log.info(f"[YC Jobs] {len(jobs)} jobs after filtering")
    except Exception as e:
        log.warning(f"[YC Jobs] Scrape failed: {e}")

    return jobs


# ── SOURCE 3: LINKEDIN VIA APIFY ──────────────────────────────────────────

def apify_linkedin_jobs() -> list[Job]:
    """
    Fetch LinkedIn job listings via Apify actor 'valig/linkedin-jobs-scraper' (free).

    Input: searchUrl built from LinkedIn jobs search with 72h filter.
    Output fields confirmed from live test: id, url, title, location, postedDate,
    companyName, companyUrl, experienceLevel, contractType, workType.
    """
    if not APIFY_API_KEY:
        log.warning("[Apify/LinkedIn] No APIFY_API_KEY — skipping")
        return []

    jobs: list[Job] = []
    try:
        from apify_client import ApifyClient  # type: ignore
        client = ApifyClient(APIFY_API_KEY)

        # Build a LinkedIn jobs search URL with 72-hour recency filter (r259200 = 72hrs in seconds)
        search_url = (
            "https://www.linkedin.com/jobs/search/"
            "?keywords=Product%20Marketing%20Manager"
            "&location=United%20States"
            "&f_TPR=r259200"          # posted in last 72 hours
            "&f_E=3%2C4"              # experience: associate + mid-senior
        )

        run_input = {
            "searchUrl": search_url,
            "count":     50,
        }
        log.info("[Apify/LinkedIn] Starting actor run (valig/linkedin-jobs-scraper)...")
        run = client.actor("valig/linkedin-jobs-scraper").call(
            run_input=run_input, timeout_secs=180
        )
        if not run:
            log.warning("[Apify/LinkedIn] Actor returned no run object")
            return []

        items = client.dataset(run["defaultDatasetId"]).list_items().items

        for item in items:
            # Field names confirmed from live test
            title      = item.get("title", "")
            company    = item.get("companyName", "")
            location   = item.get("location", "")
            job_url    = item.get("url", "")
            posted_raw = item.get("postedDate", "")

            if not title or not company:
                continue
            if not title_passes_filters(title):
                continue

            posted = parse_relative_date(str(posted_raw)) if posted_raw else (NOW_UTC - timedelta(hours=24))
            if not is_within_72h(posted):
                continue

            loc_lower = location.lower()
            if "united states" not in loc_lower and "remote" not in loc_lower and ", us" not in loc_lower:
                continue

            status, reason = classify_company(company)
            if status == "SKIP":
                continue

            jobs.append(Job(
                title=title, company=company, location=location,
                posted=posted, url=job_url,
                source="LinkedIn", stage=reason,
                stage_status=status, stage_reason=reason,
            ))

        log.info(f"[Apify/LinkedIn] {len(jobs)} jobs after filtering")
    except ImportError:
        log.warning("[Apify/LinkedIn] apify-client not installed — skipping")
    except Exception as e:
        log.warning(f"[Apify/LinkedIn] Failed: {e}")

    return jobs


# ── SOURCE 4: GOOGLE JOBS VIA APIFY ───────────────────────────────────────

def apify_google_jobs() -> list[Job]:
    """
    Fetch Google Jobs via Apify actor 'johnvc/Google-Jobs-Scraper' (free).

    Output fields confirmed from live test:
    title, company_name, location, via, share_link, extensions,
    detected_extensions, source_link, description, job_highlights,
    apply_options, job_id.

    detected_extensions often contains: {'posted_at': '3 days ago', 'salary': '...'}
    apply_options[0]['link'] is the direct apply URL.
    """
    if not APIFY_API_KEY:
        log.warning("[Apify/Google] No APIFY_API_KEY — skipping")
        return []

    jobs: list[Job] = []
    try:
        from apify_client import ApifyClient  # type: ignore
        client = ApifyClient(APIFY_API_KEY)

        run_input = {
            "query":      "Product Marketing Manager",
            "location":   "United States",
            "maxResults": 50,
        }
        log.info("[Apify/Google] Starting actor 'johnvc/Google-Jobs-Scraper'...")
        run = client.actor("johnvc/Google-Jobs-Scraper").call(
            run_input=run_input, timeout_secs=180
        )
        if not run:
            log.warning("[Apify/Google] Actor returned no run object")
            return []

        items = client.dataset(run["defaultDatasetId"]).list_items().items

        for item in items:
            # Field names confirmed from live test
            title   = item.get("title", "")
            company = item.get("company_name", "")
            location = item.get("location", "")

            # Best apply URL: first apply_option link, then share_link
            apply_options = item.get("apply_options") or []
            job_url = (
                apply_options[0].get("link", "") if apply_options
                else item.get("share_link", "")
            )

            # posted_at lives inside detected_extensions dict
            detected = item.get("detected_extensions") or {}
            posted_raw = detected.get("posted_at", "")
            salary     = detected.get("salary", "")

            if not title or not company:
                continue
            if not title_passes_filters(title):
                continue

            posted = parse_relative_date(str(posted_raw)) if posted_raw else (NOW_UTC - timedelta(hours=24))
            if not is_within_72h(posted):
                continue

            status, reason = classify_company(company)
            if status == "SKIP":
                continue

            jobs.append(Job(
                title=title, company=company, location=location,
                salary=salary, posted=posted, url=job_url, source="Google Jobs",
                stage=reason, stage_status=status, stage_reason=reason,
            ))

        log.info(f"[Apify/Google] {len(jobs)} jobs after filtering")
    except ImportError:
        log.warning("[Apify/Google] apify-client not installed — skipping")
    except Exception as e:
        log.warning(f"[Apify/Google] Failed: {e}")

    return jobs


# ── SOURCE 5: GREENHOUSE & LEVER ──────────────────────────────────────────

# Companies known to use Greenhouse job boards
GREENHOUSE_COMPANIES = [
    "stripe", "rippling", "brex", "ramp", "lattice", "carta",
    "airtable", "loom", "miro", "salesforce", "hubspot", "zendesk",
    "intercom", "pendo", "amplitude", "mixpanel", "braze", "iterable",
    "highspot", "seismic", "gong", "outreach", "salesloft", "klue",
    "productboard", "asana", "monday", "atlassian", "twilio",
]

# Companies known to use Lever job boards
LEVER_COMPANIES = [
    "figma", "linear", "notion", "canva", "vercel", "supabase", "retool",
    "segment", "crayon", "zoom", "datadog", "snowflake",
]


def _us_location(location: str) -> bool:
    """Return True if location appears to be US-based."""
    loc = location.lower()
    return (
        "united states" in loc or "remote" in loc
        or ", us" in loc or "usa" in loc
        or any(state in loc for state in [
            "new york", "san francisco", "chicago", "los angeles",
            "austin", "seattle", "boston", "denver", "atlanta",
        ])
    )


def scrape_greenhouse(company_slug: str) -> list[Job]:
    """Scrape job listings from a Greenhouse board for one company."""
    jobs: list[Job] = []
    url = f"https://boards.greenhouse.io/embed/job_board?for={company_slug}"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(resp.text, "lxml")
        for opening in soup.select(".opening"):
            link     = opening.select_one("a")
            loc_el   = opening.select_one(".location")
            if not link:
                continue
            title    = link.get_text(strip=True)
            location = loc_el.get_text(strip=True) if loc_el else ""
            job_url  = link.get("href", "")
            if not job_url.startswith("http"):
                job_url = f"https://boards.greenhouse.io{job_url}"
            if not title_passes_filters(title):
                continue
            if location and not _us_location(location):
                continue
            status, reason = classify_company(company_slug.replace("-", " "))
            jobs.append(Job(
                title=title,
                company=company_slug.replace("-", " ").title(),
                location=location or "Unknown",
                posted=NOW_UTC - timedelta(hours=12),
                url=job_url, source="Greenhouse",
                stage=reason, stage_status=status, stage_reason=reason,
            ))
    except Exception as e:
        log.debug(f"[Greenhouse] {company_slug}: {e}")
    return jobs


def scrape_lever(company_slug: str) -> list[Job]:
    """Scrape job listings from a Lever board for one company."""
    jobs: list[Job] = []
    url = f"https://jobs.lever.co/{company_slug}"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(resp.text, "lxml")
        for posting in soup.select(".posting"):
            title_el = posting.select_one("h5, .posting-name, [class*='title']")
            loc_el   = posting.select_one(".sort-by-location, [class*='location']")
            link     = posting.select_one("a.posting-btn-submit") or posting.select_one("a[href]")
            if not title_el:
                continue
            title    = title_el.get_text(strip=True)
            location = loc_el.get_text(strip=True) if loc_el else ""
            job_url  = link.get("href", "") if link else url
            if not job_url.startswith("http"):
                job_url = f"https://jobs.lever.co{job_url}"
            if not title_passes_filters(title):
                continue
            if location and not _us_location(location):
                continue
            status, reason = classify_company(company_slug.replace("-", " "))
            jobs.append(Job(
                title=title,
                company=company_slug.replace("-", " ").title(),
                location=location or "Unknown",
                posted=NOW_UTC - timedelta(hours=12),
                url=job_url, source="Lever",
                stage=reason, stage_status=status, stage_reason=reason,
            ))
    except Exception as e:
        log.debug(f"[Lever] {company_slug}: {e}")
    return jobs


def greenhouse_lever_jobs() -> list[Job]:
    """Scrape all configured Greenhouse and Lever boards."""
    jobs: list[Job] = []
    log.info(
        f"[Greenhouse/Lever] Scraping "
        f"{len(GREENHOUSE_COMPANIES)} Greenhouse + {len(LEVER_COMPANIES)} Lever boards..."
    )
    for slug in GREENHOUSE_COMPANIES:
        found = scrape_greenhouse(slug)
        if found:
            log.debug(f"[Greenhouse] {slug}: {len(found)} match(es)")
        jobs.extend(found)
    for slug in LEVER_COMPANIES:
        found = scrape_lever(slug)
        if found:
            log.debug(f"[Lever] {slug}: {len(found)} match(es)")
        jobs.extend(found)
    log.info(f"[Greenhouse/Lever] Total: {len(jobs)} jobs")
    return jobs


# ── DEDUPLICATION & AGGREGATION ────────────────────────────────────────────

def deduplicate_jobs(jobs: list[Job]) -> list[Job]:
    """
    Deduplicate by (normalized_title, normalized_company).
    When duplicates are found, merge source lists and prefer the entry
    with the most complete data.
    """
    seen: dict[str, Job] = {}
    for job in jobs:
        key = f"{job.title.lower().strip()}|{job.company.lower().strip()}"
        if key not in seen:
            seen[key] = job
            seen[key].extra_sources = [job.source]
        else:
            existing = seen[key]
            if job.source not in existing.extra_sources:
                existing.extra_sources.append(job.source)
            if not existing.salary and job.salary:
                existing.salary = job.salary
            if not existing.stage and job.stage:
                existing.stage = job.stage
            if not existing.yc_batch and job.yc_batch:
                existing.yc_batch = job.yc_batch

    result = list(seen.values())
    log.info(f"[Jobs] Dedup: {len(jobs)} → {len(result)} unique")
    return result


def fetch_all_jobs() -> list[Job]:
    """Run all job scrapers and return a deduplicated combined list."""
    all_jobs: list[Job] = []
    scrapers = [
        wellfound_jobs,
        yc_jobs,
        apify_linkedin_jobs,
        apify_google_jobs,
        greenhouse_lever_jobs,
    ]
    for fn in scrapers:
        try:
            all_jobs.extend(fn())
        except Exception as e:
            log.error(f"[Jobs] Unexpected failure in {fn.__name__}: {e}")
    return deduplicate_jobs(all_jobs)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 — SUPPLEMENTAL SIGNALS
# ═══════════════════════════════════════════════════════════════════════════

REDDIT_KEYWORDS = [
    "positioning", "pmm", "product marketing", "gtm", "go-to-market",
    "icp", "messaging", "launch", "roadmap", "discovery", "prd",
    "competitive", "battlecard", "sales enablement", "pricing",
]
REDDIT_SUBREDDITS = [
    "productmarketing", "ProductManagement", "marketing", "SaaS", "startups",
]


def fetch_reddit_signals() -> list[Signal]:
    """
    Pull top posts from PMM/PM-relevant subreddits via PRAW.
    Filters by score > 50 and keyword match.
    """
    signals: list[Signal] = []
    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET):
        log.warning("[Reddit] No credentials — skipping")
        return []
    try:
        import praw  # type: ignore
        reddit = praw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            user_agent=REDDIT_USER_AGENT,
        )
        for sub_name in REDDIT_SUBREDDITS:
            try:
                for post in reddit.subreddit(sub_name).new(limit=50):
                    posted_at = datetime.fromtimestamp(post.created_utc, tz=timezone.utc)
                    if posted_at < CUTOFF_24H:
                        continue
                    if post.score < 50:
                        continue
                    text = (post.title + " " + (post.selftext or "")).lower()
                    if not any(kw in text for kw in REDDIT_KEYWORDS):
                        continue
                    # Grab the top comment
                    top_comment = ""
                    try:
                        post.comments.replace_more(limit=0)
                        if post.comments:
                            top_comment = post.comments[0].body[:300]
                    except Exception:
                        pass
                    signals.append(Signal(
                        type="reddit",
                        title=post.title,
                        url=f"https://reddit.com{post.permalink}",
                        score=post.score,
                        comment_count=post.num_comments,
                        subreddit=sub_name,
                        top_comment=top_comment,
                        summary=(post.selftext or "")[:300],
                    ))
            except Exception as e:
                log.warning(f"[Reddit] r/{sub_name} failed: {e}")
        signals.sort(key=lambda s: s.score, reverse=True)
        log.info(f"[Reddit] {len(signals)} signals collected")
    except ImportError:
        log.warning("[Reddit] praw not installed — skipping")
    except Exception as e:
        log.warning(f"[Reddit] Init failed: {e}")
    return signals


HN_QUERIES = [
    "product marketing GTM PMM",
    "go-to-market positioning",
    "ICP messaging product launch",
    "competitive intel SaaS",
]


def fetch_hn_signals() -> list[Signal]:
    """Fetch relevant HN stories via the Algolia HN API. Returns top 5 by score."""
    signals: list[Signal] = []
    seen_urls: set[str] = set()
    ts = int(CUTOFF_24H.timestamp())

    for query in HN_QUERIES:
        try:
            resp = requests.get(
                "https://hn.algolia.com/api/v1/search",
                params={
                    "query":          query,
                    "tags":           "story",
                    "numericFilters": f"created_at_i>{ts}",
                    "hitsPerPage":    20,
                },
                timeout=REQUEST_TIMEOUT,
            )
            for hit in resp.json().get("hits", []):
                story_url = (
                    hit.get("url")
                    or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
                )
                if story_url in seen_urls:
                    continue
                seen_urls.add(story_url)
                signals.append(Signal(
                    type="hn",
                    title=hit.get("title", ""),
                    url=story_url,
                    score=hit.get("points", 0) or 0,
                    comment_count=hit.get("num_comments", 0) or 0,
                    summary=(hit.get("story_text") or "")[:300],
                ))
        except Exception as e:
            log.warning(f"[HN] Query '{query}' failed: {e}")

    signals.sort(key=lambda s: s.score, reverse=True)
    result = signals[:5]
    log.info(f"[HN] {len(result)} signals collected")
    return result


_PH_RELEVANT_TAGS = {
    "marketing", "productivity", "sales", "analytics",
    "research", "ai", "saas", "tools",
}


def fetch_product_hunt() -> list[Signal]:
    """Pull recent Product Hunt launches from the RSS feed. Returns top 5 by votes."""
    signals: list[Signal] = []
    try:
        feed = feedparser.parse("https://www.producthunt.com/feed")
        for entry in feed.entries:
            published = parse_published(entry)
            if published is None or published < CUTOFF_24H:
                continue
            title   = getattr(entry, "title", "").strip()
            link    = getattr(entry, "link",  "").strip()
            summary = _strip_html(getattr(entry, "summary", ""))
            text    = (title + " " + summary).lower()
            if not any(tag in text for tag in _PH_RELEVANT_TAGS):
                continue
            votes = 0
            m = re.search(r"(\d+)\s*(?:upvote|vote|point)", summary, re.IGNORECASE)
            if m:
                votes = int(m.group(1))
            signals.append(Signal(
                type="producthunt",
                title=title, url=link,
                score=votes, tagline=summary[:200],
            ))
        signals.sort(key=lambda s: s.score, reverse=True)
        result = signals[:5]
        log.info(f"[ProductHunt] {len(result)} signals collected")
        return result
    except Exception as e:
        log.warning(f"[ProductHunt] Failed: {e}")
        return []


_GITHUB_TOPICS = ["marketing", "saas", "gtm", "crm", "analytics"]


def fetch_github_trending() -> list[Signal]:
    """Fetch popular GitHub repos tagged with marketing/GTM topics. Returns top 5."""
    signals: list[Signal] = []
    seen_repos: set[str] = set()
    headers: dict[str, str] = {}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    for topic in _GITHUB_TOPICS:
        try:
            resp = requests.get(
                "https://api.github.com/search/repositories",
                params={
                    "q":        f"topic:{topic}+is:public",
                    "sort":     "stars",
                    "order":    "desc",
                    "per_page": 5,
                },
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 403:
                log.warning("[GitHub] Rate limited — add GITHUB_TOKEN to .env")
                break
            for repo in resp.json().get("items", []):
                repo_url = repo.get("html_url", "")
                if repo_url in seen_repos:
                    continue
                seen_repos.add(repo_url)
                signals.append(Signal(
                    type="github",
                    title=repo.get("full_name", ""),
                    url=repo_url,
                    score=repo.get("stargazers_count", 0),
                    stars=repo.get("stargazers_count", 0),
                    description=(repo.get("description") or ""),
                    language=(repo.get("language") or ""),
                ))
        except Exception as e:
            log.warning(f"[GitHub] topic '{topic}' failed: {e}")

    signals.sort(key=lambda s: s.stars, reverse=True)
    result = signals[:5]
    log.info(f"[GitHub] {len(result)} signals collected")
    return result


def fetch_all_signals() -> dict[str, list[Signal]]:
    """Run all signal fetchers and return results organized by type.

    Each source is isolated: a missing key or a failing source yields an empty
    list for that source instead of aborting the run, so the digest still builds
    from whatever is available (e.g. with only GEMINI_API_KEY set)."""
    sources = {
        "reddit":      fetch_reddit_signals,
        "hn":          fetch_hn_signals,
        "producthunt": fetch_product_hunt,
        "github":      fetch_github_trending,
    }
    out: dict[str, list[Signal]] = {}
    for key, fn in sources.items():
        try:
            out[key] = fn()
        except Exception as e:
            log.error(f"[Signals] {key} failed: {e}")
            out[key] = []
    return out


# ═══════════════════════════════════════════════════════════════════════════
# DEDUPLICATION & CONTEXT ASSEMBLY
# ═══════════════════════════════════════════════════════════════════════════

def deduplicate_articles(articles: list[Article]) -> list[Article]:
    """Deduplicate articles by URL."""
    seen: set[str] = set()
    unique: list[Article] = []
    for a in articles:
        url = a.url.rstrip("/")
        if url not in seen:
            seen.add(url)
            unique.append(a)
    log.info(f"[Dedup] Articles: {len(articles)} → {len(unique)} unique")
    return unique


def build_gemini_context(
    articles: list[Article],
    jobs:     list[Job],
    signals:  dict[str, list[Signal]],
) -> str:
    """
    Assemble all collected data into a single text string to pass to Gemini.

    Priority order for the 80,000-character cap:
      1. All job listings (always included)
      2. All supplemental signals (Reddit, HN, PH, GitHub)
      3. RSS articles — trim oldest-first, but keep ≥2 per category
    """
    parts: list[str] = []

    # ── JOBS ──────────────────────────────────────────────────────────────
    if jobs:
        lines = ["=== JOB LISTINGS ===\n"]
        for j in jobs:
            all_sources = ([j.source] + j.extra_sources) if j.extra_sources else [j.source]
            lines.append(
                f"[{j.company}] {j.title}\n"
                f"  Location: {j.location} | Stage: {j.stage} | "
                f"Comp: {j.salary or 'Not listed'} | "
                f"Source: {', '.join(set(all_sources))}\n"
                f"  URL: {j.url}\n"
            )
        parts.append("\n".join(lines))

    # ── REDDIT ────────────────────────────────────────────────────────────
    if signals.get("reddit"):
        lines = ["\n=== REDDIT SIGNALS (last 24h) ===\n"]
        for s in signals["reddit"]:
            lines.append(
                f"r/{s.subreddit} | Score: {s.score} | Comments: {s.comment_count}\n"
                f"  {s.title}\n"
                f"  {s.url}\n"
                f"  Top comment: {s.top_comment[:200] if s.top_comment else 'N/A'}\n"
            )
        parts.append("\n".join(lines))

    # ── HACKER NEWS ───────────────────────────────────────────────────────
    if signals.get("hn"):
        lines = ["\n=== HACKER NEWS (last 24h) ===\n"]
        for s in signals["hn"]:
            lines.append(
                f"Score: {s.score} | Comments: {s.comment_count}\n"
                f"  {s.title}\n  {s.url}\n"
            )
        parts.append("\n".join(lines))

    # ── PRODUCT HUNT ──────────────────────────────────────────────────────
    if signals.get("producthunt"):
        lines = ["\n=== PRODUCT HUNT LAUNCHES (last 24h) ===\n"]
        for s in signals["producthunt"]:
            lines.append(f"  {s.title} — {s.tagline}\n  Votes: {s.score} | {s.url}\n")
        parts.append("\n".join(lines))

    # ── GITHUB ────────────────────────────────────────────────────────────
    if signals.get("github"):
        lines = ["\n=== GITHUB TRENDING TOOLS ===\n"]
        for s in signals["github"]:
            lines.append(
                f"  {s.title} ({s.language}) — {s.description}\n"
                f"  Stars: {s.stars} | {s.url}\n"
            )
        parts.append("\n".join(lines))

    fixed_context = "\n".join(parts)
    remaining_chars = MAX_GEMINI_CHARS - len(fixed_context)

    # ── RSS ARTICLES ──────────────────────────────────────────────────────
    # Group by category, newest first within each group
    by_category: dict[str, list[Article]] = {}
    for a in sorted(articles, key=lambda x: x.published, reverse=True):
        by_category.setdefault(a.category, []).append(a)

    # Guarantee at least 2 articles per category
    guaranteed:  list[Article] = []
    optional:    list[Article] = []
    for cat_articles in by_category.values():
        guaranteed.extend(cat_articles[:2])
        optional.extend(cat_articles[2:])

    # Sort optional by authority (desc) then recency (desc)
    optional.sort(key=lambda a: (
        -FEED_AUTHORITY.get(a.category, 1),
        -a.published.timestamp(),
    ))

    def article_to_text(a: Article) -> str:
        body = a.content or a.summary
        return (
            f"[{a.category}] {a.source}\n"
            f"  Title: {a.title}\n"
            f"  Published: {a.published.strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"  URL: {a.url}\n"
            f"  Summary: {body[:600]}\n\n"
        )

    article_parts: list[str] = ["\n=== RSS ARTICLES (last 24h) ===\n"]
    chars_used = 0
    for a in guaranteed:
        t = article_to_text(a)
        chars_used += len(t)
        article_parts.append(t)

    trimmed = 0
    for a in optional:
        if chars_used >= remaining_chars:
            trimmed += 1
            continue
        t = article_to_text(a)
        chars_used += len(t)
        article_parts.append(t)
    if trimmed:
        log.info(f"[Context] Trimmed {trimmed} optional articles to stay under char cap")

    full_context = fixed_context + "\n".join(article_parts)
    log.info(
        f"[Context] Final size: {len(full_context):,} chars "
        f"(cap: {MAX_GEMINI_CHARS:,})"
    )
    return full_context


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4 — GEMINI DIGEST GENERATOR
# ═══════════════════════════════════════════════════════════════════════════

_SYSTEM_PERSONA = """\
You are a sharp, witty friend who actually reads the news — specifically every \
PMM and PM newsletter, community thread, job board, and product launch that matters. \
You worked in product marketing at a Series B SaaS startup and then at a Fortune 500, \
so you understand both worlds and never confuse what matters in each.

You write the way you'd text a smart friend — direct, a little opinionated, \
occasionally funny, always useful. No buzzwords. No "it's important to note." \
No "in today's fast-paced world." If something is mediocre, you say so. \
If something is genuinely worth stealing, you say that too.

Your reader is a senior PMM or PM at a US tech company — smart, time-starved, \
and deeply allergic to fluff. They want to know what actually matters today \
and what they should DO about it.\
"""

_DIGEST_PROMPT = """\
CRITICAL INSTRUCTION: You MUST write ALL 14 sections below, in order, without stopping early. \
Do not truncate, summarize, or skip any section. Every section must be fully written out. \
The 💼 JOBS PULSE section must list the actual job titles and companies from the JOB LISTINGS \
data provided — do not say "no jobs found" if job data is present.

Here is today's collected data — RSS articles, community discussions, job listings, \
product launches, and market signals from the PMM and PM world.

Write a daily digest with EXACTLY these sections in this order:

---

🔥 THE GTM SIGNAL OF THE DAY
The single most important development for PMMs and PMs today. Could be a launch \
worth studying, a positioning shift, a research finding about buyers, or a \
strategic market move.

Write exactly 5 sentences:
- What happened (sharp and factual)
- The product angle most people are missing
- What this means specifically for PMMs
- What this means specifically for PMs
- The one thing to act on or watch in the next 30 days

Don't pick incremental news. Pick the thing that will matter in retrospect.

---

🎯 POSITIONING WATCH
2–3 notable product launches, rebrands, homepage pivots, or messaging shifts \
from the past 24 hours.

For each:
[Company Name] — [One-line description of the move]
- Narrative angle: What story are they telling and to whom?
- ICP signal: Who is clearly the target buyer based on the language they use?
- What's sharp: One specific thing they did well
- What's fuzzy: One thing that's unclear or generic
- Steal this: One specific thing a PMM could adapt for their own product

---

⚔️ COMPETITIVE INTEL CORNER
2–3 competitive moves from B2B SaaS, AI tools, or enterprise software companies.

For each:
[Company] vs [Category or implied competitor]
- What changed (pricing, features, packaging, messaging, distribution)
- Why they did this now — what are they betting on?
- PMM action item: What should a competing PMM do this week?
- Battlecard update needed? Yes/No — and what specifically would change

---

🚀 LAUNCH DISSECTION
Pick ONE product launch from today's data and dissect it like a senior PMM \
writing a post-mortem.

[Product/Feature Name] by [Company]
- Launch tier: T1 / T2 / T3 — and was it positioned right for its actual impact?
- Core message: What was the hero message in their own language?
- GTM channels used: press, social, email, in-product, partner, influencer, community, paid
- Sales readiness signal: Did they publish enablement content alongside it?
- Buyer emotion targeted: Fear / Ambition / Belonging / FOMO / Frustration
- What worked: One concrete thing to replicate
- What's missing: One thing a sharp PMM would have added
- If you were the PMM: One sentence on what you'd do differently

---

📣 WHAT THE PMM/PM COMMUNITY IS SAYING
Top 4–5 posts or threads circulating in the PMM/PM world today.

For each:
[Author or Source] — [Topic or post theme]
- The core argument or insight in 1–2 sentences (paraphrase, don't quote)
- Community reaction: resonating, controversial, or being debated?
- The sharpest counter-take if one exists
- Worth engaging? Yes / Skim / Skip — one-line reason

---

💡 USE CASE OF THE DAY
A specific, reproducible PMM or PM workflow being discussed or shared today.

[Use Case Title — action-oriented]
Category: [Messaging / Competitive Intel / Launch Planning / Sales Enablement / \
Customer Research / Roadmapping / Discovery / GTM Strategy / Content / Pricing]
Role: [PMM / PM / Both]

The workflow:
Step 1: [Concrete first action]
Step 2: [What to do next]
Step 3: [Output or deliverable]

Tools used: [Specific tools — Claude, Notion, Figma, Gong, Wynter, Dovetail, etc.]
Time to complete: [Realistic estimate]
Output you get: [Exactly what artifact or insight this produces]
Why it matters now: [One sentence]

Prompt or template (if AI-assisted):
[Exact copy-paste prompt]

---

📚 LEARNING PICK OF THE DAY
The single best PMM or PM piece published or surfaced today.

[Title] — [Author] — [Source]

What it's about: 2–3 sentences on the core argument
3 actionable takeaways:
1. [Takeaway → specific application]
2. [Takeaway → specific application]
3. [Takeaway → specific application]

Who should read this: [PMM / PM / Both] — at what career stage
Read time: [Estimate]

---

🔬 DISCOVERY & RESEARCH SIGNAL
One notable finding about product discovery, user research, or customer insight.

[Topic or Research Name]
- What was found or demonstrated
- The method used
- How it changes or validates current best practice
- PM action item: What to do differently in your next discovery sprint

---

💼 PMM/PM JOBS PULSE
List the top 6–8 roles from the job data provided.

For each:
[Company] — [Title]
Stage: [Series X / Fortune 500]
Location: [City or Remote-US]
Comp: [Range if available, or 'Not listed']
Why interesting: [One sentence]
Source: [Wellfound / LinkedIn / YC / Greenhouse / Lever]

Then 2 sentences on what today's listings collectively signal about the market.

---

💸 GTM FUNDING & MARKET MOVES
Funding rounds, acquisitions, or strategic partnerships with GTM implications.

For each:
[Company] — [Amount or deal type]
- What they do (one line, no jargon)
- GTM motion: Product-led / Sales-led / Marketing-led / Enterprise / \
Community-led / Partner-led / Vertical SaaS / Usage-based
- What this signals about B2B investment direction
- PMM implication
- PM implication

---

🔧 TOOL DROP FOR PMMS & PMS
New or meaningfully updated tools for PMM or PM workflows.

For each:
[Tool Name] — [One-line description]
Category: [Competitive Intel / Message Testing / Sales Enablement / \
Customer Research / Roadmapping / Discovery / Launch Planning / Analytics]
Best for: [PMM / PM / Both]
Free tier: Yes / No / Trial
Honest take: Genuinely new capability or a feature dressed up as a launch?
Who should try it this week: [one line]

---

⚡ SPEED ROUND
10 rapid-fire updates — stats, frameworks, launches, quotes, or signals.
One punchy line each. Emoji + line. No filler. No categories. Just sharp.

---

🎮 15-MINUTE PMM/PM CHALLENGE
One hands-on skill-building exercise tied to today's digest.

Challenge: [Action-oriented title]
Role focus: [PMM / PM / Both]

The setup: [2–3 sentences]

What to do:
Step 1: [Specific action with tool and URL]
Step 2: [Next step]
Step 3: [Final deliverable]

The exact prompt or template:
[Copy-paste ready]

What good looks like vs. mediocre:
[Calibration guide]

Why this skill matters in the next 6 months: [One sentence]
Time: 15 minutes or less.

---

🧠 EDGE INSIGHT
The non-obvious pattern a top-1% PMM or PM would notice today.
NOT a summary. An original observation derived from today's signals together.

Write 4–5 sentences. Coffee conversation, not LinkedIn post.

---

FORMATTING RULES:
- Use the exact section headers and emojis above
- Never say 'consider doing X' — name the specific tool, prompt, or action
- PMM: 60% / PM: 40%
- If data is thin on a section, say so in one line and move on — no padding
- Total: 1800–2500 words
- Tone: practitioner-to-practitioner, occasionally witty, zero buzzwords
- No sentence starting with "It's important to note"
- No "in today's fast-paced world" or anything like it

TODAY'S DATA:
{context}
"""


def generate_digest(context: str) -> str:
    """
    Send context to Gemini and return the generated digest.

    Uses the current google-genai SDK (google.genai).
    Tries models in preference order. Retries once after 10s on failure.
    Raises RuntimeError if all attempts fail.
    """
    try:
        from google import genai                      # type: ignore
        from google.genai import types as genai_types # type: ignore
    except ImportError:
        raise ImportError(
            "google-genai not installed. Run: pip install google-genai"
        )

    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not set in .env")

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = _DIGEST_PROMPT.format(context=context)

    # Try models newest-first — first one that responds wins.
    # Use versioned names (e.g. gemini-2.5-flash) which work on all account tiers.
    model_names = [
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-2.0-flash-001",
        "gemini-2.0-flash-lite-001",
    ]

    for attempt in range(2):
        for model_name in model_names:
            try:
                log.info(f"[Gemini] Sending to {model_name} (attempt {attempt + 1})...")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=_SYSTEM_PERSONA,
                        temperature=0.7,
                        max_output_tokens=65536,  # gemini-2.5-flash max; all 14 sections need ~20k tokens
                    ),
                )
                digest = response.text
                log.info(f"[Gemini] Received {len(digest):,} chars from {model_name}")
                return digest
            except Exception as e:
                log.warning(f"[Gemini] {model_name} failed (attempt {attempt + 1}): {e}")
                continue

        if attempt == 0:
            log.warning("[Gemini] All models failed — retrying in 10 seconds...")
            time.sleep(10)

    raise RuntimeError(
        "[Gemini] All models failed on both attempts. "
        "Check your GEMINI_API_KEY and network connection."
    )


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5 — HTML EMAIL FORMATTER
# ═══════════════════════════════════════════════════════════════════════════

# Maps the leading emoji of each section to its accent color and key name.
SECTION_CONFIG: dict[str, dict] = {
    "🔥": {"color": "#FF6B35", "key": "gtm_signal"},
    "🎯": {"color": "#5B5BD6", "key": "positioning"},
    "⚔️": {"color": "#DC2626", "key": "competitive"},
    "🚀": {"color": "#0EA5E9", "key": "launch"},
    "📣": {"color": "#7C3AED", "key": "community"},
    "💡": {"color": "#059669", "key": "use_case"},
    "📚": {"color": "#0891B2", "key": "learning"},
    "🔬": {"color": "#16A34A", "key": "discovery"},
    "💼": {"color": "#D97706", "key": "jobs"},
    "💸": {"color": "#15803D", "key": "funding"},
    "🔧": {"color": "#9333EA", "key": "tools"},
    "⚡": {"color": "#374151", "key": "speed_round"},
    "🎮": {"color": "#DC2626", "key": "challenge"},
    "🧠": {"color": "#4338CA", "key": "edge_insight"},
}


def _inline_md(text: str) -> str:
    """
    Convert inline Markdown (bold, italic, links) to HTML.
    Input text should already have HTML special chars escaped.
    """
    text = html_lib.escape(text)
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__(.+?)__",     r"<strong>\1</strong>", text)
    # Italic
    text = re.sub(r"\*([^*]+?)\*",  r"<em>\1</em>", text)
    text = re.sub(r"_([^_]+?)_",    r"<em>\1</em>", text)
    # Markdown links
    text = re.sub(
        r"\[(.+?)\]\((https?://[^\)]+)\)",
        r'<a href="\2" style="color:#5B5BD6;text-decoration:none;">\1</a>',
        text,
    )
    # Bare URLs
    text = re.sub(
        r"(?<![\"'=])(https?://[^\s<>\"']+)",
        r'<a href="\1" style="color:#5B5BD6;text-decoration:none;font-size:12px;">\1</a>',
        text,
    )
    return text


def _md_to_html(text: str) -> str:
    """
    Convert a block of Markdown-ish text to HTML suitable for email.
    Handles: paragraphs, bullet lists, numbered lists, inline formatting.
    """
    lines = text.split("\n")
    out: list[str] = []
    in_ul = False

    for line in lines:
        stripped = line.strip()

        if not stripped:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append('<div style="height:8px;"></div>')
            continue

        # Bullet list item
        if re.match(r"^[-*•]\s", stripped):
            if not in_ul:
                out.append('<ul style="margin:8px 0;padding-left:20px;">')
                in_ul = True
            content = _inline_md(stripped[2:])
            out.append(
                f'<li style="margin:4px 0;color:#374151;line-height:1.7;">'
                f"{content}</li>"
            )
            continue

        # Close list before non-list content
        if in_ul:
            out.append("</ul>")
            in_ul = False

        # Numbered list item
        m = re.match(r"^(\d+)[.)]\s+(.*)", stripped)
        if m:
            content = _inline_md(m.group(2))
            out.append(
                f'<div style="margin:6px 0;padding:8px 12px;background:#F9FAFB;'
                f'border-radius:4px;font-size:14px;">'
                f'<strong style="color:#374151;">{m.group(1)}.</strong> {content}</div>'
            )
            continue

        # Regular paragraph
        content = _inline_md(stripped)
        out.append(
            f'<p style="margin:8px 0;color:#374151;line-height:1.7;">{content}</p>'
        )

    if in_ul:
        out.append("</ul>")

    return "\n".join(out)


def _build_card(emoji: str, header: str, body: str) -> str:
    """
    Build the HTML for one digest section card.
    Applies special styling for challenge, edge insight, and speed round cards.
    """
    cfg   = SECTION_CONFIG.get(emoji, {"color": "#374151", "key": "default"})
    color = cfg["color"]
    key   = cfg["key"]
    full_header = html_lib.escape(f"{emoji} {header}")

    # ── 🎮 Daily Challenge — full-width red card ───────────────────────────
    if key == "challenge":
        body_html = _md_to_html(body)
        return f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
<tr><td style="background:#FFF5F5;border:1px solid #FECACA;border-left:4px solid {color};
               border-radius:6px;padding:20px 24px;">
  <div style="font-size:13px;font-weight:700;letter-spacing:.05em;color:{color};
              text-transform:uppercase;margin-bottom:12px;">{full_header}</div>
  <div style="font-size:15px;line-height:1.7;color:#1A1A1A;">{body_html}</div>
  <div style="text-align:center;margin-top:20px;">
    <a href="#" style="display:inline-block;background:#1A1A1A;color:#fff;
       text-decoration:none;padding:10px 28px;border-radius:6px;
       font-size:14px;font-weight:700;letter-spacing:.03em;">TRY THIS →</a>
  </div>
</td></tr>
</table>"""

    # ── 🧠 Edge Insight — purple card, italic text ────────────────────────
    if key == "edge_insight":
        body_html = _md_to_html(body)
        return f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
<tr><td style="background:#F5F3FF;border-left:4px solid {color};border-radius:6px;
               padding:20px 24px;overflow:hidden;position:relative;">
  <div style="font-size:13px;font-weight:700;letter-spacing:.05em;color:{color};
              text-transform:uppercase;margin-bottom:12px;">{full_header}</div>
  <div style="font-size:15px;line-height:1.8;color:#1A1A1A;font-style:italic;">
    {body_html}
  </div>
  <div style="position:absolute;right:16px;bottom:-8px;font-size:80px;
              opacity:.05;line-height:1;pointer-events:none;">🧠</div>
</td></tr>
</table>"""

    # ── ⚡ Speed Round — alternating row list ─────────────────────────────
    if key == "speed_round":
        speed_lines = [l.strip() for l in body.split("\n") if l.strip()]
        rows = []
        for i, line in enumerate(speed_lines):
            bg   = "#FAFAFA" if i % 2 == 0 else "#FFFFFF"
            rows.append(
                f'<div style="padding:8px 12px;background:{bg};border-left:3px solid {color};'
                f'margin-bottom:3px;font-size:14px;color:#374151;line-height:1.6;">'
                f"{_inline_md(line)}</div>"
            )
        return f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
<tr><td style="background:#fff;border-left:4px solid {color};border-radius:6px;
               padding:20px 24px;box-shadow:0 1px 3px rgba(0,0,0,.04);">
  <div style="font-size:13px;font-weight:700;letter-spacing:.05em;color:{color};
              text-transform:uppercase;margin-bottom:12px;">{full_header}</div>
  {"".join(rows)}
</td></tr>
</table>"""

    # ── Default card ───────────────────────────────────────────────────────
    body_html = _md_to_html(body)
    return f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
<tr><td style="background:#fff;border-left:4px solid {color};border-radius:6px;
               padding:20px 24px;box-shadow:0 1px 3px rgba(0,0,0,.04);">
  <div style="font-size:13px;font-weight:700;letter-spacing:.05em;color:{color};
              text-transform:uppercase;margin-bottom:12px;">{full_header}</div>
  <div style="font-size:15px;line-height:1.7;color:#1A1A1A;">{body_html}</div>
</td></tr>
</table>"""


def _parse_sections(digest_text: str) -> list[tuple[str, str, str]]:
    """
    Split Gemini digest text into (emoji, header_title, body) tuples.
    Detects sections by lines that start with a known section emoji.
    """
    emoji_list = list(SECTION_CONFIG.keys())
    # Build pattern that matches any section-opener emoji at start of line
    emoji_re = re.compile(
        r"^(" + "|".join(re.escape(e) for e in emoji_list) + r")\s+(.+)$",
        re.MULTILINE,
    )
    matches  = list(emoji_re.finditer(digest_text))
    sections = []
    for i, match in enumerate(matches):
        emoji  = match.group(1)
        header = match.group(2).strip()
        start  = match.end()
        end    = matches[i + 1].start() if i + 1 < len(matches) else len(digest_text)
        body   = digest_text[start:end].strip()
        # Strip horizontal rule separators Gemini might include
        body = re.sub(r"^[-─=─]{3,}\s*", "", body).strip()
        body = re.sub(r"\s*[-─=─]{3,}$", "", body).strip()
        sections.append((emoji, header, body))
    return sections


def format_html_email(digest_text: str, _jobs: list[Job]) -> str:
    """
    Convert the Gemini digest text into a complete, responsive HTML email.
    Applies the Notion-inspired card design with per-section accent colors.
    """
    today_str    = NOW_UTC.strftime("%A, %B %-d, %Y")
    generated_at = NOW_UTC.strftime("%Y-%m-%d %H:%M UTC")

    sections = _parse_sections(digest_text)
    if sections:
        cards_html = "\n".join(_build_card(e, h, b) for e, h, b in sections)
    else:
        # Fallback: render the whole digest as a single card if parsing found nothing
        log.warning("[HTML] Section parsing found 0 sections — rendering as plain fallback")
        cards_html = (
            '<div style="background:#fff;border-radius:6px;padding:24px;">'
            + _md_to_html(digest_text)
            + "</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <title>PMM/PM Insider Daily — {today_str}</title>
  <style>
    @media only screen and (max-width:680px){{
      .outer-wrap{{padding:8px !important;}}
      .inner-wrap{{width:100% !important;}}
      .header-cell{{padding:20px 16px !important;}}
      .body-cell{{padding:16px 12px !important;}}
    }}
    body{{margin:0;padding:0;background:#F7F7F5;}}
    *{{box-sizing:border-box;}}
    a{{color:#5B5BD6;}}
  </style>
</head>
<body style="margin:0;padding:0;background:#F7F7F5;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">

<table class="outer-wrap" width="100%" cellpadding="0" cellspacing="0"
       style="background:#F7F7F5;padding:24px 16px;">
<tr><td align="center">

  <table class="inner-wrap" width="680" cellpadding="0" cellspacing="0"
         style="max-width:680px;width:100%;">

    <!-- HEADER -->
    <tr>
      <td class="header-cell"
          style="background:#1A1A1A;padding:28px 32px;border-radius:8px 8px 0 0;text-align:center;">
        <div style="font-size:22px;font-weight:700;color:#fff;letter-spacing:-.02em;margin-bottom:6px;">
          📋 PMM/PM INSIDER DAILY
        </div>
        <div style="font-size:13px;color:#888;margin-bottom:4px;">{today_str}</div>
        <div style="font-size:12px;color:#AAA;">
          What actually matters today for PMMs and PMs
        </div>
      </td>
    </tr>

    <!-- BODY -->
    <tr>
      <td class="body-cell" style="background:#F7F7F5;padding:20px 24px;">
        {cards_html}
      </td>
    </tr>

    <!-- FOOTER -->
    <tr>
      <td style="background:#1A1A1A;padding:24px 32px;border-radius:0 0 8px 8px;text-align:center;">
        <div style="font-size:13px;color:#888;margin-bottom:8px;">
          Built for people who want to stay in the top 1% of PMMs and PMs
        </div>
        <div style="font-size:12px;color:#555;margin-bottom:8px;">
          Generated at {generated_at}
        </div>
        <a href="#unsubscribe" style="font-size:12px;color:#666;text-decoration:underline;">
          Unsubscribe
        </a>
      </td>
    </tr>

  </table>

</td></tr>
</table>

</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6 — GMAIL SMTP SENDER
# ═══════════════════════════════════════════════════════════════════════════

def _save_html_fallback(html_content: str) -> None:
    """Save the HTML to pmm_digest_output.html when email send fails."""
    output = Path(__file__).parent / "pmm_digest_output.html"
    try:
        output.write_text(html_content, encoding="utf-8")
        log.info(f"[Fallback] Digest saved locally → {output}")
    except Exception as e:
        log.error(f"[Fallback] Could not write file: {e}")


def send_email(html_content: str) -> bool:
    """
    Send the HTML digest via Gmail SMTP with STARTTLS (port 587).

    Requires GMAIL_ADDRESS and GMAIL_APP_PASSWORD (a Google App Password,
    not your regular account password — generate one at myaccount.google.com
    under Security → App Passwords).

    On failure, saves the HTML to pmm_digest_output.html so content isn't lost.
    Returns True on success, False on failure.
    """
    if not all([GMAIL_ADDRESS, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]):
        log.error(
            "[Email] Missing one or more of: "
            "GMAIL_ADDRESS, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL"
        )
        _save_html_fallback(html_content)
        return False

    today_str = NOW_UTC.strftime("%B %-d, %Y")
    subject   = f"📋 PMM/PM Insider Daily — {today_str}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        log.info("[Email] Connecting to smtp.gmail.com:587...")
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())
        log.info(f"[Email] Sent successfully to {RECIPIENT_EMAIL}")
        return True
    except smtplib.SMTPAuthenticationError:
        log.error(
            "[Email] Authentication failed. "
            "Make sure GMAIL_APP_PASSWORD is a Google App Password "
            "(not your regular Gmail password)."
        )
        _save_html_fallback(html_content)
        return False
    except Exception as e:
        log.error(f"[Email] Send failed: {e}")
        _save_html_fallback(html_content)
        return False


# ═══════════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════

def _print_summary(stats: dict) -> None:
    """Print a human-readable run summary to stdout."""
    width = 58
    print("\n" + "═" * width)
    print("  PMM/PM INSIDER DAILY — RUN SUMMARY")
    print("═" * width)
    print(f"  RSS feeds fetched : {stats['feeds_fetched']} "
          f"(failed: {stats['feeds_failed']})")
    print(f"  Articles          : {stats['articles_raw']} raw → "
          f"{stats['articles_deduped']} unique")
    print(f"  Jobs              : {stats['jobs_raw']} raw → "
          f"{stats['jobs_deduped']} unique")
    print(f"  Reddit signals    : {stats['reddit_signals']}")
    print(f"  HN signals        : {stats['hn_signals']}")
    print(f"  Product Hunt      : {stats['ph_signals']}")
    print(f"  GitHub repos      : {stats['github_signals']}")
    print(f"  Context size      : {stats['context_chars']:,} chars")
    print(f"  Digest size       : {stats['digest_chars']:,} chars")
    print(f"  Email sent        : {'✓  Yes' if stats['email_sent'] else '✗  No (check logs)'}")
    print("═" * width + "\n")


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
    log.info(f"[Web] JSON written -> {out}")


def main() -> None:
    stats = {
        "feeds_fetched":    0, "feeds_failed": 0,
        "articles_raw":     0, "articles_deduped": 0,
        "jobs_raw":         0, "jobs_deduped": 0,
        "reddit_signals":   0, "hn_signals": 0,
        "ph_signals":       0, "github_signals": 0,
        "context_chars":    0, "digest_chars": 0,
        "email_sent":       False,
    }

    log.info("=" * 58)
    log.info("PMM/PM Insider Daily — starting run")
    log.info(f"Time: {NOW_UTC.strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 58)

    # Step 1 — Load Fortune 500 list
    load_fortune500()

    # Step 2 — Fetch RSS feeds
    log.info("[1/5] Fetching RSS feeds...")
    articles_raw = fetch_all_rss_feeds()
    stats["articles_raw"] = len(articles_raw)
    total_feeds = sum(len(v) for v in RSS_FEEDS.values())
    unique_sources = len({a.source for a in articles_raw})
    stats["feeds_fetched"] = unique_sources
    stats["feeds_failed"]  = total_feeds - unique_sources
    articles = deduplicate_articles(articles_raw)
    stats["articles_deduped"] = len(articles)

    # Step 3 — Scrape jobs
    log.info("[2/5] Scraping job listings...")
    jobs_raw = fetch_all_jobs()
    stats["jobs_raw"]    = len(jobs_raw)
    stats["jobs_deduped"] = len(jobs_raw)   # already deduped inside fetch_all_jobs

    # Step 4 — Fetch supplemental signals
    log.info("[3/5] Fetching supplemental signals...")
    signals = fetch_all_signals()
    stats["reddit_signals"] = len(signals.get("reddit",      []))
    stats["hn_signals"]     = len(signals.get("hn",          []))
    stats["ph_signals"]     = len(signals.get("producthunt", []))
    stats["github_signals"] = len(signals.get("github",      []))

    # Step 5 — Build Gemini context
    log.info("[4/5] Building Gemini context (enforcing 80k char cap)...")
    context = build_gemini_context(articles, jobs_raw, signals)
    stats["context_chars"] = len(context)

    # Step 6 — Generate digest
    log.info("[5/5] Generating digest via Gemini...")
    try:
        digest_text = generate_digest(context)
        stats["digest_chars"] = len(digest_text)
    except Exception as e:
        log.error(f"[Gemini] Fatal: {e}")
        _print_summary(stats)
        sys.exit(1)

    # Persist for the website build (runs regardless of email setting)
    write_web_json("pmm", "PMM / PM", digest_text)

    # Step 7 — Format HTML (email for this feed is OFF by default; web-only)
    log.info("[6/5] Formatting HTML...")
    html_email = format_html_email(digest_text, jobs_raw)

    # Always save a local copy (useful for debugging/preview)
    _save_html_fallback(html_email)

    # Send via Gmail SMTP only if explicitly re-enabled
    if os.getenv("SEND_EMAIL", "false").lower() == "true":
        stats["email_sent"] = send_email(html_email)
    else:
        log.info("[Email] Disabled (SEND_EMAIL != true) — web-only feed.")

    _print_summary(stats)


if __name__ == "__main__":
    main()
