"""
data_fetcher.py —— A股大盘盯盘系统 · 数据抓取层
数据源：东方财富免费行情接口(push2.eastmoney.com / push2his.eastmoney.com)
可靠性说明：
  - 指数用 stock/get(按secid) ，板块/资金用 clist(m:90+t:2行业 / m:90+t:3概念)
  - 全程 Session + 重试适配器，规避东方财富对快速请求的偶发连接重置
  - 北向资金自2024年交易所调整后不再实时披露，本系统以"全市场主力净流入"作为资金总信号
"""
import time
import re
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

UT = "fa5fd1943c7b386f172d6893dbfba10b"

# (secid, 名称)  1=上交所 0=深交所
INDICES = [
    ("1.000001", "上证指数"),
    ("0.399001", "深证成指"),
    ("0.399006", "创业板指"),
    ("1.000688", "科创50"),
    ("1.000300", "沪深300"),
]

_session = None


def _session_get():
    global _session
    if _session is None:
        s = requests.Session()
        retry = Retry(total=3, backoff_factor=0.3,
                      status_forcelist=[500, 502, 503, 504], allowed_methods=["GET"])
        s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10))
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://quote.eastmoney.com/",
        })
        _session = s
    return _session


def _get(url, params, timeout=10, tries=2):
    """带空数据重试的GET。东方财富偶发返回空/非JSON，需重试而非仅依赖连接层重试。"""
    p = dict(params)
    p.setdefault("ut", UT)
    p.setdefault("fltt", "2")
    p.setdefault("invt", "2")
    last = None
    for _ in range(tries):
        try:
            r = _session_get().get(url, params=p, timeout=timeout)
            j = r.json()
            if isinstance(j, dict) and (j.get("data") is not None or j.get("rc", -1) == 0):
                return j
            last = j
        except Exception as e:
            last = e
        time.sleep(0.4)
    return last if isinstance(last, dict) else {}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_indices():
    """核心指数实时行情(现价/涨跌幅)。主力净流入取自板块聚合，指数层不单独取。
    主源用新浪实时行情(hq.sinajs.cn，独立域名、稳定)，东方财富 stock/get 仅作兜底。
    """
    out = _fetch_indices_sina()
    if len(out) >= 4:
        return out
    # 兜底：东方财富逐 secid 拉取
    for secid, name in INDICES:
        d = None
        for _ in range(2):
            try:
                j = _get("https://push2.eastmoney.com/api/qt/stock/get", {
                    "secid": secid, "fields": "f43,f57,f58,f170",
                }, tries=1)
                d = (j or {}).get("data") or {}
                if d.get("f43") is not None:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        if d:
            out.append({
                "name": name or d.get("f58"),
                "code": d.get("f57"),
                "price": _num(d.get("f43")),
                "pct": _num(d.get("f170")),
            })
        time.sleep(0.1)
    return out


_SINA_INDEX_SYM = {
    "1.000001": "sh000001", "0.399001": "sz399001", "0.399006": "sz399006",
    "1.000688": "sh000688", "1.000300": "sh000300",
}


def _fetch_indices_sina():
    syms = [_SINA_INDEX_SYM.get(s, "") for s, _ in INDICES if s in _SINA_INDEX_SYM]
    if not syms:
        return []
    try:
        r = _session_get().get("https://hq.sinajs.cn/list=" + ",".join(syms),
                               headers={"User-Agent": "Mozilla/5.0",
                                         "Referer": "https://finance.sina.com.cn/"},
                               timeout=10)
        r.encoding = "gb18030"
        txt = r.text
        out = []
        for secid, name in INDICES:
            sym = _SINA_INDEX_SYM.get(secid)
            if not sym:
                continue
            m = re.search(r'hq_str_' + sym + r'="(.*?)";', txt)
            if not m:
                continue
            p = m.group(1).split(",")
            if len(p) < 4:
                continue
            cur = _num(p[3]); prev = _num(p[2])
            pct = (cur - prev) / prev * 100 if prev else 0.0
            out.append({"name": p[0] or name, "code": secid.split(".")[1],
                        "price": cur, "pct": round(pct, 2)})
        return out
    except Exception:
        return []


