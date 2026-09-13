#!/usr/bin/env bash
# ILANG
# TYPE: script | ROLE: refresher | PROJECT: branddeals-promo-radar
# ::STATE{@REFRESH, site:traveldeals-promo-radar.pages.dev, batch:travel4, runs_on:this machine, freq:每天一次}
# ::RULE{刷新频率等于每天一次⇒不是每几小时 页上的承诺必须跟这个频率一致}
# ::RULE{过期闸门只在管线重跑那一刻生效⇒没有按天刷新 这道闸门等于摆设 这是排程的唯一理由}
# ::RULE{刷新失败 或某一家的抓取结果是空⇒必须留痕 写进带日期的日志 让运行的人看得见 不许静默跳过}
# ::RULE{旅行站必须用本机出口刷新 不挂 GH Actions —— 老板指定"本机那条线就是我要的那个出口"}
# ::BOUNDARY{never:把抓不到写成没有优惠|scope:permanent}
# ::BOUNDARY{never:为了凑条数把过期行留在线上|scope:permanent}
#
# 用法: bash tools/refresh_travel.sh
# 作用: 本机重抓四家 -> 重建 -> 部署 -> 提交, 全过程写进 logs/travel-<日期>.log
#
# 注意: 故意不用 set -e —— 失败要被记录并继续跑完后续步骤(部署复核), 而不是中途静默退出。
set -uo pipefail
cd "$(dirname "$0")/.."

export RENDERER_NODE_PATH="${RENDERER_NODE_PATH:-C:\\Users\\Administrator\\WorkBuddy AI\\Claw\\_six\\node_modules}"

STAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DAY=$(date -u +%Y-%m-%d)
mkdir -p logs
LOG="logs/travel-$DAY.log"

# 整个脚本的输出同时打到屏幕和当天的日志里 —— 成功失败都留痕, 不是只有成功才写
exec > >(tee -a "$LOG") 2>&1

echo "=================================================================="
echo "[travel4 refresh] start $STAMP   host=$(hostname 2>/dev/null || echo unknown)"
echo "=================================================================="

FAILED=0

# ---------------- 1. 抓 ----------------
echo "[1/5] scrape travel4 on this machine's egress ..."
SCRAPE_OUT=$(python scraper.py --config .ilang/travel4.ilang --out data/travel4.json 2>&1)
SCRAPE_RC=$?
echo "$SCRAPE_OUT"
if [ "$SCRAPE_RC" -ne 0 ]; then
  echo "!! FAILURE: scraper exited $SCRAPE_RC"
  FAILED=1
else
  echo "   scraper exit 0"
fi

# 逐家核条数: 抓到 0 条 或 状态不是 ok 的, 必须单独点出来(不许静默跳过)
echo "--- per-brand result check ---"
if [ -f data/travel4.json ]; then
  python - <<'PY' || FAILED=1
import json,sys
d=json.load(open('data/travel4.json',encoding='utf-8'))
bad=0
for p in d['providers']:
    n=len(p.get('items',[])); st=p.get('status')
    flag=""
    if n==0 or st not in ('ok',):
        flag=" <-- 0 条/非 ok, 必须人工看一眼"; bad=1
    print("   %-22s %-8s items=%-4d expired_cut=%-3d %s%s" % (
        p['name'], st, n, p.get('expired_dropped',0), (p.get('note') or '')[:70], flag))
print("   expired_dropped 总数:", d['stats'].get('expired_dropped'))
sys.exit(1 if bad else 0)
PY
  [ $? -ne 0 ] && FAILED=1
else
  echo "!! FAILURE: data/travel4.json 没生成"
  FAILED=1
fi

# ---------------- 2. 建 ----------------
echo "[2/5] build static site ..."
python build.py --data data/travel4.json --config .ilang/travel4.ilang --out site_travel 2>&1
BUILD_RC=$?
if [ "$BUILD_RC" -ne 0 ]; then
  echo "!! FAILURE: build exited $BUILD_RC"
  FAILED=1
