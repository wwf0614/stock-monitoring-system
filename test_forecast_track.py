# -*- coding: utf-8 -*-
"""命中率模块回归测试：构造合成预测 -> 落盘 -> 回填 -> 统计。"""
import os, sys, json, time, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import forecast_track as ft

TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_fh.json")
ft._HISTORY_PATH = TMP
# 必须连 .bak 一起清：safe_load_json 在主文件缺失时会回退到 .bak，
# 上次测试的残留会让本次"从空库开始"的假设失效。
for p in (TMP, TMP + ".bak",
          os.path.join(os.path.dirname(TMP), "_test_fh2.json"),
          os.path.join(os.path.dirname(TMP), "_test_fh2.json.bak")):
    if os.path.exists(p):
        os.remove(p)

FAIL = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ((" | " + str(extra)) if extra else ""))
    if not cond:
        FAIL.append(name)

# ---------- 1) grade 判定矩阵 ----------
print("\n[1] grade() 判定矩阵")
cases = [
    ("up",   1.00, 1.0, "命中"),
    ("up",   0.10, 0.5, "部分"),
    ("up",  -0.10, 0.5, "部分"),
    ("up",  -0.50, 0.0, "落空"),
    ("up",  -1.50, 0.0, "反向"),
    ("down",-1.00, 1.0, "命中"),
    ("down", 0.10, 0.5, "部分"),
    ("down", 1.50, 0.0, "反向"),
    ("side", 0.20, 1.0, "命中"),
    ("side", 0.80, 0.5, "部分"),
    ("side", 2.00, 0.0, "落空"),
]
for d, r, eh, eg in cases:
    h, g = ft.grade(d, r)
    ck("grade(%s,%+.2f)=%.1f/%s" % (d, r, eh, eg), h == eh and g == eg, "实得 %.1f/%s" % (h, g))
ck("grade(None)", ft.grade("up", None) == (None, None))

# ---------- 2) 记录落盘 + 去重 ----------
print("\n[2] 记录落盘 / 去重 / 节流")
DAYS = ["2026-08-03","2026-08-04","2026-08-05","2026-08-06","2026-08-07",
        "2026-08-10","2026-08-11","2026-08-12","2026-08-13","2026-08-14"]
CLOSES = [4000,4040,4020,4060,4000,3960,3990,4030,4040,4000]
DIRS   = ["up","up","side","down","down","down","side","up","down","up"]
CONFS  = [62,70,45,72,55,68,52,60,58,66]
POSS   = [70,75,40,30,25,60,45,55,35,50]

def fake_result(date, direction, conf, position, lookback=20, last=4000.0):
    return {
        "ok": True, "lookback": lookback,
        "market": {"direction": direction, "confidence": conf,
                   "composite_score": 1.5, "suggested_position": position,
                   "regime_label": "震荡偏多", "regime_type": "震荡市",
                   "vol_annual": 16.4, "eff_lookback": 19},
        "indices": [
            {"name": "创业板指", "secid": "0.399006", "last": 2000.0, "last_date": date},
            {"name": "沪深300", "secid": "1.000300", "last": last, "last_date": date},
        ],
    }

for i, d in enumerate(DAYS):
    ck("记录 %s" % d, ft.record_forecast(
        fake_result(d, DIRS[i], CONFS[i], POSS[i], last=float(CLOSES[i]))))

recs = ft.load_records()
ck("记录数=10", len(recs) == 10, len(recs))
# 主指数应选沪深300(rank 0)，而不是数组里更靠前的创业板指(rank 2)
ck("主指数=沪深300", recs[0].get("core_name") == "沪深300", recs[0].get("core_name"))
ck("锚点价正确", recs[0].get("anchor_price") == 4000.0, recs[0].get("anchor_price"))

# 重复记录(同一天同一窗口)：值未变化且距上次写盘<120s，命中节流 -> 不重复写盘
ft.record_forecast(fake_result(DAYS[0], "up", 62, 70, last=4000.0))
recs2 = ft.load_records()
ck("去重后仍10条", len(recs2) == 10, len(recs2))
ck("节流不重复写盘", [r for r in recs2 if r["date"] == DAYS[0]][0]["samples"] == 1)

# 值发生变化 -> 应更新并写盘(把置信度从 62 改成 66)
ft.record_forecast(fake_result(DAYS[0], "up", 66, 70, last=4000.0))
recs2b = ft.load_records()
ck("值变化则更新", [r for r in recs2b if r["date"] == DAYS[0]][0]["confidence"] == 66)

# 不同 lookback -> 独立记录
ft.record_forecast(fake_result(DAYS[0], "up", 62, 70, lookback=60, last=4000.0))
ck("lookback=60 新开一条", len(ft.load_records()) == 11)