# 板块熔断：东方财富对板块列表(clist)限流极频繁，若连续失败则进入冷却，
# 停止请求一段时间让服务端限流解除，避免"越失败越猛打"自我加剧限流。
_SECTOR_FAIL_STREAK = 0
_SECTOR_COOLDOWN_UNTIL = 0.0
_SECTOR_COOLDOWN_BASE = 150   # 秒：首次熔断的冷却时长
_SECTOR_COOLDOWN_MAX = 600    # 秒：指数退避上限
_SECTOR_COOLDOWN = _SECTOR_COOLDOWN_BASE


def fetch_sectors(fs_type, pz=100, sort="f3", order=1, max_pages=6):
    """板块列表(分页补齐全量)。fs_type: m:90+t:2 行业 / m:90+t:3 概念
    东方财富单页最多返回 100 条，故 pz=100，行业~496/概念~504 需翻 6 页覆盖。
    健壮性: (1)成功则缓存，后续失败回退上次成功; (2)连续失败触发熔断冷却，
    冷却期内不请求，待限流解除后自动恢复，防止自我加剧限流。
    """
    global _SECTOR_FAIL_STREAK, _SECTOR_COOLDOWN_UNTIL, _SECTOR_COOLDOWN
    now = time.time()
    if now < _SECTOR_COOLDOWN_UNTIL:
        # 熔断冷却中：直接返回缓存(可能为空)
        return _SECTOR_CACHE.get(fs_type) or []
    out = []
    for attempt in range(2):
        rows = _fetch_sectors_once(fs_type, pz, sort, order, max_pages)
        if len(rows) >= 20:  # 至少拿到足量板块才算成功
            out = rows
            break
        time.sleep(1.5)
    cached = _SECTOR_CACHE.get(fs_type)
    if out:
        _SECTOR_CACHE[fs_type] = out
        _SECTOR_FAIL_STREAK = 0
        _SECTOR_COOLDOWN = _SECTOR_COOLDOWN_BASE   # 抓通了 → 冷却时长复位
    elif cached:
        out = cached  # 有缓存兜底，不算连续失败
        _SECTOR_FAIL_STREAK = 0
    else:
        _SECTOR_FAIL_STREAK += 1
        if _SECTOR_FAIL_STREAK >= 3:
            _SECTOR_COOLDOWN_UNTIL = now + _SECTOR_COOLDOWN
            _SECTOR_FAIL_STREAK = 0
            # 指数退避：限流往往不是一次性的，固定冷却时长会在封禁期内反复敲门，
            # 反而延长封禁。抓通一次即复位（见上）。
            _SECTOR_COOLDOWN = min(_SECTOR_COOLDOWN * 2, _SECTOR_COOLDOWN_MAX)
    return out


def _fetch_sectors_once(fs_type, pz, sort, order, max_pages):
    # push2 偶发被限流，push2delay 同为东方财富镜像域名，作为主源更易命中；
    # 两者都试一次，先到先用。
    hosts = ["https://push2delay.eastmoney.com", "https://push2.eastmoney.com"]
    rows = []
    for host in hosts:
        rows = []
        ok = False
        for pn in range(1, max_pages + 1):
            try:
                j = _get(host + "/api/qt/clist/get", {
                    "pn": str(pn), "pz": str(pz), "po": str(order), "np": "1",
                    "fid": sort, "fs": fs_type,
                    "fields": "f12,f14,f3,f62,f63,f64,f65,f66,f184",
                })
                d = (j.get("data") or {}).get("diff") or []
                rows.extend(d)
                if len(d) < pz:
                    break
            except Exception:
                break
            time.sleep(0.1)
        if len(rows) >= 20:
            ok = True
            break
    out = []
    for it in rows:
        out.append({
            "name": it.get("f14"),
            "code": it.get("f12"),
            "pct": _num(it.get("f3")),
            "main_net": _num(it.get("f62")),   # 主力净流入 = 超大单+大单
            "inst_net": _num(it.get("f63")),   # 超大单净流入(机构/庄家级)
            "big_net": _num(it.get("f64")),    # 大单净流入
            "mid_net": _num(it.get("f65")),    # 中单净流入
            "small_net": _num(it.get("f66")),  # 小单净流入(散户)
            "total_net": _num(it.get("f63")) + _num(it.get("f64")) + _num(it.get("f65")) + _num(it.get("f66")),  # 全市场净流入 = 超大+大+中+小单
            "xl_in": _num(it.get("f184")),
        })
    return out


_KLINE_CACHE = {}  # key (secid, lmt) -> (timestamp, [(date, close)])
_KLINE_TTL = 600  # 10分钟；日线数据日内几乎不变，缓存可大幅降低对行情源的请求压力
_SECTOR_CACHE = {}  # fs_type -> 上次成功的板块列表(限流时回退)

