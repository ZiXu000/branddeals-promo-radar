// tools/render_net.js — 真机渲染器: 打开页面跑完 JS, 并抓页面自己发出的 JSON 响应。
//
// 为什么需要(全部 2026-09-13 实测):
//   Carnival  /cruise-deals  静态 10.7KB 只有 53 个可见字符(Next.js 空壳) -> 渲染后才有 5 条档期
//   JetBlue   /sale          真票价不在 HTML 里, 在 airtrfx grouped-routes JSON, 直连还 401
//   Southwest /flight-deals  真票价在 GraphQL 响应里, 静态页只有骨架
//
// 关键设计: waitForUrl —— 精确等目标 API 出现, 而不是盲等固定秒数。
//   实测 JetBlue 的票价 feed 发出时机不定, 盲等 16s 有时抓不到;
//   显式等 URL 命中则每次都稳。
//
// 用法:
//   node render_net.js <url> <outPrefix> [waitMs] [urlFilter] [waitForUrl]
//     waitMs      命中后/超时后再多跑一会儿的毫秒数 (默认 12000)
//     urlFilter   只保存 URL 含此子串的 JSON (默认全存)
//     waitForUrl  显式等待这个子串对应的响应到达 (默认空=不等)
// 产出: <out>.html  <out>.txt  <out>.net.json(索引)  <out>.netN.json(响应体)
const { chromium } = require('playwright');
const fs = require('fs');

