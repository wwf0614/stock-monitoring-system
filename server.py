"""
server.py —— A股大盘盯盘系统 · 本地服务
提供:
  GET /                 仪表盘页面(dashboard.html)
  GET /api/snapshot     JSON: 行情快照 + 低风险分析(含板块轮动)
  GET /api/health       健康检查
特性:
  - 15秒缓存，避免频繁请求东方财富被限流
  - 纯标准库 http.server，无需额外依赖
  - 启动: python server.py [port]
"""
import os
import sys
import json
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# 无控制台环境(如 pythonw / 任务计划后台启动)下，把 print 输出重定向到 server.log，
# 避免日志丢失；有控制台时正常输出。
# 重定向采用可轮转的日志流：server.log 超过 2MB 自动滚动为 .1/.2/.3，避免无限增长。
class _RotatingLog:
    def __init__(self, path, max_bytes=2 * 1024 * 1024, backups=3):
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()
        self._f = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, s):
        with self._lock:
            self._f.write(s)
            self._f.flush()
            try:
                if os.path.getsize(self.path) > self.max_bytes:
                    self._rotate()
            except OSError:
                pass

    def _rotate(self):
        try:
            self._f.close()
        except Exception:
            pass
        for i in range(self.backups - 1, 0, -1):
            src, dst = f"{self.path}.{i}", f"{self.path}.{i + 1}"
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError:
                    pass
        if os.path.exists(self.path):
            try:
                os.replace(self.path, f"{self.path}.1")
            except OSError:
                pass
        self._f = open(self.path, "a", encoding="utf-8", buffering=1)

    def flush(self):
        with self._lock:
            try:
                self._f.flush()
            except Exception:
                pass


if sys.stdout is None:
    _log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.log")
    _logf = _RotatingLog(_log_path)
    sys.stdout = _logf
    sys.stderr = _logf

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import data_fetcher as df
import analysis as an
import news as news_mod
import alerts as alerts_mod
import fund_manager as fm
import trend as trend_mod
import forecast as forecast_mod
import forecast_track as track_mod
import fund_forecast as fund_fc
import wind_provider as wp
import signals as sig_mod