# 东方财富 secid -> 新浪K线 symbol(用于更稳定的K线源)
_SINA_MAP = {
    "1.000001": "sh000001", "0.399001": "sz399001", "0.399006": "sz399006",
    "1.000688": "sh000688", "1.000300": "sh000300",
    # 跨资产(risk-off 代理)：国债ETF(避险) / 黄金ETF(避险)
    "1.511010": "sh511010", "1.518880": "sh518880",
}


def _fetch_kline_sina(secid, lmt):
    """新浪财经K线(日线)，稳定性优于东方财富 push2his；返回 [(date, close)] 升序。"""
    sym = _SINA_MAP.get(secid)
    if not sym:
        return []
    try:
        r = _session_get().get(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
            params={"symbol": sym, "scale": "240", "datalen": str(lmt)},
            timeout=12)
        arr = r.json()
        out = []
        for it in arr:
            try:
                out.append((it["day"], float(it["close"])))
            except Exception:
                pass
        out.sort(key=lambda x: x[0])
        return out
    except Exception:
        return []


_KLINE_FULL_CACHE = {}
_KLINE_FULL_TTL = 120


def fetch_kline_full(secid="1.000001", lmt=60):
    """新浪日K全字段：[(date, open, high, low, close, volume)] 升序。失败返回 []。

    用于「量价配合」等需要成交量的场景（forecast.volume_price_signal）。
    仅支持 _SINA_MAP 中已映射的标的；未映射返回 []（调用方优雅降级）。
    """
    sym = _SINA_MAP.get(secid)
    if not sym:
        return []
    key = (secid, int(lmt))
    now = time.time()
    c = _KLINE_FULL_CACHE.get(key)
    if c and (now - c[0]) < _KLINE_FULL_TTL:
        return c[1]
    try:
        r = _session_get().get(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "CN_MarketData.getKLineData",
            params={"symbol": sym, "scale": "240", "ma": "no", "datalen": str(int(lmt))},
            timeout=12)
        arr = r.json()
        out = []
        for it in arr or []:
            try:
                out.append((str(it["day"])[:10], float(it["open"]), float(it["high"]),
                            float(it["low"]), float(it["close"]),
                            float(it.get("volume") or 0)))
            except Exception:
                pass
        out.sort(key=lambda x: x[0])
        if out:
            _KLINE_FULL_CACHE[key] = (now, out)
        return out
    except Exception:
        return []


def fetch_kline(secid="1.000001", lmt=65):
    """近 lmt 日收盘价 -> [(date, close)]，用于MA。新浪优先 + 东方财富回退 + 短缓存。

    东方财富 push2his 的K线接口偶发 RemoteDisconnected(限流)且相当频繁，单独依赖会
    导致MA/回测偶发失败；新浪K线源独立且稳定，作为主源，东方财富仅作兜底。
    """
    key = (secid, lmt)
    now = time.time()
    cached = _KLINE_CACHE.get(key)
    if cached and (now - cached[0]) < _KLINE_TTL:
        return cached[1]
    rows = _fetch_kline_sina(secid, lmt)
    if not rows:
        rows = _fetch_kline_eastmoney(secid, lmt)
    if rows:  # 仅缓存非空结果，失败不污染缓存
        _KLINE_CACHE[key] = (now, rows)
    return rows


# ---------------------------------------------------------------------------
# 分钟级K线：仅新浪源可用。
# 实测(2026-08-31)：东财 push2his 的 klt=15/60/101 接口会返回 0 条(限流)，
# 而新浪 quotes.sina.cn 的 CN_MarketDataService 分钟线稳定可用且含 OHLCV，
# 可支撑「日内真实波动率」与「多周期动量」计算。故分钟数据不走东方财富。
# ---------------------------------------------------------------------------
_MKLINE_CACHE = {}      # key (secid, scale, lmt) -> (ts, rows)
_MKLINE_TTL = 60        # 秒；盘中分钟线会持续新增，缓存不宜长
_SINA_MIN_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"


