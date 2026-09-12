#!/usr/bin/env python3
# ILANG
# TYPE: code | ROLE: scraper | PROJECT: branddeals-promo-radar
# ::STATE{@SCRAPER, in:.ilang/site.ilang, out:data/offers.json}
# ::RULE{只抓公开的 sitemap / feed / 官方优惠页 遵守 robots.txt 不绕反爬 不抓登录后内容}
# ::RULE{抓不到就如实标记 不编优惠 不编价格 不拿估的顶}
# ::BOUNDARY{never:编优惠 编价格 编佣金|scope:file}
"""
scraper.py — 运行时零推理 零密钥 纯标准库。

职责:
  1. 读 .ilang/site.ilang 拿配置(厂商清单/抓取入口/字段)
  2. 逐家抓公开源 提取 优惠名 价格 币种 划线价 折扣 链接 图片
  3. 写 data/offers.json (每次运行整份覆盖)

边界:
  - 不改 site.ilang, 不写死厂商清单(清单唯一真源是 site.ilang)
  - 源站拒绝(403/429)或超时: 记 status, 不伪造数据
"""

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / ".ilang" / "site.ilang"
OUT = ROOT / "data" / "offers.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
TIMEOUT = 25
RETRIES = 2


# ---------------------------------------------------------------- config ---

