# -*- coding: utf-8 -*-
"""回归测试：净值公布但夜间结算尚未跑时的市值口径（窗口期）。

背景：amount 只维护到「已结算净值日(last_settled_nav_date)」的收盘总值。
官方净值公布后、settle_daily 跑之前存在一个时间窗——若此时直接取 mv=amount，
市值会「退回」上一结算日，今天赚的钱在界面上凭空消失，而涨幅已显示官方新值，
两者自相矛盾。修法：是否乘 (1+pct%) 只认 last_settled_nav_date，不认 pct_src。

跑法：D:/python/python.exe test_settle_window.py
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import fund_manager as fm  # noqa: E402

TODAY = "2026-09-14"
LAST_TD = "2026-09-14"
PREV_NAV = "2026-09-11"

_fails = []
_passes = [0]


def _setup(fund, quote, intraday):
    fm.load_funds = lambda: [fund]
    fm.fetch_quotes = lambda codes, use_cache=True: [quote]
    fm.get_cached_intraday = lambda code: intraday
    fm._bj_date = lambda: TODAY
    fm._last_trade_date = lambda: LAST_TD
    fm.in_trading_session = lambda: False


def case(name, fund, quote, intraday, expect_mult):
    _setup(fund, quote, intraday)
    row = fm.merge_with_amounts(use_cache=True)[0]
    amt = fund["amount"]
    exp = round(amt * (1 + (row["pct"] or 0) / 100.0), 2) if expect_mult else round(amt, 2)
    got = row["market_value"]
    ok = got is not None and abs(got - exp) < 0.02
    # day_pnl 语义：该涨跌对应的金额。
    #   乘的口径(est/tick/未结算nav)：基准=amount(上一结算日收盘) → pnl = mv − amount
    #   不乘的口径(已结算nav)：amount 已含该日涨跌 → pnl = mv − mv/(1+pct%)
    if expect_mult:
        pnl_exp = round(got - amt, 2)
    else:
        denom = (1 + (row["pct"] or 0) / 100.0)
        pnl_exp = round(got - (got / denom), 2) if denom else 0.0
    pnl_ok = row["day_pnl"] is not None and abs(row["day_pnl"] - pnl_exp) < 0.02
    if ok and pnl_ok:
        _passes[0] += 1
        print("  PASS %-42s pct=%-7s src=%-4s mv=%s" % (name, row["pct"], row["pct_src"], got))
    else:
        _fails.append(name)
        print("  FAIL %-42s pct=%-7s src=%-4s mv=%s 期望=%s day_pnl=%s 期望=%s"
              % (name, row["pct"], row["pct_src"], got, exp, row["day_pnl"], pnl_exp))


base_fund = {"code": "016874", "name": "测试基金C", "amount": 1000.0,
             "buy_date": "2026-09-05", "last_settled_nav_date": PREV_NAV}

print("[窗口期：净值已公布、结算未跑]")
# nav_fresh=True 且 nav_date != last_settled → 涨跌还没滚进 amount → 必须乘
case("净值新日未结算 → 市值须含当日涨跌",
     dict(base_fund),
     {"code": "016874", "name": "测试基金C", "pct": 2.0, "nav_date": TODAY,
      "src": "nav", "intraday_pct": None},
     None, True)

print("[已结算：同一净值日不重复计息]")
# nav_date == last_settled → amount 已是该日收盘总值 → 不能再乘
case("净值日已结算 → 市值 = amount",
     dict(base_fund, last_settled_nav_date=TODAY),
     {"code": "016874", "name": "测试基金C", "pct": 2.0, "nav_date": TODAY,
      "src": "nav", "intraday_pct": None},
     None, False)

print("[盘中：净值滞后时用估算]")
# nav_fresh=False + 有估算 → est 口径 → 必须乘
case("净值滞后有估算 → est 口径乘以估算",
     dict(base_fund),
     {"code": "016874", "name": "测试基金C", "pct": 1.5, "nav_date": PREV_NAV,
      "src": "nav", "intraday_pct": 2.0},
     2.0, True)

print("[盘中：无估算回落净值]")
# nav_fresh=False 且无估算 → pct=nav_pct、pct_src=nav，但 nav_date==last_settled → 不乘
case("净值滞后无估算 → 回落净值且不重复计息",
     dict(base_fund),
     {"code": "016874", "name": "测试基金C", "pct": 1.5, "nav_date": PREV_NAV,
      "src": "nav", "intraday_pct": None},
     None, False)

print("[上市基金：tick 口径]")
case("tick → 市值含当日涨跌",
     {"code": "510300", "name": "测试ETF", "amount": 1000.0, "buy_date": "2026-09-05"},
     {"code": "510300", "name": "测试ETF", "pct": -1.2, "nav_date": None,
      "src": "tick", "intraday_pct": -1.2},
     -1.2, True)

print("[今日浮动合计：必须与 total_market_value 同源]")
# 复现 2026-09-23 老大从看板发现的问题：server 用概念板块实时涨幅重算了
# market_value / day_pnl，但 est_pnl 留在覆盖前 → 合计若读 est_pnl，
# 就会出现「市值按今天算(+)、今日浮动按上一交易日净值算(−)」两个时钟。
_ov = [
    {"amount": 1000.0, "market_value": 1010.0, "day_pnl": 10.0, "est_pnl": -5.0},
    {"amount": 500.0, "market_value": 495.0, "day_pnl": -5.0, "est_pnl": -3.0},
]
_a = fm.total_day_pnl(_ov)
_mv = sum(r["market_value"] for r in _ov)
_amt = sum(r["amount"] for r in _ov)
# 期望 10 + (−5) = +5（与 1505 − 1500 对得上），而不是 est_pnl 的 −8
if abs(_a - 5.0) < 0.02:
    _passes[0] += 1
    print("  PASS 覆盖后合计取 day_pnl                         合计=%+.2f" % _a)
else:
    _fails.append("覆盖后合计取 day_pnl")
    print("  FAIL 覆盖后合计取 day_pnl                         合计=%+.2f 期望=+5.00" % _a)
# 恒等式：市值合计 − 今日浮动 == 成本合计（est 口径下）
if abs((_mv - _a) - _amt) < 0.02:
    _passes[0] += 1
    print("  PASS 市值−浮动==成本（同一基准）                   %.2f − %.2f = %.2f" % (_mv, _a, _amt))
else:
    _fails.append("市值−浮动==成本")
    print("  FAIL 市值−浮动==成本                               %.2f − %.2f != %.2f" % (_mv, _a, _amt))
# 缺失 day_pnl 时回落 est_pnl（老数据/异常行不能炸）
if abs(fm.total_day_pnl([{"est_pnl": 3.0}, {"est_pnl": None}, {}]) - 3.0) < 0.02:
    _passes[0] += 1
    print("  PASS 缺 day_pnl 回落 est_pnl / None 与空行不炸")
else:
    _fails.append("缺 day_pnl 回落 est_pnl")
    print("  FAIL 缺 day_pnl 回落 est_pnl")
if fm.total_day_pnl([]) == 0.0 and fm.total_day_pnl(None) == 0.0:
    _passes[0] += 1
    print("  PASS 空/None 输入返回 0")
else:
    _fails.append("空输入返回 0")
    print("  FAIL 空输入返回 0")

print("[源码守卫：server 覆盖当日口径后必须同步 est_pnl]")
# 静态断言——这行一旦被删，合计又会退回覆盖前的旧值（本 bug 的根因）。
_srv = open(os.path.join(BASE, "server.py"), encoding="utf-8").read()
_g1 = 'r["est_pnl"] = r["day_pnl"]' in _srv
_g2 = "total_pnl = fm.total_day_pnl(rows)" in _srv
_g3 = "sum((r[\"est_pnl\"] or 0) for r in rows)" not in _srv
if _g1 and _g2 and _g3:
    _passes[0] += 1
    print("  PASS 覆盖后同步 est_pnl 且合计走 total_day_pnl")
else:
    _fails.append("server 汇总口径守卫")
    print("  FAIL 覆盖后同步 est_pnl 且合计走 total_day_pnl (est同步=%s 走helper=%s 无旧写法=%s)"
          % (_g1, _g2, _g3))

print("[结算脏数据护栏：可疑数据跳过待重试，绝不落账]")
# 2026-09-24 事故：净值公布瞬间接口返回过渡态脏数据，单日被错滚 -8.98%/-13.94%/
# -17.67%（官方仅 -3% 左右），写入 last_settled_nav_date 后被幂等逻辑永久固化。
# 护栏：比率路径单日滚动须与官方单日涨跌互相印证；回退路径超 9% 判脏 + 两轮一致才落账。
_saved = []


def _mk_settle_env(amount, last_settled, nav_pct, hist, nav_date="2026-09-24"):
    fm.load_funds = lambda: [{"code": "016874", "name": "测试基金C", "amount": amount,
                              "buy_date": "2026-09-05",
                              "last_settled_nav_date": last_settled}]
    fm.merge_with_amounts = lambda use_cache=False: [
        {"code": "016874", "name": "测试基金C", "nav_date": nav_date, "nav_pct": nav_pct}]
    fm.fetch_nav_history = lambda code, **k: hist
    fm.save_funds = lambda funds: _saved.append(funds)
    fm._SETTLE_PENDING.clear()
    _saved.clear()


def guard_case(name, expect_settled, expect_amount=None):
    res = fm.settle_daily()
    if expect_settled:
        ok = bool(res) and _saved and expect_amount is not None \
            and abs(_saved[0][0]["amount"] - expect_amount) < 0.02 \
            and _saved[0][0]["last_settled_nav_date"] == "2026-09-24"
    else:
        ok = not res and not _saved
    if ok:
        _passes[0] += 1
        print("  PASS %s" % name)
    else:
        _fails.append(name)
        print("  FAIL %s  settled=%s saved=%s" % (name, res, _saved))


# 比率 -8.98% 与官方单日 -3.01% 打架 → 跳过不落账（复现 9/24 事故）
_mk_settle_env(1288.08, "2026-09-23", -3.01,
               [("2026-09-24", 1.60), ("2026-09-23", 1.76)])
guard_case("G1 比率与官方单日打架 → 跳过不落账", False)

# 比率与官方一致 → 正常结算（1288.08 × 1.6756/1.7276 = 1249.31）
_mk_settle_env(1288.08, "2026-09-23", -3.01,
               [("2026-09-24", 1.6756), ("2026-09-23", 1.7276)])
guard_case("比率与官方印证一致 → 正常结算", True, 1249.31)

# 单日真实大涨 +9.5%：官方单日同样 +9.5% → 印证通过，不误伤
_mk_settle_env(1000.0, "2026-09-23", 9.5,
               [("2026-09-24", 1.095), ("2026-09-23", 1.0)])
guard_case("单日真实 ±9%+ 双源印证 → 正常结算", True, 1095.0)

# 回退路径（f10 未收录当日净值）：第一轮只记录，第二轮一致才结算
_mk_settle_env(1288.08, "2026-09-23", -3.01,
               [("2026-09-23", 1.7276)])
guard_case("G2 回退路径首轮 → 只记录不结算", False)
guard_case("G2 回退路径次轮一致 → 结算", True, round(1288.08 * 0.9699, 2))

# 回退路径 |pct|>9% → 两轮都不落账（复现 9/24 的 -13.94% 错滚）
_mk_settle_env(532.61, "2026-09-23", -13.94,
               [("2026-09-23", 0.6126)])
guard_case("G2 回退超 9% 首轮 → 判脏跳过", False)
guard_case("G2 回退超 9% 次轮 → 仍不落账", False)

print("\n通过 %d 项，失败 %d 项" % (_passes[0], len(_fails)))
sys.exit(1 if _fails else 0)
