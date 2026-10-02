# -*- coding: utf-8 -*-
"""
test_signals.py —— 投资提醒信号引擎回归测试
==========================================
用合成数据把每条规则都真正触发一遍（线上首日只有当日数据，多日分支跑不到，
所以必须靠合成历史来验证 S3/S4/S5/B2/B3 的判定逻辑，而不是等三天后才发现写错）。

运行：D:/python/python.exe test_signals.py
"""
import os
import sys
import json
import tempfile
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import signals as sg
import history_db as hdb

_ok = 0
_bad = 0


def check(name, cond, extra=""):
    global _ok, _bad
    if cond:
        _ok += 1
        print("  [PASS] %s" % name)
    else:
        _bad += 1
        print("  [FAIL] %s  %s" % (name, extra))


def board(name, kind, pct, main_yi, xl_yi, small_yi, big_yi=None, mid_yi=None):
    """构造一条板块记录（金额入参单位=亿，写成上游的「元」口径）。"""
    return {"name": name, "pct": pct,
            "main_net": main_yi * 1e8, "inst_net": xl_yi * 1e8,
            "big_net": (big_yi if big_yi is not None else main_yi - xl_yi) * 1e8,
            "mid_net": (mid_yi if mid_yi is not None else 0.0) * 1e8,
            "small_net": small_yi * 1e8,
            "total_net": (main_yi + small_yi) * 1e8}


def capital(industries=None, concepts=None, mkt_main_yi=0.0, breadth=0.5):
    ind = industries or []
    return {"industries": ind, "concepts": concepts,
            "market_main_net": mkt_main_yi * 1e8,
            "market_total_net": mkt_main_yi * 1e8,
            "breadth": breadth}


def seed(db, name, kind, series, base=None):
    """series: [(days_ago, pct, main_yi)]，days_ago>=1 才是历史（今日由实时值补齐）。

    基准取「最近交易日」(signals._today()) 而非自然日：周末跑测试时最近交易日
    是周五，若按自然日回推，关键历史会落在「当日」上被数据层正当排除，导致误报。
    这里刻意用 _today() 而不是 _snapshot_date()：前者不含时间语义（不做盘前修正），
    测试结果不会随运行时刻漂移；盘前口径另有第 8 节用固定时刻显式验证。
    """
    b = datetime.datetime.strptime(base or sg._today(), "%Y-%m-%d").date()
    conn = hdb.connect(db)
    try:
        for ago, pct, main in series:
            d = (b - datetime.timedelta(days=ago)).strftime("%Y-%m-%d")
            hdb.upsert_board_flow(conn, d, kind,
                                  [board(name, kind, pct, main, main * 0.6, -main * 0.2)],
                                  settled=True)
    finally:
        conn.close()


def run(label, cap, db, funds):
    sg.load_funds = lambda: funds
    return sg.compute(cap, db=db)


