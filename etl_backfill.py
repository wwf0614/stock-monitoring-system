# -*- coding: utf-8 -*-
"""
etl_backfill.py —— 历史数据落库 ETL（盘后 / 夜间执行）
===================================================
把 akshare_provider 取到的数据固化进 history.db，为回测/因子/风险模型提供地基。

用法：
  python etl_backfill.py                      # 默认全量：日历+指数+基金净值+指数成分+板块
  python etl_backfill.py --stocks 600519,300750 --since 2024-01-01
  python etl_backfill.py --spot               # 追加全市场横截面（~30s，勿在盘中跑）
  python etl_backfill.py --only calendar,index,fund
  python etl_backfill.py --only flow          # 只补板块资金（不依赖 akshare，可单跑）
  python etl_backfill.py --summary            # 只看库里有什么

设计要点：
  - 每一步独立 try：一个源挂了不影响其余步骤入库（东财限流是常态）。
  - 全部 upsert 幂等：同一天重复跑不会产生重复行，可安全重跑/补数据。
    flow 步骤按【最近交易日】归档（signals 决定），节假日/周末重复跑同样幂等。
  - 横截面(--spot)与财务(--financial)默认关闭：前者 30s、后者按股票数线性放大，
    都属于"重"操作，只应在盘后跑，因此不做成默认路径。
  - 依赖分层：akshare 门禁按步骤判定。flow 与 summary 只用东财免费接口，
    缺 akshare 也能跑 —— 夜间补板块资金不该被第三方库挡在门外。
"""
import os
import sys
import json
import time
import argparse
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import akshare_provider as akp
import history_db as hdb

try:
    import json_util
except Exception:
    json_util = None

BASE = os.path.dirname(os.path.abspath(__file__))
FUNDS_PATH = os.path.join(BASE, "funds.json")

CORE_INDEX = {
    "sh000001": "上证指数", "sh000300": "沪深300", "sz399001": "深证成指",
    "sz399006": "创业板指", "sh000688": "科创50",
}
CORE_INDEX_WEIGHT = ["000300", "000905", "000852"]


def _today():
    return datetime.date.today().strftime("%Y-%m-%d")


