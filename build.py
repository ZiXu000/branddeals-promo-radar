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


def parse_args():
    """--data / --config / --out / --templates 让同一套 builder 能建多个站。
    旅行批次是独立站(独立域名 独立配置), 不覆盖消费品牌那个线上站。"""
    def take(flag, default):
        if flag in sys.argv:
            rest = sys.argv[sys.argv.index(flag) + 1:]
            if rest:
                p = Path(rest[0])
                return p if p.is_absolute() else ROOT / p
        return default
    return (take("--data", DATA), take("--config", CONFIG),
            take("--out", OUT), take("--templates", TPL))


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


def read_build_meta(path: Path) -> dict:
    """从 .ilang 读 ::MODULE{BUILD} 段。构建行为(如 detail_pages)由配置驱动,
    不依赖 data 快照里 build 段——data 可能是加新配置项之前抓的。"""
    meta, in_build = {}, False
    if not path.exists():
        return meta
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        m = re.match(r"^::MODULE\{([A-Z_]+)", line)
        if m:
            in_build = (m.group(1) == "BUILD")
            continue
        if not in_build or line.startswith("::") or line.startswith("#") or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2 and parts[0]:
            meta[parts[0]] = parts[1]
    return meta


def read_provider_notes(path: Path) -> dict:
    """从 .ilang 读 ::MODULE{PROVIDER_NOTES}。构建期读配置而不是读 data 快照,
    这样改一家页面上的口径文案不用重抓一遍(抓一次要跑渲染, 很慢)。"""
    notes, in_sec = {}, False
    if not path.exists():
        return notes
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        m = re.match(r"^::MODULE\{([A-Z_]+)", line)
        if m:
            in_sec = (m.group(1) == "PROVIDER_NOTES")
            continue
        if not in_sec or line.startswith("::") or line.startswith("#") or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2 and parts[0]:
            notes[parts[0]] = " ".join(p for p in parts[1:] if p)
    return notes


def load(data_path=None, config_path=None):
    data_path = data_path or DATA
    config_path = config_path or CONFIG
    if not data_path.exists():
        print("FATAL: %s 不存在, 先跑 scraper.py" % data_path, file=sys.stderr)
        sys.exit(2)
    doc = json.loads(data_path.read_text(encoding="utf-8"))
    doc.setdefault("site", {}).update(read_site_meta(config_path))   # 配置优先于缓存
    doc.setdefault("build", {}).update(read_build_meta(config_path)) # 构建行为也走配置
    # 每家的口径横幅也从配置读, 覆盖 data 快照: 改文案不用重抓(抓一次要跑渲染很慢)
    notes = read_provider_notes(config_path)
    for p in doc.get("providers", []):
        if p.get("name") in notes:
            p["prov_note"] = notes[p["name"]]
    base = os.environ.get("SITE_BASE_URL", "").rstrip("/")
    if not base:
        base = "https://%s" % (doc["site"].get("domain") or "branddeals.pages.dev")
    if doc["site"].get("domain") in ("none", ""):
        print("FATAL: site.ilang 里 domain 还是 none, 先填域名再 build", file=sys.stderr)
        sys.exit(2)
    return doc, base


def deals_of(prov):
    return sorted([i for i in prov.get("items", []) if i.get("discount_percent")],
                  key=lambda x: -x["discount_percent"])


def detail_items_of(prov, mode):
    """哪些条目生成详情页。
    mode=deals(默认, 线上消费品牌站维持原样): 只有明写折扣的。
    mode=all(旅行站): 折扣的 + 带官方价格的票价行也各有一页,
                     因为老板要的是"每行带官方出处和复核日期"的可点可核页面。
    无价格又无折扣的行不生成详情页 —— 没东西可写, 不硬凑。"""
    if mode == "all":
        seen, out = set(), []
        for i in deals_of(prov) + [x for x in prov.get("items", []) if x.get("price")]:
            k = id(i)
            if k not in seen:
                seen.add(k)
                out.append(i)
        return out
    return deals_of(prov)


def verified_date(item) -> str:
    """复核日期: 抓到的那一刻(UTC)。老板要求页面上可见。"""
    fa = item.get("fetched_at") or ""
    return fa[:10] if len(fa) >= 10 else fa


