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
import re
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "site" / "public"

SITE_TITLE = "Khat-TING"
SITE_TAGLINE = "A daily intelligence briefing, compiled by Yash and one very caffeinated algorithm."

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
        "sections": ["Money Talk", "World Lore", "Tech Tea", "Creator Szn", "Speed Round"],
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
_SUBHEAD = re.compile(r"^\s*\*\*(.+?)\*\*:?\s*$")


def render_body(body: str, speed: bool = False) -> str:
    """Clean markdown-ish body -> styled HTML using the site's own classes."""
    if speed:
        items = [ln.strip() for ln in body.split("\n")]
        items = [re.sub(r"^\s*(?:[-–—*•]|\d+[.)])\s*", "", it) for it in items if it]
        if items:
            lis = "".join(f"<li>{_inline(it)}</li>" for it in items)
            return f'<ul class="speed">{lis}</ul>'
        return ""

    blocks = re.split(r"\n\s*\n", body.strip())
    html_parts: list[str] = []
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if all(_BULLET.match(ln) for ln in lines):
            lis = "".join(f"<li>{_inline(_BULLET.match(ln).group(1))}</li>" for ln in lines)
            html_parts.append(f"<ul>{lis}</ul>")
        elif all(_NUMBERED.match(ln) for ln in lines):
            lis = "".join(f"<li>{_inline(_NUMBERED.match(ln).group(1))}</li>" for ln in lines)
            html_parts.append(f"<ol>{lis}</ol>")
        elif len(lines) == 1 and _SUBHEAD.match(lines[0]):
            html_parts.append(f"<p class=\"subhead\">{_inline(_SUBHEAD.match(lines[0]).group(1))}</p>")
        else:
            # mixed block: emit bullets as a list, other lines as paragraphs, in order
            run_ul: list[str] = []
            for ln in lines:
                mb = _BULLET.match(ln) or _NUMBERED.match(ln)
                if mb:
                    run_ul.append(f"<li>{_inline(mb.group(1))}</li>")
                else:
                    if run_ul:
                        html_parts.append(f"<ul>{''.join(run_ul)}</ul>")
                        run_ul = []
                    sh = _SUBHEAD.match(ln)
                    if sh:
                        html_parts.append(f'<p class="subhead">{_inline(sh.group(1))}</p>')
                    else:
                        html_parts.append(f"<p>{_inline(ln.strip())}</p>")
            if run_ul:
                html_parts.append(f"<ul>{''.join(run_ul)}</ul>")
    return "\n".join(html_parts)


def render_sections(feed: dict, raw_text: str) -> str:
    secs = parse_sections(feed, raw_text)
    out: list[str] = []
    for emoji, label, body in secs:
        speed = feed["id"] == "brief" and label.lower().startswith("speed")
        body_html = render_body(body, speed=speed)
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
    for rec in issues[:12]:
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

    sections_html = render_sections(feed, issue["raw_text"])
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


def page_shell(body: str, active: str, depth: int, script: str) -> str:
    css = "../../assets/style.css" if depth else "assets/style.css"
    return f"""<!DOCTYPE html>
<html lang="en" data-accent="{active}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{SITE_TITLE}</title>
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
  var feeds=["brief","ai","pmm"];
  var tabs=[].slice.call(document.querySelectorAll(".tab"));
  var panels={}; feeds.forEach(function(f){panels[f]=document.querySelector('.issue[data-feed="'+f+'"]');});
  var root=document.documentElement;
  function show(f){
    if(!panels[f])return;
    tabs.forEach(function(t){t.setAttribute("aria-selected",t.dataset.feed===f?"true":"false");});
    feeds.forEach(function(k){if(panels[k])panels[k].hidden=(k!==f);});
    root.setAttribute("data-accent",f);
    try{localStorage.setItem("dispatch.feed",f);}catch(e){}
    if(location.hash.slice(1)!==f)history.replaceState(null,"","#"+f);
  }
  tabs.forEach(function(t){
    t.addEventListener("click",function(e){e.preventDefault();show(t.dataset.feed);});
  });
  var start=feeds.indexOf(location.hash.slice(1))>=0?location.hash.slice(1):null;
  if(!start){try{var s=localStorage.getItem("dispatch.feed");if(feeds.indexOf(s)>=0)start=s;}catch(e){}}
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
      <h1 class="wordmark"><a href="{home}">Khat-<span>TING</span></a></h1>
      <p class="tagline">{html_lib.escape(SITE_TAGLINE)}</p>
    </div>
    <div class="meta">
      <div class="today" id="today">—</div>
      <button class="toggle" id="themeBtn" type="button">Dark</button>
    </div>
  </div>
</header>
<div class="tabsbar" id="tabsbar">
  <div class="wrap">
    <div class="tabs" role="tablist" aria-label="Channels">
      {nav_tabs('brief', depth)}
    </div>
  </div>
</div>"""


def build_index(feed_issues: dict[str, list[dict]]) -> str:
    articles = []
    first = True
    for f in FEEDS:
        issues = feed_issues[f["id"]]
        latest = issues[0] if issues else None
        articles.append(render_article(f, latest, issues, depth=0, hidden=not first))
        first = False
    body = masthead(0) + '\n<main class="wrap">\n' + "\n".join(articles) + "\n</main>\n" + FOOTER
    return page_shell(body, "brief", 0, THEME_SCRIPT + INDEX_SCRIPT)


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
    return page_shell(body, feed["id"], 1, script)


FOOTER = """<footer><div class="wrap foot"><span>Khat-TING — compiled daily</span><span id="genstamp"></span></div></footer>"""

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
.meta{display:flex;align-items:center;gap:10px;flex-shrink:0;padding-top:6px;}
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
.body p{margin:0 0 14px;font-size:17.5px;color:var(--ink-soft);}
.body p.subhead{font-family:var(--disp);font-weight:700;font-size:18px;color:var(--ink);margin:18px 0 8px;}
.body ul,.body ol{margin:0 0 16px;padding-left:1.2em;}
.body li{margin-bottom:9px;font-size:17.5px;color:var(--ink-soft);}
.body a{color:var(--accent);text-decoration:none;border-bottom:1px solid color-mix(in srgb,var(--accent) 40%,transparent);}
.body a:hover{border-bottom-color:var(--accent);}
.body strong{color:var(--ink);font-weight:700;}
.body code{font-family:ui-monospace,Menlo,monospace;font-size:.9em;background:var(--card-2);padding:1px 5px;border-radius:3px;}
.body ul.speed{list-style:none;padding:0;counter-reset:s;}
.body ul.speed li{display:flex;gap:14px;align-items:baseline;margin-bottom:13px;}
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
@media (prefers-reduced-motion:reduce){*{transition:none!important;}}
"""

# ── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> None:
    feed_issues = {f["id"]: load_issues(f["id"]) for f in FEEDS}

    (OUT_DIR / "assets").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / ".nojekyll").write_text("", encoding="utf-8")
    (OUT_DIR / "assets" / "style.css").write_text(STYLE_CSS, encoding="utf-8")

    (OUT_DIR / "index.html").write_text(build_index(feed_issues), encoding="utf-8")

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

    print(f"Built site -> {OUT_DIR}")
    for f in FEEDS:
        print(f"  {f['id']:6s} {len(feed_issues[f['id']])} issue(s)")
    print(f"  {total_issues} issue page(s) total")


if __name__ == "__main__":
    main()