def fetch_kline_minute(secid="1.000001", scale=15, lmt=80):
    """分钟级K线 -> [(time, open, high, low, close)] 升序。失败返回 []。

    scale: 分钟周期(5/15/30/60)。lmt: 取最近多少根。
    仅支持 _SINA_MAP 中已映射的标的；未映射返回 []（调用方应优雅降级到纯日线）。
    """
    sym = _SINA_MAP.get(secid)
    if not sym:
        return []
    key = (secid, int(scale), int(lmt))
    now = time.time()
    cached = _MKline_cache_get(key)
    if cached is not None:
        return cached
    try:
        r = _session_get().get(_SINA_MIN_URL, params={
            "symbol": sym, "scale": str(int(scale)), "ma": "no",
            "datalen": str(int(lmt))}, timeout=12)
        arr = r.json()
        out = []
        for it in arr or []:
            try:
                out.append((it["day"], float(it["open"]), float(it["high"]),
                            float(it["low"]), float(it["close"])))
            except Exception:
                pass
        out.sort(key=lambda x: x[0])
        if out:
            _MKLINE_CACHE[key] = (now, out)
        return out
    except Exception:
        return []


def _MKline_cache_get(key):
    c = _MKLINE_CACHE.get(key)
    if not c:
        return None
    if (time.time() - c[0]) < _MKLINE_TTL:
        return c[1]
    return None


def _fetch_kline_eastmoney(secid, lmt):
    hosts = ["https://push2his.eastmoney.com", "https://push2.eastmoney.com"]
    for attempt in range(4):
        host = hosts[attempt % 2]
        try:
            j = _get(host + "/api/qt/stock/kline/get", {
                "secid": secid, "klt": "101", "fqt": "1", "end": "20500101",
                "lmt": str(lmt), "fields1": "f1,f2", "fields2": "f51,f53",
            }, tries=1)
            klines = (j.get("data", {}) or {}).get("klines", []) or []
            out = []
            for line in klines:
                try:
                    date, close = line.split(",")
                    out.append((date, float(close)))
                except Exception:
                    pass
            if out:
                return out
        except Exception:
            pass
        time.sleep(1.2)
    return []


def build_snapshot():
    """汇总一次完整快照：指数 + 行业/概念板块 + 资金 + 技术(MA)。

    重要：东方财富免费接口对"同时并发"的请求会限流/返回空(实测 ThreadPoolExecutor
    并发会令板块列表返回 0 条)，因此各取数项**串行**执行，仅用重试 + 小幅错峰提速。
    串行实测 ~3-5s，远低于并发方案的偶发超时，且数据完整(行业~496/概念~504)。
    """
    snap = {
        "ts": int(time.time() * 1000),
        "indices": [], "industries": [], "concepts": [],
        "sh_index": {"ma20": None, "ma60": None, "close": None},
        "market_main_net": 0.0,
        "breadth": None,
        "ok": True, "errors": [],
    }
    # 1) 核心指数(内部已串行+重试)
    try:
        snap["indices"] = fetch_indices()
    except Exception as e:
        snap["errors"].append(f"indices:{e}")

    # 2) 行业板块：优先复用「高频资金快照」的板块数据(避免重复抓东方财富被限流)
    try:
        _cind, _ccon = get_cached_capital(120)
        if _cind:
            ind = _cind
        else:
            ind = fetch_sectors("m:90+t:2", 100, "f3", 1, 6)
            if not ind:
                time.sleep(0.6)
                ind = fetch_sectors("m:90+t:2", 100, "f3", 1, 6)
        snap["industries"] = ind
    except Exception as e:
        snap["errors"].append(f"industries:{e}")

    # 3) 概念板块：同上，复用高频缓存
    try:
        _cind2, _ccon = get_cached_capital(120)
        con = _ccon if _ccon else None
        if not con:
            con = fetch_sectors("m:90+t:3", 100, "f3", 1, 6)
            if not con:
                time.sleep(0.6)
                con = fetch_sectors("m:90+t:3", 100, "f3", 1, 6)
        snap["concepts"] = con
    except Exception as e:
        snap["errors"].append(f"concepts:{e}")

    # 4) 上证K线 -> 技术(MA)
    try:
        kl = fetch_kline("1.000001", 65) or []
        closes = [c for _, c in kl]
        snap["sh_index"]["kline"] = closes[-60:] if closes else []
        snap["sh_index"]["close"] = closes[-1] if closes else None
        if len(closes) >= 20:
            snap["sh_index"]["ma20"] = round(sum(closes[-20:]) / 20, 2)
        if len(closes) >= 60:
            snap["sh_index"]["ma60"] = round(sum(closes[-60:]) / 60, 2)
    except Exception as e:
        snap["errors"].append(f"kline:{e}")

    ind = snap["industries"]
    con = snap["concepts"]
    if ind:
        snap["market_main_net"] = sum(x.get("main_net", 0) for x in ind)
        up = sum(1 for x in ind if (x.get("pct") or 0) > 0)
        snap["breadth"] = round(up / len(ind), 4)
    # 细分资金流(主力/机构/散户) + 板块流向排行
    # 仅用行业板块汇总：概念板块大量重叠(同一只股票隶属多个概念)，汇总会严重重复计算导致数值失真
    if ind:
        mf_inst = sum(x.get("inst_net", 0) for x in ind)       # 机构 ≈ 超大单
        mf_main = sum(x.get("main_net", 0) for x in ind)       # 主力 = 超大单+大单
        mf_retail = sum(x.get("mid_net", 0) + x.get("small_net", 0) for x in ind)  # 散户 = 中单+小单
        snap["money_flow"] = {"institution": mf_inst, "main": mf_main, "retail": mf_retail}
        by_flow = sorted(ind, key=lambda x: x.get("main_net", 0), reverse=True)
        by_flow = strip_parent_sectors(by_flow)
        snap["sector_flow_in"] = [
            {"name": x["name"], "pct": x.get("pct"), "main_net": x.get("main_net", 0)}
            for x in by_flow[:10]
        ]
        by_out = sorted(ind, key=lambda x: x.get("main_net", 0))
        by_out = strip_parent_sectors(by_out)
        snap["sector_flow_out"] = [
            {"name": x["name"], "pct": x.get("pct"), "main_net": x.get("main_net", 0)}
            for x in by_out[:10]
        ]

    snap["ok"] = len(snap["indices"]) > 0 and len(snap["industries"]) > 0
    return snap


