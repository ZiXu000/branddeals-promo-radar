#!/usr/bin/env python3
# ILANG
# TYPE: code | ROLE: builder | PROJECT: branddeals-promo-radar
# ::STATE{@BUILDER, in:data/offers.json + templates/, out:site/}
# ::RULE{读 site.ilang 与 offers.json 拿配置与数据 不硬编码厂商清单}
# ::RULE{结构化数据只用真实抓到的值 抓不到的字段一律不写}
# ::BOUNDARY{never:编价格 编有效期 编优惠|scope:file}
"""
build.py — 纯标准库静态站生成器 (零推理 零密钥)。

输入: data/offers.json (scraper.py 产出) + .ilang/site.ilang + templates/
输出: site/  (index.html provider/*.html deal/*.html compare.html sitemap.xml robots.txt assets/style.css)

边界:
  - 只渲染 offers.json 里真实存在的数据
  - price / priceValidUntil 等字段缺失时, 直接不输出该字段, 不补默认值
"""

import hashlib
import html
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "offers.json"
CONFIG = ROOT / ".ilang" / "site.ilang"
TPL = ROOT / "templates"
OUT = ROOT / "site"

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


# ------------------------------------------------------------------ utils ---

def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "x"


def money(v) -> str:
    if v in (None, ""):
        return ""
    try:
        return "$%s" % format(float(v), ",.2f")
    except Exception:                               # noqa: BLE001
        return ""


def render(tpl: str, ctx: dict) -> str:
    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}",
                  lambda m: str(ctx.get(m.group(1), "")), tpl)


def jsonld(obj) -> str:
    return ('<script type="application/ld+json">'
            + json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "</script>")


def read_site_meta(path: Path) -> dict:
    """从 site.ilang 读 @SITE 那段配置。改这里 重跑 build 站上就变。"""
    meta = {}
    if not path.exists():
        return meta
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("::STATE{@SITE"):
            for k, v in re.findall(r"([a-z_]+):([^,}]+)", line):
                meta[k.strip()] = v.strip()
    return meta


def load():
    if not DATA.exists():
        print("FATAL: %s 不存在, 先跑 scraper.py" % DATA, file=sys.stderr)
        sys.exit(2)
    doc = json.loads(DATA.read_text(encoding="utf-8"))
    doc.setdefault("site", {}).update(read_site_meta(CONFIG))   # 配置优先于缓存
    base = os.environ.get("SITE_BASE_URL", "").rstrip("/")
    if not base:
        base = "https://%s" % (doc["site"].get("domain") or "branddeals.pages.dev")
    return doc, base


def deals_of(prov):
    return sorted([i for i in prov.get("items", []) if i.get("discount_percent")],
                  key=lambda x: -x["discount_percent"])


def deal_slug(prov, item):
    h = hashlib.sha1((item.get("offer_url") or item.get("title", "")).encode()).hexdigest()[:8]
    return "%s-%s" % (slug(prov["name"]), h)


# ------------------------------------------------------------- fragments ---

def frag_deal_card(prov, item, base):
    href = "%s/deal/%s.html" % (base, deal_slug(prov, item))
    img = ('<img src="%s" alt="%s" loading="lazy">' % (esc(item["image"]), esc(item["title"]))
           if item.get("image") else '<div class="ph">no image</div>')
    was = ('<s>%s</s>' % money(item["compare_at_price"])) if item.get("compare_at_price") else ""
    pct = ('<span class="pct">-%d%%</span>' % item["discount_percent"]
           if item.get("discount_percent") else "")
    return ('<a class="card" href="%s">%s<div class="meta"><span class="brand">%s</span>%s'
            '<h3>%s</h3><p class="price">%s %s</p></div></a>'
            % (esc(href), img, esc(prov["name"]), pct, esc(item["title"]),
               esc(money(item["price"])), was))


def frag_item_row(prov, item, base):
    href = "%s/deal/%s.html" % (base, deal_slug(prov, item))
    was = (' <s>%s</s>' % money(item["compare_at_price"])) if item.get("compare_at_price") else ""
    pct = (' <span class="pct">-%d%%</span>' % item["discount_percent"]
           if item.get("discount_percent") else "")
    return ('<tr><td><a href="%s">%s</a></td><td class="num">%s%s%s</td></tr>'
            % (esc(href), esc(item["title"]), esc(money(item["price"])), was, pct))


