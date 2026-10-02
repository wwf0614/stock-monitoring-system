# -*- coding: utf-8 -*-
"""
fund_nightly.py —— 夜间维护（基金净值结算 + 板块资金落库）
==========================================================
在交易日收盘后运行，一次跑完四件事：

  1. 命中率回填：把量化研判的预测与实际次日走势对账（forecast_track）。
  2. 板块资金落库：把当日板块资金写进 history.db.board_flow_daily，
     供「投资提醒」的连续 N 日净流入/流出信号做多日累计。
  3. 净值历史续写：把持仓基金的净值序列补进 history.db.fund_nav，
     供「基金自身净值趋势」判断（此前只有一次性 ETL 写过 → 会停在旧日期）。
  4. 基金净值同步：记录当日快照并把金额滚动结算为最新市值。

触发方式：Windows 计划任务「盯盘系统-夜间净值结算」，每日 21:30 调用
（可用 Get-ScheduledTask -TaskName '盯盘系统-夜间净值结算' 查看）。
也可手动运行：D:/python/python.exe fund_nightly.py

为什么板块资金要放在这里：页面访问时也会惰性落盘，但没人打开页面的日子就断档，
「连续 3 日主力净流入」这类信号永远攒不齐样本。归档日期由 signals 取【最近交易日】，
所以周末/节假日重复跑是幂等的，不会把周五的数据写成周六。
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import fund_manager as fm


def sync_board_flow():
    """板块资金按日落盘，并回报已积累的交易日数。任何异常都不外抛。"""
    try:
        import signals as sg
    except Exception as e:
        print(f"[板块资金] 跳过: {e}")
        return
    try:
        a, b = sg.record_live_flow()
    except Exception as e:
        print(f"[板块资金] 跳过: {type(e).__name__}: {e}")
        return
    if not (a or b):
        print("[板块资金] 快照取数失败，本次未落盘（下次访问页面会惰性补）")
        return
    snap = sg._snapshot_date()
    print(f"[板块资金] 归档 {snap} 行业{a} + 概念{b}（"
          f"{'收盘定型' if sg._is_settled_now() else '盘中值'}）")
    try:
        import history_db as hdb
        conn = hdb.connect()
        try:
            total = hdb.board_flow_days(conn, True)                      # 含归档当日
            hist = hdb.board_flow_days(conn, True, exclude_date=snap)    # 不含当日（信号口径）
        finally:
            conn.close()
        print(f"[板块资金] 样本库 {total} 个交易日（信号按历史口径算 {hist} 天，"
              f"需 ≥{sg.CFG['streak_need']} 天）")
    except Exception:
        pass


def sync_fund_nav_history(max_pages=2):
    """把持仓基金的单位净值序列补进 history.db.fund_nav。任何异常都不外抛。

    为什么必须有这一步：fund_nav 此前**只被一次性的 etl_backfill.py 写过**，
    之后没有任何计划任务更新它；而 signals._fund_nav_trend 正是拿它算
    「基金自身净值近 20 日 / 偏离 MA20 / 60 日回撤」。不补就会永远停在
    ETL 那天（症状：结论卡里「基金自身净值（截至 YYYY-MM-DD）」一直是旧日期，
    而这个日期正是「板块走坏但基金净值还没坏 → 再观察一两天」的判断依据）。

    只补缺口：since_date 取库内最新一条日期，f10 翻到覆盖它即停 →
    单只通常 1 页请求，很轻。**不回写已有日期**（避免用 None 覆盖库里的日增长率）。
    """
    try:
        import history_db as hdb
        import fund_manager as _fm
    except Exception as e:
        print(f"[净值历史] 跳过: {e}")
        return 0
    try:
        funds = _fm.load_funds() or []
    except Exception as e:
        print(f"[净值历史] 跳过(读持仓): {e}")
        return 0
    if not funds:
        print("[净值历史] 无持仓，跳过")
        return 0

    conn = None
    added = 0
    try:
        conn = hdb.connect()
        for f in funds:
            code = str(f.get("code") or "").strip()
            if not code:
                continue
            try:
                row = conn.execute(
                    "SELECT date, nav FROM fund_nav WHERE code=? "
                    "ORDER BY date DESC LIMIT 1", (code,)).fetchone()
                last_date = row["date"] if row else None
                last_nav = float(row["nav"]) if (row and row["nav"]) else None
                rows = _fm.fetch_nav_history(
                    code, since_date=last_date, max_pages=max_pages) or []
                if not rows:
                    print(f"  {code} 未取到净值历史（源不可用？）")
                    continue
                # f10 返回倒序（最新在前）→ 升序，且只保留比库内更新的日期
                asc = sorted(((d, v) for d, v in rows if d), key=lambda x: x[0])
                fresh = [(d, v) for d, v in asc if not last_date or d > last_date]
                if not fresh:
                    print(f"  {code} 已是最新（{last_date}）")
                    continue
                # 最早那条新增日要用「库内最后一条」的净值做涨跌幅基准
                prev = last_nav
                out = []
                for d, nav in fresh:
                    pct = round((nav / prev - 1) * 100, 2) if prev else None
                    out.append({"净值日期": d, "单位净值": nav, "日增长率": pct})
                    prev = nav
                n = hdb.upsert_fund_nav(conn, code, out)
                added += n
                print(f"  {code} 补 {len(fresh)} 条（{fresh[0][0]}~{fresh[-1][0]}），写入 {n} 行")
            except Exception as e:
                print(f"  {code} 跳过: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[净值历史] 跳过: {type(e).__name__}: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if added:
        print(f"[净值历史] 共写入 {added} 行")
    return added


def main():
    # 先回填量化研判的实测结果：判定需要「次日K线」，故放在收盘后的夜间跑最合适。
    # 即使这里失败(网络/接口)，下次访问 /api/forecast_stats 也会惰性补回填。
    try:
        import forecast_track
        r = forecast_track.settle_pending()
        if r.get("settled"):
            print(f"[命中率回填] 新回填 {r['settled']} 条，仍待定 {r.get('pending', 0)} 条")
            for rec in forecast_track.load_records()[-6:]:
                a = rec.get("actual")
                if a:
                    print(f"  {rec.get('date')} 预测{rec.get('direction')} "
                          f"实际{a.get('ret'):+.2f}% -> {rec.get('grade')}")
        else:
            print(f"[命中率回填] 无可回填记录（待定 {r.get('pending', 0)} 条）")
    except Exception as e:
        print(f"[命中率回填] 跳过: {e}")

    # 板块资金落库放在基金结算之前：无持仓时基金那段会提前 return，
    # 资金数据不该因此被一起跳过。
    sync_board_flow()

    # 净值历史续写：同样放在基金结算之前——它只依赖持仓与 f10，
    # 失败也只打印不外抛，绝不能拖垮下面的快照/结算。
    sync_fund_nav_history()

    snap = fm.record_daily_snapshot(use_cache=False)
    if not snap:
        print("[夜间同步] 当前无基金持仓，跳过。")
        return
    # 先记录当日快照(用结算前金额)，再做结算:把金额滚动更新为最新市值
    settled = fm.settle_daily(use_cache=False)
    print(f"[夜间同步] 完成 [{snap['date']}] 持仓 {len(snap['rows'])} 只"
          f" ｜ 当日估算浮动 {snap['total_pnl']:+.2f}元")
    for r in snap["rows"]:
        pct = r["pct"]
        pct_txt = "—" if pct is None else f"{pct:+.2f}%"
        pnl = r["est_pnl"]
        pnl_txt = "—" if pnl is None else f"{pnl:+.2f}元"
        tag = {"nav": "净值", "est": "估算", "tick": "实时"}.get(r.get("pct_src"), "")
        print(f"  {r['code']} {r['name']} {pct_txt}{('['+tag+']') if tag else ''} 估算{pnl_txt}")
    if settled:
        print("[结算] 已按当日涨跌把金额更新为最新市值(每日复利滚动)：")
        for s in settled:
            print(f"  {s['code']} {s['name']} {s['pct']:+.2f}% ｜ {s['from']:.0f} → {s['to']:.0f}元")
    else:
        print("[结算] 无新结算（无涨幅数据或当日已结算）")


if __name__ == "__main__":
    main()
