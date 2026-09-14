#!/usr/bin/env python3
# ILANG
# TYPE: tool | ROLE: run-record | PROJECT: branddeals-promo-radar
# ::STATE{@REC, in:data/<site>.json + <site>.baseline.json + <site>.state.json, out:logs/<site>-<date>.log}
# ::RULE{运行记录⇒写进固定路径的带日期文件 全成功 有失败 某家 0 条 三种状态都写}
# ::RULE{某一家抓到 0 条 或那一家请求失败⇒当天记录里必须有显式的一行 点出是哪一家 不许静默跳过}
# ::RULE{本次抓回来的总条数比上一次成功少一半以上⇒视为这次没抓成 不部署 线上留旧快照 留痕}
# ::RULE{某一家连着几次都抓不到⇒当它跟消费站那 6 家一样标成已知抓不到 从闸里摘出去 其余家照跑}
# ::RULE{标成已知抓不到⇒留一行痕 写清哪天标的 依据是什么}
# ::RULE{摘出去不是删掉⇒它哪天抓回来了 要能自己回到闸里}
# ::RULE{判据是条数不是流程状态⇒run 绿只说明流程跑完 不说明抓到了东西}
# ::BOUNDARY{never:把抓不到写成没有优惠|scope:permanent}
# ::BOUNDARY{never:用上一次的快照冒充今天刷新过|scope:permanent}
# ::BOUNDARY{never:靠人去改基线来放行|scope:permanent}
"""写当天运行记录 + 判健康。退出码: 0=ALL_OK(可部署), 1=不健康(不许部署)。

用法:
  python tools/run_record.py record   <data.json> <baseline.json> <logfile> [site]
  python tools/run_record.py baseline <data.json> <baseline.json>            # 成功部署后记基线

三种状态都写进记录: ALL_OK / BRAND_REGRESSION(某家上次有条这次0条或失败, 点名) /
SHRINK_GUARD(总条数不足上次成功一半)。

出口(2026-09-14 加): 某一家连续 MISS_THRESHOLD 次都抓不到 -> 标成"已知抓不到", 从闸里摘出去,
其余家照常判。它哪天抓回来了, 自动解除标记、回到闸里 —— 不需要人去改基线。
连续次数记在 <site>.state.json(每次运行都更新, 与"只有部署成功才更新"的基线分开)。
"""
import json
import os
import sys
from datetime import datetime, timezone

MISS_THRESHOLD = 3      # 连续几次抓不到 -> 标成已知抓不到、从闸里摘出


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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


def state_path_for(baseline_path: str) -> str:
    if baseline_path.endswith(".baseline.json"):
        return baseline_path[: -len(".baseline.json")] + ".state.json"
    return baseline_path + ".state.json"


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
    st_path = state_path_for(base_path)

    d = load(data_path)
    base = load(base_path) or {}
    base_total = base.get("total")
    base_brands = base.get("brands") or {}
    base_date = (base.get("date") or "")[:10]
    state = load(st_path) or {}
    st_brands = state.setdefault("brands", {})

    lines = ["", "=== %s run %s ===" % (site, now())]

    if d is None:
        lines += ["  !! 抓取结果文件缺失或无法解析: %s" % data_path,
                  "verdict: FETCH_FAIL（结果文件都没有）-> NOT deployed, 线上留旧快照"]
        append_record(log_path, lines)
        print("\n".join(lines))
        return 1

    regressions, excluded, total = [], [], 0

    for p in d.get("providers", []):
        name = p.get("name", "?")
        n = len(p.get("items", []))
        st = p.get("status")
        total += n
        prev = base_brands.get(name)
        rec = st_brands.get(name) or {}
        lines.append("  %-22s %-8s items=%-4d expired_cut=%-3d %s" % (
            name, st, n, p.get("expired_dropped", 0), (p.get("note") or "")[:70]))

        missing = (n == 0) or (st != "ok")

        if missing:
            rec["miss_streak"] = int(rec.get("miss_streak", 0)) + 1
            if not rec.get("known_missing_since") and rec["miss_streak"] >= MISS_THRESHOLD:
                # ::RULE{某一家连着几次都抓不到⇒标成已知抓不到 从闸里摘出去 留一行痕}
                rec["known_missing_since"] = today()
                rec["reason"] = "连续 %d 次抓到 0 条/失败" % rec["miss_streak"]
                lines.append("      <-- KNOWN-MISSING: %s 连续 %d 次抓不到（%s），"
                             "自 %s 起标为已知抓不到，本次起从闸里摘出（页面上不写它还有优惠）"
                             % (name, rec["miss_streak"], p.get("note") or "-",
                                rec["known_missing_since"]))
            elif rec.get("known_missing_since"):
                lines.append("      <-- KNOWN-MISSING(仍在): %s 自 %s 起标为已知抓不到"
                             "（连续 %d 次；本次仍从闸里摘出）"
                             % (name, rec["known_missing_since"], rec["miss_streak"]))
            else:
                lines.append("      <-- MISS %d/%d: %s 本次抓不到（连续 %d 次；满 %d 次将标为已知抓不到）"
                             % (rec["miss_streak"], MISS_THRESHOLD, name,
                                rec["miss_streak"], MISS_THRESHOLD))
        else:
            if rec.get("known_missing_since"):
                # ::RULE{摘出去不是删掉⇒它哪天抓回来了 要能自己回到闸里}
                lines.append("      <-- BACK: %s 本次抓回 %d 条，自动回到闸里"
                             "（原标记 %s 解除，原因曾为: %s）"
                             % (name, n, rec["known_missing_since"], rec.get("reason") or "-"))
                rec.pop("known_missing_since", None)
                rec.pop("reason", None)
            rec["miss_streak"] = 0

        st_brands[name] = rec
        if rec.get("known_missing_since"):
            excluded.append(name)
        else:
            # 闸只判"没被摘出去"的家
            if n == 0:
                if prev:
                    regressions.append("%s(0条, 上次%d)" % (name, prev))
            elif st != "ok":
                if prev:
                    regressions.append("%s(状态%s, 上次%d条)" % (name, st, prev))

    stats = d.get("stats") or {}
    lines.append("total=%d  blocked=%s  expired_dropped=%s" % (
        total, stats.get("blocked"), stats.get("expired_dropped")))
    if excluded:
        lines.append("excluded(已知抓不到, 已从闸里摘出)=%s" % ", ".join(excluded))

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
        tail = ("（已摘出已知抓不到: %s）" % ", ".join(excluded)) if excluded else ""
        lines.append("verdict: ALL_OK%s -> deploy" % tail)
        rc = 0

    # 连续次数每次运行都更新(与基线分开): 被拦的那次也要记, 否则出口永远攒不够次数。
    state["updated"] = now()
    try:
        os.makedirs(os.path.dirname(st_path) or ".", exist_ok=True)
        with open(st_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:                                        # noqa: BLE001
        lines.append("  !! 状态文件写失败: %s" % e)

    append_record(log_path, lines)
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
