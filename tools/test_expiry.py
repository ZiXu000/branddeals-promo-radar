"""过期规则自测: 证明"会读结束日期 + 过期行直接摘掉"确实生效。
用 Carnival 实测到的两种真实写法 + 已到期/未到期两种情况。
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scraper  # noqa: E402

TODAY = date(2026, 9, 13)

CASES = [
    # (页面原文, 期望结束日, 说明)
    ("Hurry! Ends Monday, September 5th", date(2026, 9, 5), "老板点名的已过期档期"),
    ("Hurry! Ends Monday, September 7th", date(2026, 9, 7), "老板点名的已过期档期"),
    ("Hurry! Ends Monday, September 14th", date(2026, 9, 14), "未过期 应保留"),
    ("Offer ends November 21, 2027", date(2027, 11, 21), "带年份 未过期"),
    ("Offer ends March 3, 2026", date(2026, 3, 3), "带年份 已过期"),
    ("Book and travel by 11/13", date(2026, 11, 13), "mm/dd 未过期"),
    ("Book and travel by 09/01", date(2026, 9, 1), "mm/dd 已过期"),
]

print("=== 结束日期解析 ===")
ok = True
for text, want, note in CASES:
    got = scraper.parse_enddate_from_text(text, TODAY)
    flag = "OK " if got == want else "FAIL"
    if got != want:
        ok = False
    print("  %s %-42s -> %-12s (want %s) %s" % (flag, text, got, want, note))

print("\n=== drop_expired 摘行 ===")
items = [
    {"title": "Endless Summer Flash Sale", "valid_until": "2026-09-05"},
    {"title": "More Time More Perks", "valid_until": "2026-09-07"},
    {"title": "Free 3rd & 4th Guest", "valid_until": "2026-09-14"},
    {"title": "Casino Tournament Access", "valid_until": "2027-11-21"},
    {"title": "Honors Advance Purchase", "valid_until": None},
]
keep, cut = scraper.drop_expired(items, TODAY)
print("  输入 %d 条 -> 保留 %d 条, 摘掉 %d 条" % (len(items), len(keep), len(cut)))
for c in cut:
    print("    CUT  %-32s expired_on=%s" % (c["title"], c["expired_on"]))
for k in keep:
    print("    KEEP %-32s valid_until=%s" % (k["title"], k["valid_until"]))

expect_keep = {"Free 3rd & 4th Guest", "Casino Tournament Access", "Honors Advance Purchase"}
expect_cut = {"Endless Summer Flash Sale", "More Time More Perks"}
assert {k["title"] for k in keep} == expect_keep, keep
assert {c["title"] for c in cut} == expect_cut, cut
assert ok, "日期解析有用例没对上"
print("\n全部断言通过: 过期的两条被摘掉, 未过期与长期档期保留。")
