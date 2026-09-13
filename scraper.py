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
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / ".ilang" / "site.ilang"
OUT = ROOT / "data" / "offers.json"
RENDERER = ROOT / "tools" / "render_net.js"
RENDER_DIR = ROOT / ".render"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
TIMEOUT = 25
RETRIES = 2


# ---------------------------------------------------------------- config ---

def read_config(path: Path) -> dict:
    """解析 I-Lang 配置。任何一行含 '|' 且不以 '::' / '#' 开头 => 数据行。"""
    cfg = {"site": {}, "providers": [], "fields": [], "build": {}, "notes": {}}
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
                # 第 6 列: 渲染类适配器的 JSON 参数(选择器/等待串等), 可空
                "params": parts[5] if len(parts) > 5 else "",
            })
        elif section == "PROVIDER_NOTES" and len(parts) >= 2 and parts[0]:
            # 每家页面上必须如实写的口径。老板要求: 每行带官方出处+复核日期, 不许含糊。
            cfg["notes"][parts[0]] = " | ".join(p for p in parts[1:] if p)
        elif section == "FIELDS":
            cfg["fields"] = [p for p in parts if p]
        elif section == "BUILD" and len(parts) >= 2:
            cfg["build"][parts[0]] = parts[1]
    return cfg


# ------------------------------------------------------------------- http ---

def http_get(url: str, extra_headers: dict = None) -> tuple:
    """返回 (status, body_bytes, final_url, error)。跟随重定向, 带重试。
    extra_headers: 额外请求头(如直连品牌 JSON API 需要的 Accept/Referer)。"""
    last = ("", b"", url, "unknown")
    for attempt in range(RETRIES + 1):
        headers = {
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
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, headers=headers)
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


# ---------------------------------------------------------- expiry rule ---
# ::RULE{valid_until 已过⇒这条直接摘掉 不许留着冒充有效}
# 官方优惠页常年挂着已结束的档期(实测 Carnival /cruise-deals 里 9月5日/7日
# 到期的活动仍在页面上)。照抄就是把过期优惠推给用户, 所以解析出结束日期后
# 凡是早于今天的行一律丢弃, 并在 meta 里如实记下丢了几条。

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
MONTHS.update({m[:3].lower(): i for m, i in list(MONTHS.items())})


def parse_iso(s) -> date:
    """2026-09-14 / 2026-09-14T00:00:00Z / 09/14/2026 -> date, 拿不到就 None。"""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s[:len(fmt) + 6].strip("Z "), fmt).date()
        except ValueError:
            continue
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _resolve_year(month: int, day: int, today: date) -> date:
    """页面只写 'September 14th' 不写年份: 取今年, 若已过去超 60 天则算明年。"""
    for y in (today.year, today.year + 1):
        try:
            d = date(y, month, day)
        except ValueError:
            return None
        if (today - d).days <= 60:
            return d
    return None


# 页面正文里的结束日期写法。实测覆盖 Carnival / Hilton 两种格式。
ENDDATE_PATTERNS = [
    # "Offer ends November 21, 2027" / "Ends September 14, 2026"
    re.compile(r"\b(?:offer|sale|promo(?:tion)?|deal|booking)\s+ends?\s+"
               r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})", re.I),
    # "Hurry! Ends Monday, September 14th"
    # 负向断言: 后面紧跟年份的交给上面那条带年份的模式, 否则会被推成明年。
    re.compile(r"\bends?\s+(?:[A-Za-z]+,\s*)?([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
               r"(?!\s*,?\s*\d{4})\b", re.I),
    # "Book and travel by 11/13" / "Valid through 2027-05-31"
    re.compile(r"\b(?:book|travel|valid)\s*(?:and\s+travel)?\s*(?:by|through|until)\s+"
               r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", re.I),
]


