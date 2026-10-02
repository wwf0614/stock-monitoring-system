"""
wind_provider.py — 万得(Wind)金融终端 数据源适配层
================================================
前置条件：
  1. 本机已安装并【登录】Wind 金融终端 (默认装在 D:\WindNET)。
     w.start() 必须能连上本地已登录的 Wind 终端，否则取不到数。
  2. WindPy 已装入本系统 Python（安装命令见接入指南）：
        D:/python/python.exe D:\WindNET\bin\installWindPy.py D:\WindNET

设计原则（稳字当头）：
  - ensure_wind()：惰性连接 + 重试冷却。Wind 未开/未登录时返回 False，
    绝不抛异常、绝不阻塞；调用方据此降级到 data_fetcher 的东财/新浪源。
  - 每个 fetch_*：Wind 优先，异常/未连接 → 委托 data_fetcher 原实现（优雅降级）。
    即使某个 Wind 字段写错导致异常，也只有那一项降级，不影响整体。
  - build_snapshot()：返回与 data_fetcher.build_snapshot() 同形状 dict。
    第一阶段用 Wind 增强【实时指数】与【上证日K线】；板块/资金仍由东财提供
    （避免引入未校准的 Wind 板块字段，后续可扩展 w.wset 替换）。
  - 运行协调：盯盘系统常驻后台运行，而 Wind 终端可能后开/重启。
    ensure_wind 每次调用都尝试重连（带 60s 冷却），无需严格启动顺序。

后续扩展点（待启动终端后实测校准字段）：
  - fetch_sectors / build_capital_snapshot：用 w.wset 取板块列表 + w.wsq 取板块主力净流入，
    替换当前委托东财的板块/资金部分（东财免费接口限流最严重，价值最大）。
"""
import time
import threading

try:
    from WindPy import w
except Exception:
    w = None

import data_fetcher as df

# ---------- Wind 代码映射（与 df.INDICES 对应） ----------
WIND_INDICES = [
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
    ("000300.SH", "沪深300"),
]
# 东财 secid -> Wind 代码（K线用）
_SECID_TO_WIND = {
    "1.000001": "000001.SH",
    "0.399001": "399001.SZ",
    "0.399006": "399006.SZ",
    "1.000688": "000688.SH",
    "1.000300": "000300.SH",
    "1.511010": "511010.SH",   # 国债ETF (risk-off 代理)
    "1.518880": "518880.SH",   # 黄金ETF (risk-off 代理)
}

_lock = threading.Lock()
_WIND_READY = False
_WIND_TRY_AT = 0.0
_WIND_RETRY_GAP = 60  # 连不上时 60s 内不重复尝试 start()


def wind_available():
    """WindPy 是否已装入当前 Python。"""
    return w is not None


def ensure_wind():
    """确保 Wind 已连接，返回是否可用。带重试冷却，绝不抛异常。"""
    global _WIND_READY, _WIND_TRY_AT
    if w is None:
        return False
    if _WIND_READY and getattr(w, "isconnected", lambda: False)():
        return True
    now = time.time()
    if now - _WIND_TRY_AT < _WIND_RETRY_GAP:
        return _WIND_READY
    _WIND_TRY_AT = now
    try:
        if not w.isconnected():
            r = w.start()
            if r.ErrorCode != 0:
                _WIND_READY = False
                return False
        _WIND_READY = True
        return True
    except Exception:
        _WIND_READY = False
        return False


def wind_ready():
    return _WIND_READY


def _sf(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_indices():
    """实时指数行情。Wind w.wsq 优先；失败/未连 → 降级东财。同 df.fetch_indices 形状。"""
    if not ensure_wind():
        return df.fetch_indices()
    try:
        codes = ",".join(c for c, _ in WIND_INDICES)
        r = w.wsq(codes, "rt_last,rt_pct_chg")
        if r.ErrorCode != 0 or not r.Data or len(r.Data) < 2:
            return df.fetch_indices()
        last, pct = r.Data[0], r.Data[1]
        out = []
        for i, (code, name) in enumerate(WIND_INDICES):
            c = _sf(last[i]) if i < len(last) else 0.0
            p = _sf(pct[i]) if i < len(pct) else 0.0
            out.append({"name": name, "code": code.split(".")[0],
                        "price": c, "pct": round(p, 2)})
        return out
    except Exception:
        return df.fetch_indices()


def fetch_kline(secid="1.000001", lmt=65):
    """日线收盘序列 [(date, close)]。Wind w.wsd 优先，降级东财。"""
    wind_code = _SECID_TO_WIND.get(secid)
    if not wind_code or not ensure_wind():
        return df.fetch_kline(secid, lmt)
    try:
        end = time.strftime("%Y%m%d")
        r = w.wsd(wind_code, "close", "-%dD" % (int(lmt) + 10), end, "Fill=Previous")
        if r.ErrorCode != 0 or not r.Data or not r.Times:
            return df.fetch_kline(secid, lmt)
        out = []
        for t, c in zip(r.Times, r.Data[0]):
            try:
                out.append((t.strftime("%Y-%m-%d"), float(c)))
            except Exception:
                pass
        return out[-lmt:]
    except Exception:
        return df.fetch_kline(secid, lmt)


def fetch_kline_minute(secid="1.000001", scale=15, lmt=80):
    """分钟线 [(time, o, h, l, c)]。Wind w.wsi 优先，降级东财/新浪。"""
    wind_code = _SECID_TO_WIND.get(secid)
    if not wind_code or not ensure_wind():
        return df.fetch_kline_minute(secid, scale, lmt)
    try:
        r = w.wsi(wind_code, "open,high,low,close", "", "", "BarSize=%d;Days=1" % int(scale))
        if r.ErrorCode != 0 or not r.Data or len(r.Data) < 4 or not r.Times:
            return df.fetch_kline_minute(secid, scale, lmt)
        o, h, l, c = r.Data[0], r.Data[1], r.Data[2], r.Data[3]
        out = []
        for i, t in enumerate(r.Times):
            out.append((t.strftime("%Y-%m-%d %H:%M"),
                        _sf(o[i]), _sf(h[i]), _sf(l[i]), _sf(c[i])))
        return out[-lmt:]
    except Exception:
        return df.fetch_kline_minute(secid, scale, lmt)


def _fill_sh(snap, kline):
    closes = [c for _, c in kline]
    snap["sh_index"]["kline"] = closes[-60:] if closes else []
    snap["sh_index"]["close"] = closes[-1] if closes else None
    if len(closes) >= 20:
        snap["sh_index"]["ma20"] = round(sum(closes[-20:]) / 20, 2)
    if len(closes) >= 60:
        snap["sh_index"]["ma60"] = round(sum(closes[-60:]) / 60, 2)


def build_snapshot():
    """同 df.build_snapshot 形状。Wind 增强【实时指数 + 上证日K线】；板块/资金仍东财。"""
    if not ensure_wind():
        return df.build_snapshot()
    try:
        snap = df.build_snapshot()
        wi = fetch_indices()
        if wi:
            snap["indices"] = wi
        wk = fetch_kline("1.000001", 65)
        if wk:
            _fill_sh(snap, wk)
        return snap
    except Exception:
        return df.build_snapshot()


def build_capital_snapshot():
    """资金/板块轻量快照：暂由东财提供（Wind 板块字段待启动终端后校准）。"""
    return df.build_capital_snapshot()
