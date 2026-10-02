# -*- coding: utf-8 -*-
"""
akshare_provider.py —— AKShare 长历史 / 横截面数据适配层（盘后 ETL 专用）
=======================================================================
为什么需要它：
  data_fetcher 与 wind_provider 都只给「近 60~90 根」的短序列，且没有全市场横截面、
  复权历史、财务指标、指数成分权重。量化分析(回测/因子/风险模型)缺的正是这一层。
  本模块只补数据，不做任何实时展示，因此不改 server 的刷新链路。

实测环境约束（2026-09-19，akshare 1.18.88，本机 D:/python）：
  - akshare 的 *_em 接口打的是 push2 / push2his.eastmoney.com，本机这两个域名持续
    RemoteDisconnected —— 与 data_fetcher.py:182/237/321 记录的东财限流是同一现象。
    实测失效：stock_zh_a_hist、index_zh_a_hist、stock_board_industry_name_em、
    stock_board_concept_name_em、stock_board_industry_cons_em、stock_zh_a_spot_em。
    所以本模块【不封装】这些通道，避免把已知不可用的源接进链路。
  - 实测可用：新浪(stock_zh_index_daily / stock_zh_a_daily / stock_zh_a_spot)、
    同花顺(板块列表、财务摘要)、中证(csindex 成分权重与滚动市盈率)、
    上交所(两融明细)、api.fund.eastmoney.com(f10 场外基金净值，不同主机故不受影响)。
  - stock_zh_a_spot() 全市场 5500+ 行实测 ~30s（内部 70 次分页请求）。
    因此本模块只允许盘后 ETL / 夜间任务调用，严禁挂进 server 的实时刷新线程。

设计原则（与 wind_provider 同构，稳字当头）：
  - ensure_akshare()：惰性导入 + 失败冷却，未安装/异常一律 False，绝不抛异常。
  - 每个 fetch_*：成功返回 list[dict]，失败返回 None；调用方跳过本步而非中断整个 ETL。
  - 返回值一律经 _records() 规范化为 JSON 原生类型（日期转字符串、NaN/NaT 转 None）。
    akshare 返回里 date 列与 NaN 极常见，直接 json.dumps 会 TypeError —— 这一步是必须的。
  - 全部结果带 TTL 缓存：日线/净值/财务日内不变，横截面按分钟缓存。
"""
import os
import time
import math
import threading
import datetime
import contextlib

try:
    import akshare as ak
except Exception:
    ak = None

try:
    import pandas as pd
except Exception:
    pd = None

_lock = threading.Lock()
_CACHE = {}

_AK_TRY_AT = 0.0
_AK_RETRY_GAP = 300      # 导入失败后 5 分钟内不重复尝试
_TQDM_ENV = "TQDM_DISABLE"

# TTL（秒）
_TTL_DAILY = 6 * 3600    # 日线/净值：收盘后不再变化
_TTL_SPOT = 300          # 全市场横截面：5 分钟
_TTL_STATIC = 7 * 86400  # 交易日历、板块名录、指数成分：周级变动

# 核心指数：东财 secid -> 新浪代码
SINA_INDEX = {
    "1.000001": "sh000001",   # 上证指数
    "1.000300": "sh000300",   # 沪深300
    "0.399001": "sz399001",   # 深证成指
    "0.399006": "sz399006",   # 创业板指
    "1.000688": "sh000688",   # 科创50
}


def ak_ready():
    return ak is not None


def ensure_akshare():
    """确认 akshare 可用。带失败冷却，绝不抛异常、绝不阻塞调用方。"""
    global ak
    if ak is not None:
        return True
    now = time.time()
    with _lock:
        if now - _AK_TRY_AT < _AK_RETRY_GAP:
            return False
        _AK_TRY_AT = now
        try:
            import akshare as _ak
            ak = _ak
            return True
        except Exception:
            return False


@contextlib.contextmanager
def _quiet():
    """akshare 部分接口内部用 tqdm 往 stderr 刷进度条，会污染 server.log / 看门狗日志。"""
    old = os.environ.get(_TQDM_ENV)
    os.environ[_TQDM_ENV] = "1"
    try:
        with open(os.devnull, "w", encoding="utf-8") as dn:
            with contextlib.redirect_stderr(dn):
                yield
    finally:
        if old is None:
            os.environ.pop(_TQDM_ENV, None)
        else:
            os.environ[_TQDM_ENV] = old


def _cache_get(key, ttl):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _cache_put(key, val):
    if val is not None:
        _CACHE[key] = (time.time(), val)
    return val