def parse_enddate_from_text(text: str, today: date) -> date:
    """从一段正文里读出最晚的那个结束日期。读不出返回 None。"""
    found = []
    for pi, pat in enumerate(ENDDATE_PATTERNS):
        for m in pat.finditer(text or ""):
            g = m.groups()
            if pi == 2:                                  # mm/dd[/yy]
                mm, dd = int(g[0]), int(g[1])
                if g[2]:
                    yy = int(g[2])
                    yy += 2000 if yy < 100 else 0
                    try:
                        found.append(date(yy, mm, dd))
                    except ValueError:
                        pass
                else:
                    d = _resolve_year(mm, dd, today)
                    if d:
                        found.append(d)
            elif pi == 1:                                # Month D (无年份)
                mon = MONTHS.get(g[0][:3].lower())
                if not mon:
                    continue
                d = _resolve_year(mon, int(g[1]), today)
                if d:
                    found.append(d)
            else:                                        # Month D, YYYY
                mon = MONTHS.get(g[0][:3].lower())
                if not mon or not g[2]:
                    continue
                try:
                    found.append(date(int(g[2]), mon, int(g[1])))
                except ValueError:
                    pass
    return max(found) if found else None


def drop_expired(items: list, today: date = None) -> tuple:
    """摘掉 valid_until 已过的行。返回 (保留的, 被摘掉的)。
    valid_until 为空 => 长期有效档期, 保留(不编日期)。"""
    today = today or datetime.now(timezone.utc).date()
    keep, cut = [], []
    for it in items:
        d = parse_iso(it.get("valid_until"))
        if d and d < today:
            it["expired_on"] = d.isoformat()
            cut.append(it)
        else:
            keep.append(it)
    return keep, cut


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


# ------------------------------------------------- rendered (JS) adapters ---
# 为什么需要: 实测多家旅行站的优惠页是 SPA 空壳 —— HTTP 200 + 标题正确,
# 但正文里一个价格都没有(Carnival 静态 53 个可见字符 / Klook 静态只有 3 个码 /
# JetBlue 的真票价只在 airtrfx 的 JSON 响应里, 直连还 401)。
# 这一组适配器先跑 tools/render_net.js 让页面把 JS 跑完, 再从渲染结果里取数。
# 渲染器不可用时如实返回 render_unavailable, 不退回静态结果冒充有数据。

def run_renderer(url: str, wait_ms: int = 12000, url_filter: str = "",
                 wait_for: str = "", extract: dict = None) -> tuple:
    """调 Node 渲染器。返回 (ok, info_dict)。
    url_filter: 只保存 URL 含此子串的 JSON。
    wait_for:   显式等这个子串的响应到达(航司票价 feed 用它才稳)。
    extract:    DOM 抽取规格 {card, fields}, 给了就产出 .dom.json。"""
    if not RENDERER.exists():
        return False, {"note": "tools/render_net.js missing"}
    outdir = RENDER_DIR
    outdir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "_",
                  urllib.parse.urlparse(url).netloc + urllib.parse.urlparse(url).path)[:60]
    prefix = str(outdir / slug)
    # 位置参数顺序固定: url out waitMs filter waitFor extractB64, 缺的补空串
    import base64
    ext_b64 = ""
    if extract:
        ext_b64 = base64.b64encode(json.dumps(extract).encode("utf-8")).decode("ascii")
    cmd = ["node", str(RENDERER), url, prefix, str(wait_ms),
           url_filter or "", wait_for or "", ext_b64]
    env = dict(os.environ)
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH",
                   str(Path(os.environ.get("LOCALAPPDATA", "~")) / "ms-playwright"))
    # 让渲染器找到已装的 playwright(可能在仓库外的 node_modules)
    extra_np = env.get("RENDERER_NODE_PATH")
    if extra_np:
        env["NODE_PATH"] = extra_np
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           cwd=str(ROOT), env=env)
    except subprocess.TimeoutExpired:
        return False, {"note": "renderer timeout 300s"}
    except Exception as e:                                        # noqa: BLE001
        return False, {"note": "renderer failed: %s" % e}
    line = (p.stdout or "").strip().splitlines()
    info = {"note": (p.stderr or "")[-300:]}
    for l in reversed(line):
        l = l.strip()
        if l.startswith("{") and l.endswith("}"):
            try:
                info = json.loads(l)
                break
            except Exception:                                     # noqa: BLE001
                continue
    info["prefix"] = prefix
    return Path(prefix + ".html").exists(), info