# 无锚点(指数缺失) -> 不记录
ck("无锚点不记录", ft.record_forecast({"ok": True, "lookback": 20,
    "market": {"direction": "up", "confidence": 60}, "indices": []}) is False)
ck("ok=False 不记录", ft.record_forecast({"ok": False}) is False)

# ---------- 3) 回填 ----------
print("\n[3] 回填(settle)")
KLINES = {"1.000300": list(zip(DAYS, [float(c) for c in CLOSES]))}
ft.df = types.SimpleNamespace(fetch_kline=lambda secid, lmt=12: KLINES.get(secid, []))
res = ft.settle_pending()
print("   ", res)
# 11 条记录里，只有 08-14 那条还没有次日K线 -> 应回填 10 条、待定 1 条
ck("回填条数=10", res["settled"] == 10, res["settled"])
recs3 = ft.load_records()
done = [r for r in recs3 if r.get("actual")]
ck("已判定=10", len(done) == 10, len(done))
exp_hits = [1.0, 1.0, 0.0, 0.5, 1.0, 1.0, 0.0, 0.5, 0.5, 1.0]
got_hits = [r["hit"] for r in done]
ck("命中序列正确", got_hits == exp_hits, "%s vs %s" % (got_hits, exp_hits))
ck("次日日期正确", done[0]["actual"]["date"] == "2026-08-04", done[0]["actual"]["date"])
ck("次日收益正确", abs(done[0]["actual"]["ret"] - 1.0) < 0.01, done[0]["actual"]["ret"])
ck("待定=1(最后一天无次日K线)", res["pending"] == 1, res["pending"])
# 幂等：再跑一次不应重复回填
res2 = ft.settle_pending()
ck("重复回填=0", res2["settled"] == 0, res2["settled"])

# ---------- 4) 统计 ----------
print("\n[4] 统计(build_stats)")
st = ft.build_stats()
print("   ", json.dumps({k: v for k, v in st.items() if k != "recent"}, ensure_ascii=False))
ck("ready=True", st["ready"] is True)
ck("样本数=11", st["total"] == 11, st["total"])
ck("已判定=10", st["settled"] == 10, st["settled"])
ck("加权命中率=65.0%", abs(st["hit_rate"] - 65.0) < 0.1, st["hit_rate"])
ck("严格命中率=50.0%", abs(st["strict_hit_rate"] - 50.0) < 0.1, st["strict_hit_rate"])
ck("Brier有值", st["brier"] is not None and 0 <= st["brier"] <= 1, st["brier"])
cal = {c["bucket"]: c for c in st["calibration"]}
ck("校准档位齐全", set(cal.keys()) == {"35-50", "50-58", "58-66", "66-100"}, list(cal.keys()))
ck("66+档实际=50.0%", abs(cal["66-100"]["real"] - 50.0) < 0.1, cal["66-100"])
ck("58-66档实际=83.3%", abs(cal["58-66"]["real"] - 83.3) < 0.1, cal["58-66"])
ck("置信度与实际同向可比(低档<=高档或反之均可统计)",
   all(c["n"] == 0 or c["real"] is not None for c in st["calibration"]))
pe = st.get("position_edge")
ck("仓位分组有效", pe is not None and pe["low_next_ret"] < pe["high_next_ret"], pe)
ck("按方向分类存在", set(st["by_direction"].keys()) == {"up", "down", "side"},
   list(st["by_direction"].keys()))
ck("recent 最多10条", len(st["recent"]) == 10, len(st["recent"]))
ck("JSON可序列化", isinstance(json.dumps(st, ensure_ascii=False), str))

# ---------- 5) 样本不足降级 ----------
print("\n[5] 样本不足降级")
ft._HISTORY_PATH = os.path.join(os.path.dirname(TMP), "_test_fh2.json")
if os.path.exists(ft._HISTORY_PATH):
    os.remove(ft._HISTORY_PATH)
ft.record_forecast(fake_result("2026-09-01", "up", 60, 50, last=4100.0))
st2 = ft.build_stats()
ck("ready=False", st2["ready"] is False)
ck("有提示文案", "样本" in (st2.get("note") or ""), st2.get("note"))
ck("无命中率数字(不编造)", st2.get("hit_rate") is None)

# ---------- 6) 文件损坏自愈 ----------
print("\n[6] 损坏文件自愈")
with open(ft._HISTORY_PATH, "w", encoding="utf-8") as f:
    f.write('{"records": [{"key": "broken"')
try:
    r = ft.load_records()
    ck("损坏后回退不崩溃", isinstance(r, list), r)
except Exception as e:
    ck("损坏后回退不崩溃", False, e)

os.remove(TMP)
if os.path.exists(ft._HISTORY_PATH):
    os.remove(ft._HISTORY_PATH)

print("\n" + ("=" * 50))
print("失败 %d 项" % len(FAIL))
for f_ in FAIL:
    print("  - " + f_)
sys.exit(1 if FAIL else 0)