# ---- 访问口令(API token) ----
def load_api_token():
    cfg_path = os.path.join(BASE, "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        t = cfg.get("api_token")
        if t:
            return t
    except Exception:
        pass
    return "ashare-local"  # 兜底(正常已由 config.json 提供)

API_TOKEN = load_api_token()


def get_data_source():
    """数据源选择：eastmoney(默认,零消耗万得) / auto(东财优先,失败兜底万得) / wind(强制万得)。

    万得为试用账号、额度有限，故默认 eastmoney；仅用户显式改配置后才启用万得。
    读配置失败时安全回退 eastmoney，绝不抛异常。
    """
    try:
        cfg_path = os.path.join(BASE, "config.json")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        ds = cfg.get("data_source", "eastmoney")
        if ds not in ("eastmoney", "auto", "wind"):
            return "eastmoney"
        return ds
    except Exception:
        return "eastmoney"


PROTECTED = {"/api/snapshot", "/api/refresh", "/api/funds", "/api/fund_quotes", "/api/board_analysis", "/api/capital_rotation", "/api/index_kline", "/api/forecast_stats", "/api/forecast", "/api/fund_forecast", "/api/invest_tips"}

# 预测命中率回填：每天首次访问统计接口时惰性触发一次(30分钟内不重复)，
# 夜间任务 fund_nightly.py 也会显式调一次，双保险。
_LAST_SETTLE = {"ts": 0.0}
_SETTLE_MIN_GAP = 1800


def maybe_settle_forecasts():
    now = time.time()
    if now - _LAST_SETTLE["ts"] < _SETTLE_MIN_GAP:
        return
    try:
        track_mod.settle_pending()
        _LAST_SETTLE["ts"] = now
    except Exception:
        # 失败(网络/接口)时 10 分钟后重试，不阻塞统计返回
        _LAST_SETTLE["ts"] = now - _SETTLE_MIN_GAP + 600

# 后台刷新间隔：build_payload 依赖东方财富，偶发较慢(数秒~数十秒)，
# 故用后台线程定时刷新，客户端始终拿到已缓存的(近实时)数据，绝不阻塞。
REFRESH_INTERVAL = 40  # 秒（完整快照：含K线/新闻/告警/总结）
CAPITAL_REFRESH_INTERVAL = 10  # 秒（盘中：资金流向+板块轮动 高频轻量通道）
CAPITAL_IDLE_INTERVAL = 300    # 秒（休市/周末：板块资金是静止的收盘值，按 10s 猛抓不产生任何
                               #   新信息，只会白耗东方财富额度——9/21 夜间板块整晚全空即由此而来）
_cache = {"ts": 0, "data": None, "lock": threading.Lock()}
_cap_cache = {"ts": 0, "data": None, "lock": threading.Lock()}
_ready = threading.Event()  # 首次成功构建后置位；预热期间客户端收到 warming_up

# 持仓中期趋势(盘面/各基金净值/主概念)后台预热缓存：board_analysis 只读它，绝不同步计算
_TREND_CACHE = {"ts": 0.0, "data": None, "lock": threading.Lock()}
_TREND_INTERVAL = 300  # 秒：后台每 5 分钟巡检；底层按各自 TTL 增量刷新(多数命中缓存，很快)


def build_payload():
    snap = df.build_snapshot()
    nw = news_mod.fetch_news()
    ana = an.compute_analysis(snap, nw)
    al = alerts_mod.run_alerts(snap, ana)
    return {
        "snapshot": snap,
        "analysis": ana,
        "news": nw,
        "alerts": al,
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _refresh_worker():
    """后台定时刷新完整快照缓存；首次成功后置位 _ready。失败保留上次缓存(优雅降级)。
    盘中按 REFRESH_INTERVAL，休市同样降频（它内部也会抓板块，不降就白耗额度）。"""
    while True:
        try:
            data = build_payload()
            with _cache["lock"]:
                _cache["data"] = data
                _cache["ts"] = time.time()
            _ready.set()
        except Exception as e:
            # 预热阶段若一直失败，保持 warming_up；已就绪后失败则保留旧缓存
            print(f"[refresh] build 失败: {e}")
        time.sleep(_poll_interval(REFRESH_INTERVAL))


def build_capital_payload():
    """高频资金流向+板块轮动载荷：仅轻量快照(行业+概念板块)，并落盘轮动状态。"""
    snap = df.build_capital_snapshot()
    rot = an.update_rotation_state(snap)  # 实时落盘轮动(细粒度)
    # 全量行业板块(含主力净流入)，供基金按"所属板块资金流向"实时调仓
    sectors = [
        {"name": x["name"], "main_net": x.get("main_net", 0.0)}
        for x in snap.get("industries", [])
    ]
    # 全量概念板块实时净进场(名称 -> {net, pct})，供基金「概念敞口」匹配展示
    concept_net = {
        x["name"]: {"net": x.get("total_net", 0.0), "pct": x.get("pct", 0.0)}
        for x in snap.get("concepts", []) if x.get("name")
    }
    return {
        "capital": {
            "market_main_net": snap.get("market_main_net", 0.0),
            "market_total_net": snap.get("market_total_net", 0.0),
            "breadth": snap.get("breadth"),
            "money_flow": snap.get("money_flow", {"institution": 0.0, "main": 0.0, "retail": 0.0}),
            "sector_flow_in": snap.get("sector_flow_in", []),
            "sector_flow_out": snap.get("sector_flow_out", []),
            "concept_flow_in": snap.get("concept_flow_in", []),
            "concept_flow_out": snap.get("concept_flow_out", []),
            "concept_net": concept_net,
        },
        "sectors": sectors,
        "rotation": rot,
        "ts": snap.get("ts"),
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_capital_sector_map():
    """从 15s 高频资本缓存构建 {板块名: 主力净流入_亿元} 与全市场主力净流入(亿元)。
    返回 (sector_map_yi, overall_yi)。基金调仓以它为实时信号源。"""
    p = get_capital_payload()
    if not p:
        return {}, 0.0
    cap = p.get("capital", {})
    m = {x["name"]: x.get("main_net", 0.0) / 1e8 for x in (p.get("sectors") or [])}
    overall = (cap.get("market_main_net") or 0.0) / 1e8
    return m, overall


def _poll_interval(active):
    """后台轮询间隔：交易时段用 active 秒，休市统一降到 CAPITAL_IDLE_INTERVAL。

    为什么必须分开：休市时行情/板块/资金都是静止的收盘值，高频重抓不产生新信息，
    却持续消耗东方财富的请求额度 —— 全天候 10s 轮询等于自己把自己送进限流窗口，
    而一旦被限流，盘中该看的实时数据反而全丢了。盘中频率不变，只是别在没人看的时候猛抓。
    """
    try:
        if sig_mod and sig_mod.is_trading_now():
            return active
    except Exception:
        pass
    return max(active, CAPITAL_IDLE_INTERVAL)


def _cap_refresh_worker():
    """后台刷新资金流向/板块轮动缓存；失败保留上次缓存。盘中高频、休市低频。"""
    while True:
        try:
            data = build_capital_payload()
            with _cap_cache["lock"]:
                _cap_cache["data"] = data
                _cap_cache["ts"] = time.time()
        except Exception as e:
            print(f"[cap-refresh] 失败: {e}")
        time.sleep(_poll_interval(CAPITAL_REFRESH_INTERVAL))


def _trend_refresh_worker():
    """后台预热 + 周期刷新持仓中期趋势，写入 _TREND_CACHE。
    启动即先算一次(预热)，之后每 _TREND_INTERVAL 秒刷新；接口层只读缓存、不阻塞。"""
    while True:
        try:
            rows = fm.merge_with_amounts()
            data = trend_mod.get_trends_for_funds(rows, use_cache=True)
            with _TREND_CACHE["lock"]:
                _TREND_CACHE["data"] = data
                _TREND_CACHE["ts"] = time.time()
        except Exception as e:
            print(f"[trend-refresh] 失败(保留上次缓存): {e}")
        time.sleep(_TREND_INTERVAL)


def _fund_refresh_worker():
    """后台定期刷新基金行情缓存(忽略TTL)，让顶部「我的基金」面板既快又近实时。
    同时刷新盘中实时估算涨幅(重仓股加权)，使基金卡涨幅在交易时段实时滚动。
    并在官方净值一公布就立即结算滚动持仓总值——不必等到夜间 21:30 任务，
    否则「涨幅已切官方值、总值还停在上一个结算日」，市值会短暂退回、自相矛盾。"""
    while True:
        try:
            fm.refresh_fund_cache()
            fm.estimate_all_intraday()
            # 幂等：同一净值日只结算一次(skip by last_settled_nav_date)，无变化不写盘。
            # 官方净值未公布时，nav_date 仍是上一交易日且已结算 → 直接跳过，不做无谓请求。
            try:
                fm.settle_daily(use_cache=True)
            except Exception as e:
                print(f"[fund-refresh] 结算跳过: {e}")
        except Exception as e:
            print(f"[fund-refresh] 失败: {e}")
        time.sleep(20)


# ---- 后台工作线程看门狗 ----
# 三个后台线程均为 while True + 内部 try/except，正常不会因异常退出；
# 但为防止极端情况下线程意外终止后无人重启、导致数据停更，这里加一个看门狗：
# 每 30s 检测一次，发现某线程已死则自动拉起新实例。
_WORKERS = []  # [(name, target, thread)]


def _start_worker(name, target):
    t = threading.Thread(target=target, daemon=True, name=name)
    t.start()
    _WORKERS.append([name, target, t])
    return t


def _watchdog_worker():
    while True:
        time.sleep(30)
        for i, (name, target, t) in enumerate(_WORKERS):
            if not t.is_alive():
                print(f"[watchdog] 检测到工作线程 {name} 已退出，正在重启…")
                nt = threading.Thread(target=target, daemon=True, name=name)
                nt.start()
                _WORKERS[i] = [name, target, nt]


def get_capital_payload(force=False):
    """返回高频资金载荷；force 时同步重建。"""
    if force:
        data = build_capital_payload()
        with _cap_cache["lock"]:
            _cap_cache["data"] = data
            _cap_cache["ts"] = time.time()
        return data
    with _cap_cache["lock"]:
        if _cap_cache["data"] is not None:
            return _cap_cache["data"]
    return build_capital_payload()


def get_payload(force=False):
    """返回缓存数据；force 时同步重建。预热未完成且非 force 返回 None。"""
    if force:
        data = build_payload()
        with _cache["lock"]:
            _cache["data"] = data
            _cache["ts"] = time.time()
        return data
    with _cache["lock"]:
        if _cache["data"] is not None:
            return _cache["data"]
    # 缓存为空(预热中)
    if not _ready.is_set():
        return None
    # 就绪后若间隙为空(理论上不会)，兜底同步构建
    data = build_payload()
    with _cache["lock"]:
        _cache["data"] = data
        _cache["ts"] = time.time()
    return data


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        try:
            qs = parse_qs(urlparse(self.path).query)
            return qs.get("token", [""])[0] == API_TOKEN
        except Exception:
            return False

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in PROTECTED and not self._auth_ok():
            self._send(401, {"error": "unauthorized", "msg": "需要访问口令(token)"})
            return
        if path in ("/", "/index.html"):
            self._serve_dashboard()
        elif path == "/api/snapshot":
            try:
                data = get_payload()
                if data is None:
                    self._send(200, {"status": "warming_up",
                                     "msg": "行情数据初始化中，请稍候刷新…"})
                else:
                    self._send(200, data)
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/api/health":
            self._send(200, {"ok": True, "ready": _ready.is_set(),
                             "time": time.strftime("%H:%M:%S")})
        elif path == "/api/datasource":
            # 数据源状态：当前模式 / 万得是否连上 / WindPy 是否已装入
            self._send(200, {"data_source": get_data_source(),
                             "wind_ready": wp.wind_ready(),
                             "wind_installed": wp.wind_available()})
        elif path == "/api/refresh":
            try:
                data = get_payload(force=True)
                get_capital_payload(force=True)
                self._send(200, data)
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/api/index_kline":
            # 核心指数近期收盘序列(新浪优先+东财兜底, 带TTL缓存)，供仪表盘迷你走势图
            try:
                qs = parse_qs(urlparse(self.path).query)
                try:
                    # 上限放到 250：Hero 走势图默认窗口 60 日，但支持向左平移看更早，
                    # 所以要多取一截(前端默认请求 120 日)留作平移缓冲。
                    lmt = max(10, min(250, int(qs.get("lmt", ["120"])[0])))
                except ValueError:
                    lmt = 120
                series = []
                use_wind = get_data_source() != "eastmoney"
                for secid, name in df.INDICES:
                    try:
                        if use_wind:
                            pts = wp.fetch_kline(secid, lmt=lmt) or []
                        else:
                            pts = df.fetch_kline(secid, lmt=lmt) or []
                    except Exception:
                        pts = []
                    if pts:
                        series.append({"secid": secid, "name": name,
                                       "points": [[d, c] for d, c in pts]})
                self._send(200, {"ok": True, "series": series,
                                 "server_time": time.strftime("%Y-%m-%d %H:%M:%S")})
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/api/capital_rotation":
            try:
                p = get_capital_payload()
                if p is None:
                    self._send(200, {"status": "warming_up",
                                     "msg": "资金流向数据初始化中，请稍候刷新…"})
                else:
                    self._send(200, p)
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/api/invest_tips":
            # 投资提醒：板块资金流信号。内部会「每日首次访问惰性落盘」当日板块资金，
            # 攒够历史后连续多日类信号才自动启用（与预测命中率回填同一套路）。
            try:
                self._send(200, sig_mod.tips())
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/api/funds":
            try:
                funds = fm.load_funds()
                self._send(200, {"funds": funds})
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/api/fund_quotes":
            try:
                rows = fm.merge_with_amounts()  # 命中 TTL 缓存，面板秒开
                # 用 15s 高频资本通道的板块主力净流入，给每只基金标注所属板块与板块资金
                cap_payload = get_capital_payload()
                cap = (cap_payload or {}).get("capital", {})
                smap, overall = get_capital_sector_map()
                fm.attach_sector(rows, smap, overall)
                # 概念敞口实时净进场：匹配基金 concepts 与资本通道的概念板块净进场
                concept_net = cap.get("concept_net", {}) or {}
                for r in rows:
                    cons = fm.fund_concepts(r["code"], r)
                    detail = []
                    agg = 0.0
                    for c in cons:
                        if c in concept_net:
                            cn = concept_net[c]
                            detail.append({
                                "name": c,
                                "net_yi": round(cn.get("net", 0.0) / 1e8, 2),
                                "pct": cn.get("pct", 0.0),
                            })
                            agg += cn.get("net", 0.0)
                    r["concepts"] = cons
                    r["concept_detail"] = detail
                    r["concept_net_yi"] = round(agg / 1e8, 2)
                    # ETF联接/指数联接等无个股持仓的基金：用主概念实时涨跌幅作为盘中估算兜底
                    # (仅限官方净值尚未覆盖最近交易日时；净值已公布则保持净值涨幅，避免覆盖准确值)
                    if r.get("intraday_pct") is None and not r.get("nav_fresh") and detail:
                        cp = detail[0].get("pct")
                        if isinstance(cp, (int, float)):
                            r["intraday_pct"] = round(cp, 2)
                            r["is_live"] = True
                            # 口径必须如实标注：覆盖后的涨幅是「概念板块估算」，不是官方净值。
                            # 原先只改 pct 不改 pct_src，导致明细里把估算值标成「官方净值 X日」。
                            r["pct_src"] = "est"
                            r["est_src"] = "concept"     # 供前端区分「概念板块」与「重仓股加权」
                            # 用概念实时涨幅重算市值(merge 阶段用的是滞后净值)。
                            # 基准必须是未滚入当日涨跌的 amount——用 market_value 会在
                            # 净值滞后但未结算时把当日涨跌复利两次。
                            base = float(r.get("amount") or 0) or float(r.get("market_value") or 0)
                            r["pct"] = round(cp, 2)
                            r["market_value"] = round(base * (1 + cp / 100.0), 2)
                            r["day_pnl"] = round(r["market_value"] - base, 2)
                            # est_pnl 必须跟着走：merge 阶段 est_pnl ≡ day_pnl，
                            # 这里重算了当日口径却留下旧的 est_pnl → 合计会拿它当
                            # 「今日浮动」，于是「市值按今天算、浮动按上一交易日净值算」，
                            # 两个时钟并存（2026-09-23 被老大看出）。
                            r["est_pnl"] = r["day_pnl"]
                total_amount = sum(r["amount"] for r in rows)
                # 合计一律走 total_day_pnl（优先 day_pnl）——与 total_market_value 同源
                total_pnl = fm.total_day_pnl(rows)
                total_market_value = sum((r.get("market_value") or 0) for r in rows)
                snap = fm.latest_snapshot()
                nav_dates = sorted({r.get("nav_date") for r in rows if r.get("nav_date")})
                self._send(200, {
                    "rows": rows,
                    "total_amount": total_amount,
                    "total_pnl": total_pnl,
                    "total_market_value": total_market_value,
                    "nav_dates": nav_dates,
                    "last_sync": snap.get("date") if snap else None,
                    "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/api/board_analysis":
            try:
                p = get_payload()
                if p is None:
                    self._send(200, {"status": "warming_up",
                                     "msg": "行情数据初始化中，请稍候刷新…"})
                    return
                a = p["analysis"]
                regime = a["regime"]
                rows = fm.merge_with_amounts()
                # 用 15s 高频资本通道的板块主力净流入，驱动基金调仓建议(基金=板块篮子)
                smap, overall = get_capital_sector_map()
                # 中期趋势(1-3 个月)：盘面宽基 + 各基金净值/主概念板块；带 TTL 缓存，命中秒级
                # 中期趋势：只读后台预热缓存，绝不在此同步计算(冷启动/概念K线限流时降级为当日信号)
                with _TREND_CACHE["lock"]:
                    trends = _TREND_CACHE["data"]
                # 基金级预测：逐只对「真实持仓」用净值/主概念序列研判 + 概率区间
                try:
                    ff = fund_fc.forecast_funds(rows)
                    ff_map = {x["code"]: x for x in ff.get("funds", []) if x.get("ok")}
                except Exception:
                    ff, ff_map = {"ok": False, "funds": [], "portfolio": {}}, {}
                # 赛道新闻情绪：把全市场新闻按用户赛道(AI算力/半导体/CPO/PCB/创新药)归集
                try:
                    _news_items = (p.get("news") or {}).get("items") or []
                    _tracks = news_mod.concepts_to_tracks(
                        [c for r in rows for c in (fm.fund_concepts(r["code"], r) or [])])
                    news_topic = news_mod.topic_sentiment(_news_items, _tracks or None)
                except Exception:
                    news_topic = {"tracks": {}, "top": [], "matched": 0}
                # 概念敞口实时数据（与 /api/fund_quotes 同源）：先取，供 advise 用主概念题材信号 + 调仓表展示
                cap_payload = get_capital_payload() or {}
                cap = cap_payload.get("capital", {})
                concept_net = cap.get("concept_net", {}) or {}
                adv = fm.advise(regime, rows, sector_map_yi=smap, overall_yi=overall,
                                trends=trends, news_label=a.get("news_label"),
                                concept_net=concept_net, fund_forecast_map=ff_map)
                for r in adv.get("items", []):
                    cons = fm.fund_concepts(r["code"], r)
                    detail = []
                    agg = 0.0
                    for c in cons:
                        if c in concept_net:
                            cn = concept_net[c]
                            detail.append({
                                "name": c,
                                "net_yi": round(cn.get("net", 0.0) / 1e8, 2),
                                "pct": cn.get("pct", 0.0),
                            })
                            agg += cn.get("net", 0.0)
                    r["concepts"] = cons
                    r["concept_detail"] = detail
                    r["concept_net_yi"] = round(agg / 1e8, 2)
                # 实时资金面(主力/机构/散户)要点，供下一步方向使用
                cap_payload = get_capital_payload() or {}
                cap = cap_payload.get("capital", {})
                capital_info = {
                    "market_main_net": (cap.get("market_main_net") or 0.0) / 1e8,
                    "money_flow": {k: (v or 0) / 1e8 for k, v in (cap.get("money_flow") or {}).items()},
                }
                nxt = an.build_next_step(a, adv, capital=capital_info,
                                         market_trend=(trends or {}).get("market") if trends else None)
                snap = fm.latest_snapshot()
                self._send(200, {
                    "regime": regime,
                    "regime_label": a["regime_label"],
                    "confidence": a["confidence"],
                    "scores": a["scores"],
                    "recommendation": a["recommendation"],
                    "summary": a["summary"],
                    "next_step": nxt,
                    "fund_advice": adv,
                    "fund_forecast": ff,
                    "news_topic": news_topic,
                    "market_trend": (trends or {}).get("market") if trends else None,
                    "last_sync": snap.get("date") if snap else None,
                    "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/api/forecast_stats":
            # 量化研判的实测命中率：先惰性回填一次(每日一次)，再返回统计
            try:
                maybe_settle_forecasts()
                st = track_mod.build_stats()
                # 反向重校准后的当前参数(样本不足时 applied=False)
                try:
                    st["tuned"] = track_mod.tuned_params()
                except Exception:
                    st["tuned"] = None
                st["server_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self._send(200, st)
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/api/fund_forecast":
            # 基金级预测：对准真实持仓(净值/主概念序列)，含概率化未来区间
            try:
                rows = fm.merge_with_amounts()
                res = fund_fc.forecast_funds(rows)
                res["server_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self._send(200, res)
            except Exception as e:
                self._send(500, {"error": str(e)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path in PROTECTED and not self._auth_ok():
            self._send(401, {"error": "unauthorized", "msg": "需要访问口令(token)"})
            return
        if path == "/api/funds":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                code = body.get("code", "")
                action = body.get("action")
                if action == "reset":
                    ok, res = fm.set_amount(code, body.get("amount", 0))
                elif action == "cost":
                    ok, res = fm.set_cost(code, body.get("cost", 0))
                elif action == "buy":
                    ok, res = fm.buy_more(code, body.get("amount", 0))
                elif action == "sell":
                    ok, res = fm.redeem(code, body.get("amount", 0),
                                        body.get("cur_value"))
                else:
                    ok, res = fm.add_fund(code, body.get("amount", 0),
                                          body.get("category"))
                if not ok:
                    self._send(400, {"error": res})
                else:
                    self._send(200, {"ok": True, "result": res,
                                     "funds": fm.load_funds()})
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/api/forecast":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length).decode("utf-8") or "{}"
                body = json.loads(raw) or {}
                lb = int(body.get("lookback", 20))
                res = forecast_mod.forecast_market(lookback=lb)
                if not res.get("ok"):
                    self._send(200, {"ok": False, "error": res.get("reason", "分析失败")})
                else:
                    self._send(200, {"ok": True, "result": res})
            except Exception as e:
                self._send(500, {"error": str(e)})
        else:
            self._send(404, {"error": "not found"})

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if path in PROTECTED and not self._auth_ok():
            self._send(401, {"error": "unauthorized", "msg": "需要访问口令(token)"})
            return
        if path == "/api/funds":
            try:
                qs = parse_qs(urlparse(self.path).query)
                code = qs.get("code", [""])[0]
                removed = fm.remove_fund(code)
                self._send(200, {"ok": removed, "funds": fm.load_funds()})
            except Exception as e:
                self._send(500, {"error": str(e)})
        else:
            self._send(404, {"error": "not found"})

    def _serve_dashboard(self):
        fp = os.path.join(BASE, "dashboard.html")
        try:
            with open(fp, "r", encoding="utf-8") as f:
                html = f.read()
            self._send(200, html, "text/html; charset=utf-8")
        except Exception as e:
            self._send(500, f"<h1>dashboard.html 缺失: {e}</h1>")

    def log_message(self, fmt, *args):
        pass  # 静默日志


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    # 后台刷新线程(守护)：启动即开始首次构建，之后按各自间隔刷新
    _start_worker("_refresh", _refresh_worker)
    _start_worker("_cap_refresh", _cap_refresh_worker)
    _start_worker("_fund_refresh", _fund_refresh_worker)
    _start_worker("_trend_refresh", _trend_refresh_worker)
    # 看门狗：守护后台线程，意外退出自动重启
    threading.Thread(target=_watchdog_worker, daemon=True, name="_watchdog").start()
    # 仅当数据源含万得(auto/wind)时启动即尝试连接；eastmoney 模式不消耗万得任何额度
    if get_data_source() != "eastmoney":
        try:
            wp.ensure_wind()
        except Exception:
            pass
    # 预热：立即算一次盘中实时估算涨幅，避免首屏为空(后台线程异步，不阻塞启动)
    try:
        fm.estimate_all_intraday()
    except Exception as e:
        print(f"[warmup-intraday] 失败: {e}")
    # 中期趋势改由 _trend_refresh_worker 后台预热 + 周期刷新；board_analysis 只读缓存、不阻塞。
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"📡 A股盯盘系统已启动: http://localhost:{port}  (Ctrl+C 退出)")
    print("   首次行情构建中(后台)，稍候刷新页面即可看到数据…")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        srv.shutdown()


if __name__ == "__main__":
    main()
