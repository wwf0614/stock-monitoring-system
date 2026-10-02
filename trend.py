# -*- coding: utf-8 -*-
"""
trend.py —— A股盯盘系统 · 中期趋势(1-3 个月)数据层

为什么需要它：
  原调仓建议只依赖「当日」信号(regime + 板块主力净流入 + 当日涨跌)，
  容易被"一日游"行情误导(下跌趋势里一根大阳线、或上行趋势里追当日高点)。
  本模块补充「盘面 / 持仓基金 近期 1-3 个月」的中期维度：

  1) 盘面中期趋势 get_market_trend()：以上证 / 沪深300 / 创业板指 90 日 K 线，
     计算近 1 月 / 近 3 月涨跌与中期方向(上行 / 震荡 / 下行)。源=新浪 K 线，稳定。
  2) 持仓基金中期趋势 get_fund_trend(code)：以基金单位净值历史(东方财富 f10 lsjz)，
     计算近 1 月 / 近 3 月收益与最大回撤。源=f10，独立接口、不限流。
  3) 主概念板块中期趋势 get_concept_trend(name)：概念板块 90 日 K 线(东方财富 push2his)，
     计算近 1 月 / 近 3 月。源偶发限流，带重试 + 兜底(失败返回 None，不阻塞主功能)。

所有结果均带 TTL 缓存：净值/盘面日内基本不变(净值每日更新、指数 90 日趋势稳定)，
板块历史日线按日缓存；服务启动时预热，运行时秒级命中。

缓存策略(秒)：
  _NAV_TTL    = 6h      净值历史日内不变
  _MARKET_TTL = 15min   指数 90 日趋势日内极稳
  _BKMAP_TTL  = 1day    概念板块代码表极少变动
  _BOARD_TTL  = 1day    板块历史日线按日更新
"""
import os
import json
import time
import threading
import urllib.request
import urllib.parse

try:
    import data_fetcher as df
except Exception:
    df = None

_BASE = os.path.dirname(os.path.abspath(__file__))
_UL = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_lock = threading.Lock()

# ---- 缓存 ----
_NAV_CACHE = {}        # code -> (ts, result)
_MARKET_CACHE = (0.0, None)
_BKMAP_CACHE = (0.0, None)
_BOARD_CACHE = {}      # concept -> (ts, result)

_NAV_TTL = 6 * 3600
_MARKET_TTL = 15 * 60
_BKMAP_TTL = 24 * 3600
_BOARD_TTL = 24 * 3600
_BOARD_NEG_TTL = 30 * 60   # 概念K线失败(限流)时，30 分钟后重试，不反复卡

# 用于盘面中期趋势的指数(secid, 名称)
_MARKET_INDICES = [
    ("1.000001", "上证指数"),
    ("1.000300", "沪深300"),
    ("0.399006", "创业板指"),
]


# ---------------------------------------------------------------------------
# 通用 HTTP
# ---------------------------------------------------------------------------
def _http_get(url, referer, timeout=15, tries=3, sleep_s=1.2):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": _UL, "Referer": referer})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "ignore")
            if raw:
                return raw
        except Exception as e:
            last = e
            time.sleep(sleep_s * (i + 1))
    return None


# ---------------------------------------------------------------------------
# 1) 持仓基金净值历史 + 中期趋势
# ---------------------------------------------------------------------------
def fetch_nav_history(code, days=90):
    """拉取基金单位净值历史(东方财富 f10 lsjz)。返回升序 [(date_str, nav_float)]。
    注意：接口返回为【降序】(最新在前)，本函数统一翻转为升序。"""
    code = str(code).strip()
    if not code.isdigit():
        return []
    url = ("https://api.fund.eastmoney.com/f10/lsjz?fundCode=%s"
           "&pageIndex=1&pageSize=%d&startDate=&endDate=&_=%d"
           % (code, days, int(time.time() * 1000)))
    raw = _http_get(url, "http://fundf10.eastmoney.com/")
    if not raw:
        return []
    try:
        j = json.loads(raw)
        lst = (j.get("Data") or {}).get("LSJZList") or []
    except Exception:
        return []
    out = []
    for it in lst:
        d = it.get("FSRQ")
        v = it.get("DWJZ")
        if not d or v in (None, ""):
            continue
        try:
            out.append((d, float(v)))
        except Exception:
            continue
    # 翻转：降序 -> 升序(旧->新)
    out.reverse()
    return out