# ------------------------------------------------------------ page ctxs ---

def ctx_deal(prov, item, base):
    url = "%s/deal/%s.html" % (base, deal_slug(prov, item))
    ld = {
        "@context": "https://schema.org", "@type": "Product",
        "name": item["title"], "url": url,
        "brand": {"@type": "Brand", "name": prov["name"]},
        "offers": {
            "@type": "Offer",
            "url": item.get("offer_url") or url,
            "price": item["price"],
            "priceCurrency": item.get("currency") or "USD",
            "availability": "https://schema.org/InStock" if item.get("available", True)
                            else "https://schema.org/OutOfStock",
        },
    }
    if item.get("valid_until"):
        ld["offers"]["priceValidUntil"] = item["valid_until"]
    if item.get("image"):
        ld["image"] = item["image"]
    crumb = {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": prov["name"],
             "item": "%s/provider/%s.html" % (base, slug(prov["name"]))},
            {"@type": "ListItem", "position": 3, "name": item["title"], "item": url},
        ],
    }
    rows = [("Price", money(item["price"])),
            ("Was", money(item["compare_at_price"]) if item.get("compare_at_price") else ""),
            ("Discount", ("-%d%%" % item["discount_percent"]) if item.get("discount_percent") else ""),
            ("Currency", item.get("currency") or "USD"),
            ("Brand", prov["name"]),
            ("Source", item.get("source_url") or ""),
            ("Fetched (UTC)", item.get("fetched_at") or ""),
            ("Valid until", item.get("valid_until") or "")]
    spec = "".join("<tr><th>%s</th><td>%s</td></tr>" % (esc(k), esc(v)) for k, v in rows if v)
    return {
        "title": esc(item["title"]),
        "provider": esc(prov["name"]),
        "provider_url": "%s/provider/%s.html" % (base, slug(prov["name"])),
        "price": esc(money(item["price"])),
        "was": esc(money(item["compare_at_price"]) if item.get("compare_at_price") else ""),
        "pct": esc(("-%d%% off" % item["discount_percent"]) if item.get("discount_percent") else ""),
        "image_block": ('<img class="hero" src="%s" alt="%s" loading="lazy">'
                        % (esc(item["image"]), esc(item["title"]))) if item.get("image") else "",
        "spec": spec,
        "out_url": esc(item.get("offer_url") or ""),
        "jsonld": jsonld(ld) + jsonld(crumb),
        "canonical": esc(url),
        "fetched": esc(item.get("fetched_at") or ""),
    }


def ctx_provider(prov, base):
    url = "%s/provider/%s.html" % (base, slug(prov["name"]))
    items, deals = prov.get("items", []), deals_of(prov)
    prices = [i["price"] for i in items if i.get("price")]
    ld = {
        "@context": "https://schema.org", "@type": "Product",
        "name": "%s deals & coupons" % prov["name"], "url": url,
        "brand": {"@type": "Brand", "name": prov["name"]},
    }
    if prices:
        ld["offers"] = {"@type": "AggregateOffer", "priceCurrency": "USD",
                        "lowPrice": min(prices), "highPrice": max(prices),
                        "offerCount": len(prices), "url": prov["homepage"]}
    crumb = {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": prov["name"], "item": url}],
    }
    if deals:
        body = ('<h2>On sale now (%d)</h2><table class="items">%s</table>'
                % (len(deals), "".join(frag_item_row(prov, i, base) for i in deals)))
    elif items:
        body = ('<h2>Current catalog (%d)</h2><p class="note">No discounted items found on this '
                'crawl. Prices below are live from the official feed.</p><table class="items">%s</table>'
                % (len(items), "".join(frag_item_row(prov, i, base) for i in items[:40])))
    else:
        body = '<p class="note">%s</p>' % esc({
            "blocked": "The source site refused our crawler (HTTP 403/429). Nothing is shown — "
                       "we never invent prices.",
            "robots_disallow": "robots.txt disallows crawling this source. Nothing is shown.",
        }.get(prov.get("status"), "No offers found at the official source on this crawl."))
    return {
        "provider": esc(prov["name"]), "homepage": esc(prov["homepage"]),
        "status": esc(prov.get("status", "")), "count": len(items),
        "dealcount": len(deals), "body": body,
        "jsonld": jsonld(ld) + jsonld(crumb), "canonical": esc(url),
    }