# ---------- 高频资金流向 / 板块轮动轻量快照 ----------
# 资金流向与板块轮动只依赖行业+概念板块的主力净流入/涨跌，不依赖 K线/新闻。
# 单独高频刷新(默认15s)以贴近"实时"，并复用给完整快照(避免重复抓取东方财富被限流)。
_LAST_CAPITAL = {"ts": 0, "industries": [], "concepts": [], "lock": threading.Lock()}


def strip_parent_sectors(items):
    """移除被其细分行业覆盖的父级板块(如'通信'下已有'通信设备'等)，避免重复统计。

    规则：若某板块名 NAME 是列表中另一板块名 OTHER 的严格前缀(OTHER != NAME 且
    OTHER.startswith(NAME))，则视为父级，予以剔除，只保留细分项。
    """
    names = {x.get("name", "") for x in items}
    return [x for x in items if not any(
        other != x.get("name", "") and other.startswith(x.get("name", ""))
        for other in names
    )]


# 元概念(宽基/指数/外资/风格/事件类)，覆盖全市场、数值巨大，会淹没真正的题材细分，
# 在"概念资金"榜单中予以剔除，只保留题材级细分(CPO/PCB/光模块/5G 等)。
META_CONCEPT_DENY = [
    "融资融券", "融券", "融资", "MSCI", "富时罗素", "罗素", "标普", "标准普尔",
    "股通", "HS300", "沪深300", "上证50", "上证180", "中证", "科创50", "科创100",
    "大盘股", "中盘股", "小盘股", "微盘股",
    "中报预增", "年报预增", "预增", "预减", "中报", "年报", "季报", "分红", "高送转", "送转",
    "百元股", "高价股", "低价股", "破净", "破发",
    "重仓", "社保", "QFII", "信托", "证金", "汇金", "险资",
    "央企", "国企改革", "中字头", "混改",
    "可转债", "转债", "次新", "新股", "ST", "退市",
    "北向", "北证", "龙头", "核心资产", "蓝筹", "权重",
    "深成指", "上证指数", "创业板指",
    # 风格/指数/估值/波动类元概念
    "风格", "深成", "创业板综", "高市净率", "低市净率", "高市盈率", "低市盈率",
    "振幅", "昨日", "红利", "股息", "成长", "价值", "上证", "深证", "成指", "综指",
    "高股息", "低估值", "大盘", "小盘",
]


def is_meta_concept(name):
    return any(kw in name for kw in META_CONCEPT_DENY)