def _trend_from_series(series, label_prefix=""):
    """series: 升序 [(date, value)]。返回 {ret_1m, ret_3m, max_dd, n_days,
    first_date, last_date, window_note, ok}。ret_1m/ret_3m 为百分比(小数)。"""
    if len(series) < 2:
        return {"ok": False, "n_days": len(series)}
    vals = [v for _, v in series]
    dates = [d for d, _ in series]
    last = vals[-1]
    first = vals[0]
    n = len(vals)
    # 近 1 月 ≈ 21 交易日；近 3 月 ≈ 63 交易日
    i1 = max(0, n - 1 - 21)
    i3 = max(0, n - 1 - 63)
    ret_1m = (last / vals[i1] - 1.0) if vals[i1] else None
    ret_3m = (last / vals[i3] - 1.0) if (vals[i3] and n >= 45) else None
    # 最大回撤
    peak = vals[0]
    max_dd = 0.0
    for v in vals:
        if v > peak:
            peak = v
        dd = (v / peak - 1.0) if peak else 0.0
        if dd < max_dd:
            max_dd = dd
    months = round(n / 21.0, 1)
    if ret_3m is None:
        note = "成立约 %.1f 个月，暂无完整 3 月数据" % months
    else:
        note = "近 %d 个交易日(约 %.1f 个月)" % (n, months)
    return {
        "ok": True,
        "ret_1m": round(ret_1m * 100, 2) if ret_1m is not None else None,
        "ret_3m": round(ret_3m * 100, 2) if ret_3m is not None else None,
        "max_dd": round(max_dd * 100, 2),
        "n_days": n,
        "first_date": dates[0],
        "last_date": dates[-1],
        "window_note": note,
    }


def get_fund_trend(code, use_cache=True):
    """持仓基金中期趋势(净值口径)。带 TTL 缓存。"""
    code = str(code).strip()
    now = time.time()
    if use_cache:
        with _lock:
            c = _NAV_CACHE.get(code)
        if c and (now - c[0]) < _NAV_TTL:
            return c[1]
    hist = fetch_nav_history(code, days=90)
    res = _trend_from_series(hist, "fund")
    with _lock:
        _NAV_CACHE[code] = (now, res)
    return res


# ---------------------------------------------------------------------------
# 2) 盘面中期趋势(宽基指数 90 日 K 线)
# ---------------------------------------------------------------------------
def _index_closes(secid, lmt=90):
    if df is None:
        return []
    try:
        kl = df.fetch_kline(secid, lmt=lmt) or []
        # fetch_kline 返回 [(date, close)] 升序
        return [(d, float(c)) for d, c in kl if c not in (None, "")]
    except Exception:
        return []


def get_market_trend(use_cache=True):
    """盘面中期趋势：上证 / 沪深300 / 创业板指 90 日 K 线 -> 近1月/近3月 + 方向。"""
    global _MARKET_CACHE
    now = time.time()
    if use_cache and (now - _MARKET_CACHE[0]) < _MARKET_TTL and _MARKET_CACHE[1]:
        return _MARKET_CACHE[1]
    idx = {}
    for secid, name in _MARKET_INDICES:
        closes = _index_closes(secid, lmt=90)
        t = _trend_from_series(closes, name) if closes else {"ok": False}
        idx[name] = t
    # 以沪深300 为盘面中枢(兼具大/中盘代表性)
    core = idx.get("沪深300") or idx.get("上证指数") or {}
    r1 = core.get("ret_1m")
    r3 = core.get("ret_3m")
    if r1 is not None and r3 is not None:
        if r1 > 0 and r3 > 0:
            label = "中期上行"
        elif r1 < 0 and r3 < 0:
            label = "中期下行"
        else:
            label = "中期震荡"
    elif r1 is not None:
        label = "中期上行" if r1 > 0 else "中期下行"
    else:
        label = "—"
    res = {
        "ok": core.get("ok", False),
        "label": label,
        "ret_1m": r1,
        "ret_3m": r3,
        "indices": idx,
        "updated": time.strftime("%Y-%m-%d %H:%M"),
    }
    _MARKET_CACHE = (now, res)
    return res