def main():
    tmp = tempfile.mkdtemp(prefix="sgtest_")
    db0 = os.path.join(tmp, "t0.db")
    # 「趋势库」：给几只板块铺 3 天历史净流入。多日趋势是买入候选的唯一依据，
    # 凡是需要出现买入候选的用例都必须用这个库；空库（db0）按设计就是"没有候选"。
    db_t = os.path.join(tmp, "trend.db")
    for _nm, _base in (("半导体", 10.0), ("创新药", 8.0), ("国产芯片", 55.0),
                       ("融资融券", 110.0), ("MSCI中国", 85.0)):
        seed(db_t, _nm, "concept",
             [(3, 1.0, _base * 0.9), (2, 0.9, _base), (1, 1.1, _base * 1.15)])

    print("\n== 1. 冷启动：只有当日数据（单日流入不构成买入理由）==")
    # 旧版这里会因「当日 300 亿流入」直接给出买入候选——那正是"基金当天涨就让你加仓"的来源。
    # 场外基金 T 日按收盘净值确认、T+1 生效，单日流入不可交易，所以现在必须为空。
    cap = capital(concepts=[board("半导体", "concept", 3.0, 20.0, 12.0, -5.0)],
                  mkt_main_yi=200.0, breadth=0.8)
    r = run("cold", cap, db0, [{"code": "017811", "name": "测试AI", "concepts": ["半导体"]}])
    check("ok 且无异常", r["ok"] is True, r.get("error"))
    check("days=0", r["days"] == 0, r["days"])
    check("pending 说明多日趋势未启用",
          any("B1/B2/S3" == p["rule"] for p in r["pending"]), r["pending"])
    check("pending 提示对照信号未启用", any("B3/S4/S5" == p["rule"] for p in r["pending"]))
    check("冷启动不出买入候选（单日流入不构成买入理由）", r["buy"] == [], r["buy"])
    check("结论不因单日流入给「可买」", r["verdict"]["action"] == "持有", r["verdict"])
    check("结论卡恒定说明 T+1 交易口径",
          "T+1" in (r["verdict"].get("note") or ""), r["verdict"].get("note"))

    print("\n== 2. 卖出：散户接盘 / 超大单撤离（当日即可算）==")
    cap = capital(concepts=[board("创新药", "concept", -1.2, -8.0, -6.5, 5.0)],
                  mkt_main_yi=-300.0, breadth=0.2)
    r = run("sell1", cap, db0, [{"code": "012738", "name": "测试药", "concepts": ["创新药"]}])
    ids = [x["rule"] for x in r["sell"]]
    check("S1 散户接盘命中", "S1" in ids, ids)
    check("S2 超大单撤离命中", "S2" in ids, ids)
    check("S6 市场普跌叠加命中", "S6" in ids, ids)
    check("卖出证据含具体数值", all("%" in x["evidence"] or "亿" in x["evidence"] for x in r["sell"]))

    print("\n== 2b. S7 拉高派发：上涨中的主力流出不得漏报 ==")
    cap = capital(concepts=[board("PCB", "concept", 1.69, -60.2, 80.1, -27.3),
                            board("CPO概念", "concept", 2.48, -30.0, -20.0, 12.0),
                            board("MLCC", "concept", 1.00, -10.0, -4.0, 5.0)],
                  mkt_main_yi=800.0, breadth=0.8)
    r = run("pump", cap, db0, [{"code": "A", "concepts": ["PCB"]},
                               {"code": "B", "concepts": ["CPO概念"]},
                               {"code": "C", "concepts": ["MLCC"]}])
    ids = sorted(x["rule"] for x in r["sell"])
    check("S7 命中（涨幅 > +0.5% 且主力净流出、小单接盘）", "S7" in ids, ids)
    check("不再只认横盘/下跌（S1 不重复报同一板块）", "S1" not in ids, ids)
    check("主力流出而小单也流出的板块不误报",
          "PCB" not in [x["board"] for x in r["sell"]], r["sell"])
    sevs = sorted(x["sev"] for x in r["sell"] if x["rule"] == "S7")
    check("涨幅≥pump_pct 记 danger、1.0% 记 warn", sevs == ["danger", "warn"], sevs)
    ev = next(x["evidence"] for x in r["sell"] if x["rule"] == "S7")
    check("S7 证据含涨幅与流出额", "%" in ev and "亿" in ev, ev)
    scan = {x["rule"]: x for x in r["sell_scan"]}
    check("sell_scan 覆盖 7 条卖出规则", set(scan) == {"S1", "S2", "S3", "S4", "S5", "S6", "S7"},
          sorted(scan))
    check("sell_scan 把已触发的标 fired", scan["S7"]["status"] == "fired", scan["S7"])
    check("S1 未触发但有原因（指向已按 S7 出提醒）",
          scan["S1"]["status"] == "off" and "S7" in scan["S1"]["reason"], scan["S1"])

    print("\n== 2c. 卖出为空时，每条规则都要给出原因 ==")
    cap = capital(concepts=[board("PCB", "concept", 1.69, -60.2, 80.1, -27.3),
                            board("半导体", "concept", 3.00, 200.0, 90.0, 150.0)],
                  mkt_main_yi=1198.0, breadth=0.88)
    r = run("noout", cap, db0, [{"code": "A", "concepts": ["PCB"]},
                                {"code": "B", "concepts": ["半导体"]}])
    check("卖出侧确实为空", r["sell"] == [], r["sell"])
    scan = {x["rule"]: x for x in r["sell_scan"]}
    check("7 条全部有未触发原因", len(scan) == 7 and all(x["reason"] for x in scan.values()),
          {k: v["reason"] for k, v in scan.items()})
    check("S1/S7 原因点明「非机构兑现结构」",
          ("小单也净流出" in scan["S7"]["reason"] or "净流入" in scan["S7"]["reason"]),
          scan["S7"]["reason"])
    check("S1 与 S7 各自说明触发条件（不重复同一句）",
          scan["S1"]["reason"] != scan["S7"]["reason"]
          and "0.5%" in scan["S1"]["reason"] and "0.5%" in scan["S7"]["reason"],
          (scan["S1"]["reason"], scan["S7"]["reason"]))
    check("S2 原因给出超大单实际值", "亿" in scan["S2"]["reason"], scan["S2"]["reason"])
    check("S3 原因含还差几个交易日", "交易日" in scan["S3"]["reason"], scan["S3"]["reason"])
    check("S6 原因含市场实际数值", "%" in scan["S6"]["reason"], scan["S6"]["reason"])

    def _bd(ago):
        """相对今天的买入日：让持有天数与运行时刻无关，测试结果稳定。"""
        return (datetime.date.today() - datetime.timedelta(days=ago)).isoformat()

    print("\n== 2d. 卖出信号要落到具体持仓上，并带上调仓成本 ==")
    cap = capital(concepts=[board("算力概念", "concept", 0.92, -3.2, -1.0, 7.6)],
                  mkt_main_yi=186.0, breadth=0.92)
    fund = {"code": "016874", "name": "测试混合C", "amount": 1000.0,
            "buy_date": _bd(20), "concepts": ["算力概念"]}
    r = run("exit", cap, db0, [fund])
    s7 = next((x for x in r["sell"] if x["rule"] == "S7"), None)
    check("S7 命中", s7 is not None, r["sell"])
    check("信号带出受影响基金（否则用户不知道该动谁）",
          s7 and [c["code"] for c in s7.get("funds", [])] == ["016874"], s7 and s7.get("funds"))
    ex = (s7 or {}).get("exit") or {}
    check("exit 含持有天数/费率/赎回费/划算门槛",
          ex.get("days") is not None and ex.get("fee_rate") is not None
          and ex.get("fee_amount") is not None and ex.get("need_gain") is not None, ex)
    check("赎回费 = 金额 × 费率",
          ex.get("fee_amount") == round(1000.0 * ex.get("fee_rate", 0) / 100.0, 2), ex)
    check("划算门槛 = 赎回费率 + 空仓期波动成本",
          ex.get("need_gain") == round(ex.get("fee_rate", 0) + sg.fm.out_of_market_cost_pct(), 2), ex)
    check("持有 19 天落在 0.5% 档（C 类未满 30 天）", ex.get("fee_rate") == 0.5, ex)
    check("未踩 7 日红线时 warn=False", ex.get("warn") is False, ex)

    print("\n== 2e. 算不出持有天数时，费率整块不出现（缺失即不渲染）==")
    r = run("exit_none", cap, db0, [{"code": "X", "name": "无日期混合C", "concepts": ["算力概念"]}])
    s7 = next((x for x in r["sell"] if x["rule"] == "S7"), None)
    check("缺 buy_date → 不挂 funds/exit",
          s7 is not None and "exit" not in s7 and "funds" not in s7, s7)

    print("\n== 2f. 一个板块命中多只基金：反查表 + 成本取最保守 ==")
    cap = capital(concepts=[board("PCB", "concept", 1.2, -9.0, -2.0, 4.0),
                            board("半导体", "concept", 2.0, 40.0, 20.0, 10.0)],
                  mkt_main_yi=50.0, breadth=0.6)
    fs = [{"code": "A", "name": "甲混合C", "amount": 1000.0, "buy_date": _bd(2), "concepts": ["PCB"]},
          {"code": "B", "name": "乙混合C", "amount": 2000.0, "buy_date": _bd(100), "concepts": ["PCB"]}]
    bfm = sg.board_funds_map(fs, ["PCB", "半导体"])
    check("反查表是一对多", [x["code"] for x in bfm.get("PCB", [])] == ["A", "B"], bfm)
    check("未命中的板块不在表里", "半导体" not in bfm, bfm)
    r = run("exit_multi", cap, db0, fs)
    s = r["sell"][0]
    check("同板块两只基金都列出", len(s.get("funds", [])) == 2, s.get("funds"))
    check("取最短持有天数（最保守）", s["exit"]["days"] == min(c["days"] for c in s["funds"]),
          s["exit"])
    check("取最高费率", s["exit"]["fee_rate"] == max(c["fee_rate"] for c in s["funds"]), s["exit"])
    check("赎回费按各基金合计",
          abs(s["exit"]["fee_amount"] - sum(c["fee_amount"] for c in s["funds"])) < 0.02, s["exit"])
    check("只要有一只未满 7 日就给出红线警告", s["exit"]["warn"] is True, s["exit"])
    check("买入信号不带费率字段（不越界）",
          all("exit" not in x for x in r["buy"]), r["buy"])

    print("\n== 2g. 买入候选命中已持有主题要标出来（与结论层「加仓」同口径）==")
    cap = capital(concepts=[board("半导体", "concept", 2.0, 20.0, 12.0, -5.0),
                            board("创新药", "concept", 1.8, 15.0, 9.0, -3.0)],
                  mkt_main_yi=200.0, breadth=0.8)
    fs = [{"code": "017811", "name": "东方人工智能主题混合C", "amount": 900.0,
           "buy_date": _bd(20), "concepts": ["半导体"]}]
    r = run("held_buy", cap, db_t, fs)
    b_semi = next((x for x in r["buy"] if x["board"] == "半导体"), None)
    check("已持有主题的候选被标注", b_semi is not None and b_semi.get("held") is True, b_semi)
    check("并说清是哪只持仓覆盖了它",
          b_semi is not None and [f["code"] for f in b_semi.get("held_funds", [])] == ["017811"],
          b_semi)
    b_inn = next((x for x in r["buy"] if x["board"] == "创新药"), None)
    check("未持有的主题不误标", b_inn is not None and b_inn.get("held") is False, b_inn)
    check("未持有时不挂 held_funds（缺失即不渲染）",
          b_inn is not None and "held_funds" not in b_inn, b_inn)
    # 两张卡片不能自相矛盾：结论说「加仓」，信号层同一条板块就必须带 held
    check("此场景结论为「加仓」（候选=已持有主题）",
          r["verdict"].get("action") == "加仓", r["verdict"])
    check("结论层加仓与信号层 held 同口径",
          all(x.get("held") for x in (r["verdict"].get("buy_actions") or [])),
          r["verdict"].get("buy_actions"))
    check("标注后买入信号仍不带费率字段（不越界）",
          all("exit" not in x for x in r["buy"]), r["buy"])
    r2 = run("held_none", cap, db_t, [])
    check("一个持仓都没有时不标注 held（不猜）",
          bool(r2["buy"]) and all("held" not in x for x in r2["buy"]), r2["buy"])

    print("\n== 2h. 板块快照为空时不得甩锅给 funds.json ==")
    # 盘后/接口没更新 → 板块快照本身是空的（industries/concepts 都是 []），
    # 这时赖 funds.json 会把人骗去改配置。线上就是这么发生的。
    empty_cap = capital(concepts=[], industries=[])
    r = run("empty_snap", empty_cap, db0, [fund])
    check("空快照不抛异常", r.get("ok") is True, r.get("error"))
    rows = r["sell_scan"]
    # 走"没有可用板块"分支时会先补 7 条，再追加一条真实的 S6 行 → 共 8 行
    check("7 条规则照样给原因", len(rows) >= 7 and all(x["reason"] for x in rows), rows)
    check("原因指向「快照为空」",
          len([x for x in rows if "快照为空" in x["reason"]]) >= 7, rows)
    check("不再误导用户去改 funds.json",
          not any("funds.json" in x["reason"] for x in rows), rows)
    pend = [p for p in r["pending"] if p["rule"] == "S*"]
    check("pending 同样说明是快照为空（两处同口径）",
          bool(pend) and "快照为空" in pend[0]["why"], (pend, r.get("held_boards")))
    # 有板块、但持仓真的没配 concepts → 这才是配置问题，message 要指回 funds.json
    r = run("no_concepts", cap, db0, [{"code": "Z", "name": "无概念混合C", "amount": 100.0}])
    rows = r["sell_scan"]
    check("持仓确实没配 concepts 时才指向 funds.json",
          len([x for x in rows if "funds.json" in x["reason"]]) >= 7, rows)

    print("\n== 3. 多日：连续净流出 S3 / 流入转流出 S4 / 高位滞涨 S5 ==")
    db1 = os.path.join(tmp, "t1.db")
    seed(db1, "半导体", "concept", [(3, 1.0, -6e8 / 1e8), (2, -0.5, -40.0), (1, -0.8, -30.0)])
    seed(db1, "创新药", "concept", [(1, 2.6, 50.0)])
    seed(db1, "CPO概念", "concept", [(1, 3.2, 20.0)])
    cap = capital(concepts=[board("半导体", "concept", -1.0, -20.0, -8.0, 6.0),
                            board("创新药", "concept", -0.2, -12.0, -3.0, 4.0),
                            board("CPO概念", "concept", 0.1, -9.0, -2.0, 3.0)],
                  mkt_main_yi=50.0, breadth=0.5)
    funds = [{"code": "A", "concepts": ["半导体"]}, {"code": "B", "concepts": ["创新药"]},
             {"code": "C", "concepts": ["CPO概念"]}]
    r = run("multi_sell", cap, db1, funds)
    ids = sorted(x["rule"] for x in r["sell"])
    check("历史天数已计入", r["days"] >= 3, r["days"])
    check("S3 连续3日净流出命中", "S3" in ids, ids)
    check("S4 流入转流出命中", "S4" in ids, ids)
    check("S5 高位滞涨命中", "S5" in ids, ids)
    check("pending 已清空多日项", not any(p["rule"] == "B1/B2/S3" for p in r["pending"]), r["pending"])

    dbx = os.path.join(tmp, "tx.db")
    seed(dbx, "半导体", "concept", [(0, 1.0, 20.0), (2, 1.0, 25.0), (1, 1.0, 30.0)])  # 含今天一条
    connx = hdb.connect(dbx)
    xd = hdb.board_flow_days(connx, True, exclude_date=sg._snapshot_date())
    check("days 排除当日：3 条落库只算 2 天历史", xd == 2, xd)
    connx.close()

    print("\n== 4. 多日趋势：B1 趋势主线 / B2 趋势回调 / B3 流出转流入 ==")
    db2 = os.path.join(tmp, "t2.db")
    # 趋势向上：3 天历史全为净流入、区间累计收涨
    seed(db2, "半导体", "concept", [(3, 1.2, 30.0), (2, 0.8, 25.0), (1, 1.5, 40.0)])
    seed(db2, "机器人", "concept", [(1, -2.0, -30.0)])
    cap = capital(concepts=[board("半导体", "concept", 2.2, 35.0, 20.0, -6.0),
                            board("机器人", "concept", 1.8, 22.0, 12.0, -4.0)],
                  mkt_main_yi=300.0, breadth=0.7)
    r = run("multi_buy", cap, db2, [{"code": "A", "concepts": ["半导体"]}])
    bids = sorted(x["rule"] for x in r["buy"])
    check("B1 多日趋势主线命中", "B1" in bids, bids)
    check("当日涨 2.2%（未追高）给 B1 而非 B2", "B2" not in bids, bids)
    check("B3 流出转大幅流入命中", "B3" in bids, bids)
    check("买入按强度降序", r["buy"] == sorted(r["buy"], key=lambda x: -(x.get("strength") or 0)))
    check("B1 证据写的是区间而非单日",
          all("天为正" in x["evidence"] and "区间" in x["evidence"]
              for x in r["buy"] if x["rule"] == "B1"), r["buy"])
    check("多日信号带出区间口径字段（win_days/win_pct）",
          all(x.get("win_days") and x.get("win_pct") is not None
              for x in r["buy"] if x["rule"] in ("B1", "B2", "B3")), r["buy"])

    # 趋势中的回调 = T+1 最佳买点：同一段历史，当日由涨转跌
    cap_pb = capital(concepts=[board("半导体", "concept", -0.8, 12.0, 8.0, -6.0)],
                     mkt_main_yi=300.0, breadth=0.7)
    r_pb = run("pullback", cap_pb, db2, [])
    b2 = [x for x in r_pb["buy"] if x["rule"] == "B2"]
    check("趋势中回调命中 B2（T+1 最佳买点）", len(b2) == 1 and b2[0]["board"] == "半导体", r_pb["buy"])
    check("回调日不要求当日上涨（修正「当天涨才买」）", bool(b2) and b2[0]["pct"] < 0, b2)
    check("回调日证据点明按当日收盘确认",
          bool(b2) and "收盘" in b2[0]["evidence"], b2)
    # 当日大涨则降级为「等回踩」，不给买入结论
    cap_ch = capital(concepts=[board("半导体", "concept", 3.2, 45.0, 25.0, -6.0)],
                     mkt_main_yi=300.0, breadth=0.7)
    r_ch = run("chase", cap_ch, db2, [])
    check("当日大涨 → 结论等回踩而非可买",
          r_ch["verdict"]["action"] == "等回踩", r_ch["verdict"])

    print("\n== 4b. 同一板块不得既报「多日确认风险」又当买入候选 ==")
    # 线上真实出现过的自相矛盾：光纤概念一边被判「资金由流入转流出（S4，已多日确认）」，
    # 一边又被列为「趋势中回调（T+1 最佳买点）」，两张卡打架，用户无从下手。
    db4 = os.path.join(tmp, "t4.db")
    seed(db4, "光纤概念", "concept", [(3, 1.0, 12.0), (2, 0.6, 10.0), (1, 1.32, 14.0)])
    cap = capital(concepts=[board("光纤概念", "concept", -0.2, -12.0, -5.0, 3.5)],
                  mkt_main_yi=100.0, breadth=0.6)
    r = run("conflict", cap, db4, [{"code": "016874", "concepts": ["光纤概念"]}])
    confirmed = [s["board"] for s in r["sell"] if s.get("confirmed")]
    check("该板块确实命中多日确认的卖出信号", "光纤概念" in confirmed, r["sell"])
    # 未过滤前它确实是买入候选，冲突才真实存在（否则这条测试是空转）
    conn4 = hdb.connect(db4)
    raw = sg.buy_signals(sg._board_index(cap), conn4, {})
    conn4.close()
    check("未过滤前它确实是买入候选（证明冲突真实存在）",
          "光纤概念" in [x["board"] for x in raw], raw)
    check("但已被剔除出买入候选（两张卡不得同时说该买和该卖）",
          "光纤概念" not in [x["board"] for x in r["buy"]], r["buy"])
    check("结论也不会说「加仓」（不能一边确认风险一边加仓）",
          r["verdict"]["action"] != "加仓", r["verdict"])

    print("\n== 5. 噪音控制：元概念不进买入候选 ==")
    cap = capital(concepts=[board("融资融券", "concept", 1.7, 380.0, 530.0, 320.0),
                            board("MSCI中国", "concept", 1.5, 280.0, 310.0, 220.0),
                            board("国产芯片", "concept", 2.9, 230.0, 125.0, 180.0)],
                  mkt_main_yi=1000.0, breadth=0.9)
    r = run("meta", cap, db_t, [])
    names = [x["board"] for x in r["buy"]]
    check("融资融券被剔除", "融资融券" not in names, names)
    check("MSCI中国被剔除", "MSCI中国" not in names, names)
    check("真实题材保留", "国产芯片" in names, names)

    print("\n== 6. 存量博弈降权（用已落库的指数量能）==")
    db3 = os.path.join(tmp, "t3.db")
    # 必须给趋势窗口铺历史，否则买入候选恒为空、比不出降权效果
    seed(db3, "国产芯片", "concept", [(3, 1.0, 49.5), (2, 0.9, 55.0), (1, 1.1, 63.25)])
    conn = hdb.connect(db3)
    today = datetime.date.today()
    vols = [4e10] * 5 + [1e11] * 15        # 插入顺序 i=0 是最新一天 → 近 5 日缩量
    for i, v in enumerate(vols):
        d = (today - datetime.timedelta(days=i + 1)).strftime("%Y-%m-%d")
        conn.execute("INSERT OR REPLACE INTO bar_daily(code,date,close,volume,adjust)"
                     " VALUES('sh000300',?,?,?,'')", (d, 4000 + i, v))
    conn.commit()
    conn.close()
    cap = capital(concepts=[board("国产芯片", "concept", 2.9, 230.0, 125.0, 180.0)],
                  mkt_main_yi=1000.0, breadth=0.9)
    sg.load_funds = lambda: []
    r_norm = sg.compute(cap, db=db_t)
    r_shrink = sg.compute(cap, db=db3)
    check("识别为存量", r_shrink["market"]["volume_state"] == "存量", r_shrink["market"])
    check("正常口径无量能判定", r_norm["market"]["volume_state"] is None, r_norm["market"])
    s_n = next((x["strength"] for x in r_norm["buy"] if x["rule"] == "B1"), None)
    s_s = next((x["strength"] for x in r_shrink["buy"] if x["rule"] == "B1"), None)
    check("存量下强度被降权", s_n is not None and s_s is not None and s_s < s_n, (s_n, s_s))
    check("降权证据写进文案", "存量" in next(x["evidence"] for x in r_shrink["buy"] if x["rule"] == "B1"))

    print("\n== 7. 健壮性：异常输入不得抛出 ==")
    real_df = sg.df
    try:
        sg.df = None                     # 断掉取数兜底，否则这里会真去联网拿快照
        r = sg.compute(None, db=db0)
        check("快照缺失返回 ok=False 而非抛异常", r["ok"] is False and "error" in r, r)
    finally:
        sg.df = real_df
    r = sg.compute(capital(concepts=None, industries=None), db=os.path.join(tmp, "none.db"))
    check("空板块不崩", r["ok"] is True and r["sell"] == [] and r["buy"] == [])
    r = sg.compute(capital(concepts=[{"name": "X"}]), db=db0)
    check("字段缺失不崩", r["ok"] is True)
    r = sg.compute(capital(concepts=[board("半导体", "concept", 3.0, 20.0, 12.0, -5.0)],
                           mkt_main_yi=200.0), db=db0)
    check("持仓无概念时给出 pending 说明", any("S*" == p["rule"] for p in r["pending"]), r["pending"])
    ind = [{"name": "半导体", "pct": 2.0, "main_net": 2e9, "inst_net": 1e9,
            "big_net": 1e9, "mid_net": 0.0, "small_net": 5e8, "total_net": 2.5e9}]
    snap = sg._snapshot_from(ind, [])
    check("_snapshot_from 汇总净额与广度", abs(snap["market_main_net"] - 2e9) < 1 and snap["breadth"] == 1.0, snap)
    keep = sg.df
    try:
        class Stub(object):
            @staticmethod
            def get_cached_capital(max_age=0):
                return (None, None)      # 真实返回形状：无数据时是二元组，本身为真值

            @staticmethod
            def build_capital_snapshot():
                raise RuntimeError("上游挂了")

            @staticmethod
            def is_meta_concept(n):
                return False
        sg.df = Stub()
        r = sg.compute(None, db=db0)
        check("(None,None) 不被误当成有数据", r["ok"] is False, r)
    finally:
        sg.df = keep
    try:
        json.dumps(r, ensure_ascii=False)
        check("输出可 JSON 序列化", True)
    except Exception as e:
        check("输出可 JSON 序列化", False, e)
    check("规则说明与提示同源输出", len(r["rules"]) >= 8 and len(r["notes"]) >= 3)
    check("做不了的规则显式声明", len(r["unsupported"]) >= 2)

    print("\n== 8. 交易日口径：盘前/周末不得写错日期 ==")
    cal_ok = False
    try:
        c0 = hdb.connect()
        cal_ok = hdb.last_trade_date(c0, "2026-09-19") is not None
        c0.close()
    except Exception:
        cal_ok = False
    if cal_ok:
        DT = datetime.datetime
        fri_pre = DT(2026, 9, 18, 8, 0)        # 周五盘前：接口返回的仍是周四收盘值
        fri_mid = DT(2026, 9, 18, 10, 0)       # 周五盘中
        fri_close = DT(2026, 9, 18, 15, 30)    # 周五收盘后
        sat = DT(2026, 9, 19, 10, 0)           # 周六
        check("周六映射到最近交易日、不落自然日",
              sg._trade_date(datetime.date(2026, 9, 19)) < "2026-09-19",
              sg._trade_date(datetime.date(2026, 9, 19)))
        check("盘前快照归属上一交易日（不得记成今天）",
              sg._snapshot_date(fri_pre) == sg._trade_date(datetime.date(2026, 9, 17))
              and sg._snapshot_date(fri_pre) != "2026-09-18",
              sg._snapshot_date(fri_pre))
        check("盘中快照归属当日", sg._snapshot_date(fri_mid) == "2026-09-18",
              sg._snapshot_date(fri_mid))
        check("周末快照归属上一交易日", sg._snapshot_date(sat) == "2026-09-18",
              sg._snapshot_date(sat))
        check("盘前：归属日已是过去 → 可定型", sg._is_settled_now(fri_pre) is True,
              sg._is_settled_now(fri_pre))
        check("盘中：未收盘不定型", sg._is_settled_now(fri_mid) is False,
              sg._is_settled_now(fri_mid))
        check("收盘后：已定型", sg._is_settled_now(fri_close) is True,
              sg._is_settled_now(fri_close))
        check("周末访问：上一交易日可定型", sg._is_settled_now(sat) is True,
              sg._is_settled_now(sat))
    else:
        print("  [SKIP] 交易日历未灌，跳过盘前/周末口径用例")

    dbt = os.path.join(tmp, "trade.db")
    real_snap = sg._snapshot_date
    real_settled = sg._is_settled_now
    try:
        sg._snapshot_date = lambda *a, **k: "2026-09-18"   # 假装"快照归属交易日=周五"
        sg._is_settled_now = lambda *a, **k: True
        sg.record_daily_flow(capital(concepts=[board("半导体", "concept", 1.0, 20.0, 12.0, -5.0)]),
                             db=dbt)
    finally:
        sg._snapshot_date = real_snap
        sg._is_settled_now = real_settled
    c1 = hdb.connect(dbt)
    rows = [dict(x) for x in c1.execute("SELECT DISTINCT date, settled FROM board_flow_daily ORDER BY date")]
    c1.close()
    check("落盘日期=归档交易日(2026-09-18)而非自然日",
          [r["date"] for r in rows] == ["2026-09-18"], rows)
    check("定型写入 settled=1", bool(rows) and rows[0]["settled"] == 1, rows)

    print("\n== 9. 夜间落库入口 record_live_flow ==")
    db_live = os.path.join(tmp, "live.db")
    keep = sg.df
    try:
        class Dead(object):
            @staticmethod
            def get_cached_capital(max_age=0):
                return (None, None)

            @staticmethod
            def build_capital_snapshot():
                raise RuntimeError("上游挂了")
        sg.df = Dead()
        check("取数失败返回 (0,0) 且不抛异常", sg.record_live_flow(db=db_live) == (0, 0))
    finally:
        sg.df = keep
    real_live = sg._live_capital
    try:
        # 必须用 lambda *a, **k：_live_capital 现在带 db 参数，无参 lambda 会被
        # record_live_flow 的 except 吞成 "取数失败"，表现为"什么都没写"（踩过）
        sg._live_capital = lambda *a, **k: capital(concepts=[board("半导体", "concept", 1.0, 20.0, 12.0, -5.0)])
        a, b = sg.record_live_flow(db=db_live)
        check("正常路径落盘概念板块 1 条", b == 1 and a == 0, (a, b))
        conn = hdb.connect(db_live)
        n = conn.execute("SELECT COUNT(*) n FROM board_flow_daily").fetchone()["n"]
        conn.close()
        check("夜间入口写库可见", n >= 1, n)
    finally:
        sg._live_capital = real_live

    print("\n== 10. 持有天数口径：从确认日起算，三处口径不得打架 ==")
    fm = sg.fm
    if fm is None:
        print("  [SKIP] fund_manager 未加载")
    else:
        # 1) 语义：必须扣掉确认日滞后（股混/债/ETF联接 T+1，QDII/FOF T+2）
        d0 = datetime.date(2026, 6, 1)
        for rawd in (0, 1, 6, 7, 8, 29, 30, 31):
            f = {"code": "T", "name": "测试混合C", "amount": 1000.0,
                 "buy_date": (d0 - datetime.timedelta(days=rawd)).isoformat()}
            hd = fm.holding_days(f, d0)
            check("自然日 %d → 持有 %d 天（扣 T+1）" % (rawd, max(0, rawd - 1)),
                  hd == max(0, rawd - 1), hd)
        q = {"code": "Q", "name": "某某QDII", "amount": 1000.0,
             "buy_date": (d0 - datetime.timedelta(days=10)).isoformat()}
        check("QDII 扣 2 天确认日", fm.holding_days(q, d0) == 8, fm.holding_days(q, d0))

        # 2) 一致性：switch_cost(提示/建议用) 必须与 _fee_rate(真实赎回用) 同档。
        #    原来 switch_cost 走的 holding_days 没扣确认日，会把恰好卡在 7 日红线上
        #    的基金报成 0.5% 而非 1.5%——3 倍费率差，正是"频繁调仓=负期望"的判断基础。
        today = datetime.date.today()
        bad = []
        for rawd in (0, 5, 6, 7, 8, 28, 29, 30, 31, 100):
            f = {"code": "T", "name": "测试混合C", "amount": 1000.0,
                 "buy_date": (today - datetime.timedelta(days=rawd)).isoformat()}
            a = fm.redemption_fee_rate(fm.holding_days(f), f["name"])
            b = fm._fee_rate(f["buy_date"], f)
            if abs((a or 0.0) - (b or 0.0)) > 1e-9:
                bad.append((rawd, a, b))
        check("switch_cost 与 redeem 费率口径一致（含 7/30 日红线）", not bad, bad)
        fh = {"code": "T", "name": "测试混合C", "amount": 1000.0,
              "buy_date": (today - datetime.timedelta(days=300)).isoformat()}
        fha = {"code": "T", "name": "测试混合A", "amount": 1000.0,
               "buy_date": (today - datetime.timedelta(days=300)).isoformat()}
        check("同日买入：C 类已免赎、A 类仍需 0.5%",
              fm.switch_cost(fh)["fee_rate"] == 0.0 and fm.switch_cost(fha)["fee_rate"] == 0.5,
              (fm.switch_cost(fh), fm.switch_cost(fha)))
        check("缺 buy_date 时持有天数返回 None（不猜）",
              fm.holding_days({"code": "T", "name": "无日期C"}) is None)

    print("\n== 11. 结论：该买还是该卖（verdict 阶梯）==")
    M_OK = {"breadth": 0.8, "main_net_yi": 200.0, "volume_state": "放量"}
    M_WEAK = {"breadth": 0.2, "main_net_yi": -300.0}
    M_DIL = {"breadth": 0.8, "main_net_yi": 200.0, "volume_state": "存量"}
    B_STRONG = [{"rule": "B1", "board": "半导体", "kind": "concept", "pct": 1.2,
                 "strength": 9.2, "main": 40.0}]
    B_WEAK = [{"rule": "B1", "board": "半导体", "kind": "concept", "pct": 1.2,
               "strength": 3.0, "main": 20.0}]

    def sell_item(rule, board, sev, rate, warn, days=15, amount=1000.0, confirmed=True):
        ctx = {"code": "F1", "name": "测试混合C", "amount": amount, "days": days,
               "fee_rate": rate, "fee_amount": round(amount * rate / 100.0, 2),
               "need_gain": rate + 2.6, "warn": warn}
        return {"rule": rule, "side": "sell", "board": board, "sev": sev,
                "title": "测试", "funds": [ctx], "confirmed": confirmed,
                "exit": {"n": 1, "days": days, "fee_rate": rate,
                         "fee_amount": ctx["fee_amount"], "need_gain": ctx["need_gain"],
                         "warn": warn}}

    v = sg.verdict([], B_STRONG, M_OK, 1, 3, ["PCB"])
    check("无风险+强候选(非持仓) → 可买", v["action"] == "可买", v)
    check("可买落在 buy 侧配色", v["tone"] == "buy", v["tone"])
    check("可买给到场外标的或如实说明没有", bool(v["reasons"]) and any(
        ("场外标的" in r or "暂未核验" in r) for r in v["reasons"]), v["reasons"])

    v = sg.verdict([], B_STRONG, M_OK, 1, 3, ["半导体"])
    check("候选正是已持有主题 → 加仓", v["action"] == "加仓", v)
    check("加仓不建议新开第二只同名基金", any("不新开" in r for r in v["reasons"]), v["reasons"])

    v = sg.verdict([], [dict(B_STRONG[0], pct=3.6)], M_OK, 1, 3, ["PCB"])
    check("当日已涨超阈值 → 等回踩（不追高）", v["action"] == "等回踩", v)

    v = sg.verdict([], B_STRONG, M_DIL, 1, 3, ["PCB"])
    check("存量博弈 → 不出买入结论", v["action"] == "持有", v)
    v = sg.verdict([], B_STRONG, M_WEAK, 1, 3, ["PCB"])
    check("市场普跌 → 不出买入结论", v["action"] == "持有", v)
    v = sg.verdict([], B_WEAK, M_OK, 1, 3, ["PCB"])
    check("候选强度不足 → 持有", v["action"] == "持有", v)

    v = sg.verdict([sell_item("S4", "半导体", "warn", 0.5, False)],
                   [], M_OK, 1, 3, ["半导体"])
    check("单条 warn 未达阈值 → 持有", v["action"] == "持有", v)
    check("多日确认条数如实上报", v["confirmed_n"] == 1, v)

    # ---- T+1 核心口径：单日风险不得驱动减仓（"当天跌就让我减仓"的修正点）----
    v = sg.verdict([sell_item("S1", "半导体", "danger", 0.5, False, confirmed=False)],
                   [], M_OK, 1, 3, ["半导体"])
    check("单日 danger 但未多日确认 → 持有（不动手）", v["action"] == "持有", v)
    check("理由点明是单日、并解释为什么不动手",
          any("单日" in r for r in v["reasons"]), v["reasons"])
    check("不把单日风险算进多日确认计数", v["confirmed_n"] == 0, v)
    # 同样的单日风险，被多日确认后即可减仓
    v = sg.verdict([sell_item("S1", "半导体", "danger", 0.5, False, confirmed=True)],
                   [], M_OK, 1, 3, ["半导体"])
    check("同类信号一旦被多日确认 → 减仓", v["action"] == "减仓", v)

    # ---- 买入侧：只有单日/反转初段不得给买入结论 ----
    B_SINGLE = [{"rule": "B3", "board": "半导体", "kind": "concept", "pct": 2.0,
                 "strength": 9.0, "main": 40.0, "win_days": 3, "win_pct": -1.0}]
    v = sg.verdict([], B_SINGLE, M_OK, 2, 3, ["半导体"])
    check("反转初段(B3)不给买入结论", v["action"] == "持有", v)
    check("并说明趋势尚未确立", "尚未确立" in v["headline"], v["headline"])
    check("T+1 交易口径恒定下发", "T+1" in (v.get("note") or ""), v.get("note"))

    # 追高候选不该盖过更好的回调买点（结论层跳过追高去选可动手的）
    B_MIX = [{"rule": "B1", "board": "追高板块", "kind": "concept", "pct": 4.0,
              "strength": 9.9, "main": 50.0, "win_days": 4, "win_pct": 8.0},
             {"rule": "B2", "board": "回调板块", "kind": "concept", "pct": -0.6,
              "strength": 7.0, "main": 30.0, "win_days": 4, "win_pct": 4.0}]
    v = sg.verdict([], B_MIX, M_OK, 2, 3, ["回调板块"])
    check("结论层跳过追高候选、选当日回调的那个", (v["buy_actions"] or [{}])[0].get("board") == "回调板块",
          v["buy_actions"])
    check("它正是已持有主题 → 加仓（不被追高候选带偏）", v["action"] == "加仓", v)

    v = sg.verdict([sell_item("S4", "半导体", "warn", 0.5, False),
                    sell_item("S5", "PCB", "warn", 0.5, False)], [], M_OK, 1, 3, ["半导体"])
    check("风险提醒达 verdict_sell_n → 减仓", v["action"] == "减仓", v)
    check("减仓落到具体持仓", [x["code"] for x in v["hold_actions"]] == ["F1"], v["hold_actions"])
    check("减仓理由是分批而非清仓", any("分批" in r for r in v["reasons"]), v["reasons"])
    v = sg.verdict([sell_item("S1", "半导体", "danger", 0.5, False)], [], M_OK, 1, 3, ["半导体"])
    check("danger 级单条即触发减仓", v["action"] == "减仓", v)

    # 费率红线优先于信号：这正是「小资金频繁调仓=负期望」的落点
    v = sg.verdict([sell_item("S1", "半导体", "danger", 1.5, True, days=5)], [], M_OK, 1, 3, ["半导体"])
    check("未满 7 日红线 → 暂不动手（费率压过信号）", v["action"] == "暂不动手", v)
    check("并说清等多久", any("满 7 日" in r for r in v["reasons"]), v["reasons"])
    v = sg.verdict([sell_item("S1", "半导体", "danger", 0.75, False)], [], M_OK, 1, 3, ["半导体"])
    check("费率高于可接受上限 → 暂不动手", v["action"] == "暂不动手", v)

    v = sg.verdict([sell_item("S1", "半导体", "danger", 1.5, True),
                    sell_item("S5", "PCB", "warn", 0.5, False)], [], M_OK, 1, 3, ["半导体"])
    check("一 fund 踩线即整体保守", v["action"] == "暂不动手", v)

    check("同板块多基金不重复罗列原因",
          len([r for r in sg.verdict(
              [sell_item("S5", "CPO概念", "warn", 0.5, False),
               dict(sell_item("S5", "CPO概念", "warn", 0.5, False),
                    funds=[{"code": "F2", "name": "乙C", "amount": 500.0, "days": 15,
                            "fee_rate": 0.5, "fee_amount": 2.5, "need_gain": 3.1, "warn": False}])],
              [], M_OK, 1, 3, ["CPO概念"])["reasons"] if "CPO概念" in r]) == 1)

    check("空输入不抛异常且给「持有」",
          sg.verdict(None, None, None)["action"] == "持有"
          and sg.verdict([], [], {})["action"] == "持有")
    check("compute 输出恒含 verdict 字段（前端缺失即不渲染）",
          "verdict" in sg.compute(capital(concepts=[board("半导体", "concept", 3.0, 20.0, 12.0, -5.0)]),
                                  db=db0))
    check("结论阶梯随接口下发", len(sg.VERDICT_LADDER) >= 5)
    check("阶梯文案没有 %% 转义残留",
          not any("%%" in a or "%%" in d for a, d in sg.VERDICT_LADDER))
    check("待补数据只提示不阻断结论",
          sg.verdict([], B_STRONG, M_OK, 0, 3, ["PCB"])["pending_days"] == 2)
    if sg.ac is not None:
        check("场外标的取自「决策」页同源库",
              (sg._overseas_vehicle("半导体") or {}).get("code") == "007301",
              sg._overseas_vehicle("半导体"))
        check("库里没核验到的行业如实返回 None（不硬凑）",
              sg._overseas_vehicle("创新药") is None, sg._overseas_vehicle("创新药"))

    print("\n== 11. 降级数据源：只展示、不出建议、绝不回写 ==")
    # 实时板块拿不到时会回退到库里最近收盘格局。数据的"新鲜度"按快照归属日判定：
    #   降级日 == 当前快照归属日（盘前/盘后/周末）→ 它就是当前快照本身，可正常出信号；
    #   降级日 != 归属日（盘中实时源断了）→ 陈旧，只展示、不出买卖结论。
    cur = sg._snapshot_date()
    stale_cap = capital(concepts=[board("半导体", "concept", 9.9, 30.0, 25.0, -5.0)])
    stale_cap["fallback"] = {"kind": "history", "as_of": "2026-09-18"}
    r_stale = sg.compute(stale_cap, db=db0)
    check("陈旧数据 stale=True 且如实带来源日期",
          r_stale.get("stale") is True and r_stale.get("data_date") == "2026-09-18",
          (r_stale.get("stale"), r_stale.get("data_date"), r_stale.get("data_src")))
    check("陈旧数据不得产出买入信号", r_stale.get("buy") == [], r_stale.get("buy"))
    check("陈旧数据不得产出卖出/体检信号（避免「没数据」被读成「没风险」）",
          r_stale.get("sell") == [] and r_stale.get("sell_scan") == [],
          (r_stale.get("sell"), r_stale.get("sell_scan")))
    check("陈旧数据结论必须是「观望」",
          (r_stale.get("verdict") or {}).get("action") == "观望", r_stale.get("verdict"))
    check("陈旧原因写进待补卡（页面要看得见为什么停用）",
          any("实时数据不可用" in (p.get("title") or "") for p in (r_stale.get("pending") or [])),
          r_stale.get("pending"))

    same_cap = capital(concepts=[board("半导体", "concept", 9.9, 30.0, 25.0, -5.0)])
    same_cap["fallback"] = {"kind": "history", "as_of": cur}
    r_same = sg.compute(same_cap, db=db0)
    check("降级日==快照归属日 → 不算陈旧（盘前/盘后读到的就是当前快照）",
          r_same.get("stale") is False, (r_same.get("stale"), cur))
    check("来源日期仍如实标注为历史日", r_same.get("data_date") == cur, r_same.get("data_date"))
    check("非陈旧时信号引擎照常工作", (r_same.get("verdict") or {}).get("action") != "观望",
          r_same.get("verdict"))

    # 降级数据绝不能回写：它带着上一交易日的数字，写进去会把那天记到今天名下，
    # 而 settled=1 一旦写死就是永久脏数据（同「禁用自然日归档」那类事故）。
    db_fb = os.path.join(tmp, "fallback.db")
    fb_cap = capital(concepts=[board("半导体", "concept", 1.0, 20.0, 12.0, -5.0)])
    fb_cap["fallback"] = {"kind": "history", "as_of": "2026-09-18"}
    check("降级数据不回写（返回 0,0）", sg.record_daily_flow(fb_cap, db=db_fb) == (0, 0))
    c2 = hdb.connect(db_fb)
    n2 = c2.execute("SELECT COUNT(*) n FROM board_flow_daily").fetchone()["n"]
    c2.close()
    check("库里确实一条都没写", n2 == 0, n2)

    # 字段恒存在纪律：前端 renderTips 按 t.stale / t.data_date 渲染，缺了会静默少一块
    for k in ("data_src", "data_date", "stale"):
        check("compute 恒含字段 %s" % k, k in r_stale and k in r_same)
    r_bad = sg.compute({}, db=db0)
    check("快照不可用分支也带来源字段",
          r_bad.get("ok") is False
          and all(k in r_bad for k in ("data_src", "data_date", "stale")), r_bad.get("data_src"))

    print("\n结果: %d 通过 / %d 失败" % (_ok, _bad))
    return 1 if _bad else 0


if __name__ == "__main__":
    sys.exit(main())