def ctx_compare(doc, base):
    rows = []
    for p in doc["providers"]:
        items, deals = p.get("items", []), deals_of(p)
        best = deals[0] if deals else None
        rows.append('<tr><td><a href="%s/provider/%s.html">%s</a></td><td>%s</td><td>%d</td><td>%s</td></tr>'
                    % (base, slug(p["name"]), esc(p["name"]), esc(p.get("status", "")),
                       len(items),
                       esc(("-%d%% · %s" % (best["discount_percent"], best["title"][:64]))
                           if best else "—")))
    ld = {"@context": "https://schema.org", "@type": "ItemList",
          "name": "Tracked brands comparison",
          "itemListElement": [{"@type": "ListItem", "position": n + 1, "name": p["name"],
                               "url": "%s/provider/%s.html" % (base, slug(p["name"]))}
                              for n, p in enumerate(doc["providers"])]}
    return {"rows": "".join(rows), "jsonld": jsonld(ld),
            "canonical": esc(base + "/compare.html")}


# ------------------------------------------------------------------ main ---

def main() -> int:
    doc, base = load()
    brand = doc["site"].get("brand", "BrandDeals")
    providers = doc["providers"]

    if OUT.exists():
        shutil.rmtree(OUT)
    for d in ("assets", "provider", "deal"):
        (OUT / d).mkdir(parents=True)

    (OUT / "assets" / "style.css").write_text(CSS.strip(), encoding="utf-8")

    t_base = (TPL / "base.html").read_text(encoding="utf-8")
    t_idx = (TPL / "index.html").read_text(encoding="utf-8")
    t_prov = (TPL / "provider.html").read_text(encoding="utf-8")
    t_deal = (TPL / "deal.html").read_text(encoding="utf-8")
    t_cmp = (TPL / "compare.html").read_text(encoding="utf-8")

    def wrap(inner: str, title: str, desc: str, canonical: str, og: str) -> str:
        return render(t_base, {
            "inner": inner, "title": esc(title), "desc": esc(desc),
            "canonical": esc(canonical), "base": base, "brand": esc(brand),
            "og_type": og, "generated": esc(doc["generated_at"]),
        })

    # ---- index
    all_deals = sorted([(p, i) for p in providers for i in deals_of(p)],
                       key=lambda x: -x[1]["discount_percent"])
    cards = "".join(frag_deal_card(p, i, base) for p, i in all_deals[:12]) or \
        '<p class="note">No discounted items across tracked brands on this crawl. ' \
        'This page updates automatically every few hours.</p>'
    plist = "".join(
        '<a class="prow" href="%s/provider/%s.html"><span class="pn">%s</span>'
        '<span class="pc">%d offers · %d on sale</span></a>'
        % (base, slug(p["name"]), esc(p["name"]), len(p.get("items", [])), len(deals_of(p)))
        for p in providers)
    ld_index = {"@context": "https://schema.org", "@type": "ItemList",
                "name": "%s — tracked brands" % brand,
                "itemListElement": [{"@type": "ListItem", "position": n + 1, "name": p["name"],
                                     "url": "%s/provider/%s.html" % (base, slug(p["name"]))}
                                    for n, p in enumerate(providers)]}
    now = datetime.now(timezone.utc)
    idx_inner = render(t_idx, {
        "cards": cards, "provider_list": plist,
        "stats": "%d brands · %d offers · %d on sale"
                 % (len(providers), doc["stats"]["items"], doc["stats"]["deals"]),
        "month": "%s %d" % (MONTHS[now.month - 1], now.year),
        "jsonld": jsonld(ld_index)})
    (OUT / "index.html").write_text(wrap(
        idx_inner, "%s — official brand deals & coupons" % brand,
        "Auto-updating tracker of official deals from %d consumer brands. "
        "Real prices pulled from public feeds." % len(providers),
        base + "/", "website"), encoding="utf-8")

    # ---- provider + deal pages
    for p in providers:
        c = ctx_provider(p, base)
        (OUT / "provider" / ("%s.html" % slug(p["name"]))).write_text(wrap(
            render(t_prov, c), "%s deals & coupons — %s" % (p["name"], brand),
            "Live offers and discounts from %s, refreshed automatically." % p["name"],
            c["canonical"], "website"), encoding="utf-8")
        for i in deals_of(p):
            d = ctx_deal(p, i, base)
            (OUT / "deal" / ("%s.html" % deal_slug(p, i))).write_text(wrap(
                render(t_deal, d), "%s — %s" % (i["title"], p["name"]),
                ("%s from %s at %s" % (i["title"], p["name"], money(i["price"]))).strip(),
                d["canonical"], "product"), encoding="utf-8")

    # ---- compare
    cc = ctx_compare(doc, base)
    (OUT / "compare.html").write_text(wrap(
        render(t_cmp, cc), "Compare tracked brands", "Coverage and best discounts per brand.",
        cc["canonical"], "website"), encoding="utf-8")

    # ---- sitemap + robots
    urls = [("", doc["generated_at"]), ("compare.html", doc["generated_at"])]
    for p in providers:
        urls.append(("provider/%s.html" % slug(p["name"]), doc["generated_at"]))
        for i in deals_of(p):
            urls.append(("deal/%s.html" % deal_slug(p, i), doc["generated_at"]))
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    sm += ["  <url><loc>%s/%s</loc><lastmod>%s</lastmod></url>" % (base, u, lm[:10])
           for u, lm in urls]
    sm.append("</urlset>")
    (OUT / "sitemap.xml").write_text("\n".join(sm), encoding="utf-8")
    (OUT / "robots.txt").write_text(
        "User-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n" % base, encoding="utf-8")

    n_html = len(list(OUT.rglob("*.html")))
    print("built site/ -> %s" % OUT)
    print("  providers=%d deals=%d html=%d sitemap_urls=%d"
          % (len(providers), doc["stats"]["deals"], n_html, len(urls)))
    return 0