# ---------------------------------------------------------------------------
# 3) 主概念板块中期趋势(概念板块 90 日 K 线)
# ---------------------------------------------------------------------------
def _concept_bk_map(use_cache=True):
    """概念板块名 -> BK 代码。clist m:90+t:3，全量翻页。带 1 日缓存。"""
    global _BKMAP_CACHE
    now = time.time()
    if use_cache and (now - _BKMAP_CACHE[0]) < _BKMAP_TTL and _BKMAP_CACHE[1]:
        return _BKMAP_CACHE[1]
    m = {}
    if df is None:
        return m
    try:
        rows = df.fetch_sectors("m:90+t:3", 100, "f3", 1, 6) or []
        for it in rows:
            nm = it.get("name")
            cd = it.get("code")
            if nm and cd:
                m[nm] = cd
    except Exception:
        pass
    _BKMAP_CACHE = (now, m)
    return m


def _board_kline(bk_code, lmt=90):
    """板块 90 日 K 线(东方财富 push2his)。返回升序 [(date, close)]。
    东方财富历史K线接口偶发限流(RemoteDisconnected)，这里做多轮重试 +
    指数退避，且单次调用总耗时可控，避免拖垮主流程。"""
    secid = "90." + str(bk_code)
    hosts = ["https://push2his.eastmoney.com", "https://push2.eastmoney.com"]
    # 快速失败：该历史K线接口偶发限流/断连，重试预算压到最低，避免拖垮趋势预热(单概念最坏约 2×8s)。
    for host in hosts:
        try:
            url = (host + "/api/qt/stock/kline/get?secid=%s&klt=101&fqt=1"
                   "&end=20500101&lmt=%d&fields1=f1,f2,f3&fields2=f51,f53"
                   % (secid, lmt))
            raw = _http_get(url, "https://quote.eastmoney.com/",
                            tries=1, timeout=8, sleep_s=0.5)
            if not raw:
                continue
            j = json.loads(raw)
            kl = (j.get("data") or {}).get("klines") or []
            out = []
            for line in kl:
                parts = line.split(",")
                if len(parts) >= 2:
                    try:
                        out.append((parts[0], float(parts[1])))
                    except Exception:
                        continue
            if out:
                return out
        except Exception:
            continue
    return []


def get_concept_trend(concept, use_cache=True):
    """主概念板块中期趋势。失败(限流/无映射)返回 None，不阻塞主流程。"""
    if not concept:
        return None
    now = time.time()
    if use_cache:
        with _lock:
            c = _BOARD_CACHE.get(concept)
        if c and (now - c[0]) < (_BOARD_TTL if c[1] else _BOARD_NEG_TTL):
            return c[1]
    bk = _concept_bk_map().get(concept)
    res = None
    if bk:
        kl = _board_kline(bk, lmt=90)
        if len(kl) >= 2:
            t = _trend_from_series(kl, concept)
            if t.get("ok"):
                res = t
    with _lock:
        _BOARD_CACHE[concept] = (now, res)
    return res


# ---------------------------------------------------------------------------
# 便捷聚合：给 advise 一次性准备 盘面 + 各基金(净值+主概念) 中期趋势
# ---------------------------------------------------------------------------
def get_trends_for_funds(rows, use_cache=True):
    """rows: merge_with_amounts 的输出(含 code / concepts)。
    返回 {market: {...}, funds: {code: {nav:{...}, concept:{name, trend|None}}}, ok}。"""
    market = get_market_trend(use_cache=use_cache)
    funds = {}
    for r in rows or []:
        code = r.get("code")
        concepts = r.get("concepts") or []
        nav = get_fund_trend(code, use_cache=use_cache) if code else {"ok": False}
        concept_trend = None
        main_concept = None
        if concepts:
            main_concept = concepts[0]
            concept_trend = get_concept_trend(main_concept, use_cache=use_cache)
        funds[code] = {
            "nav": nav,
            "main_concept": main_concept,
            "concept": concept_trend,
        }
    return {"ok": True, "market": market, "funds": funds}


def warmup(rows=None):
    """服务启动时预热：盘面趋势 + (可选)各基金趋势，避免首屏卡顿。"""
    try:
        get_market_trend(use_cache=False)
    except Exception:
        pass
    if rows:
        try:
            get_trends_for_funds(rows, use_cache=False)
        except Exception:
            pass


if __name__ == "__main__":
    import pprint
    print("== 盘面中期趋势 ==")
    pprint.pprint(get_market_trend(use_cache=False))
    print("== 基金 017811 净值中期趋势 ==")
    pprint.pprint(get_fund_trend("017811", use_cache=False))
    print("== 概念 半导体概念 中期趋势 ==")
    pprint.pprint(get_concept_trend("半导体概念", use_cache=False))