def _read_rendered(prefix: str) -> tuple:
    def rd(p):
        try:
            return Path(p).read_text(encoding="utf-8", errors="ignore")
        except Exception:                                         # noqa: BLE001
            return ""
    return rd(prefix + ".html"), rd(prefix + ".txt")


def _shell_check(html: str, text: str) -> bool:
    """判定是否仍是 JS 空壳: 可见字符极少且有 SPA 标记。"""
    vis = len(re.sub(r"\s", "", text or ""))
    if vis >= 600:
        return False
    return bool(re.search(r"data-next-head|__NEXT_DATA__|id=\"root\"|data-reactroot", html or ""))


def adapt_render_jsonld(prov: dict) -> tuple:
    """渲染后解 JSON-LD; 没有 JSON-LD 就退回扫可见正文的价格。"""
    ok, info = run_renderer(prov["source"])
    if not ok:
        return [], {"status": "render_unavailable", "http": info.get("status"),
                    "note": info.get("note", "")}
    html, text = _read_rendered(info["prefix"])
    if _shell_check(html, text):
        return [], {"status": "empty", "http": info.get("status"),
                    "note": "still JS shell after render (%d visible chars)"
                            % len(re.sub(r"\s", "", text))}
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    items = _extract_jsonld(html, info.get("url", prov["source"]), prov, fetched)
    if not items:
        items = scan_prices(html, prov, info.get("url", prov["source"]), fetched)
    return items, {"status": "ok" if items else "empty", "http": info.get("status"),
                   "count": len(items), "renderer": True,
                   "note": "" if items else "rendered but no price found"}


def _walk_json_fares(node, today: date, out: list, page_url: str, source: str,
                     fetched: str, depth: int = 0):
    """在渲染期抓到的 API JSON 里递归找带价对象。只认页面自己给的价格。"""
    if depth > 12 or isinstance(node, str):
        return
    if isinstance(node, list):
        for n in node:
            _walk_json_fares(n, today, out, page_url, source, fetched, depth + 1)
        return
    if not isinstance(node, dict):
        return
    price = None
    # formattedTotalPrice("$69") 优先: 它就是页面显示给用户的那个数。
    # totalPrice/usdTotalPrice 常是 68.4 这种未取整的内部值, 只在没有格式化值时用。
    fm = node.get("formattedTotalPrice") or node.get("formattedUsdTotalPrice")
    if isinstance(fm, str):
        pm = re.search(r"\d[\d,]*(?:\.\d{1,2})?", fm.replace(",", ""))
        if pm:
            price = money(pm.group(0))
    if not price:
        for k in ("totalPrice", "usdTotalPrice", "price", "amount", "lowestPrice", "from_price"):
            if k in node:
                price = money(node[k])
                if price:
                    break
    if price and price > 0:
        origin = node.get("originCity") or node.get("origin") or ""
        dest = node.get("destinationCity") or node.get("destination") or ""
        dep = node.get("formattedDepartureDate") or node.get("departureDate") or ""
        if origin and dest:
            title = "%s to %s" % (origin, dest)
        else:
            title = clean_title(node.get("name") or node.get("title") or "")
        if dep:
            title = "%s (depart %s)" % (title, dep) if title else clean_title(str(dep))
        if title:
            ftype = str(node.get("flightType") or "").replace("_", " ").title()
            if ftype:
                title = "%s %s" % (title, ftype)
            out.append({
                "title": clean_title(title),
                "price": price,
                "currency": node.get("currencyCode") or "USD",
                "compare_at_price": None,
                "discount_percent": 0,
                "available": True,
                "offer_url": page_url,
                "image": node.get("destinationCityImage") or node.get("originCityImage") or "",
                # 航班的"结束日期"= 出发日; 过去的航班不能再卖, 交给过期规则摘掉
                "valid_until": parse_iso(dep).isoformat() if parse_iso(dep) else None,
                "source_url": source,
                "fetched_at": fetched,
                "extract": "rendered_api",
            })
    for v in node.values():
        if isinstance(v, (dict, list)):
            _walk_json_fares(v, today, out, page_url, source, fetched, depth + 1)