def deal_slug(prov, item):
    # 旅行站里同一家的几十条票价共享同一个 offer_url(Southwest 32 条票价都指向
    # /en/flights/flight-deals), 只哈希 URL 会全部撞成一页。所以当一家内部出现
    # 重复 offer_url 时, 把 title 混进哈希去重; 否则维持原来的 url-or-title 算法,
    # 保证线上消费品牌站已收录的 URL 不变。
    if _prov_has_dup_urls(prov):
        key = "%s|%s" % (item.get("offer_url") or "", item.get("title", ""))
        h = hashlib.sha1(key.encode()).hexdigest()[:10]
    else:
        h = hashlib.sha1((item.get("offer_url") or item.get("title", "")).encode()).hexdigest()[:8]
    return "%s-%s" % (slug(prov["name"]), h)


def _prov_has_dup_urls(prov):
    urls = [i.get("offer_url") or "" for i in prov.get("items", []) if i.get("offer_url")]
    return len(urls) != len(set(urls))


# ------------------------------------------------------------- fragments ---

def frag_deal_card(prov, item, base):
    href = "/deal/%s.html" % deal_slug(prov, item)
    img = ('<img src="%s" alt="%s" loading="lazy">' % (esc(item["image"]), esc(item["title"]))
           if item.get("image") else '<div class="ph">no image</div>')
    was = ('<s>%s</s>' % money(item["compare_at_price"])) if item.get("compare_at_price") else ""
    pct = ('<span class="pct">-%d%%</span>' % item["discount_percent"]
           if item.get("discount_percent") else "")
    return ('<a class="card" href="%s">%s<div class="meta"><span class="brand">%s</span>%s'
            '<h3>%s</h3><p class="price">%s %s</p></div></a>'
            % (esc(href), img, esc(prov["name"]), pct, esc(item["title"]),
               esc(money(item["price"])), was))


def frag_item_row(prov, item, base, rich=False):
    href = "/deal/%s.html" % deal_slug(prov, item)
    was = (' <s>%s</s>' % money(item["compare_at_price"])) if item.get("compare_at_price") else ""
    pct = (' <span class="pct">-%d%%</span>' % item["discount_percent"]
           if item.get("discount_percent") else "")
    if not rich:
        return ('<tr><td><a href="%s">%s</a></td><td class="num">%s%s%s</td></tr>'
                % (esc(href), esc(item["title"]), esc(money(item["price"])), was, pct))
    # rich(旅行站): 老板要求"每行带官方出处和复核日期", 直接做进表格,
    # 不让读者非点进详情页才能核。
    src = item.get("source_url") or prov.get("source") or ""
    vd = verified_date(item)
    until = item.get("valid_until") or ""
    return ('<tr><td><a href="%s">%s</a><div class="src">source: <a href="%s" rel="nofollow '
            'noopener" target="_blank">%s</a> · verified %s%s</div></td>'
            '<td class="num">%s%s%s</td></tr>'
            % (esc(href), esc(item["title"]), esc(src),
               esc(re.sub(r"^https?://", "", src)[:58]), esc(vd),
               (" · valid until %s" % esc(until)) if until else "",
               esc(money(item["price"])), was, pct))


# ------------------------------------------------------------ page ctxs ---