(async () => {
  const url = process.argv[2];
  const out = process.argv[3];
  const waitMs = parseInt(process.argv[4] || '12000', 10);
  const filter = (process.argv[5] || '').toLowerCase();
  const waitFor = (process.argv[6] || '').toLowerCase();
  // 第 7 参: base64(JSON) 的 DOM 抽取规格。给了就在浏览器里按 CSS 选择器抽卡片,
  // 输出 <out>.dom.json。这是比"切整页文本"可靠得多的路子: 每个字段都来自确定节点。
  const extractB64 = process.argv[7] || '';
  let extractSpec = null;
  if (extractB64) {
    try { extractSpec = JSON.parse(Buffer.from(extractB64, 'base64').toString('utf8')); }
    catch (e) { extractSpec = null; }
  }
  if (!url || !out) {
    console.log('usage: node render_net.js <url> <outPrefix> [waitMs] [urlFilter] [waitForUrl] [extractB64]');
    process.exit(2);
  }

  const browser = await chromium.launch({
    headless: true,
    args: ['--disable-blink-features=AutomationControlled', '--no-sandbox', '--lang=en-US'],
  });
  const ctx = await browser.newContext({
    locale: 'en-US',
    timezoneId: 'America/New_York',
    viewport: { width: 1440, height: 1000 },
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36',
  });
  await ctx.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
  });

  const page = await ctx.newPage();
  const captured = [];
  let targetSeen = null;

  page.on('response', async (resp) => {
    try {
      const u = resp.url();
      const ul = u.toLowerCase();
      if (filter && !ul.includes(filter)) return;
      const ct = (resp.headers()['content-type'] || '');
      if (!ct.includes('json')) return;
      const body = await resp.text();
      if (!body || body.length < 40) return;
      if (!/\d{2,}/.test(body)) return;          // 没有数字的 JSON 对价格无意义
      // waitFor 命中判定: 只看 URL。URL 是端点的精确标识, 不会误判;
      // 之前试过同时匹配响应体, 结果 JetBlue 的 placementSettings 配置里
      // 也写了 "grouped-routes" 字样, 造成假阳性(真票价端点根本没发)。
      if (waitFor && !targetSeen && ul.includes(waitFor)) {
        targetSeen = u.slice(0, 400);
      }
      captured.push({ url: u.slice(0, 400), status: resp.status(), body: body.slice(0, 900000) });
    } catch (e) { /* 单个响应失败不影响整体 */ }
  });

  let status = 0, err = '';
  try {
    const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    status = resp ? resp.status() : 0;

    if (waitFor) {
      // 轮询等目标 API 到达, 最多 waitMs。命中就走, 不盲等。
      // JetBlue 的票价组件是懒加载: 必须滚到它在视口里才会发 grouped-routes 请求。
      // 所以这里渐进滚到页底(分多步触发每一屏的 IntersectionObserver), 再等命中。
      const deadline = Date.now() + waitMs;
      let scrolledToBottom = false;
      while (Date.now() < deadline && !targetSeen) {
        if (!scrolledToBottom) {
          // 一屏一屏往下滚到底
          for (let s = 0; s < 12 && !targetSeen; s++) {
            await page.mouse.wheel(0, 600).catch(() => {});
            await page.waitForTimeout(500);
          }
          await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight)).catch(() => {});
          scrolledToBottom = true;
        } else {
          // 到底了还没命中: 小幅抖动 + 等, 给慢请求留时间
          await page.mouse.wheel(0, -300).catch(() => {});
          await page.waitForTimeout(400);
          await page.mouse.wheel(0, 300).catch(() => {});
          await page.waitForTimeout(800);
        }
      }
      if (!targetSeen) err = 'waitForUrl not seen: ' + waitFor;
      await page.waitForTimeout(2500);                    // 命中后再收一下尾部响应
    } else {
      await page.waitForTimeout(Math.floor(waitMs / 2));
      for (let s = 0; s < 3; s++) {
        await page.mouse.wheel(0, 1200).catch(() => {});
        await page.waitForTimeout(Math.floor(waitMs / 6));
      }
    }
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {});
  } catch (e) { err = (err + '|' + (e.message || String(e))).slice(0, 240); }

  let html = '', text = '';
  try { html = await page.content(); } catch (e) {}
  try { text = await page.evaluate(() => document.body ? document.body.innerText : ''); } catch (e) {}

  // ---- DOM 抽取: 在浏览器里按"锚点 + 向上爬到卡片"取每张卡 ----
  // 为什么用 anchorClimb 而不是固定 card class: Hilton/Klook 的卡片 class 都带
  // 易变哈希(tailwind / data-v-xxxx), 但标题元素稳定(Hilton h3[id^=headline] 14 个 /
  // Klook .one-coupon-item 3 个)。所以以标题为锚, 向上爬到第一个文本明显变长的祖先
  // (= 含描述的那张卡), 不依赖任何 class 名。比"切整页文本"可靠: 每张卡边界确定。
  let domItems = null;
  if (extractSpec && extractSpec.anchor) {
    try {
      domItems = await page.evaluate((spec) => {
        const txt = (el) => (el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '');
        const minExtra = (spec.minExtra != null ? spec.minExtra : 15);
        const out = [];
        const seenCards = new Set();
        const anchors = Array.from(document.querySelectorAll(spec.anchor));
        for (const a of anchors) {
          const titleEl = spec.title ? a.querySelector(spec.title) : a;
          const title = txt(titleEl);
          if (!title || title.length < 2) continue;
          // 向上爬到第一个文本比标题长 minExtra 以上的祖先 = 卡片
          let card = a;
          let atext = txt(a);
          let p = a.parentElement;
          while (p && p !== document.body) {
            if (txt(p).length > atext.length + minExtra) { card = p; break; }
            p = p.parentElement;
          }
          const cardText = txt(card).slice(0, 800);
          const sig = title + '|' + cardText.length;   // 去重: 同名同长卡只算一次
          if (seenCards.has(sig)) continue;
          seenCards.add(sig);
          // 卡片里的链接(优先 cta)
          let href = '';
          const link = card.querySelector('a[href]');
          if (link) href = link.getAttribute('href') || '';
          out.push({ title, cardText, href });
        }
        return out;
      }, extractSpec);
    } catch (e) { domItems = null; }
  }

  fs.writeFileSync(out + '.html', html, 'utf8');
  fs.writeFileSync(out + '.txt', text, 'utf8');
  if (domItems !== null) {
    fs.writeFileSync(out + '.dom.json', JSON.stringify(domItems, null, 2), 'utf8');
  }
  fs.writeFileSync(out + '.net.json',
    JSON.stringify(captured.map((c, i) => ({ i, url: c.url, status: c.status, bytes: c.body.length })), null, 2), 'utf8');
  captured.forEach((c, i) => fs.writeFileSync(out + '.net' + i + '.json', c.body, 'utf8'));

  console.log(JSON.stringify({
    url, status, err, bytes: html.length,
    visibleChars: text.replace(/\s/g, '').length,
    targetSeen,
    domItems: domItems === null ? null : domItems.length,
    title: (html.match(/<title[^>]*>([\s\S]*?)<\/title>/i) || [, ''])[1].trim().slice(0, 120),
    jsonResponses: captured.map(c => c.url.replace(/^https?:\/\/[^/]+/, '').slice(0, 140)),
  }));
  await browser.close();
  process.exit(0);
})();