def adapt_render_api(prov: dict) -> tuple:
    """渲染页面并解析它自己发出的 JSON API 响应(航司票价走这条)。
    params 列(JSON): {filter, waitFor, waitMs}。filter 选 JSON, waitFor 精确等票价 feed。"""
    pm = _parse_params(prov)
    url_filter = pm.get("filter", prov.get("affiliate") or "")
    wait_for = pm.get("waitFor", "")
    wait_ms = int(pm.get("waitMs", 22000))
    ok, info = run_renderer(prov["source"], wait_ms=wait_ms,
                            url_filter=url_filter, wait_for=wait_for)
    if not ok:
        return [], {"status": "render_unavailable", "http": info.get("status"),
                    "note": info.get("note", "")}
    prefix = info["prefix"]
    html, _ = _read_rendered(prefix)
    if info.get("status") in (403, 429, 444, 451):
        return [], {"status": "blocked", "http": info.get("status"),
                    "note": "renderer blocked: %s" % info.get("title", "")}
    today = datetime.now(timezone.utc).date()
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    page_url = info.get("url", prov["source"])
    items = []
    try:
        idx = json.loads(Path(prefix + ".net.json").read_text(encoding="utf-8"))
    except Exception:                                             # noqa: BLE001
        idx = []
    for entry in idx:
        if entry.get("status", 200) != 200:
            continue
        try:
            blob = Path("%s.net%d.json" % (prefix, entry["i"])).read_text(encoding="utf-8")
            _walk_json_fares(json.loads(blob), today, items, page_url, prov["source"], fetched)
        except Exception:                                         # noqa: BLE001
            continue
    seen, uniq = set(), []
    for it in items:
        k = (it["title"].lower(), it["price"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    note = ""
    if wait_for and not info.get("targetSeen"):
        note = "fare feed '%s' not loaded this run (lazy widget; flaky)" % wait_for
    if uniq:
        return uniq, {"status": "ok", "http": info.get("status"), "count": len(uniq),
                      "renderer": True, "json_feeds": len(idx), "note": note}
    # 票价 feed 这次没到 -> 如实报 empty, 不退回文本扫描冒充有数据
    return [], {"status": "empty", "http": info.get("status"), "count": 0,
                "renderer": True, "note": note or "no fare JSON captured"}


def _parse_params(prov: dict) -> dict:
    """params 列是 JSON, 解析失败就返回空 dict(不抛)。"""
    raw = prov.get("params") or ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:                                             # noqa: BLE001
        return {}


def adapt_render_dom(prov: dict) -> tuple:
    """渲染后按 DOM 锚点抽卡片(Hilton/Klook/JetBlue 这类)。
    params(JSON):
      anchor        标题元素选择器(稳定, 不带易变 class), 例 'h3[id^=headline]'
      title         锚点内真正放标题的子选择器, 空则用锚点自身文本
      minExtra      向上爬到卡片时, 文本要比标题长多少才算卡片(默认 15)
      currency      价格币种(默认 USD)
      requireSignal 真=只保留含明确优惠信号($价 / %off / code)的卡, 滤掉纯营销块
      fareFilter    非空则同一次渲染里机会性收这个子串的票价 JSON 并入
                    (JetBlue 票价 feed 实测懒加载偶发触发, 收到就赚, 收不到不影响促销块)
    ::RULE{抓不到 price 就不写 price, 折扣只用页面明写的百分比, 绝不编}。"""
    pm = _parse_params(prov)
    anchor = pm.get("anchor")
    if not anchor:
        return [], {"status": "error", "note": "render_dom needs params.anchor"}
    extract = {"anchor": anchor, "minExtra": int(pm.get("minExtra", 15))}
    if pm.get("title"):
        extract["title"] = pm["title"]
    fare_filter = pm.get("fareFilter", "")
    ok, info = run_renderer(prov["source"], wait_ms=int(pm.get("waitMs", 14000)),
                            url_filter=fare_filter, extract=extract)
    if not ok:
        return [], {"status": "render_unavailable", "http": info.get("status"),
                    "note": info.get("note", "")}
    if info.get("status") in (403, 429, 444, 451):
        return [], {"status": "blocked", "http": info.get("status"),
                    "note": "renderer blocked: %s" % info.get("title", "")}
    html, text = _read_rendered(info["prefix"])
    if _shell_check(html, text):
        return [], {"status": "empty", "http": info.get("status"),
                    "note": "still JS shell after render (%d visible chars)"
                            % len(re.sub(r"\s", "", text or ""))}
    try:
        cards = json.loads(Path(info["prefix"] + ".dom.json").read_text(encoding="utf-8"))
    except Exception:                                             # noqa: BLE001
        cards = []

    today = datetime.now(timezone.utc).date()
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    page_url = info.get("url", prov["source"])
    currency = pm.get("currency", "USD")
    require_signal = bool(pm.get("requireSignal"))
    items, seen = [], set()
    for c in cards:
        title = clean_title(c.get("title", ""))
        ctext = c.get("cardText", "") or ""
        if not title or len(title) < 3:
            continue
        # 价格: 卡片明写的 $xx 或 US$xx
        price = None
        pmz = re.search(r"(?:US)?\$\s?(\d[\d,]*(?:\.\d{1,2})?)", ctext)
        if pmz:
            price = money(pmz.group(1))
        # 折扣: 只认页面明写的折扣语境("X% off" / "save up to X%")。
        # "get up to 100% more points" 这种赠送语境不算折扣 —— 否则会把买积分
        # 送一误标成"1折", 违背"折扣只认页面明写、不许自己算"。
        pct = 0
        _m = re.search(r"(\d{1,3})\s*%\s*off", ctext, re.I)
        if not _m:
            _m = re.search(r"save\s+(?:up\s+to\s+)?(\d{1,3})\s*%", ctext, re.I)
        if not _m:
            _m = re.search(r"up\s+to\s+(\d{1,3})\s*%\s*(?:off|discount|savings)", ctext, re.I)
        if _m:
            pct = int(_m.group(1))
        # promo code: 卡片明写的"Promo code: XXX"/"coupon code XXX"/"Offer Code: XXX"
        # 只抄官方页面上写的码, 绝不拿第三方码(Klook 实测页面就带码)。
        code = ""
        _cm = re.search(r"(?:promo|coupon|offer|discount|rate)?\s*code:?\s*([A-Z0-9][A-Z0-9_-]{2,24})",
                        ctext, re.I)
        if _cm:
            code = _cm.group(1).strip()
        # requireSignal: 没有 价格/折扣/码 任一信号的卡 = 纯营销文案, 丢掉。
        # (JetBlue /sale 的 "Overseas the day." "Got points?" 就是这种, 不是优惠)
        if require_signal and not (price or pct or code):
            continue
        end = parse_enddate_from_text(ctext, today)
        href = c.get("href", "") or ""
        if href.startswith("/"):
            href = urllib.parse.urljoin(page_url, href)
        offer_url = href or page_url
        if code and code.lower() not in title.lower():
            title = "%s (code %s)" % (title, code)
        key = (title.lower(), price, pct)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "title": title,
            "price": price,                       # 没有就 None, 不编
            "currency": currency if price else None,
            "compare_at_price": None,
            "discount_percent": pct,              # 只用页面明写的百分比
            "available": True,
            "offer_url": offer_url,
            "image": "",
            "valid_until": end.isoformat() if end else None,
            "source_url": prov["source"],
            "fetched_at": fetched,
            "extract": "rendered_dom",
        })

    # 机会性并入票价 feed(只有 fareFilter 设了才走)。JetBlue 票价组件懒加载,
    # 实测多数时候触发不到; 触发到了就把真票价并进来, 触发不到就只用上面的促销块。
    fare_added = 0
    if fare_filter:
        fare_items = _collect_fare_json(info["prefix"], today, page_url, prov["source"], fetched)
        for it in fare_items:
            key = (it["title"].lower(), it["price"])
            if key in seen:
                continue
            seen.add(key)
            items.append(it)
            fare_added += 1

    note = ""
    if fare_filter:
        note = "%d promo cards + %d fare rows%s" % (
            len(items) - fare_added, fare_added,
            " (fare feed not triggered this run)" if not fare_added else "")
    if not items:
        note = note or "%d DOM cards but none had price/discount" % len(cards)
    return items, {"status": "ok" if items else "empty", "http": info.get("status"),
                   "count": len(items), "renderer": True, "dom_cards": len(cards),
                   "fare_rows": fare_added, "note": note}