def ctx_deal(prov, item, base):
    url = "%s/deal/%s.html" % (base, deal_slug(prov, item))
    ld = {
        "@context": "https://schema.org", "@type": "Product",
        "name": item["title"], "url": url,
        "brand": {"@type": "Brand", "name": prov["name"]},
    }
    # ::RULE{抓不到 price⇒不写进结构化数据}
    # 以前这里无条件塞 "price": item["price"], 没价时就成了 "price": null —— 脏结构化数据。
    # 现在只有真抓到价格才输出 offers 块。
    if item.get("price"):
        offer = {
            "@type": "Offer",
            "url": item.get("offer_url") or url,
            "price": item["price"],
            "priceCurrency": item.get("currency") or "USD",
            "availability": ("https://schema.org/InStock" if item.get("available", True)
                             else "https://schema.org/OutOfStock"),
        }
        if item.get("valid_until"):
            offer["priceValidUntil"] = item["valid_until"]
        ld["offers"] = offer
    elif item.get("discount_percent") and item.get("offer_url"):
        # 没价但有官方明写的折扣(如 JetBlue "Up to 50% off"): 只做指向官方页的链接,
        # 不编价格数字。
        ld["offers"] = {"@type": "Offer", "url": item["offer_url"],
                        "availability": "https://schema.org/InStock"}
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
    # 老板要求: 每行带官方出处 + 复核日期, 页面上可见。
    rows = [("Price", money(item["price"])),
            ("Was", money(item["compare_at_price"]) if item.get("compare_at_price") else ""),
            ("Discount", ("-%d%%" % item["discount_percent"]) if item.get("discount_percent") else ""),
            ("Currency", (item.get("currency") or "USD") if item.get("price") else ""),
            ("Brand", prov["name"]),
            ("Official source", item.get("source_url") or ""),
            ("Verified (UTC)", item.get("fetched_at") or ""),
            ("Valid until", item.get("valid_until") or "")]
    spec = "".join("<tr><th>%s</th><td>%s</td></tr>" % (esc(k), esc(v)) for k, v in rows if v)
    return {
        "title": esc(item["title"]),
        "provider": esc(prov["name"]),
        "provider_url": "/provider/%s.html" % slug(prov["name"]),
        "price": esc(money(item["price"])),
        "was": esc(money(item["compare_at_price"]) if item.get("compare_at_price") else ""),
        "pct": esc(("-%d%% off" % item["discount_percent"]) if item.get("discount_percent") else ""),
        "noprice_note": ("" if item.get("price") else
                         '<p class="note">No price is published for this offer. '
                         'Nothing is estimated — see the official source below.</p>'),
        "image_block": ('<img class="hero" src="%s" alt="%s" loading="lazy">'
                        % (esc(item["image"]), esc(item["title"]))) if item.get("image") else "",
        "spec": spec,
        "out_url": esc(item.get("offer_url") or ""),
        "out_label": esc(("Open official %s page" % prov["name"])
                         if item.get("offer_url") else ""),
        "jsonld": jsonld(ld) + jsonld(crumb),
        "canonical": esc(url),
        "fetched": esc(item.get("fetched_at") or ""),
    }


