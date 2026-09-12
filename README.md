# BrandDeals — Promo Radar

Auto-updating tracker of **official** deals and coupon pages from consumer brands
(meal kits, beauty, apparel, supplements, software subscriptions). Prices are pulled
from each brand's own public product feed or official page — never invented.

- **Live site:** https://branddeals-promo-radar.pages.dev
- **Data:** [`data/offers.json`](data/offers.json) — rewritten on every crawl
- **Site rules:** [`.ilang/site.ilang`](.ilang/site.ilang)

## How it works

```
scraper.py   → fetch public feeds / official pages  → data/offers.json
build.py     → render static site                   → site/
.github/workflows/update.yml → cron every 6h: scrape + build + commit
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
(JSON-LD `Product`/`Offer` on an official page). `scraper.py` and `build.py`
both read this file; there is no second hard-coded list anywhere.

## Deploy on Cloudflare Pages

1. Connect the repo in Cloudflare Pages.
2. Build command: `python build.py`
3. Build output directory: `site`
4. (Optional) set env var `SITE_BASE_URL` to your custom domain.

## Ground rules

- Only public sitemaps, feeds and official deal pages are crawled; `robots.txt` is respected.
- If a brand's server refuses the crawler (403/429) the page says so and shows nothing.
  We never fill the gap with a guess.
- Monetization only through public terms of established affiliate networks.

---

Site rules are described with the I-Lang protocol — see `.ilang/site.ilang` (protocol: ilang.ai).