def _call(fn, *args, **kwargs):
    """统一入口：可用性检查 + 静默 + 异常吞掉。失败返回 None 而非抛异常。

    tries>1 用于会被上游风控的接口：新浪横截面连续请求若干次后会返回 HTML 页
    （表现为 demjson 'Can not decode value starting with character <'），
    退避重试可跨过瞬时风控；因此横截面必须每日至多一次，靠 TTL 缓存兜住。
    """
    if not ensure_akshare():
        return None
    tries = int(kwargs.pop("tries", 1))
    gap = float(kwargs.pop("gap", 3.0))
    for i in range(max(1, tries)):
        try:
            with _quiet():
                return fn(*args, **kwargs)
        except Exception:
            if i + 1 >= tries:
                return None
            time.sleep(gap * (i + 1))
    return None


def _py(v):
    """把 pandas/numpy 标量转成 JSON 原生值。NaN/NaT/NA 一律 None（缺失不污染下游统计）。"""
    if v is None:
        return None
    t = type(v)
    if t is bool or t is str:
        return v
    if t is int:
        return v
    if t is float:
        return None if (math.isnan(v) or math.isinf(v)) else v
    if pd is not None and isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S") if (v.hour or v.minute) else v.strftime("%Y-%m-%d")
    if isinstance(v, (datetime.date, datetime.time)):
        return v.strftime("%Y-%m-%d") if isinstance(v, datetime.date) else v.isoformat()
    try:
        return _py(v.item())          # numpy 标量
    except Exception:
        pass
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except Exception:
        s = str(v).strip()
        return None if s in ("", "nan", "NaN", "NaT", "None", "<NA>") else s


def _records(df, keep=None):
    """DataFrame -> list[dict]（规范化）。keep 为需要保留的列名白名单。"""
    if df is None or not hasattr(df, "to_dict"):
        return None
    try:
        rows = df.to_dict(orient="records")
    except Exception:
        return None
    out = []
    for r in rows:
        d = {}
        for k, v in r.items():
            if keep is not None and k not in keep:
                continue
            pv = _py(v)
            if pv is not None:
                d[str(k)] = pv
        if d:
            out.append(d)
    return out or None


def to_sina_symbol(code):
    """6 位代码 -> 新浪 symbol。北交所用 bj，其余按首位判 sh/sz。"""
    c = str(code or "").strip().lower()
    for p in ("sh", "sz", "bj"):
        if c.startswith(p):
            return c
    c = "".join(ch for ch in c if ch.isdigit())
    if len(c) != 6:
        return None
    if c.startswith(("60", "68", "9", "5", "11", "113", "204")):
        return "sh" + c
    if c.startswith(("4", "8")):
        return "bj" + c
    return "sz" + c


# ---------------- 交易日历 ----------------

def fetch_trade_calendar():
    """全部交易日 ['YYYY-MM-DD']。用于 ETL 补齐缺失日期，避免把周末当断点。"""
    key = "cal"
    c = _cache_get(key, _TTL_STATIC)
    if c is not None:
        return c
    df = _call(ak.tool_trade_date_hist_sina)
    out = [r["trade_date"] for r in (_records(df) or []) if r.get("trade_date")]
    return _cache_put(key, out or None)


def is_trading_day(date_str):
    cal = fetch_trade_calendar()
    if not cal:
        wd = datetime.datetime.strptime(date_str, "%Y-%m-%d").weekday()
        return wd < 5
    return date_str in set(cal)


# ---------------- 日线（新浪，长历史 + 复权）----------------

def fetch_index_daily(sina_code="sh000300"):
    """指数日线全量历史（实测 5996 根）。[{date,open,high,low,close,volume}]"""
    key = "idx:" + sina_code
    c = _cache_get(key, _TTL_DAILY)
    if c is not None:
        return c
    df = _call(ak.stock_zh_index_daily, symbol=sina_code)
    return _cache_put(key, _records(df, keep={"date", "open", "high", "low", "close", "volume"}))


def fetch_stock_daily(code, start_date=None, end_date=None, adjust="qfq"):
    """个股日线（新浪）。adjust: qfq 前复权 / hfq 后复权 / 空串不复权。

    回测必须用复权价，否则除权跳空会被当成真实暴跌。
    """
    sym = to_sina_symbol(code)
    if not sym:
        return None
    key = "stk:%s:%s:%s:%s" % (sym, start_date or "", end_date or "", adjust)
    c = _cache_get(key, _TTL_DAILY)
    if c is not None:
        return c
    kw = {"symbol": sym, "adjust": adjust or ""}
    if start_date:
        kw["start_date"] = str(start_date).replace("-", "")
    if end_date:
        kw["end_date"] = str(end_date).replace("-", "")
    df = _call(ak.stock_zh_a_daily, **kw)
    return _cache_put(key, _records(df, keep={
        "date", "open", "high", "low", "close", "volume", "amount", "outstanding_share", "turnover"}))


# ---------------- 全市场横截面 ----------------

def fetch_market_spot():
    """全 A 股横截面（实测 5564 行 / ~30s）。仅供盘后取因子快照，勿进实时线程。"""
    key = "spot"
    c = _cache_get(key, _TTL_SPOT)
    if c is not None:
        return c
    df = _call(ak.stock_zh_a_spot, tries=3, gap=5)
    return _cache_put(key, _records(df))


