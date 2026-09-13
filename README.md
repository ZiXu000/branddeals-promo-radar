# BrandDeals — Promo Radar

Auto-updating tracker of **official** deals and coupon pages from consumer brands
(meal kits, beauty, apparel, supplements, software subscriptions). Prices are pulled
from each brand's own public product feed or official page — never invented.

- **Live site:** https://branddeals-promo-radar.pages.dev
- **Data:** [`data/offers.json`](data/offers.json) — rewritten on every crawl
- **Site rules:** [`.ilang/site.ilang`](.ilang/site.ilang)

## How it works

```
job update (no secrets)                     job deploy (needs CF token)
  scraper.py   → data/offers.json  ──┐
  build.py     → site/               ├─ artifact ─→ wrangler pages deploy → pages.dev
  git commit + push  ────────────────┘
  .github/workflows/update.yml → cron every 6h, and on push to main
```

Zero servers. Zero API keys. Zero runtime inference. Pure Python standard library.
The only cost is $0: GitHub Actions on a public repo and Cloudflare Pages free tier.

## Run locally

```bash
python scraper.py     # crawl public sources -> data/offers.json
python build.py       # render -> site/
python -m http.server -d site 8000   # preview at http://localhost:8000
```

## Add or change a brand

Edit the `PROVIDERS` module in `.ilang/site.ilang` — one line per brand:

```
Name | https://homepage | https://source-feed-or-page | adapter | affiliate-url
```

Adapters available: `shopify_sale` (public `/products.json`), `jsonld_offers`
(JSON-LD `Product`/`Offer` on an official page), `sitemap_jsonld` (walk the
official sitemap, then read JSON-LD off each product page). `scraper.py` and
`build.py` both read this file; there is no second hard-coded list anywhere.

## Deploy on Cloudflare Pages

The `deploy` job in `update.yml` publishes `site/` with
`wrangler pages deploy` on every run, so there is nothing to connect by hand.
It needs two repository secrets:

- `CLOUDFLARE_API_TOKEN` — must include **Account → Cloudflare Pages → Edit**
- `CLOUDFLARE_ACCOUNT_ID`

If the token is absent the deploy step skips itself and the scrape/build/commit
half still runs, so the repo never stops updating.

Two things worth knowing if you set this up again from scratch:

- `wrangler pages project create` and `pages project list` both call
  `/memberships` first. A token without `User → Memberships → Read` makes
  wrangler print `Unable to get membership roles` and then stop silently with
  exit code 0 — it looks like a permissions problem but nothing is logged.
  Create the project with the API instead:
  `POST /accounts/{id}/pages/projects` with `{"name": "...", "production_branch": "main"}`.
  `wrangler pages deploy` does *not* check memberships and works fine.
- A freshly created deployment URL can return an empty body or refuse the
  connection for a few minutes while it propagates. Do not treat that as a
  failed deploy; re-check before retrying.

## Ground rules

- Only public sitemaps, feeds and official deal pages are crawled; `robots.txt` is respected.
- If a brand's server refuses the crawler (403/429) the page says so and shows nothing.
  We never fill the gap with a guess.
- Monetization only through public terms of established affiliate networks.

---

Site rules are described with the I-Lang protocol — see `.ilang/site.ilang` (protocol: ilang.ai).