def read_config(path: Path) -> dict:
    """解析 I-Lang 配置。任何一行含 '|' 且不以 '::' / '#' 开头 => 数据行。"""
    cfg = {"site": {}, "providers": [], "fields": [], "build": {}}
    section = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^::MODULE\{([A-Z_]+)", line)
        if m:
            section = m.group(1)
            continue
        if line.startswith("::STATE"):
            for k, v in re.findall(r"([a-z_]+):([^,}]+)", line):
                cfg["site"][k.strip()] = v.strip()
            continue
        if line.startswith("::") or line.startswith("#"):
            continue
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if section == "PROVIDERS" and len(parts) >= 4 and parts[0]:
            cfg["providers"].append({
                "name": parts[0],
                "homepage": parts[1],
                "source": parts[2],
                "adapter": parts[3],
                "affiliate": parts[4] if len(parts) > 4 else "",
            })
        elif section == "FIELDS":
            cfg["fields"] = [p for p in parts if p]
        elif section == "BUILD" and len(parts) >= 2:
            cfg["build"][parts[0]] = parts[1]
    return cfg


# ------------------------------------------------------------------- http ---

def http_get(url: str) -> tuple:
    """返回 (status, body_bytes, final_url, error)。跟随重定向, 带重试。"""
    last = ("", b"", url, "unknown")
    for attempt in range(RETRIES + 1):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,*/*;q=0.8"),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return (r.status, r.read(), r.geturl(), None)
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            last = (e.code, body, url, "HTTP %s" % e.code)
            if e.code in (403, 404, 429, 451):     # 不再重试硬拒绝
                break
        except Exception as e:                      # noqa: BLE001
            last = ("", b"", url, "%s: %s" % (type(e).__name__, e))
        time.sleep(1.5 * (attempt + 1))
    return last


def robots_allowed(source_url: str) -> bool:
    """轻量 robots.txt 检查: 只看 User-agent: * 段里的 Disallow 前缀。"""
    try:
        p = urllib.parse.urlparse(source_url)
        root = "%s://%s" % (p.scheme, p.netloc)
        st, body, _, _ = http_get(root + "/robots.txt")
        if st != 200 or not body:
            return True                             # 拿不到 robots 就当允许
        text = body.decode("utf-8", "ignore")
        disallows, active = [], False
        for line in text.splitlines():
            line = line.split("#")[0].strip()
            if not line:
                continue
            k, _, v = line.partition(":")
            k, v = k.strip().lower(), v.strip()
            if k == "user-agent":
                active = v in ("*",)
            elif active and k == "disallow" and v:
                disallows.append(v)
        path = p.path or "/"
        for d in disallows:
            if d != "/" and path.startswith(d):
                return False
            if d == "/":
                return False
        return True
    except Exception:                               # noqa: BLE001
        return True


# --------------------------------------------------------------- helpers ---

def money(v) -> float:
    try:
        return round(float(str(v).replace(",", "").strip()), 2)
    except Exception:                               # noqa: BLE001
        return None


def discount_pct(price, compare_at):
    if price and compare_at and compare_at > price > 0:
        return int(round((compare_at - price) / compare_at * 100))
    return 0


def strip_tags(s: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def clean_title(t: str) -> str:
    return re.sub(r"\s+", " ", strip_tags(t))[:160]


# -------------------------------------------------------------- adapters ---

def adapt_shopify_sale(prov: dict) -> tuple:
    """Shopify 公开 /products.json。有划线价的算 deal, 其余算在售商品。"""
    st, body, final, err = http_get(prov["source"])
    if st != 200 or not body:
        return [], {"status": "blocked" if st in (403, 429, 451) else "error",
                    "http": st, "note": err or "no body"}
    try:
        data = json.loads(body.decode("utf-8", "ignore"))
    except Exception as e:                          # noqa: BLE001
        return [], {"status": "error", "http": st, "note": "bad json: %s" % e}

    host = "%s://%s" % (urllib.parse.urlparse(final).scheme,
                        urllib.parse.urlparse(final).netloc)
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    items, seen = [], set()
    for prod in data.get("products", []):
        handle = prod.get("handle", "")
        if not handle or handle in seen:
            continue
        seen.add(handle)
        variants = prod.get("variants") or []
        if not variants:
            continue
        v = min(variants, key=lambda x: money(x.get("price")) or 9e9)
        price = money(v.get("price"))
        compare = money(v.get("compare_at_price"))
        if price is None:
            continue
        img = ""
        if prod.get("images"):
            img = (prod["images"][0].get("src") or "")
        items.append({
            "title": clean_title(prod.get("title", "")),
            "price": price,
            "currency": "USD",
            "compare_at_price": compare,
            "discount_percent": discount_pct(price, compare),
            "available": bool(v.get("available", True)),
            "offer_url": "%s/products/%s" % (host, handle),
            "image": img,
            "valid_until": None,
            "source_url": prov["source"],
            "fetched_at": fetched,
            "extract": "feed",
        })
    return items, {"status": "ok", "http": st, "count": len(items)}


def _extract_jsonld(html: str, page_url: str, prov: dict, fetched: str) -> list:
    """从一段 HTML 里抽 JSON-LD 的 Product / ProductGroup 优惠。"""
    items = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
            return
        if not isinstance(node, dict):
            return
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(x in ("Product", "ProductGroup") for x in types):
            offers = node.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict):
                price = money(offers.get("price") or offers.get("lowPrice"))
                if price:
                    img = node.get("image")
                    if isinstance(img, list):
                        img = img[0] if img else ""
                    if isinstance(img, dict):
                        img = img.get("url", "")
                    items.append({
                        "title": clean_title(node.get("name", "")),
                        "price": price,
                        "currency": offers.get("priceCurrency") or "USD",
                        "compare_at_price": None,
                        "discount_percent": 0,
                        "available": "OutOfStock" not in str(offers.get("availability", "")),
                        "offer_url": offers.get("url") or node.get("url") or page_url,
                        "image": img if isinstance(img, str) else "",
                        "valid_until": offers.get("priceValidUntil"),
                        "source_url": prov["source"],
                        "fetched_at": fetched,
                        "extract": "jsonld",
                    })
        for k, v in node.items():
            if k == "hasVariant":                   # 变体不单独算条目, 只用主商品
                continue
            if isinstance(v, (dict, list)):
                walk(v)

    for blob in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.I | re.S):
        try:
            walk(json.loads(blob.strip()))
        except Exception:                           # noqa: BLE001
            continue
    return items


def adapt_jsonld_offers(prov: dict) -> tuple:
    """抓官方页, 解 JSON-LD 里的 Product/Offer。用于非 Shopify 品牌。"""
    st, body, final, err = http_get(prov["source"])
    if st != 200 or not body:
        return [], {"status": "blocked" if st in (403, 429, 451) else "error",
                    "http": st, "note": err or "no body"}
    html = body.decode("utf-8", "ignore")
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    items = _extract_jsonld(html, final, prov, fetched)
    if items:
        return items, {"status": "ok", "http": st, "count": len(items)}
    scanned = scan_prices(html, prov, final, fetched)
    return scanned, {"status": "ok" if scanned else "empty",
                     "http": st, "count": len(scanned),
                     "note": "" if scanned else "no price found in page text"}


PRICE_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
HEAD_RE = re.compile(r"<h[1-4][^>]*>(.*?)</h[1-4]>", re.I | re.S)
CLEAN_TXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ,.'&+/()%:-]{1,59}$")
SKIP_CTX = ("shipping", "orders over", "order over", "free delivery", "minimum spend",
            "gift card", "per month billed", "tax")


def scan_prices(html_text: str, prov: dict, page_url: str, fetched: str) -> list:
    """兜底适配器: 官方页没有 JSON-LD 时, 从可见正文扫真实价格。
    标题优先取该价格之前最近的 h1-h4 小标题; 拿不到干净标题就跳过这条,
    宁可少几条也不往站上放 JSON/CSS 碎片。全部数字都来自页面, 不生成价格。"""
    clean = re.sub(r"<(script|style|noscript|template|svg)[^>]*>.*?</\1>",
                   " ", html_text, flags=re.S | re.I)
    text = re.sub(r"\s+", " ", strip_tags(clean))
    headings = [clean_title(m.group(1)) for m in HEAD_RE.finditer(clean)]
    headings = [h for h in headings if 2 < len(h) < 60 and CLEAN_TXT.match(h)]
    out, seen = [], set()
    for m in PRICE_RE.finditer(text):
        price = money(m.group(1))
        if not price or price <= 0:
            continue
        ctx = text[max(0, m.start() - 90):m.start()].strip()
        if any(s in ctx.lower() for s in SKIP_CTX):
            continue
        title = ""
        window = text[max(0, m.start() - 400):m.start() + 5].lower()
        for h in reversed(headings):
            if h.lower() in window:
                title = h
                break
        if not title:                               # 没有标题就用干净正文片段
            words = re.sub(r"^[^A-Za-z]+", "", ctx).split()
            words = words[1:] if len(words) > 1 else words   # 丢掉可能被截断的首词
            frag = " ".join(words[-8:]).strip(" ,.")
            title = frag if CLEAN_TXT.match(frag) else ""
        if not title:
            continue                                # 标题不干净 -> 丢掉, 不凑数
        title = clean_title(title)
        key = (title.lower(), price)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "title": title,
            "price": price,
            "currency": "USD",
            "compare_at_price": None,
            "discount_percent": 0,
            "available": True,
            "offer_url": page_url,
            "image": "",
            "valid_until": None,
            "source_url": prov["source"],
            "fetched_at": fetched,
            "extract": "page_text",
        })
        if len(out) >= 8:
            break
    return out


def adapt_sitemap_jsonld(prov: dict) -> tuple:
    """抓 sitemap.xml, 取 /products/ 链接抽样逐页解 JSON-LD。"""
    st, body, final, err = http_get(prov["source"])
    if st != 200 or not body:
        return [], {"status": "blocked" if st in (403, 429, 451) else "error",
                    "http": st, "note": err or "no body"}
    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", body.decode("utf-8", "ignore"), re.S)
    seen, urls = set(), []
    for u in locs:
        u = u.strip()
        if "/products/" in u and u not in seen:
            seen.add(u)
            urls.append(u)
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    items = []
    for u in urls[:15]:
        try:
            s2, b2, f2, _ = http_get(u)
            if s2 == 200 and b2:
                items.extend(_extract_jsonld(b2.decode("utf-8", "ignore"), f2, prov, fetched))
        except Exception:                           # noqa: BLE001
            continue
    return items, {"status": "ok" if items else "empty", "http": st, "count": len(items)}


ADAPTERS = {
    "shopify_sale": adapt_shopify_sale,
    "jsonld_offers": adapt_jsonld_offers,
    "sitemap_jsonld": adapt_sitemap_jsonld,
}


# ------------------------------------------------------------------ main ---

def main() -> int:
    if not CONFIG.exists():
        print("FATAL: 找不到 %s" % CONFIG, file=sys.stderr)
        return 2
    cfg = read_config(CONFIG)
    print("config: brand=%s providers=%d" % (cfg["site"].get("brand"), len(cfg["providers"])))

    providers, total = [], 0
    for prov in cfg["providers"]:
        name, src = prov["name"], prov["source"]
        if not robots_allowed(src):
            print("  %-18s SKIP robots.txt" % name)
            providers.append({**prov, "status": "robots_disallow", "items": []})
            continue
        fn = ADAPTERS.get(prov["adapter"])
        if not fn:
            print("  %-18s SKIP unknown adapter %s" % (name, prov["adapter"]))
            providers.append({**prov, "status": "no_adapter", "items": []})
            continue
        try:
            items, meta = fn(prov)
        except Exception as e:                      # noqa: BLE001
            items, meta = [], {"status": "error", "note": "%s: %s" % (type(e).__name__, e)}
        deals = sum(1 for i in items if i.get("discount_percent"))
        total += len(items)
        print("  %-18s %-8s items=%-4d deals=%-3d %s" %
              (name, meta.get("status"), len(items), deals, meta.get("note", "")))
        providers.append({**prov, "status": meta.get("status", "ok"),
                          "http": meta.get("http"), "note": meta.get("note", ""),
                          "items": items})

    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "site": cfg["site"],
        "fields": cfg["fields"],
        "build": cfg["build"],
        "stats": {
            "providers": len(providers),
            "items": total,
            "deals": sum(1 for p in providers for i in p["items"] if i.get("discount_percent")),
        },
        "providers": providers,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote %s  providers=%d items=%d deals=%d"
          % (OUT, doc["stats"]["providers"], doc["stats"]["items"], doc["stats"]["deals"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
