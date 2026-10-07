# The Khat-TING

A daily newsletter website with three channels — **Daily Brief**, **AI Insider**, and **PMM / PM** — each generated automatically every morning and published to one GitHub Pages site. The email versions still run where you want them (AI Insider keeps emailing; Brief and PMM are web-only).

## How it works

```
feeds/<feed>/            the generator scripts (one per channel)
  brief/digest.py        → data/brief/<date>.json
  ai/ai_digest.py        → data/ai/<date>.json   (also emails)
  pmm/pmm_digest.py      → data/pmm/<date>.json
data/<feed>/<date>.json  the archive — one record per issue (raw digest text)
site/build_site.py       reads every JSON and renders the "Loud" site
site/public/             the built site (generated in CI, not committed)
.github/workflows/       automation (see below)
```

1. Each morning a **feed workflow** runs its generator. The script pulls its sources, writes the day's digest to `data/<feed>/<date>.json`, and commits it. (AI Insider also sends its email.)
2. When a feed finishes, **Build & deploy** runs `build_site.py`, which turns every archived issue into the site and deploys it to GitHub Pages.
3. Presentation lives entirely in `build_site.py` — the generators only emit raw text, so redesigning the site never touches them.

Schedules (UTC): PMM 10:00 · AI 11:00 · Brief 12:00 · site rebuild 12:30.

## Email on/off

Controlled per run by the `SEND_EMAIL` env var, set in each feed's workflow:
`feed-ai.yml` → `true`, `feed-brief.yml` and `feed-pmm.yml` → `false`. Flip the value to change it.

## First-time setup (one-time, in GitHub)

1. **Add repository secrets** (Settings → Secrets and variables → Actions) — copy these from your old repos:
   `GEMINI_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `RECIPIENT_EMAIL`,
   `APIFY_API_KEY`, `CRUNCHBASE_API_KEY`, `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`.
2. **Enable Pages:** Settings → Pages → Source → **GitHub Actions**.
3. **Kick it off:** Actions tab → run each feed once (Run workflow), or wait for the morning crons. The first feed run populates the site.

## Run locally

```bash
cp .env.example .env     # fill in your keys
pip install -r feeds/pmm/requirements.txt   # (or whichever feed)
python feeds/pmm/pmm_digest.py              # writes data/pmm/<date>.json
python site/build_site.py                   # builds site/public/
open site/public/index.html
```