def _collect_fare_json(prefix: str, today: date, page_url: str, source: str,
                       fetched: str) -> list:
    """从渲染抓到的 .net*.json 里收票价行(航司 API 共用结构)。失败返回空表。"""
    out = []
    try:
        idx = json.loads(Path(prefix + ".net.json").read_text(encoding="utf-8"))
    except Exception:                                             # noqa: BLE001
        return out
    for entry in idx:
        if entry.get("status", 200) != 200:
            continue
        try:
            blob = Path("%s.net%d.json" % (prefix, entry["i"])).read_text(encoding="utf-8")
            _walk_json_fares(json.loads(blob), today, out, page_url, source, fetched)
        except Exception:                                         # noqa: BLE001
            continue
    return out


def adapt_carnival_api(prov: dict) -> tuple:
    """Carnival 官方 deals API 直连(实测无需渲染, 返回 5 条干净结构化档期)。
    源: https://www.carnival.com/cruisesearch/api/deals?sort=recommended&showBest=true
    字段全部来自官方 JSON: title / topSailingPrice / benefits / offerCode / bookingEndDate / ctaUrl。
    ::RULE{页面挂着已过期档期(实测 9/5 9/7), 结束日交给 drop_expired 摘掉, 不照抄}。"""
    url = prov["source"]
    headers = {"Accept": "application/json", "Referer": "https://www.carnival.com/cruise-deals"}
    st, body, final, err = http_get(url, extra_headers=headers)
    if st != 200 or not body:
        return [], {"status": "blocked" if st in (403, 429, 444, 451) else "error",
                    "http": st, "note": err or "no body"}
    try:
        data = json.loads(body.decode("utf-8", "ignore"))
    except Exception as e:                                        # noqa: BLE001
        return [], {"status": "error", "http": st, "note": "bad json: %s" % e}
    deals = data.get("deals") or []
    today = datetime.now(timezone.utc).date()
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    host = "https://www.carnival.com"
    items = []
    for d in deals:
        title = clean_title(d.get("title", ""))
        if not title:
            continue
        price = money(d.get("topSailingPrice"))
        benefits = [clean_title(b) for b in (d.get("benefits") or []) if b]
        # 结束日: 优先 bookingEndDate 字段, 没有就从 benefits 文案里读
        end = parse_iso(d.get("bookingEndDate"))
        if not end:
            for b in benefits:
                end = parse_enddate_from_text(b, today)
                if end:
                    break
        # 折扣百分比: 从 benefits 里读官方写的 "Up to 55% off"
        pct = 0
        for b in benefits:
            m = re.search(r"(\d{1,3})\s*%\s*off", b, re.I)
            if m:
                pct = int(m.group(1))
                break
        cta = d.get("ctaUrl") or ""
        offer_url = (host + cta) if cta.startswith("/") else (cta or prov["homepage"])
        code = d.get("offerCode") or d.get("couponCode") or ""
        if code and code.lower() not in title.lower():
            title = "%s (code %s)" % (title, code)
        items.append({
            "title": title,
            "price": price,
            "currency": "USD" if price else None,
            "compare_at_price": None,
            "discount_percent": pct,
            "available": True,
            "offer_url": offer_url,
            "image": (host + d["imageUrl"]) if str(d.get("imageUrl", "")).startswith("/") else d.get("imageUrl", ""),
            "valid_until": end.isoformat() if end else None,
            "source_url": url,
            "fetched_at": fetched,
            "extract": "official_api",
            "benefits": benefits[:6],
        })
    return items, {"status": "ok" if items else "empty", "http": st, "count": len(items),
                   "note": "carnival official deals API, %d raw" % len(deals)}