fi

# ---------------- 3. 部署 ----------------
# build 失败就绝不部署: 实测过 wrangler 在这种情况下会显示 "Uploaded 0 files" + success,
# 线上其实还是旧内容(可能挂着已过期的档期)。那是假成功, 这里必须把它刹住。
DEPLOYED=0
if [ "${BUILD_RC:-1}" -ne 0 ]; then
  echo "!! SKIP DEPLOY: 上一步 build 失败, 站点没重建 —— 此时部署只会把旧内容再传一遍(假成功)"
  FAILED=1
else
  echo "[3/5] deploy to Cloudflare Pages ..."
  npx wrangler@4 pages deploy site_travel \
    --project-name traveldeals-promo-radar --branch main --commit-dirty=true 2>&1
  DEPLOY_RC=$?
  if [ "$DEPLOY_RC" -ne 0 ]; then
    echo "!! FAILURE: deploy exited $DEPLOY_RC"
    FAILED=1
  else
    DEPLOYED=1
  fi
fi

# ---------------- 4. 部署后复核 ----------------
echo "[4/5] verify production (从本机出口核, 打不开就说明没上线) ..."
[ "$DEPLOYED" -eq 0 ] && echo "   (本次没部署, 下面核到的是上一次的线上内容, 不能当成本次成功)"
for u in "" "provider/southwest-airlines.html" "provider/carnival-cruise-line.html" \
         "provider/hilton.html" "provider/jetblue.html"; do
  code=$(curl -sL -o /dev/null -w "%{http_code}" --max-time 30 "https://traveldeals-promo-radar.pages.dev/$u")
  echo "   $code  /$u"
  [ "$code" != "200" ] && { echo "   !! 非 200, 记入失败"; FAILED=1; }
done

# ---------------- 5. 提交快照 ----------------
echo "[5/5] commit data snapshot ..."
git add data/travel4.json .ilang/travel4.ilang scraper.py build.py tools/ 2>&1
if git diff --cached --quiet; then
  echo "   no changes this run (数据没变)"
else
  git commit -q -m "chore(travel): refresh travel4 $STAMP" 2>&1
  # timeout 保护: 这台机器上 git-over-https 偶尔会卡住; 且部署已完成,
  # push 失败不该把整个刷新记成失败(但必须留痕)。
  # 这台机器上 git-over-https 到 github 明显偏慢(实测单次要数分钟), 给足超时并重试一次。
  # push 失败必须留痕, 但不算刷新失败 —— 部署已经完成, 只是审计快照没进远程。
  # 实测这台机器 git-over-https 到 github 单次 push 要 ~240 秒, 300s 仍会撞线, 给到 600s。
  PUSH_TIMEOUT=600
  PUSHED=0
  for attempt in 1 2; do
    if timeout "$PUSH_TIMEOUT" git push 2>&1; then
      echo "   pushed snapshot to origin/main (第 $attempt 次尝试, 超时上限 ${PUSH_TIMEOUT}s)"
      PUSHED=1
      break
    fi
    echo "!! push 第 $attempt 次失败(超时上限 ${PUSH_TIMEOUT}s)"
  done
  if [ "$PUSHED" -eq 0 ]; then
    echo "!! WARN: git push 两次都没成(超时上限 ${PUSH_TIMEOUT}s)。"
    echo "!!       部署已完成、线上是新的, 但审计快照只在本机, 需人工补推:"
    echo "!!         cd $(pwd) && git push"
    FAILED=1
  fi
fi

echo "=================================================================="
if [ "$FAILED" -ne 0 ]; then
  echo "[travel4 refresh] FINISHED WITH PROBLEMS  $STAMP"
  echo "   本次有失败项, 见上面对应 !! 行。日志: $LOG"
else
  echo "[travel4 refresh] OK  $STAMP"
fi
echo "   log: $LOG"
echo "   site: https://traveldeals-promo-radar.pages.dev/"
echo "=================================================================="
exit "$FAILED"