def _load_funds():
    if json_util:
        d = json_util.safe_load_json(FUNDS_PATH, default={})
    else:
        try:
            with open(FUNDS_PATH, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
    return [x.get("code") for x in (d.get("funds") or []) if x.get("code")]


def _log(msg, ok=None):
    tag = "" if ok is None else ("  [OK]  " if ok else "  [SKIP]")
    print("%s%s" % (tag, msg), flush=True)


def step_calendar(conn):
    cal = akp.fetch_trade_calendar()
    if not cal:
        _log("交易日历取数失败", False)
        return 0
    n = hdb.upsert_calendar(conn, cal)
    _log("交易日历 %d 天（最新 %s）" % (n, cal[-1]), True)
    return n


def step_index(conn):
    total = 0
    for sina, name in CORE_INDEX.items():
        rows = akp.fetch_index_daily(sina)
        if not rows:
            _log("指数 %s %s 取数失败" % (sina, name), False)
            continue
        n = hdb.upsert_bars(conn, sina, rows, adjust="", kind="index")
        total += n
        _log("指数 %-8s %-6s %d 根  %s ~ %s" % (sina, name, n, rows[0].get("date"), rows[-1].get("date")), True)
    return total


def step_fund(conn):
    codes = _load_funds()
    if not codes:
        _log("funds.json 无持仓，跳过净值", False)
        return 0
    total = 0
    for c in codes:
        rows = akp.fetch_fund_nav(c)
        if not rows:
            _log("基金 %s 净值取数失败" % c, False)
            continue
        n = hdb.upsert_fund_nav(conn, c, rows)
        total += n
        _log("基金 %s %d 条净值  %s ~ %s" % (c, n, rows[0].get("净值日期"), rows[-1].get("净值日期")), True)
    return total


def step_weight(conn):
    total = 0
    for idx in CORE_INDEX_WEIGHT:
        rows = akp.fetch_index_weight(idx)
        if not rows:
            _log("指数成分 %s 取数失败" % idx, False)
            continue
        n = hdb.upsert_index_weight(conn, idx, rows)
        total += n
        _log("指数成分 %s %d 只（穿透/重合度用）" % (idx, n), True)
    return total


def step_board(conn):
    a = akp.fetch_industry_boards()
    b = akp.fetch_concept_boards()
    n = hdb.upsert_boards(conn, "industry", a or []) + hdb.upsert_boards(conn, "concept", b or [])
    _log("板块名录 行业%d + 概念%d = %d" % (len(a or []), len(b or []), n), bool(n))
    return n


def step_stocks(conn, codes, since):
    total = 0
    for c in codes:
        rows = akp.fetch_stock_daily(c, start_date=since, adjust="qfq")
        if not rows:
            _log("个股 %s 历史取数失败（新浪通道）" % c, False)
            continue
        n = hdb.upsert_bars(conn, c, rows, adjust="qfq", kind="stock")
        total += n
        _log("个股 %s %d 根前复权  %s ~ %s" % (c, n, rows[0].get("date"), rows[-1].get("date")), True)
    return total


def step_spot(conn):
    # 新浪横截面通道连续请求会被风控（返回 HTML），实测每日一次是安全节奏。
    if hdb.watermark(conn, "spot") == _today():
        _log("全市场横截面今日已抓取，跳过（该通道每日至多一次）", True)
        return 0
    rows = akp.fetch_market_spot()
    if not rows:
        _log("全市场横截面取数失败（多为新浪风控，明日再试）", False)
        return 0
    n = hdb.upsert_spot(conn, _today(), rows)
    _log("全市场横截面 %d 只（因子/分位数用）" % n, True)
    return n


def step_valuation(conn, since):
    # csindex 只覆盖中证指数；创业板指 399006 属深证系列，实测返回空，不在列表内。
    total = 0
    for idx in ["000300", "000905", "000852", "000016"]:
        rows = akp.fetch_index_valuation(idx, start_date=since)
        if not rows:
            _log("指数估值 %s 取数失败" % idx, False)
            continue
        n = hdb.upsert_index_valuation(conn, idx, rows)
        total += n
        pes = [r.get("滚动市盈率") for r in rows if r.get("滚动市盈率")]
        _log("指数估值 %s %d 条  最新PE=%s" % (idx, n, pes[-1] if pes else "-"), True)
    return total


def step_financial(conn, codes):
    total = 0
    for c in codes:
        ind = akp.fetch_financial_indicator(c)
        ab = akp.fetch_financial_abstract(c)
        n = hdb.upsert_json_rows(conn, "fin_indicator", ("code", "date"), c, ind or [])
        n += hdb.upsert_json_rows(conn, "fin_abstract", ("code", "period"), c, ab or [],
                                  date_keys=("报告期", "date"))
        _log("财务 %s 指标%d + 摘要%d" % (c, len(ind or []), len(ab or [])), bool(n))
        total += n
    return total


def step_flow(conn, db_path=None):
    """板块资金按日落盘（投资提醒的「连续 N 日」信号依赖它）。

    页面访问时也会惰性落盘，但没人打开页面的日子会断档，导致多日信号永远攒不齐，
    所以夜间任务里也保留这一步。已 settled 的当日记录不会被盘中值覆盖。

    归档日期由 signals 统一决定（最近交易日，非自然日）：节假日/周末重复跑是幂等的，
    不会把周五的数据写成周六。这一步只用 data_fetcher + signals，不依赖 akshare。
    """
    try:
        import signals as sg
    except Exception as e:
        _log("signals 不可用，跳过板块资金：%s: %s" % (type(e).__name__, e), False)
        return 0
    a, b = sg.record_live_flow(db=db_path)
    if not (a or b):
        _log("板块资金快照取数失败，未落盘（多为东财限流，下次访问会惰性补）", False)
        return 0
    _log("板块资金落盘 行业%d + 概念%d（归档 %s，%s）"
         % (a, b, sg._snapshot_date(), "收盘定型" if sg._is_settled_now() else "盘中值"), True)
    return a + b


STEPS = ["calendar", "index", "fund", "weight", "board", "valuation", "flow"]
# 需要 akshare 的步骤；其余（flow / summary）无第三方依赖，缺 akshare 也能跑
NEED_AKSHARE = {"calendar", "index", "fund", "weight", "board",
                "valuation", "stocks", "spot", "financial"}


def main():
    ap = argparse.ArgumentParser(description="量化历史数据落库 ETL（盘后执行）")
    ap.add_argument("--db", default=hdb.default_path())
    ap.add_argument("--since", default="2024-01-01", help="个股/估值的起始日期")
    ap.add_argument("--only", default="", help="逗号分隔：calendar,index,fund,weight,board,flow,stocks,spot,valuation,financial")
    ap.add_argument("--stocks", default="", help="逗号分隔的6位股票代码，做前复权日线")
    ap.add_argument("--spot", action="store_true", help="抓取全市场横截面（约30s，盘后跑）")
    ap.add_argument("--financial", action="store_true", help="抓取 --stocks 列表的财务数据（慢）")
    ap.add_argument("--summary", action="store_true", help="只打印库内概况")
    args = ap.parse_args()

    want = [s.strip() for s in args.only.split(",") if s.strip()] or STEPS
    if args.spot and "spot" not in want:
        want.append("spot")
    if args.stocks and "stocks" not in want:
        want.append("stocks")
    if args.financial and "financial" not in want:
        want.append("financial")

    conn = hdb.connect(args.db)
    print("库: %s | akshare %s" % (args.db, akp.stats().get("akshare_version")))

    if args.summary:
        print(json.dumps(hdb.summary(conn), ensure_ascii=False, indent=2))
        conn.close()
        return 0

    # 依赖门禁按步骤判定：flow/summary 只用东财免费接口，不该被 akshare 挡在门外。
    need_ak = [s for s in want if s in NEED_AKSHARE]
    if need_ak and not akp.ensure_akshare():
        print("akshare 不可用（未安装或导入失败），无法执行：%s" % ",".join(need_ak))
        print("无第三方依赖的步骤：--summary / --only flow")
        conn.close()
        return 1

    t0 = time.time()
    res = {}
    plan = {
        "calendar": lambda: step_calendar(conn),
        "index": lambda: step_index(conn),
        "fund": lambda: step_fund(conn),
        "weight": lambda: step_weight(conn),
        "board": lambda: step_board(conn),
        "flow": lambda: step_flow(conn, args.db),
        "stocks": lambda: step_stocks(conn, [x.strip() for x in args.stocks.split(",") if x.strip()], args.since),
        "spot": lambda: step_spot(conn),
        "valuation": lambda: step_valuation(conn, args.since),
        "financial": lambda: step_financial(conn, [x.strip() for x in args.stocks.split(",") if x.strip()]),
    }
    for s in want:
        fn = plan.get(s)
        if not fn:
            _log("未知步骤 %s" % s, False)
            continue
        try:
            res[s] = fn()
            if res[s]:
                hdb.set_watermark(conn, s, _today())
        except Exception as e:
            res[s] = 0
            _log("步骤 %s 异常: %s: %s" % (s, type(e).__name__, e), False)

    print("\n库内概况:")
    print(json.dumps(hdb.summary(conn), ensure_ascii=False, indent=2))
    print("\n本次写入 %s  用时 %.1fs" % (json.dumps(res, ensure_ascii=False), time.time() - t0))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
