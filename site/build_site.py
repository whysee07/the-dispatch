#!/usr/bin/env python3
"""
build_site.py
═══════════════════════════════════════════════════════════════════════════
Static-site generator for The Dispatch.

Reads every feed's JSON archive from  data/<feed>/<YYYY-MM-DD>.json
and renders a three-tab newsletter site (the "Loud" design) into
site/public/ :

    public/
      index.html                      ← latest issue of each feed, tabbed
      issues/<feed>/<date>.html        ← one page per past issue
      assets/style.css
      .nojekyll

No network, no dependencies beyond the standard library. Presentation lives
entirely here, so the generator scripts only need to emit raw digest text.
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import shutil
from datetime import date, datetime, timezone
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "site" / "public"
ASSETS_SRC = ROOT / "site" / "assets"          # committed assets (og images, etc.) copied into output

SITE_TITLE = "The Khat-TING"
# Longer, keyword-rich <title> for the homepage (SERP sweet spot ~55 chars).
SITE_TITLE_SEO = "The Khat-TING — Daily AI, Product Marketing & World News"
SITE_TAGLINE = "A daily intelligence briefing, compiled by Yash and one very caffeinated algorithm."
# Absolute base URL (no trailing slash). Override with SITE_URL env for a custom domain.
SITE_URL = os.environ.get("SITE_URL", "https://whysee07.github.io/the-dispatch").rstrip("/")
SITE_DESC = "A daily three-channel intelligence briefing — Daily Brief, AI Insider, and PMM / PM — auto-compiled every morning. Read today's issue."

# ── FEED CONFIG ──────────────────────────────────────────────────────────────
# `mode` selects how raw_text is split into sections:
#   titles   — split on a fixed list of plain section titles
#   emoji    — split on lines that open with a known section emoji
#   markdown — split on markdown / bold / emoji headers (generic)

PMM_EMOJIS = ["🔥", "🎯", "⚔️", "🚀", "📣", "💡", "📚", "🔬", "💼", "💸", "🔧", "⚡", "🎮", "🧠"]

FEEDS = [
    {
        "id": "brief",
        "nav": "Daily Brief",
        "name": "Daily Brief",
        "headline": 'The world, before your <span class="mark">coffee\'s</span> cold.',
        "dek": "Money, power, tech and culture — one sharp morning read.",
        "about": "<b>Daily Brief</b> scans finance, geopolitics, tech and culture each "
                 "morning, then writes it up with actual personality.",
        "pipe": ["RSS ×16", "Gemini 2.5", "07:00 ET"],
        "mode": "titles",
        "sections": ["Money Talk", "World Lore", "Tech Tea", "Creator Szn", "Supply Lines", "Speed Round"],
    },
    {
        "id": "ai",
        "nav": "AI Insider",
        "name": "AI Insider",
        "headline": 'What actually shipped in AI <span class="mark">today</span>.',
        "dek": "Models, research and tooling — minus the hype.",
        "about": "<b>AI Insider</b> pulls from RSS, GitHub and Hacker News. "
                 "The one channel that <b>still lands in your inbox.</b>",
        "pipe": ["RSS", "GitHub", "HN", "+ email"],
        "mode": "markdown",
    },
    {
        "id": "pmm",
        "nav": "PMM / PM",
        "name": "PMM / PM",
        "headline": 'Today\'s GTM signal, and <span class="mark">who\'s hiring</span>.',
        "dek": "Product-marketing intelligence, compiled daily.",
        "about": "<b>PMM / PM</b> aggregates 30+ sources, scrapes five job boards, and "
                 "pulls community signals into one daily GTM read.",
        "pipe": ["RSS ×30", "Jobs ×5", "Reddit", "HN", "PH"],
        "mode": "emoji",
    },
]
FEED_BY_ID = {f["id"]: f for f in FEEDS}

# ── CULTURE ("The Weekend") ───────────────────────────────────────────────────
# Three structured sub-sections under the top-level "The Weekend" switch.
CULTURE_SECTIONS = [
    {"id": "music", "nav": "Music",
     "headline": 'What\'s <span class="mark">worth hearing</span> this week.',
     "dek": "Industry moves, new tracks and the charts — world and India."},
    {"id": "movies", "nav": "Movies",
     "headline": 'What to <span class="mark">watch</span> tonight.',
     "dek": "One pick for tonight, what's new, and the one worth your evening."},
    {"id": "photo", "nav": "Photography",
     "headline": 'A week in <span class="mark">pictures</span>.',
     "dek": "A photo of the day, the day's news photo, and the weekend gallery."},
]
CULTURE_IDS = [s["id"] for s in CULTURE_SECTIONS]

# ── LOAD ─────────────────────────────────────────────────────────────────────

def load_issues(feed_id: str) -> list[dict]:
    """Return a feed's issues, newest first, each with an added `_num` (chronological)."""
    folder = DATA_DIR / feed_id
    issues: list[dict] = []
    if folder.exists():
        for p in folder.glob("*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
                if rec.get("raw_text", "").strip():
                    issues.append(rec)
            except (json.JSONDecodeError, OSError):
                continue
    issues.sort(key=lambda r: r.get("date", ""))
    for i, rec in enumerate(issues, start=1):
        rec["_num"] = i
    issues.reverse()  # newest first
    return issues

# ── PARSE raw_text → sections ────────────────────────────────────────────────

def _split_titles(text: str, titles: list[str]) -> list[tuple[str, str, str]]:
    pattern = r"(?mi)^\s*(?:#+\s*)?(" + "|".join(re.escape(t) for t in titles) + r")\s*[:\n]"
    parts = re.split(pattern, text)
    out: list[tuple[str, str, str]] = []
    i = 1
    while i < len(parts) - 1:
        label = parts[i].strip()
        body = parts[i + 1].strip()
        if body:
            out.append(("", label, body))
        i += 2
    return out


def _split_emoji(text: str, emojis: list[str]) -> list[tuple[str, str, str]]:
    emoji_re = re.compile(r"^(" + "|".join(re.escape(e) for e in emojis) + r")\s*(.+)$", re.M)
    matches = list(emoji_re.finditer(text))
    out: list[tuple[str, str, str]] = []
    for idx, m in enumerate(matches):
        emoji = m.group(1)
        label = m.group(2).strip().strip("*").strip()
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        body = re.sub(r"^[-–—=─]{3,}\s*", "", body).strip()
        body = re.sub(r"\s*[-–—=─]{3,}$", "", body).strip()
        if body or label:
            out.append((emoji, label, body))
    return out


def _is_header(line: str) -> str | None:
    """Return the header text if this line reads as a section header, else None."""
    s = line.strip()
    if not s:
        return None
    m = re.match(r"^#{1,4}\s+(.+?)\s*#*$", s)
    if m:
        return m.group(1).strip().strip("*").strip()
    m = re.match(r"^\*\*(.+?)\*\*:?\s*$", s)
    if m and len(m.group(1)) < 70:
        return m.group(1).strip()
    # leading emoji / symbol + short title
    m = re.match(r"^([^\w\s#>*\-•]+)\s*(.{2,70})$", s)
    if m and not s.endswith((".", "!", "?", ",", ";")) and len(s) < 72:
        return (m.group(1) + " " + m.group(2).strip().strip("*")).strip()
    return None


def _split_markdown(text: str) -> list[tuple[str, str, str]]:
    lines = text.split("\n")
    out: list[tuple[str, str, str]] = []
    cur_label: str | None = None
    buf: list[str] = []

    def flush():
        body = "\n".join(buf).strip()
        if cur_label is not None or body:
            out.append(("", cur_label or "", body))

    for raw in lines:
        if raw.strip() == "---":
            continue
        hdr = _is_header(raw)
        if hdr is not None:
            flush()
            cur_label = hdr
            buf = []
        else:
            buf.append(raw)
    flush()
    return [s for s in out if s[1] or s[2]]


def parse_sections(feed: dict, raw_text: str) -> list[tuple[str, str, str]]:
    """-> list of (emoji, label, body_text)."""
    mode = feed["mode"]
    if mode == "titles":
        secs = _split_titles(raw_text, feed["sections"])
    elif mode == "emoji":
        secs = _split_emoji(raw_text, PMM_EMOJIS)
    else:
        secs = _split_markdown(raw_text)
    if not secs:  # fallback: render whole thing as one unlabelled section
        secs = [("", "", raw_text.strip())]
    return secs

# ── RENDER body text → clean HTML ────────────────────────────────────────────

def _inline(text: str) -> str:
    t = html_lib.escape(text)
    t = re.sub(r"\[(.+?)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"__(.+?)__", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", r"<em>\1</em>", t)
    t = re.sub(r"`([^`]+?)`", r"<code>\1</code>", t)
    t = re.sub(r"(?<![\">=])(https?://[^\s<>\")]+)", r'<a href="\1" target="_blank" rel="noopener">\1</a>', t)
    return t


_BULLET = re.compile(r"^\s*[-–—*•]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_SUBHEAD_ONLY = re.compile(r"^\s*\*\*([^*]+?)\*\*:?\s*$")
_BOLD_LEAD = re.compile(r"^\s*\*\*(.+?)\*\*\s*[:.—–-]?\s*(.*)$", re.S)
_FIRST_SENT = re.compile(r"^(.{0,200}?[.!?…])\s+(\S.*)$", re.S)


def _split_item(text: str) -> tuple[str, str]:
    """Split one item into (headline, detail). Prefer a bold lead-in; else first sentence."""
    text = text.strip()
    m = _BOLD_LEAD.match(text)
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2).strip()
    m = _FIRST_SENT.match(text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return text, ""


def _render_detail(detail: str) -> str:
    """Detail text -> paragraphs (blank-line separated; soft newlines joined)."""
    out = []
    for para in re.split(r"\n\s*\n", detail.strip()):
        para = re.sub(r"\s*\n\s*", " ", para.strip())
        if para:
            out.append(f"<p>{_inline(para)}</p>")
    return "".join(out)


def _items_from_body(body: str) -> list[tuple]:
    """Group a section body into ('sub', text) headings and ('item', text) units.

    Bullet/numbered lines start items; following non-bullet lines are that item's
    detail. If a section has no bullets, each blank-line-separated paragraph is an item."""
    lines = body.split("\n")
    has_bullets = any(_BULLET.match(l) or _NUMBERED.match(l) for l in lines)
    items: list[tuple] = []
    if has_bullets:
        cur = None
        for ln in lines:
            s = ln.strip()
            mb = _BULLET.match(ln) or _NUMBERED.match(ln)
            if mb:
                if cur is not None:
                    items.append(("item", cur))
                cur = mb.group(1).strip()
            elif not s:
                if cur is not None:
                    cur += "\n"
            elif _SUBHEAD_ONLY.match(s):
                if cur is not None:
                    items.append(("item", cur)); cur = None
                items.append(("sub", _SUBHEAD_ONLY.match(s).group(1).strip()))
            else:
                cur = (cur + "\n" + s) if cur is not None else s
        if cur is not None:
            items.append(("item", cur))
    else:
        for para in re.split(r"\n\s*\n", body.strip()):
            p = para.strip()
            if not p:
                continue
            sm = _SUBHEAD_ONLY.match(p)
            items.append(("sub", sm.group(1).strip()) if sm else ("item", p))
    return items


_CITE = re.compile(r"\s*\[S(\d+)\]")


def _pop_cites(text: str) -> tuple[str, list[int]]:
    """Pull [S#] markers out of text; return (clean_text, [ids])."""
    ids = [int(n) for n in _CITE.findall(text)]
    clean = _CITE.sub("", text)
    return clean.strip(), ids


def _render_source_links(ids: list[int], smap: dict) -> str:
    """Render a small 'source' link row for the cited sources that have URLs."""
    seen, links = set(), []
    for i in ids:
        if i in seen or i not in smap:
            continue
        seen.add(i)
        s = smap[i]
        if not s.get("url"):
            continue
        label = html_lib.escape(s.get("source") or "source")
        links.append(f'<a class="src-link" href="{html_lib.escape(s["url"])}" '
                     f'target="_blank" rel="noopener">{label} ↗</a>')
    if not links:
        return ""
    return f'<div class="src-links">{"".join(links)}</div>'


def render_body(body: str, speed: bool = False, smap: dict | None = None) -> str:
    """Render a section body. Normal sections become collapsible items
    (scannable headline, expand for detail); Speed Round stays a flat list."""
    smap = smap or {}
    if speed:
        raw = [ln.strip() for ln in body.split("\n")]
        raw = [_pop_cites(re.sub(r"^\s*(?:[-–—*•]|\d+[.)])\s*", "", it))[0] for it in raw if it.strip()]
        raw = [it for it in raw if it]
        if not raw:
            return ""
        lis = "".join(f"<li>{_inline(it)}</li>" for it in raw)
        return f'<ul class="speed">{lis}</ul>'

    parts: list[str] = []
    for item in _items_from_body(body):
        if item[0] == "sub":
            clean, _ = _pop_cites(item[1])
            parts.append(f'<p class="subhead">{_inline(clean)}</p>')
            continue
        clean, ids = _pop_cites(item[1])
        headline, detail = _split_item(clean)
        detail_html = _render_detail(detail)
        src_html = _render_source_links(ids, smap)
        if detail_html or src_html:
            parts.append(
                f'<details class="item"><summary>{_inline(headline)}</summary>'
                f'<div class="item-body">{detail_html}{src_html}</div></details>'
            )
        else:
            parts.append(f'<div class="item item--flat">{_inline(headline)}</div>')
    return "\n".join(parts)


def render_sections(feed: dict, raw_text: str, sources: list[dict] | None = None) -> str:
    smap = {s["id"]: s for s in (sources or []) if "id" in s}
    secs = parse_sections(feed, raw_text)
    out: list[str] = []
    for emoji, label, body in secs:
        speed = "speed round" in label.lower()
        body_html = render_body(body, speed=speed, smap=smap)
        if not body_html.strip():
            continue
        label_html = ""
        if label:
            lead = f'<span class="se">{html_lib.escape(emoji)}</span> ' if emoji else ""
            label_html = f'<h3 class="sectlabel">{lead}{html_lib.escape(label)}</h3>'
        out.append(f'<section class="section">{label_html}<div class="body">{body_html}</div></section>')
    if not out:
        out.append('<section class="section"><div class="body"><p>This issue is being compiled.</p></div></section>')
    return "\n".join(out)

# ── CULTURE RENDERING ─────────────────────────────────────────────────────────

def load_culture(section_id: str) -> list[dict]:
    folder = DATA_DIR / "culture" / section_id
    issues: list[dict] = []
    if folder.exists():
        for p in folder.glob("*.json"):
            try:
                issues.append(json.loads(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
    issues.sort(key=lambda r: r.get("date", ""))
    for i, rec in enumerate(issues, start=1):
        rec["_num"] = i
    issues.reverse()
    return issues


def _esc(s) -> str:
    return html_lib.escape(str(s if s is not None else ""))


def _geo(geo: str) -> str:
    if geo == "india":
        return '<span class="geo in">India</span>'
    if geo == "world":
        return '<span class="geo w">World</span>'
    return ""


def _fill(url: str, alt: str = "") -> str:
    return f'<img src="{_esc(url)}" alt="{_esc(alt)}" loading="lazy">' if url else ""


def _sect(emoji: str, label: str, body_html: str) -> str:
    return (f'<section class="section"><h3 class="sectlabel"><span class="se">{emoji}</span> '
            f'{_esc(label)}</h3><div class="body">{body_html}</div></section>')


def _culture_music(d: dict) -> str:
    out = []
    news = d.get("news") or []
    if news:
        rows = "".join(
            f'<div class="cn"><a class="cn-t" href="{_esc(n.get("url"))}" target="_blank" '
            f'rel="noopener">{_esc(n.get("title"))}</a><span class="cn-s">{_esc(n.get("source"))}'
            f'{(" · " + _esc(n.get("age"))) if n.get("age") else ""}</span></div>'
            for n in news)
        out.append(_sect("🎧", "On the record", f'<div class="cnews">{rows}</div>'))
    tracks = d.get("tracks") or []
    if tracks:
        rows = ""
        for t in tracks:
            title = _esc(t.get("title"))
            if t.get("url"):
                title = f'<a href="{_esc(t["url"])}" target="_blank" rel="noopener">{title}</a>'
            rows += (f'<div class="ctrk"><div class="art">{_fill(t.get("art",""), t.get("title",""))}</div>'
                     f'<div><h4>{title}</h4><div class="by">{_esc(t.get("artist"))}</div>'
                     f'<div class="why">{_esc(t.get("why"))}</div>'
                     f'<div class="tags"><span class="pill">{_esc(t.get("genre"))}</span>'
                     f'{_geo(t.get("geo",""))}</div></div></div>')
        out.append(_sect("💿", "New tracks", f'<div class="ctracks">{rows}</div>'))
    charts = d.get("charts") or {}
    if charts.get("global") or charts.get("india"):
        def col(title, items):
            lis = "".join(f'<li>{_esc(i.get("title"))}<span class="by">{_esc(i.get("artist"))}</span></li>'
                          for i in (items or []))
            return f'<div class="cchart"><h5>{title}</h5><ol>{lis}</ol></div>'
        body = (f'<div class="ccharts">{col("Global Top 5", charts.get("global"))}'
                f'{col("India Top 5", charts.get("india"))}</div>')
        out.append(_sect("📈", "Top of the list", body))
    return "\n".join(out)


def _culture_movies(d: dict) -> str:
    out = []
    t = d.get("tonight") or {}
    if t:
        chips = "".join(f'<span class="pill">{_esc(w)}</span>' for w in (t.get("where") or []))
        chips += _geo(t.get("geo", ""))
        body = (f'<div class="cpick"><div class="poster">{_fill(t.get("poster",""), t.get("title",""))}</div>'
                f'<div><h4>{_esc(t.get("title"))}</h4><div class="m">{_esc(t.get("meta"))}</div>'
                f'<p>{_esc(t.get("why"))}</p><div class="chips">{chips}</div></div></div>')
        out.append(_sect("🍿", "Tonight's pick", body))
    rel = d.get("releases") or []
    if rel:
        rows = ""
        for r in rel:
            rows += (f'<div class="crl"><div class="crl-t"><b>{_esc(r.get("title"))}</b> — '
                     f'{_esc(r.get("take"))} {_geo(r.get("geo",""))}</div>'
                     f'<div class="score">{_esc(r.get("score"))}</div></div>')
        out.append(_sect("🎬", "New &amp; notable", f'<div class="crel">{rows}</div>'))
    c = d.get("critic") or {}
    if c:
        out.append(_sect("⭐", "Critic's pick",
                   f'<div class="ccritic"><h4>{_esc(c.get("title"))}</h4><p>{_esc(c.get("blurb"))}</p></div>'))
    return "\n".join(out)


def _culture_photo(d: dict) -> str:
    out = []

    def frame(f):
        return (f'<div class="cframe"><div class="ph">{_fill(f.get("image",""), f.get("title",""))}</div>'
                f'<div class="cap"><b>{_esc(f.get("title"))}.</b> {_esc(f.get("caption"))}</div>'
                f'<div class="credit">{_esc(f.get("credit"))}</div></div>')
    if d.get("potd"):
        out.append(_sect("📷", "Photo of the day", frame(d["potd"])))
    if d.get("news_photo"):
        out.append(_sect("📰", "The day's news photo", frame(d["news_photo"])))
    gal = d.get("gallery") or []
    if gal:
        cells = "".join(f'<div><div class="g">{_fill(g.get("image",""), g.get("caption",""))}</div>'
                        f'<div class="gcap">{_esc(g.get("caption"))}</div></div>' for g in gal)
        out.append(_sect("🖼️", "The gallery", f'<div class="cgallery">{cells}</div>'))
    return "\n".join(out)


_CULTURE_BODY = {"music": _culture_music, "movies": _culture_movies, "photo": _culture_photo}


def culture_rail(section_id: str, issues: list[dict]) -> str:
    latest = issues[0] if issues else {}
    if section_id == "music":
        title = "Genres on rotation"
        rows = "".join(f'<div class="crow">{_esc(g)}</div>' for g in (latest.get("genres") or [])) \
            or '<div class="crow"><span class="num">—</span></div>'
    elif section_id == "movies":
        title = "Streaming on"
        rows = "".join(f'<div class="crow">{_esc(s)}</div>' for s in (latest.get("services") or [])) \
            or '<div class="crow"><span class="num">—</span></div>'
    else:
        title = "Past frames"
        rows = "".join(f'<div class="crow">{fmt_long(r["date"])}<span class="num">#{r["_num"]}</span></div>'
                       for r in issues[:5]) or '<div class="crow"><span class="num">No frames yet</span></div>'
    return (f'<aside class="rail"><div class="railbox crail">'
            f'<p class="railtitle">{title}</p>{rows}</div></aside>')


def culture_tabs(active: str, depth: int) -> str:
    prefix = "" if depth == 0 else "../../"
    tabs = []
    for s in CULTURE_SECTIONS:
        sel = "true" if s["id"] == active else "false"
        tabs.append(f'<a class="tab" role="tab" aria-selected="{sel}" data-feed="{s["id"]}" '
                    f'id="tab-{s["id"]}" href="{prefix}index.html#{s["id"]}">{_esc(s["nav"])}</a>')
    return "\n      ".join(tabs)


def render_culture_panel(sec: dict, issues: list[dict], depth: int, hidden: bool) -> str:
    hidden_attr = " hidden" if hidden else ""
    latest = issues[0] if issues else None
    kdate = fmt_kicker(latest["date"]) if latest else "This week"
    body = _CULTURE_BODY[sec["id"]](latest) if latest else ""
    if not body.strip():
        body = '<section class="section"><div class="body"><p>This section is being compiled.</p></div></section>'
    rail = culture_rail(sec["id"], issues)
    return f"""  <article class="issue cult" data-accent="culture" data-feed="{sec['id']}" data-group="weekend" role="tabpanel" aria-labelledby="tab-{sec['id']}"{hidden_attr}>
    <div class="kicker"><span class="big">The Weekend</span><span class="sm">{kdate}</span></div>
    <h2 class="headline">{sec['headline']}</h2>
    <p class="dek">{sec['dek']}</p>
    <div class="layout">
      <div class="col">
{body}
      </div>
{rail}
    </div>
  </article>"""


# ── DATES ────────────────────────────────────────────────────────────────────

def fmt_long(d: str) -> str:
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%b %-d, %Y")
    except ValueError:
        return d


def fmt_kicker(d: str) -> str:
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%a %-d %b")
    except ValueError:
        return d

# ── RENDER structural pieces ─────────────────────────────────────────────────

def render_rail(feed: dict, issues: list[dict], current_date: str, depth: int) -> str:
    prefix = "../../" * 0 if depth == 0 else "../../"
    rows = []
    for rec in issues[:7]:
        d = rec["date"]
        href = f'{prefix}issues/{feed["id"]}/{d}.html'
        cls = ' class="current"' if d == current_date else ""
        rows.append(f'<li><a href="{href}"{cls}>{fmt_long(d)} <span class="num">#{rec["_num"]}</span></a></li>')
    if not rows:
        rows.append('<li><span class="num">No back issues yet</span></li>')
    pipe = "".join(f"<span>{html_lib.escape(p)}</span>" for p in feed["pipe"])
    return f"""      <aside class="rail" data-accent="{feed['id']}">
        <div class="railbox"><p class="railtitle">Back issues</p>
          <ul class="archive">{''.join(rows)}</ul></div>
        <div class="railbox"><p class="railtitle">About this feed</p>
          <p class="about">{feed['about']}</p>
          <div class="pipe">{pipe}</div></div>
      </aside>"""


def render_article(feed: dict, issue: dict | None, issues: list[dict], depth: int, hidden: bool) -> str:
    hidden_attr = " hidden" if hidden else ""
    if issue is None:
        inner = (f'<div class="kicker"><span class="big">No issues yet</span></div>'
                 f'<h2 class="headline">{feed["headline"]}</h2>'
                 f'<p class="dek">The first {feed["name"]} issue publishes after tomorrow morning\'s run.</p>')
        rail = render_rail(feed, issues, "", depth)
        return (f'<article class="issue" data-accent="{feed["id"]}" data-feed="{feed["id"]}" '
                f'role="tabpanel" aria-labelledby="tab-{feed["id"]}"{hidden_attr}>{inner}'
                f'<div class="layout"><div class="col"><section class="section"><div class="body">'
                f'<p>Check back soon.</p></div></section></div>{rail}</div></article>')

    sections_html = render_sections(feed, issue["raw_text"], issue.get("sources"))
    rail = render_rail(feed, issues, issue["date"], depth)
    return f"""  <article class="issue" data-accent="{feed['id']}" data-feed="{feed['id']}" role="tabpanel" aria-labelledby="tab-{feed['id']}"{hidden_attr}>
    <div class="kicker"><span class="big">Issue {issue['_num']}</span><span class="sm">{fmt_kicker(issue['date'])}</span></div>
    <h2 class="headline">{feed['headline']}</h2>
    <p class="dek">{feed['dek']}</p>
    <div class="layout">
      <div class="col">
{sections_html}
      </div>
{rail}
    </div>
  </article>"""


def nav_tabs(active: str, depth: int) -> str:
    prefix = "" if depth == 0 else "../../"
    tabs = []
    for f in FEEDS:
        sel = "true" if f["id"] == active else "false"
        href = f'{prefix}index.html#{f["id"]}'
        tabs.append(f'<a class="tab" role="tab" aria-selected="{sel}" data-feed="{f["id"]}" '
                    f'id="tab-{f["id"]}" href="{href}">{html_lib.escape(f["nav"])}</a>')
    return "\n      ".join(tabs)


def page_shell(body: str, active: str, depth: int, script: str,
               title: str | None = None, desc: str | None = None,
               page_url: str = "", og_image_feed: str | None = None,
               og_type: str = "website") -> str:
    css = "../../assets/style.css" if depth else "assets/style.css"
    base = "../../" if depth else ""
    title = title or SITE_TITLE
    desc = (desc or SITE_DESC).replace('"', "&quot;")
    canonical = page_url or SITE_URL
    og_img = f"{SITE_URL}/assets/og-{og_image_feed or 'brief'}.png"
    title_esc = html_lib.escape(title)
    return f"""<!DOCTYPE html>
<html lang="en" data-accent="{active}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_esc}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{canonical}">
<meta property="og:site_name" content="{SITE_TITLE}">
<meta property="og:title" content="{title_esc}">
<meta property="og:description" content="{desc}">
<meta property="og:type" content="{og_type}">
<meta property="og:url" content="{canonical}">
<meta property="og:image" content="{og_img}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="{title_esc} — read today's issue">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title_esc}">
<meta name="twitter:description" content="{desc}">
<meta name="twitter:image" content="{og_img}">
<meta name="twitter:image:alt" content="{title_esc} — read today's issue">
<link rel="alternate" type="application/rss+xml" title="{SITE_TITLE} — Daily Brief" href="{base}feeds/brief.xml">
<link rel="alternate" type="application/rss+xml" title="{SITE_TITLE} — AI Insider" href="{base}feeds/ai.xml">
<link rel="alternate" type="application/rss+xml" title="{SITE_TITLE} — PMM / PM" href="{base}feeds/pmm.xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,600;12..96,700;12..96,800&family=Hanken+Grotesk:wght@400;500;600;700;800&display=swap">
<link rel="stylesheet" href="{css}">
</head>
<body>
{body}
<script>
{script}
</script>
</body>
</html>"""

# ── PAGE BUILDERS ────────────────────────────────────────────────────────────

INDEX_SCRIPT = """
(function(){
  var paper=["brief","ai","pmm"], week=["music","movies","photo"], all=paper.concat(week);
  var tabs=[].slice.call(document.querySelectorAll(".tab"));
  var panels={}; all.forEach(function(f){panels[f]=document.querySelector('.issue[data-feed="'+f+'"]');});
  var root=document.documentElement;
  var papertabs=document.getElementById("papertabs"), weektabs=document.getElementById("weektabs");
  var split=document.getElementById("split");
  var lastPaper="brief", lastWeek="music";
  function show(f){
    if(!panels[f])return;
    var wk=week.indexOf(f)>=0;
    if(wk){lastWeek=f;}else{lastPaper=f;}
    tabs.forEach(function(t){t.setAttribute("aria-selected",t.dataset.feed===f?"true":"false");});
    all.forEach(function(k){if(panels[k])panels[k].hidden=(k!==f);});
    if(papertabs)papertabs.hidden=wk;
    if(weektabs)weektabs.hidden=!wk;
    if(split)split.querySelectorAll("button").forEach(function(b){
      b.setAttribute("aria-pressed",((b.dataset.mode==="weekend")===wk)?"true":"false");});
    root.setAttribute("data-accent",wk?"culture":f);
    try{localStorage.setItem("dispatch.feed",f);}catch(e){}
    if(location.hash.slice(1)!==f)history.replaceState(null,"","#"+f);
  }
  tabs.forEach(function(t){
    t.addEventListener("click",function(e){e.preventDefault();show(t.dataset.feed);});
  });
  if(split)split.querySelectorAll("button").forEach(function(b){
    b.addEventListener("click",function(){show(b.dataset.mode==="weekend"?lastWeek:lastPaper);});});
  var start=all.indexOf(location.hash.slice(1))>=0?location.hash.slice(1):null;
  if(!start){try{var s=localStorage.getItem("dispatch.feed");if(all.indexOf(s)>=0)start=s;}catch(e){}}
  show(start||"brief");
  var d=new Date(),t=document.getElementById("today");
  if(t)t.textContent=d.toLocaleDateString("en-US",{weekday:"short",month:"short",day:"numeric",year:"numeric"});
  themeInit();
})();
"""

THEME_SCRIPT = """
function themeInit(){
  var root=document.documentElement,btn=document.getElementById("themeBtn");
  if(!btn)return;
  function sysDark(){return window.matchMedia&&matchMedia("(prefers-color-scheme:dark)").matches;}
  function curDark(){var t=root.getAttribute("data-theme");return t?t==="dark":sysDark();}
  function paint(){btn.textContent=curDark()?"Light":"Dark";}
  try{var st=localStorage.getItem("dispatch.theme");if(st)root.setAttribute("data-theme",st);}catch(e){}
  paint();
  btn.addEventListener("click",function(){var n=curDark()?"light":"dark";root.setAttribute("data-theme",n);
    try{localStorage.setItem("dispatch.theme",n);}catch(e){}paint();});
}
"""


def masthead(depth: int) -> str:
    home = "index.html" if depth == 0 else "../../index.html"
    return f"""<header class="masthead" id="mast">
  <div class="wrap masthead-row">
    <div>
      <h1 class="wordmark"><a href="{home}">The Khat-<span>TING</span></a></h1>
      <p class="tagline">{html_lib.escape(SITE_TAGLINE)}</p>
    </div>
    <div class="meta">
      <div class="meta-row">
        <div class="today" id="today">—</div>
        <button class="toggle" id="themeBtn" type="button">Dark</button>
      </div>
      <div class="split" id="split" role="tablist" aria-label="Section">
        <button type="button" data-mode="paper" aria-pressed="true">The Paper</button>
        <button type="button" data-mode="weekend" aria-pressed="false">The Weekend</button>
      </div>
    </div>
  </div>
</header>
<div class="tabsbar" id="tabsbar">
  <div class="wrap">
    <div class="tabs" id="papertabs" role="tablist" aria-label="Channels">
      {nav_tabs('brief', depth)}
    </div>
    <div class="tabs" id="weektabs" role="tablist" aria-label="The Weekend" hidden>
      {culture_tabs('music', depth)}
    </div>
  </div>
</div>"""


def build_index(feed_issues: dict[str, list[dict]], culture_issues: dict[str, list[dict]]) -> str:
    articles = []
    first = True
    for f in FEEDS:
        issues = feed_issues[f["id"]]
        latest = issues[0] if issues else None
        articles.append(render_article(f, latest, issues, depth=0, hidden=not first))
        first = False
    for sec in CULTURE_SECTIONS:
        articles.append(render_culture_panel(sec, culture_issues.get(sec["id"], []), depth=0, hidden=True))
    body = masthead(0) + '\n<main class="wrap">\n' + "\n".join(articles) + "\n</main>\n" + FOOTER
    return page_shell(body, "brief", 0, THEME_SCRIPT + INDEX_SCRIPT,
                      title=SITE_TITLE_SEO, page_url=SITE_URL + "/", og_type="website")


def build_issue_page(feed: dict, issue: dict, issues: list[dict]) -> str:
    back = '<p class="backlink"><a href="../../index.html#' + feed["id"] + '">← Latest &amp; all channels</a></p>'
    article = render_article(feed, issue, issues, depth=1, hidden=False)
    # single-feed page: force accent, drop tab interactivity
    body = masthead(1) + '\n<main class="wrap">\n' + back + article + "\n</main>\n" + FOOTER
    script = THEME_SCRIPT + """
(function(){
  var d=new Date(),t=document.getElementById("today");
  if(t)t.textContent=d.toLocaleDateString("en-US",{weekday:"short",month:"short",day:"numeric",year:"numeric"});
  themeInit();
  document.querySelectorAll('.tab').forEach(function(t){
    t.setAttribute('aria-selected', t.dataset.feed==='%s'?'true':'false');
  });
})();
""" % feed["id"]
    page_title = f"{feed['name']} · {fmt_long(issue['date'])} — {SITE_TITLE}"
    page_desc = f"{feed['name']} for {fmt_long(issue['date'])}: {SITE_DESC}"
    page_url = f"{SITE_URL}/issues/{feed['id']}/{issue['date']}.html"
    return page_shell(body, feed["id"], 1, script, title=page_title, desc=page_desc,
                      page_url=page_url, og_image_feed=feed["id"], og_type="article")


FOOTER = """<footer><div class="wrap foot"><span>The Khat-TING — compiled daily</span><span id="genstamp"></span></div></footer>"""

# ── CSS ──────────────────────────────────────────────────────────────────────

STYLE_CSS = r""":root{
  --paper:#F2F3F0; --ink:#17160F; --ink-soft:#4B4A42; --muted:#86857B;
  --rule:#DADAD2; --card:#FFFFFF; --card-2:#EAEBE5;
  --pop:#FFDE2E;
  --disp:"Bricolage Grotesque",system-ui,sans-serif;
  --body:"Hanken Grotesk",system-ui,sans-serif;
  --maxw:1120px;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#121211; --ink:#F4F2E9; --ink-soft:#B2B0A4; --muted:#7C7A70;
  --rule:#2A2A26; --card:#1A1A17; --card-2:#232320; color-scheme:dark;
}}
:root[data-theme="dark"]{
  --paper:#121211; --ink:#F4F2E9; --ink-soft:#B2B0A4; --muted:#7C7A70;
  --rule:#2A2A26; --card:#1A1A17; --card-2:#232320; color-scheme:dark;
}
[data-accent="brief"]{--accent:#FB3B2F;}
[data-accent="ai"]{--accent:#6B4BFF;}
[data-accent="pmm"]{--accent:#FF6A17;}
[data-accent]{--on-accent:#FFFFFF; --wash:color-mix(in srgb, var(--accent) 14%, var(--card));}

*{box-sizing:border-box;}
html,body{margin:0;}
body{background:var(--paper);color:var(--ink);font-family:var(--body);line-height:1.5;
  -webkit-font-smoothing:antialiased;padding-top:env(safe-area-inset-top,0px);}
a{color:inherit;}
.wrap{max-width:var(--maxw);margin:0 auto;padding-inline:22px;}
.mark{background:var(--pop);background-size:100% 62%;background-repeat:no-repeat;background-position:0 78%;
  padding:0 .08em;color:#17160F;box-decoration-break:clone;-webkit-box-decoration-break:clone;}

.masthead{padding-block:26px 0;}
.masthead-row{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap;}
.wordmark{font-family:var(--disp);font-weight:800;font-size:clamp(40px,11vw,92px);line-height:.84;
  letter-spacing:-.035em;text-transform:uppercase;margin:0;}
.wordmark a{text-decoration:none;}
.wordmark span{color:var(--accent);}
.tagline{font-family:var(--body);font-weight:600;font-size:14px;color:var(--ink-soft);margin:10px 0 0;max-width:38ch;}
.meta{display:flex;flex-direction:column;align-items:flex-end;gap:12px;flex-shrink:0;padding-top:6px;}
.meta-row{display:flex;align-items:center;gap:10px;}
.split{display:inline-flex;gap:3px;background:var(--card);border:1px solid var(--rule);border-radius:999px;padding:4px;}
.split button{font-family:var(--disp);font-weight:700;font-size:13px;padding:7px 15px;border:none;background:none;color:var(--muted);border-radius:999px;cursor:pointer;}
.split button[aria-pressed="true"]{background:var(--ink);color:var(--paper);}
.tabs[hidden]{display:none;}
.today{font-family:var(--disp);font-weight:600;font-size:12px;color:var(--ink-soft);text-align:right;line-height:1.3;}
.toggle{font-family:var(--disp);font-weight:700;font-size:12px;background:var(--ink);color:var(--paper);
  border:none;border-radius:2px;padding:9px 13px;cursor:pointer;}
.toggle:hover{background:var(--accent);color:var(--on-accent);}

.tabsbar{position:sticky;top:env(safe-area-inset-top,0px);z-index:20;background:var(--paper);
  border-top:3px solid var(--ink);border-bottom:3px solid var(--ink);margin-top:20px;}
.tabs{display:flex;overflow-x:auto;scrollbar-width:none;}
.tabs::-webkit-scrollbar{display:none;}
.tab{background:none;border:none;border-right:1px solid var(--rule);cursor:pointer;text-decoration:none;
  font-family:var(--disp);font-weight:700;font-size:clamp(16px,2.4vw,20px);color:var(--muted);
  padding:14px 20px;white-space:nowrap;letter-spacing:-.02em;}
.tab[aria-selected="true"]{color:var(--on-accent);background:var(--accent);}
.tab:focus-visible{outline:3px solid var(--accent);outline-offset:-3px;}

.backlink{font-family:var(--disp);font-weight:600;font-size:14px;margin:28px 0 -8px;}
.backlink a{color:var(--muted);text-decoration:none;}
.backlink a:hover{color:var(--accent);}

.issue{padding-block:30px 60px;}
.issue[hidden]{display:none;}
.kicker{display:inline-flex;align-items:baseline;gap:12px;background:var(--accent);color:var(--on-accent);
  font-family:var(--disp);font-weight:700;padding:7px 14px;border-radius:2px;margin-bottom:18px;}
.kicker .big{font-size:20px;letter-spacing:-.02em;}
.kicker .sm{font-size:12px;opacity:.85;}
.headline{font-family:var(--disp);font-weight:800;letter-spacing:-.035em;line-height:.92;
  font-size:clamp(38px,8vw,80px);margin:0;}
.dek{font-family:var(--body);font-weight:500;color:var(--ink-soft);font-size:clamp(16px,2.6vw,21px);
  margin-top:16px;max-width:34ch;}

.layout{display:grid;grid-template-columns:1fr 300px;gap:52px;align-items:start;margin-top:40px;}
@media (max-width:800px){.layout{grid-template-columns:1fr;gap:40px;}}
.col{min-width:0;}

.section{margin-bottom:40px;}
.section:last-child{margin-bottom:0;}
.sectlabel{font-family:var(--disp);font-weight:800;font-size:clamp(22px,4vw,30px);letter-spacing:-.03em;
  margin:0 0 18px;line-height:1.02;}
.sectlabel .se{font-size:.8em;}
.body > *:first-child{margin-top:0;}
.body a{color:var(--accent);text-decoration:none;border-bottom:1px solid color-mix(in srgb,var(--accent) 40%,transparent);}
.body a:hover{border-bottom-color:var(--accent);}
.body strong{color:var(--ink);font-weight:700;}
.body code{font-family:ui-monospace,Menlo,monospace;font-size:.9em;background:var(--card-2);
  padding:1px 5px;border-radius:3px;overflow-wrap:anywhere;word-break:break-word;}
/* Everything in the body must wrap, never push the column wider (which overlapped the rail) */
.body,.body details.item > summary,.body .item--flat,.body .item-body p{overflow-wrap:anywhere;}

/* ── Collapsible items ──
   NOTE: these are block (not flex) with an absolutely-positioned marker, so inline
   <strong>/<code>/<a> stay inline and wrap. Flex would make each a separate column. */
.body details.item{border-bottom:1px solid var(--rule);}
.body details.item:last-child,.body .item--flat:last-child{border-bottom:none;}
.body details.item > summary{list-style:none;cursor:pointer;display:block;position:relative;
  padding:15px 0 15px 23px;font-family:var(--disp);font-weight:700;font-size:clamp(17px,2.4vw,20px);
  letter-spacing:-.018em;line-height:1.2;color:var(--ink);transition:color .12s;}
.body details.item > summary::-webkit-details-marker{display:none;}
.body details.item > summary::before{content:"+";position:absolute;left:0;top:15px;
  font-family:var(--disp);font-weight:800;color:var(--accent);line-height:1.2;}
.body details.item[open] > summary::before{content:"\2013";}
.body details.item > summary:hover{color:var(--accent);}
.body details.item > summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:2px;}
.body .item-body{padding:2px 0 17px 23px;}
.body .item-body p{margin:0 0 10px;font-size:16.5px;line-height:1.55;color:var(--ink-soft);}
.body .item-body p:last-child{margin-bottom:0;}
.body .src-links{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;}
.body .src-links .src-link{font-family:var(--disp);font-weight:600;font-size:12px;letter-spacing:.01em;
  text-decoration:none;color:var(--accent);background:var(--wash);border:1px solid var(--accent);
  border-radius:999px;padding:3px 11px;white-space:nowrap;}
.body .src-links .src-link:hover{background:var(--accent);color:var(--on-accent);}
.body .item--flat{display:block;position:relative;padding:15px 0 15px 23px;border-bottom:1px solid var(--rule);
  font-family:var(--disp);font-weight:700;font-size:clamp(17px,2.4vw,20px);letter-spacing:-.018em;
  line-height:1.2;color:var(--ink);}
.body .item--flat::before{content:"\2022";position:absolute;left:0;top:15px;color:var(--accent);line-height:1.2;}
.body .subhead{font-family:var(--disp);font-weight:800;font-size:14px;letter-spacing:.02em;
  text-transform:uppercase;color:var(--accent);margin:22px 0 2px;}

/* Speed Round stays a flat numbered list */
.body ul.speed{list-style:none;margin:0;padding:0;counter-reset:s;}
.body ul.speed li{display:flex;gap:14px;align-items:baseline;margin-bottom:13px;font-size:17px;color:var(--ink-soft);}
.body ul.speed li:last-child{margin-bottom:0;}
.body ul.speed li::before{counter-increment:s;content:counter(s,decimal-leading-zero);
  font-family:var(--disp);font-weight:800;font-size:14px;color:var(--accent);flex-shrink:0;}

.rail{display:grid;gap:22px;}
@media (min-width:801px){.rail{position:sticky;top:calc(env(safe-area-inset-top,0px) + 74px);}}
.railbox{border:3px solid var(--ink);border-radius:4px;padding:18px;background:var(--card);}
.railtitle{font-family:var(--disp);font-weight:800;font-size:16px;letter-spacing:-.02em;margin:0 0 14px;}
.archive{list-style:none;margin:0;padding:0;display:grid;gap:3px;}
.archive a{display:flex;justify-content:space-between;align-items:baseline;gap:10px;text-decoration:none;
  color:var(--ink-soft);padding:8px;margin-inline:-8px;border-radius:3px;font-family:var(--disp);font-weight:600;font-size:15px;}
.archive a:hover{background:var(--accent);color:var(--on-accent);}
.archive .num{font-size:12px;color:var(--muted);}
.archive a:hover .num{color:var(--on-accent);}
.archive a.current{color:var(--accent);}
.about{font-size:15px;color:var(--ink-soft);margin:0;font-weight:500;}
.about b{color:var(--ink);font-weight:700;}
.pipe{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px;}
.pipe span{font-family:var(--disp);font-weight:600;font-size:12px;background:var(--card-2);border-radius:3px;padding:3px 8px;color:var(--ink-soft);}

footer{border-top:3px solid var(--ink);padding-block:22px 44px;margin-top:20px;}
.foot{font-family:var(--disp);font-weight:600;font-size:13px;color:var(--muted);display:flex;
  justify-content:space-between;gap:12px;flex-wrap:wrap;}
/* ── Culture ("The Weekend") ── magenta channel + editorial module styles ── */
[data-accent="culture"]{--accent:#E5327E;}
.cult .cnews .cn{padding:13px 0;border-bottom:1px solid var(--rule);}
.cult .cnews .cn:last-child{border-bottom:none;}
.cult .cnews .cn-t{font-family:var(--disp);font-weight:700;font-size:clamp(16px,2.3vw,19px);
  letter-spacing:-.015em;text-decoration:none;color:var(--ink);border:none;}
.cult .cnews .cn-t:hover{color:var(--accent);}
.cult .cnews .cn-s{display:block;font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--muted);margin-top:4px;letter-spacing:.04em;}
.cult .art,.cult .poster,.cult .ph,.cult .g{background:var(--card-2);border:1px solid var(--rule);border-radius:8px;overflow:hidden;}
.cult .art img,.cult .poster img,.cult .ph img,.cult .g img{width:100%;height:100%;object-fit:cover;display:block;}
.cult .ctracks .ctrk{display:grid;grid-template-columns:54px 1fr;gap:15px;padding:14px 0;border-bottom:1px solid var(--rule);}
.cult .ctracks .ctrk:last-child{border-bottom:none;}
.cult .ctrk .art{width:54px;height:54px;}
.cult .ctrk h4{font-family:var(--disp);font-weight:700;font-size:16px;margin:0;letter-spacing:-.01em;}
.cult .ctrk h4 a{text-decoration:none;color:var(--ink);border:none;}
.cult .ctrk h4 a:hover{color:var(--accent);}
.cult .ctrk .by{font-size:13px;color:var(--ink-soft);}
.cult .ctrk .why{font-size:13px;color:var(--muted);margin-top:4px;}
.cult .ctrk .tags{display:flex;gap:6px;margin-top:7px;flex-wrap:wrap;align-items:center;}
.cult .pill{font-family:ui-monospace,Menlo,monospace;font-size:9px;letter-spacing:.06em;text-transform:uppercase;
  border:1px solid var(--rule);border-radius:999px;padding:3px 8px;color:var(--ink-soft);}
.cult .geo{font-family:ui-monospace,Menlo,monospace;font-size:8.5px;letter-spacing:.1em;text-transform:uppercase;padding:3px 7px;border-radius:5px;}
.cult .geo.in{background:var(--pop);color:#17160F;}
.cult .geo.w{border:1px solid var(--rule);color:var(--muted);}
.cult .ccharts{display:grid;grid-template-columns:1fr 1fr;gap:26px;}
@media (max-width:520px){.cult .ccharts{grid-template-columns:1fr;}}
.cult .cchart h5{font-family:ui-monospace,Menlo,monospace;font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);margin:0 0 6px;}
.cult .cchart ol{margin:0;padding:0;list-style:none;counter-reset:c;}
.cult .cchart li{counter-increment:c;display:flex;gap:11px;align-items:baseline;padding:9px 0;border-bottom:1px solid var(--rule);font-size:15px;font-weight:600;}
.cult .cchart li:last-child{border-bottom:none;}
.cult .cchart li::before{content:counter(c);font-family:var(--disp);font-weight:800;color:var(--accent);width:18px;}
.cult .cchart .by{color:var(--muted);font-size:12px;font-weight:500;margin-left:auto;}
.cult .cpick{display:grid;grid-template-columns:118px 1fr;gap:20px;}
@media (max-width:480px){.cult .cpick{grid-template-columns:1fr;}}
.cult .cpick .poster{aspect-ratio:2/3;}
.cult .cpick h4{font-family:var(--disp);font-weight:800;font-size:clamp(20px,3vw,26px);letter-spacing:-.02em;margin:0;}
.cult .cpick .m{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--muted);letter-spacing:.04em;margin:6px 0 10px;}
.cult .cpick p{font-size:15px;color:var(--ink-soft);margin:0 0 12px;}
.cult .chips{display:flex;gap:7px;flex-wrap:wrap;align-items:center;}
.cult .crel .crl{display:flex;gap:14px;justify-content:space-between;align-items:baseline;padding:13px 0;border-bottom:1px solid var(--rule);}
.cult .crel .crl:last-child{border-bottom:none;}
.cult .crel .crl-t{font-size:16px;font-weight:600;line-height:1.5;}
.cult .crel .crl-t b{font-family:var(--disp);font-weight:700;}
.cult .crel .score{font-family:var(--disp);font-weight:800;font-size:19px;color:var(--accent);flex-shrink:0;}
.cult .ccritic h4{font-family:var(--disp);font-weight:800;font-size:clamp(20px,3vw,24px);letter-spacing:-.02em;margin:0 0 7px;}
.cult .ccritic p{font-size:15px;color:var(--ink-soft);margin:0;max-width:60ch;}
.cult .cframe .ph{aspect-ratio:16/9;width:100%;}
.cult .cap{font-size:14px;color:var(--ink-soft);margin-top:11px;}
.cult .cap b{color:var(--ink);font-weight:700;}
.cult .credit{font-family:ui-monospace,Menlo,monospace;font-size:10px;color:var(--muted);letter-spacing:.04em;margin-top:4px;}
.cult .cgallery{display:grid;grid-template-columns:repeat(3,1fr);gap:13px;}
@media (max-width:520px){.cult .cgallery{grid-template-columns:1fr 1fr;}}
.cult .cgallery .g{aspect-ratio:1;width:100%;}
.cult .gcap{font-family:ui-monospace,Menlo,monospace;font-size:9.5px;color:var(--muted);margin-top:6px;}
.crail .crow{display:flex;justify-content:space-between;align-items:baseline;gap:10px;padding:10px 0;
  border-bottom:1px solid var(--rule);font-family:var(--disp);font-weight:600;font-size:15px;}
.crail .crow:last-child{border-bottom:none;}
.crail .crow .num{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--muted);font-weight:500;}

@media (prefers-reduced-motion:reduce){*{transition:none!important;}}
"""

# ── FEEDS / SEO ──────────────────────────────────────────────────────────────

def _plain_summary(raw_text: str, limit: int = 280) -> str:
    """Strip markup/markers from a digest for an RSS description."""
    t = re.sub(r"\[S\d+\]", "", raw_text)
    t = re.sub(r"[#*_`>]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return (t[:limit] + "…") if len(t) > limit else t


def _rfc822(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
    except ValueError:
        dt = datetime.now(timezone.utc)
    return format_datetime(dt)


def build_rss(feed: dict, issues: list[dict]) -> str:
    items = []
    for rec in issues[:30]:
        url = f"{SITE_URL}/issues/{feed['id']}/{rec['date']}.html"
        title = html_lib.escape(f"{feed['name']} — {fmt_long(rec['date'])}")
        desc = html_lib.escape(_plain_summary(rec.get("raw_text", "")))
        items.append(
            f"    <item>\n"
            f"      <title>{title}</title>\n"
            f"      <link>{url}</link>\n"
            f"      <guid isPermaLink=\"true\">{url}</guid>\n"
            f"      <pubDate>{_rfc822(rec['date'])}</pubDate>\n"
            f"      <description>{desc}</description>\n"
            f"    </item>"
        )
    chan_title = html_lib.escape(f"{SITE_TITLE} — {feed['name']}")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        '  <channel>\n'
        f'    <title>{chan_title}</title>\n'
        f'    <link>{SITE_URL}/</link>\n'
        f'    <description>{html_lib.escape(feed["dek"])}</description>\n'
        '    <language>en-us</language>\n'
        f'    <atom:link href="{SITE_URL}/feeds/{feed["id"]}.xml" rel="self" type="application/rss+xml"/>\n'
        + "\n".join(items) + "\n"
        '  </channel>\n'
        '</rss>\n'
    )


def build_sitemap(feed_issues: dict) -> str:
    urls = [f"{SITE_URL}/"]
    for f in FEEDS:
        for rec in feed_issues[f["id"]]:
            urls.append(f"{SITE_URL}/issues/{f['id']}/{rec['date']}.html")
    body = "\n".join(f"  <url><loc>{u}</loc></url>" for u in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{body}\n</urlset>\n")


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> None:
    feed_issues = {f["id"]: load_issues(f["id"]) for f in FEEDS}
    culture_issues = {sec["id"]: load_culture(sec["id"]) for sec in CULTURE_SECTIONS}

    (OUT_DIR / "assets").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "feeds").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / ".nojekyll").write_text("", encoding="utf-8")
    (OUT_DIR / "assets" / "style.css").write_text(STYLE_CSS, encoding="utf-8")

    # Copy committed assets (OG images, etc.) into the output
    if ASSETS_SRC.exists():
        for p in ASSETS_SRC.iterdir():
            if p.is_file():
                shutil.copy2(p, OUT_DIR / "assets" / p.name)

    (OUT_DIR / "index.html").write_text(build_index(feed_issues, culture_issues), encoding="utf-8")

    total_issues = 0
    for f in FEEDS:
        issues = feed_issues[f["id"]]
        dest = OUT_DIR / "issues" / f["id"]
        dest.mkdir(parents=True, exist_ok=True)
        for rec in issues:
            (dest / f'{rec["date"]}.html').write_text(
                build_issue_page(f, rec, issues), encoding="utf-8"
            )
            total_issues += 1
        # Per-channel RSS feed
        (OUT_DIR / "feeds" / f'{f["id"]}.xml').write_text(build_rss(f, issues), encoding="utf-8")

    # SEO: sitemap + robots
    (OUT_DIR / "sitemap.xml").write_text(build_sitemap(feed_issues), encoding="utf-8")
    (OUT_DIR / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n", encoding="utf-8"
    )

    print(f"Built site -> {OUT_DIR}")
    for f in FEEDS:
        print(f"  {f['id']:6s} {len(feed_issues[f['id']])} issue(s)")
    print(f"  {total_issues} issue page(s) total")


if __name__ == "__main__":
    main()
