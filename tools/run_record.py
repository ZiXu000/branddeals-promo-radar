#!/usr/bin/env python3
# ILANG
# TYPE: tool | ROLE: run-record | PROJECT: branddeals-promo-radar
# ::STATE{@REC, in:data/<site>.json + data/<site>.baseline.json, out:logs/<site>-<date>.log}
# ::RULE{运行记录⇒写进固定路径的带日期文件 全成功 有失败 某家 0 条 三种状态都写}
# ::RULE{某一家抓到 0 条 或那一家请求失败⇒当天记录里必须有显式的一行 点出是哪一家 不许静默跳过}
# ::RULE{本次抓回来的总条数比上一次成功少一半以上⇒视为这次没抓成 不部署 线上留旧快照 留痕}
# ::RULE{判据是条数不是流程状态⇒run 绿只说明流程跑完 不说明抓到了东西}
# ::RULE{同一个坑按类核⇒旅行站与消费站用同一套判据}
# ::BOUNDARY{never:把抓不到写成没有优惠|scope:permanent}
# ::BOUNDARY{never:用上一次的快照冒充今天刷新过|scope:permanent}
"""写当天运行记录 + 判健康。退出码: 0=ALL_OK(可部署), 1=不健康(不许部署)。

用法:
  python tools/run_record.py record   <data.json> <baseline.json> <logfile> [site]
  python tools/run_record.py baseline <data.json> <baseline.json>            # 成功部署后记基线

三种状态都会写进记录:
  全成功          verdict: ALL_OK
  某家 0 条/失败   verdict: BRAND_REGRESSION（点名是哪一家）或 EMPTY_BRAND
  缩水一半以上     verdict: SHRINK_GUARD（不部署, 线上留旧快照）

为什么按"上次有没有条"判异常而不是"只要 0 条就报警":
  消费站那批里有几家是长期被源站拒的(403/404), 它们本来常年 0 条 ——
  若"任何一家 0 条都不部署", 消费站会被永久冻住。所以判据是**回归**:
  "上次抓到过, 这次变 0/失败"才算异常。旅行站四家本来都有条, 所以任何一家归零都会被抓到。
"""
import json
import os
import sys
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def append_record(log_path: str, lines: list) -> None:
    d = os.path.dirname(log_path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def brand_counts(doc) -> dict:
    return {p.get("name", "?"): len(p.get("items", [])) for p in doc.get("providers", [])}


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    # ---- 成功部署之后, 把这次的条数记成下次的基线 ----
    if mode == "baseline":
        data_path, base_path = sys.argv[2], sys.argv[3]
        d = load(data_path) or {}
        with open(base_path, "w", encoding="utf-8") as f:
            json.dump({"date": now(),
                       "total": (d.get("stats") or {}).get("items", 0),
                       "brands": brand_counts(d)}, f, ensure_ascii=False, indent=2)
        print("baseline set: total=%d" % (d.get("stats") or {}).get("items", 0))
        return 0

    if mode != "record":
        print("usage: run_record.py record|baseline ...", file=sys.stderr)
        return 2

    data_path, base_path, log_path = sys.argv[2], sys.argv[3], sys.argv[4]
    site = sys.argv[5] if len(sys.argv) > 5 else "site"

    d = load(data_path)
    base = load(base_path) or {}
    base_total = base.get("total")
    base_brands = base.get("brands") or {}
    base_date = (base.get("date") or "")[:10]

    lines = ["", "=== %s run %s ===" % (site, now())]

    if d is None:
        lines += ["  !! 抓取结果文件缺失或无法解析: %s" % data_path,
                  "verdict: FETCH_FAIL（结果文件都没有）-> NOT deployed, 线上留旧快照"]
        append_record(log_path, lines)
        print("\n".join(lines))
        return 1

    regressions, zeros, total = [], [], 0
    for p in d.get("providers", []):
        name = p.get("name", "?")
        n = len(p.get("items", []))
        st = p.get("status")
        total += n
        prev = base_brands.get(name)
        lines.append("  %-22s %-8s items=%-4d expired_cut=%-3d %s" % (
            name, st, n, p.get("expired_dropped", 0), (p.get("note") or "")[:70]))

        if n == 0:
            zeros.append(name)
            if prev:                      # 上次抓到过, 这次 0 -> 回归, 必须点出来
                lines.append("      <-- ZERO: %s 本次抓到 0 条（上次 %d 条；不是没有优惠，是没抓到）"
                             % (name, prev))
                regressions.append("%s(0条, 上次%d)" % (name, prev))
            else:
                lines.append("      <-- ZERO: %s 本次抓到 0 条（此前也无条，非新增异常）" % name)
        elif st != "ok":
            lines.append("      <-- FAIL: %s 状态=%s（该家本次请求失败）" % (name, st))
            if prev:
                regressions.append("%s(状态%s, 上次%d条)" % (name, st, prev))

    stats = d.get("stats") or {}
    lines.append("total=%d  blocked=%s  expired_dropped=%s" % (
        total, stats.get("blocked"), stats.get("expired_dropped")))

    shrink = False
    if base_total:
        floor = base_total / 2.0
        shrink = total < floor
        lines.append("baseline=%d (%s)  shrink_floor=%.0f  -> %s" % (
            base_total, base_date or "?", floor,
            "本次 %d < %.0f, 触发下限闸" % (total, floor) if shrink
            else "本次 %d >= %.0f, 通过" % (total, floor)))
    else:
        lines.append("baseline=无（首次记录，本次不套下限闸）")

    if regressions:
        lines.append("verdict: BRAND_REGRESSION（%s）-> NOT deployed, 线上留旧快照"
                     % "; ".join(regressions))
        rc = 1
    elif shrink:
        lines.append("verdict: SHRINK_GUARD（总条数不足上次成功的一半）-> NOT deployed, 线上留旧快照")
        rc = 1
    else:
        lines.append("verdict: ALL_OK -> deploy")
        rc = 0

    append_record(log_path, lines)
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
