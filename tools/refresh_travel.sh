#!/usr/bin/env bash
# ILANG
# TYPE: script | ROLE: refresher | PROJECT: branddeals-promo-radar
# ::STATE{@REFRESH, site:traveldeals-promo-radar.pages.dev, batch:travel4, runs_on:this machine}
# ::RULE{旅行站必须用本机出口刷新 不挂 GH Actions —— 老板指定"本机那条线就是我要的那个出口"}
# ::RULE{航司站(Southwest/JetBlue/Hilton)走渲染 渲染器要 playwright; GH 的 IP 可能被这些站封}
# ::BOUNDARY{never:用别的出口的数据冒充本机出口|scope:file}
#
# 用法: bash tools/refresh_travel.sh
# 作用: 在本机重抓四家 -> 重建旅行站 -> 部署到 Cloudflare Pages -> 提交数据快照
set -euo pipefail
cd "$(dirname "$0")/.."

# 渲染器(playwright)装在仓库外, 指给它
export RENDERER_NODE_PATH="${RENDERER_NODE_PATH:-C:\\Users\\Administrator\\WorkBuddy AI\\Claw\\_six\\node_modules}"

echo "[1/4] scrape travel4 on THIS machine's egress ..."
python scraper.py --config .ilang/travel4.ilang --out data/travel4.json

echo "[2/4] build static site ..."
python build.py --data data/travel4.json --config .ilang/travel4.ilang --out site_travel

echo "[3/4] deploy to Cloudflare Pages ..."
npx wrangler@4 pages deploy site_travel \
  --project-name traveldeals-promo-radar \
  --branch main --commit-dirty=true

echo "[4/4] commit data snapshot (site HTML is a build artifact, not committed) ..."
git add data/travel4.json .ilang/travel4.ilang scraper.py build.py tools/
if git diff --cached --quiet; then
  echo "  no changes this run"
else
  git commit -m "chore(travel): refresh travel4 $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  git push
fi
echo "done -> https://traveldeals-promo-radar.pages.dev/"