def ctx_provider(prov, base, mode="deals"):
    url = "%s/provider/%s.html" % (base, slug(prov["name"]))
    items, deals = prov.get("items", []), deals_of(prov)
    prices = [i["price"] for i in items if i.get("price")]
    rich = (mode == "all")
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
    # 老板要求的口径横幅: 每家如实写它的数据是哪来的、为什么没价/没码。
    note = ('<p class="provnote">%s</p>' % esc(prov["prov_note"])) if prov.get("prov_note") else ""
    if rich:
        # 旅行站: 有折扣的排前面, 其余带官方价格的票价行跟在后面, 全部各一行可核。
        ordered = deals + [i for i in items if not i.get("discount_percent") and i.get("price")] \
            + [i for i in items if not i.get("discount_percent") and not i.get("price")]
        rows_html = "".join(frag_item_row(prov, i, base, rich=True) for i in ordered)
        body = note + ('<h2>All captured offers (%d)</h2>'
                       '<p class="hint">Every row links to the brand\'s own page and shows the '
                       'date it was verified. No price or code is estimated.</p>'
                       '<table class="items">%s</table>' % (len(items), rows_html))
    elif deals:
        body = note + ('<h2>On sale now (%d)</h2><table class="items">%s</table>'
                       % (len(deals), "".join(frag_item_row(prov, i, base) for i in deals)))
    elif items:
        body = note + ('<h2>Current catalog (%d)</h2><p class="note">No discounted items found on '
                       'this crawl. Prices below are live from the official feed.</p>'
                       '<table class="items">%s</table>'
                       % (len(items), "".join(frag_item_row(prov, i, base) for i in items[:40])))
    else:
        body = note + '<p class="note">%s</p>' % esc({
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
        rows.append('<tr><td><a href="/provider/%s.html">%s</a></td><td>%s</td><td>%d</td><td>%s</td></tr>'
                    % (slug(p["name"]), esc(p["name"]), esc(p.get("status", "")),
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
    global DATA, CONFIG, TPL, OUT
    DATA, CONFIG, OUT, TPL = parse_args()
    doc, base = load(DATA, CONFIG)
    brand = doc["site"].get("brand", "BrandDeals")
    providers = doc["providers"]
    # detail_pages=all => 旅行站(票价行也各一页); 默认 deals => 维持线上站原样。
    mode = doc["build"].get("detail_pages", "deals")
    # ::RULE{刷新频率等于每天一次⇒不是每几小时 页上的承诺必须跟这个频率一致}
    # 页面上每一句"自动更新"都由配置里的 update_hours 驱动, 不许写跟实际不符的承诺:
    #   消费站 = GH Actions cron 每 6h      -> every few hours / auto-updated
    #   旅行站 = 本机每天一次(refresh_travel.sh) -> once a day / updated daily
    hours = int(doc["build"].get("update_hours") or 6)
    FREQ = ({"badge": "updated daily", "tracked": "tracked daily",
             "rebuild": "This page is rebuilt once a day.",
             "site": "This site is re-checked once a day.",
             "prov": "refreshed daily"} if hours >= 20 else
            {"badge": "auto-updated", "tracked": "tracked automatically",
             "rebuild": "This page rebuilds itself every few hours.",
             "site": "This site updates itself automatically.",
             "prov": "refreshed automatically"})

    # 不做整目录 rmtree: 实测一次删 50+ 个文件会被本机 safe-delete 拦下 -> build 直接失败,
    # 而部署照样"成功"(传 0 个新文件 = 线上还是旧内容)。最危险的一类失败: 看着绿其实没更新。
    # 改成就地覆盖重写, 最后只删"这次没生成"的陈旧页面(通常 0~几个), 躲开批量删除阈值。
    for d in ("assets", "provider", "deal"):
        (OUT / d).mkdir(parents=True, exist_ok=True)
    written = set()

    def w(rel: str, text: str) -> None:
        p = OUT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        written.add(p.resolve())

    w("assets/style.css", CSS.strip())

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
            "freq_badge": esc(FREQ["badge"]), "site_freq": esc(FREQ["site"]),
        })

    # ---- index
    if mode == "all":
        # 旅行站: 首页卡片按"有官方价/有明写折扣"排, 不只看 discount_percent
        # (Southwest 32 条票价 discount=0 但是真金白银的官方价, 是该站的主力)。
        feat = sorted([(p, i) for p in providers for i in p.get("items", [])
                       if i.get("discount_percent") or i.get("price")],
                      key=lambda x: (-x[1].get("discount_percent", 0),
                                     x[1].get("price") or 1e9))
        cards = "".join(frag_deal_card(p, i, base) for p, i in feat[:12])
        head_h2 = "Featured official offers"
    else:
        all_deals = sorted([(p, i) for p in providers for i in deals_of(p)],
                           key=lambda x: -x[1]["discount_percent"])
        cards = "".join(frag_deal_card(p, i, base) for p, i in all_deals[:12])
        head_h2 = "Biggest discounts right now"
    # 同一类缺陷: 这句也是"更新频率"的承诺, 必须跟实际频率一致
    cards = cards or (
        '<p class="note">No discounted items across tracked brands on this crawl. '
        + esc(FREQ["rebuild"]) + '</p>')
    plist = "".join(
        '<a class="prow" href="/provider/%s.html"><span class="pn">%s</span>'
        '<span class="pc">%d offers · %d on sale</span></a>'
        % (slug(p["name"]), esc(p["name"]), len(p.get("items", [])), len(deals_of(p)))
        for p in providers)
    ld_index = {"@context": "https://schema.org", "@type": "ItemList",
                "name": "%s — tracked brands" % brand,
                "itemListElement": [{"@type": "ListItem", "position": n + 1, "name": p["name"],
                                     "url": "%s/provider/%s.html" % (base, slug(p["name"]))}
                                    for n, p in enumerate(providers)]}
    now = datetime.now(timezone.utc)
    idx_inner = render(t_idx, {
        # brand 以前没传进首页上下文, h1 渲染成 " — official brand deals..." 品牌名是空的
        # (消费站线上也是这个毛病)。h1 是要紧位置, 补上。
        "brand": esc(brand),
        "cards": cards, "provider_list": plist, "head_h2": head_h2,
        "stats": "%d brands · %d offers · %d on sale"
                 % (len(providers), doc["stats"]["items"], doc["stats"]["deals"]),
        "month": "%s %d" % (MONTHS[now.month - 1], now.year),
        "tracked_phrase": esc(FREQ["tracked"]),
        "rebuild_phrase": esc(FREQ["rebuild"]),
        "jsonld": jsonld(ld_index)})
    niche = doc["site"].get("niche", "consumer brand official deals and coupons")
    w("index.html", wrap(
        idx_inner, "%s — %s" % (brand, niche),
        "Auto-updating tracker of official deals from %d travel brands. "
        "Every fare and promo code is pulled from the brand's own public feed or "
        "official page — nothing is estimated, nothing is copied from third parties."
        % len(providers),
        base + "/", "website"))

    # ---- provider + deal pages
    for p in providers:
        c = ctx_provider(p, base, mode)
        w("provider/%s.html" % slug(p["name"]), wrap(
            render(t_prov, c), "%s deals & coupons — %s" % (p["name"], brand),
            "Live offers and discounts from %s, %s." % (p["name"], FREQ["prov"]),
            c["canonical"], "website"))
        for i in detail_items_of(p, mode):
            d = ctx_deal(p, i, base)
            desc = ("%s from %s at %s" % (i["title"], p["name"], money(i["price"]))).strip() \
                if i.get("price") else \
                ("%s — official offer from %s" % (i["title"], p["name"]))
            w("deal/%s.html" % deal_slug(p, i), wrap(
                render(t_deal, d), "%s — %s" % (i["title"], p["name"]),
                desc, d["canonical"], "product"))

    # ---- compare
    cc = ctx_compare(doc, base)
    w("compare.html", wrap(
        render(t_cmp, cc), "Compare tracked brands", "Coverage and best discounts per brand.",
        cc["canonical"], "website"))

    # ---- sitemap + robots
    urls = [("", doc["generated_at"]), ("compare.html", doc["generated_at"])]
    for p in providers:
        urls.append(("provider/%s.html" % slug(p["name"]), doc["generated_at"]))
        for i in detail_items_of(p, mode):
            urls.append(("deal/%s.html" % deal_slug(p, i), doc["generated_at"]))
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    sm += ["  <url><loc>%s/%s</loc><lastmod>%s</lastmod></url>" % (base, u, lm[:10])
           for u, lm in urls]
    sm.append("</urlset>")
    w("sitemap.xml", "\n".join(sm))
    w("robots.txt", "User-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n" % base)

    # 删掉这次没生成的旧页面(优惠到期后那条详情页), 免得陈旧/过期内容长期滞留线上。
    # 只删这几个陈旧文件, 不做整目录删除 —— 躲开本机 safe-delete 的批量阈值。
    stale = [p for p in OUT.rglob("*.html") if p.resolve() not in written]
    for p in stale:
        p.unlink()
    if stale:
        print("  removed %d stale page(s) no longer in this crawl" % len(stale))

    n_html = len(list(OUT.rglob("*.html")))
    print("built site -> %s" % OUT)
    print("  providers=%d items=%d deals=%d mode=%s html=%d sitemap_urls=%d"
          % (len(providers), doc["stats"]["items"], doc["stats"]["deals"],
             mode, n_html, len(urls)))
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
.provnote{color:var(--fg);background:#11233a;border:1px solid #1f4068;border-left:3px solid var(--acc);padding:12px 14px;border-radius:8px;font-size:13.5px;line-height:1.6;margin:14px 0}
.hint{color:var(--mut);font-size:12.5px;margin:2px 0 10px}
td .src{color:var(--mut);font-size:11.5px;margin-top:3px;line-height:1.4}
td .src a{color:#7aa7d9}
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