CSS = """
:root{--bg:#0d1117;--card:#161b22;--line:#21262d;--fg:#e6edf3;--mut:#8b949e;--acc:#2f81f7;--ok:#3fb950}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
header{border-bottom:1px solid var(--line);padding:14px 20px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
header .b{font-weight:700;font-size:18px}header .s{color:var(--mut);font-size:13px}
nav a{color:var(--mut);font-size:14px;margin-right:14px}
main{max-width:1080px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:26px;margin:6px 0 4px}h2{font-size:18px;margin:26px 0 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;display:block;color:inherit}
.card img,.card .ph{width:100%;height:150px;object-fit:cover;background:#0b0f14;display:flex;align-items:center;justify-content:center;color:#30363d;font-size:12px}
.card .meta{padding:10px 12px}
.card h3{font-size:14px;margin:6px 0;font-weight:600;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.brand{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px}
.price{margin:4px 0 0;font-weight:700}.price s{color:var(--mut);font-weight:400;margin-left:6px}
.pct{float:right;background:var(--ok);color:#04120a;font-size:11px;font-weight:700;padding:2px 6px;border-radius:5px}
table.items,table.cmp{width:100%;border-collapse:collapse;font-size:14px}
table.items td,table.cmp td,table.cmp th{border-bottom:1px solid var(--line);padding:8px 6px;text-align:left}
td.num{text-align:right;white-space:nowrap}td.num s{color:var(--mut);margin-left:6px}
.prow{display:flex;justify-content:space-between;border-bottom:1px solid var(--line);padding:9px 2px;color:inherit}
.prow .pc{color:var(--mut);font-size:13px}
.note{color:var(--mut);background:var(--card);border:1px solid var(--line);padding:12px;border-radius:8px}
table.spec{width:100%;border-collapse:collapse;margin:12px 0}
table.spec th{width:170px;text-align:left;color:var(--mut);font-weight:500;padding:7px 0;border-bottom:1px solid var(--line);vertical-align:top}
table.spec td{padding:7px 0;border-bottom:1px solid var(--line);word-break:break-all}
.hero{max-width:100%;max-height:340px;border-radius:10px;border:1px solid var(--line)}
.btn{display:inline-block;background:var(--acc);color:#fff;padding:10px 18px;border-radius:8px;font-weight:600;margin:8px 0}
.crumbs{color:var(--mut);font-size:13px;margin-bottom:8px}
footer{border-top:1px solid var(--line);color:var(--mut);font-size:12px;padding:18px 20px;max-width:1080px;margin:0 auto}
"""


if __name__ == "__main__":
    sys.exit(main())