# ---------------- 场外基金净值（f10，与 push2 不同主机）----------------

def fetch_fund_nav(code, indicator="单位净值走势"):
    """场外开放式基金净值历史（实测单只 900 条）。[{净值日期,单位净值,日增长率}]

    这是当前系统最缺的一块：fund_history.json 只有 25 条自采快照，
    而基金级回测与回撤计算需要整段净值序列。
    """
    code = str(code or "").strip()
    if not code:
        return None
    key = "nav:%s:%s" % (code, indicator)
    c = _cache_get(key, _TTL_DAILY)
    if c is not None:
        return c
    df = _call(ak.fund_open_fund_info_em, symbol=code, indicator=indicator)
    return _cache_put(key, _records(df))


# ---------------- 板块（同花顺）----------------

def fetch_industry_boards():
    """同花顺行业板块名录（实测 90 个）。[{name,code}]"""
    key = "bd:ind"
    c = _cache_get(key, _TTL_STATIC)
    if c is not None:
        return c
    df = _call(ak.stock_board_industry_name_ths)
    return _cache_put(key, _records(df))


def fetch_concept_boards():
    """同花顺概念板块名录（实测 375 个，与持仓 concepts 对得上）。[{name,code}]"""
    key = "bd:con"
    c = _cache_get(key, _TTL_STATIC)
    if c is not None:
        return c
    df = _call(ak.stock_board_concept_name_ths)
    return _cache_put(key, _records(df))


# ---------------- 指数成分与估值（中证）----------------

def fetch_index_weight(index_code="000300"):
    """指数成分股与权重（实测 000300 有 300 行，含权重）。

    有了它才能做「持仓穿透」：把基金重仓与指数权重对上，量化主题重合度。
    """
    key = "cw:" + str(index_code)
    c = _cache_get(key, _TTL_STATIC)
    if c is not None:
        return c
    df = _call(ak.index_stock_cons_weight_csindex, symbol=str(index_code))
    return _cache_put(key, _records(df))


def fetch_index_valuation(index_code="H30533", start_date=None, end_date=None):
    """中证指数历史行情 + 滚动市盈率（实测 709 行，PE 可直接算估值分位）。

    注意：csindex 接口必须同时给 start_date 与 end_date，缺 end_date 时 akshare
    会收到空表并抛 "Length mismatch: Expected axis has 0 elements"，故这里补默认值。
    """
    today = datetime.date.today().strftime("%Y%m%d")
    kw = {"symbol": str(index_code),
          "start_date": str(start_date or "20150101").replace("-", ""),
          "end_date": str(end_date or today).replace("-", "")}
    key = "val:%s:%s" % (index_code, start_date)
    c = _cache_get(key, _TTL_DAILY)
    if c is not None:
        return c
    df = _call(ak.stock_zh_index_hist_csindex, **kw)
    return _cache_put(key, _records(df))


# ---------------- 财务 ----------------

def fetch_financial_indicator(code, start_year="2023"):
    """个股财务指标（新浪，实测 86 列）。用于质量/成长因子：ROE、毛利率、资产负债率。"""
    c6 = (to_sina_symbol(code) or "")[2:]
    if not c6:
        return None
    key = "fin:%s:%s" % (c6, start_year)
    c = _cache_get(key, _TTL_STATIC)
    if c is not None:
        return c
    df = _call(ak.stock_financial_analysis_indicator, symbol=c6, start_year=str(start_year))
    return _cache_put(key, _records(df))


def fetch_financial_abstract(code, indicator="按报告期"):
    """财务摘要（同花顺，实测 122 期）。含净利润/营收同比增速，成长因子主料。"""
    c6 = (to_sina_symbol(code) or "")[2:]
    if not c6:
        return None
    key = "abs:%s:%s" % (c6, indicator)
    c = _cache_get(key, _TTL_STATIC)
    if c is not None:
        return c
    df = _call(ak.stock_financial_abstract_ths, symbol=c6, indicator=indicator)
    return _cache_put(key, _records(df))


# ---------------- 杠杆资金 ----------------

def fetch_margin_detail_sse(date_str):
    """上交所融资融券明细（实测单日 2000 行）。杠杆情绪维度，当前系统完全没有。"""
    d = str(date_str).replace("-", "")
    key = "mgn:" + d
    c = _cache_get(key, _TTL_DAILY)
    if c is not None:
        return c
    df = _call(ak.stock_margin_detail_sse, date=d)
    return _cache_put(key, _records(df))


def clear_cache():
    _CACHE.clear()


def stats():
    return {"akshare_loaded": ak is not None,
            "akshare_version": getattr(ak, "__version__", None) if ak else None,
            "cache_entries": len(_CACHE)}