ADAPTERS = {
    "shopify_sale": adapt_shopify_sale,
    "jsonld_offers": adapt_jsonld_offers,
    "sitemap_jsonld": adapt_sitemap_jsonld,
    "render_jsonld": adapt_render_jsonld,
    "render_api": adapt_render_api,
    "render_dom": adapt_render_dom,
    "carnival_api": adapt_carnival_api,
}

def main() -> int:
    global OUT
    cfg_path = CONFIG
    if "--config" in sys.argv:
        rest = sys.argv[sys.argv.index("--config") + 1:]
        if rest:
            cfg_path = Path(rest[0])
            if not cfg_path.is_absolute():
                cfg_path = ROOT / cfg_path
    if not cfg_path.exists():
        print("FATAL: 找不到 %s" % cfg_path, file=sys.stderr)
        return 2
    cfg = read_config(cfg_path)

    # 只跑指定几家: python scraper.py --only "Southwest,Klook"
    only = None
    if "--only" in sys.argv:
        arg = sys.argv[sys.argv.index("--only") + 1:]
        if arg:
            only = [x.strip().lower() for x in arg[0].split(",") if x.strip()]
    write_out = "--no-write" not in sys.argv
    if "--out" in sys.argv:                       # 旅行批次单独出文件, 不覆盖线上站数据
        rest = sys.argv[sys.argv.index("--out") + 1:]
        if rest:
            OUT = Path(rest[0])
            if not OUT.is_absolute():
                OUT = ROOT / OUT

    providers = [p for p in cfg["providers"]
                 if not only or p["name"].lower() in only]
    print("config: brand=%s providers=%d%s" % (cfg["site"].get("brand"),
          len(providers), (" (filtered: %s)" % ",".join(only)) if only else ""))

    today = datetime.now(timezone.utc).date()
    total, expired_total = [], 0
    rows, cut_all = [], 0
    for prov in providers:
        name, src = prov["name"], prov["source"]
        if not robots_allowed(src):
            print("  %-22s SKIP robots.txt" % name)
            rows.append({**prov, "status": "robots_disallow", "items": []})
            continue
        fn = ADAPTERS.get(prov["adapter"])
        if not fn:
            print("  %-22s SKIP unknown adapter %s" % (name, prov["adapter"]))
            rows.append({**prov, "status": "no_adapter", "items": []})
            continue
        try:
            items, meta = fn(prov)
        except Exception as e:                      # noqa: BLE001
            items, meta = [], {"status": "error", "note": "%s: %s" % (type(e).__name__, e)}

        # ::RULE{valid_until 已过⇒这条直接摘掉 不许留着冒充有效}
        raw_n = len(items)
        items, cut = drop_expired(items, today)
        cut_all += len(cut)
        if cut:
            meta["note"] = ((meta.get("note") or "") + " | dropped %d expired (%s)"
                            % (len(cut), ", ".join(sorted({c["expired_on"] for c in cut})))).strip(" |")

        deals = sum(1 for i in items if i.get("discount_percent"))
        rows.append({**prov, "status": meta.get("status", "ok"),
                     "http": meta.get("http"), "note": meta.get("note", ""),
                     "prov_note": cfg.get("notes", {}).get(prov["name"], ""),
                     "renderer": bool(meta.get("renderer")),
                     "raw_count": raw_n, "expired_dropped": len(cut),
                     "items": items})
        print("  %-22s %-10s items=%-4d deals=%-3d expired_cut=%-2d %s%s" %
              (name, meta.get("status"), len(items), deals, len(cut),
               "[render] " if meta.get("renderer") else "", meta.get("note", "")))

    stats = {
        "providers": len(rows),
        "items": sum(len(p["items"]) for p in rows),
        "deals": sum(1 for p in rows for i in p["items"] if i.get("discount_percent")),
        "expired_dropped": cut_all,
        "rendered_providers": sum(1 for p in rows if p.get("renderer")),
        "blocked": sum(1 for p in rows if p.get("status") == "blocked"),
    }
    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "today_utc": today.isoformat(),
        "site": cfg["site"],
        "fields": cfg["fields"],
        "build": cfg["build"],
        "stats": stats,
        "providers": rows,
    }
    if write_out:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print("wrote %s  providers=%d items=%d deals=%d expired_dropped=%d"
              % (OUT, stats["providers"], stats["items"], stats["deals"], cut_all))
    else:
        print("(--no-write) providers=%d items=%d deals=%d expired_dropped=%d"
              % (stats["providers"], stats["items"], stats["deals"], cut_all))
    return 0


if __name__ == "__main__":
    sys.exit(main())