def build_capital_snapshot():
    """轻量快照：仅行业+概念板块的主力净流入/涨跌，供资金流向与板块轮动高频刷新。"""
    snap = {
        "ts": int(time.time() * 1000),
        "industries": [], "concepts": [],
        "market_main_net": 0.0, "market_total_net": 0.0, "breadth": None,
        "money_flow": {"institution": 0.0, "main": 0.0, "retail": 0.0},
        "sector_flow_in": [], "sector_flow_out": [],
        "concept_flow_in": [], "concept_flow_out": [],
    }
    try:
        ind = fetch_sectors("m:90+t:2", 100, "f3", 1, 5)
    except Exception:
        ind = []
    try:
        con = fetch_sectors("m:90+t:3", 100, "f3", 1, 5)
    except Exception:
        con = []
    snap["industries"] = ind
    snap["concepts"] = con
    if ind:
        snap["market_main_net"] = sum(x.get("main_net", 0) for x in ind)
        snap["market_total_net"] = sum(x.get("total_net", 0) for x in ind)
        up = sum(1 for x in ind if (x.get("pct") or 0) > 0)
        snap["breadth"] = round(up / len(ind), 4) if ind else None
        mf_inst = sum(x.get("inst_net", 0) for x in ind)
        mf_main = sum(x.get("main_net", 0) for x in ind)
        mf_retail = sum(x.get("mid_net", 0) + x.get("small_net", 0) for x in ind)
        snap["money_flow"] = {"institution": mf_inst, "main": mf_main, "retail": mf_retail}
        # 行业板块资金流向
        # 流入 Top: 按 total_net(全市场净流入)降序 → 谁钱进得最多
        # 流出 Top: 按 main_net(主力净流入)升序 → 谁主力撤得最狠(total_net 全正时无法反映真实流出)
        by_flow = sorted(ind, key=lambda x: x.get("total_net", 0), reverse=True)
        by_flow = strip_parent_sectors(by_flow)
        snap["sector_flow_in"] = [
            {"name": x["name"], "pct": x.get("pct"), "net": x.get("total_net", 0)}
            for x in by_flow[:10]
        ]
        by_out = sorted(ind, key=lambda x: x.get("main_net", 0))
        by_out = strip_parent_sectors(by_out)
        snap["sector_flow_out"] = [
            {"name": x["name"], "pct": x.get("pct"), "net": x.get("main_net", 0)}
            for x in by_out[:10]
        ]
    if con:
        # 概念板块资金流向: CPO/PCB/光模块/5G 等细分主题
        # 先剔除元概念(宽基/指数/外资/风格类)，只保留题材级细分
        thematic = [x for x in con if not is_meta_concept(x.get("name", ""))]
        by_con = sorted(thematic, key=lambda x: x.get("total_net", 0), reverse=True)
        by_con = strip_parent_sectors(by_con)
        snap["concept_flow_in"] = [
            {"name": x["name"], "pct": x.get("pct"), "net": x.get("total_net", 0)}
            for x in by_con[:10]
        ]
        by_con_out = sorted(thematic, key=lambda x: x.get("main_net", 0))
        by_con_out = strip_parent_sectors(by_con_out)
        snap["concept_flow_out"] = [
            {"name": x["name"], "pct": x.get("pct"), "net": x.get("main_net", 0)}
            for x in by_con_out[:10]
        ]
    # 同步到共享缓存，供完整快照复用(避免重复抓取板块)
    with _LAST_CAPITAL["lock"]:
        _LAST_CAPITAL["ts"] = snap["ts"]
        _LAST_CAPITAL["industries"] = list(ind)
        _LAST_CAPITAL["concepts"] = list(con)
    return snap


def get_cached_capital(max_age=120):
    """返回最近一次轻量快照的板块数据(若未过期)，供完整快照复用。"""
    with _LAST_CAPITAL["lock"]:
        if _LAST_CAPITAL["industries"] and (time.time() * 1000 - _LAST_CAPITAL["ts"]) < max_age * 1000:
            return list(_LAST_CAPITAL["industries"]), list(_LAST_CAPITAL["concepts"])
    return None, None


if __name__ == "__main__":
    s = build_snapshot()
    print("indices:", [(i["name"], i["pct"]) for i in s["indices"]])
    print("industries:", len(s["industries"]), "concepts:", len(s["concepts"]))
    print("SH close/ma20/ma60:", s["sh_index"]["close"], s["sh_index"]["ma20"], s["sh_index"]["ma60"])
    print("breadth:", s["breadth"], "market_main_net(亿):", round(s["market_main_net"]/1e8, 1))
    print("top3 industry:", [(x["name"], x["pct"], round(x["main_net"]/1e8, 2)) for x in s["industries"][:3]])
    print("errors:", s["errors"])
