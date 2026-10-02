# -*- coding: utf-8 -*-
"""
signals.py —— 投资提醒 · 板块资金流信号引擎
==========================================
把「连续净流入看资金、量价配合看趋势、龙头走坏看离场」这套方法论变成可自动判定的规则。
输出两组：持仓主题的【卖出/风险提醒】 + 全市场的【买入候选】。

数据现实（决定哪些规则现在能算、哪些要等积累）：
  - 当日可算：主力(超大+大单)/中单/小单净流入、板块涨跌、市场广度、全市场主力净额。
    字段来自 data_fetcher.fetch_sectors 的 f62~f66，已在快照里。
  - 需多日累计：连续 3/5/10 日净流入、流入转流出、高位滞涨。
    rotation_state.json 只留最后一个快照，所以多日口径依赖 board_flow_daily 表；
    该表从上线当天开始积累，未攒够天数时本模块【不输出】对应信号，只在 pending 里
    说明"已积累 N/3 天"——绝不拿不足的天数硬凑出结论。
  - 做不了：龙头股突破/滞涨、板块内补涨扩散。需要「板块→成分股」映射，
    而东财 cons 接口在本机被限流(见 akshare_provider 顶部实测记录)，同花顺 cons 在
    akshare 1.18.88 不存在。因此这类规则显式列入 unsupported，不假装能算。
  - 市场量能(增量 vs 存量)：用 history.db 里已落库的指数成交额算 5日/20日均量，
    这个不依赖板块资金积累，今天就能用。

口径说明：所有金额字段上游为「元」，本模块统一换算成「亿」再比较。
"""
import os
import json
import math
import time
import datetime

try:
    import json_util
except Exception:
    json_util = None

try:
    import history_db as hdb
except Exception:
    hdb = None

try:
    import data_fetcher as df
except Exception:
    df = None

# 费率/持有天数口径只在 fund_manager 维护一处（REDEEM_FEE_C/A、持仓起点 T+1、
# 空仓期波动成本）。这里复用它，绝不另抄一张费率表——抄一份就会随监管/产品改动漂移。
# fund_manager 顶层只有常量与函数定义、无副作用，且不反向 import signals，无循环依赖。
try:
    import fund_manager as fm
except Exception:
    fm = None

# 场外基金参考库（FUND_SECTOR_MAP/FUND_BROAD）与「决策」页同源，只 import 不复制，
# 避免两页给出互相矛盾的标的。analysis 顶层只依赖 os/json/time，无循环依赖。
try:
    import analysis as ac
except Exception:
    ac = None

_BASE = os.path.dirname(os.path.abspath(__file__))
_FUNDS_PATH = os.path.join(_BASE, "funds.json")

# ---------------- 阈值（集中放这里，便于回测时做参数敏感性） ----------------
CFG = {
    "ind_in_yi": 15.0,      # 行业板块主力净流入"较大"门槛（亿）
    "con_in_yi": 5.0,       # 概念板块体量小，门槛更低
    "xl_out_yi": 5.0,       # 超大单单日流出警戒线（亿）
    "up_pct": 0.5,          # 资金流入需配合的价格涨幅（%）
    "pump_pct": 1.5,        # 涨幅达到此值视为"拉高"，用于判定拉高派发的严重度
    "down_pct": -0.5,       # 资金流出需配合的价格跌幅（%）
    "streak_need": 3,       # "连续多日"最少天数
    "top_n": 12,            # 买入候选只从流入榜前 N 挑，避免长尾噪音
    "breadth_weak": 0.35,   # 上涨行业占比低于此值视为普跌
    "mkt_out_yi": 150.0,    # 全市场主力净流出警戒线（亿）
    "vol_expand": 1.10,     # 5日均量/20日均量 高于此值 = 增量资金入场
    "vol_shrink": 0.85,     # 低于此值 = 存量博弈，板块流入多为轮动而非新增
    # --- 结论（该买还是该卖）---
    "verdict_sell_n": 2,    # 持仓风险提醒达到此条数即倾向「减仓」
    "verdict_buy_score": 6.0,   # 买入候选强度达到此值才给「可买」
    "verdict_fee_ok": 0.5,  # 可接受的赎回费率(%)上限；高于此值视为「动手不划算」
    "verdict_pct_max": 2.5,  # 当日涨幅≥此值＝追高（场外基金按当日收盘确认，成本已被抬高）
    # --- 多日趋势（T+1 基金口径的核心：单日涨跌不可交易，趋势才可交易）---
    "trend_win": 5,         # 趋势窗口：最近 N 个交易日（含当日实时快照）
    "trend_min_hist": 2,    # 至少要有这么多「历史交易日」才谈得上趋势（窗口 cnt >= 3）
    "trend_cum_mult": 2.0,  # 窗口累计净流入门槛 = 单日门槛 × 此系数
    "trend_persist": 0.6,   # 窗口内主力净流入为正的天数占比下限
    "pullback_n": 3,        # 给「趋势中回调」单独留的候选名额（它常挤不进纯排名前 N，却是最好买点）
    "watch_n": 5,           # B3 反转观察类候选上限（不给买入结论，多了只是噪音）
}

# 规则文案：前端「规则说明」折叠区直接引用，保证提示与说明同源
# confirm 字段说明这条规则用的是「单日」还是「多日」数据 —— 结论层据此决定能不能动手：
# 场外基金 T 日下单按 T 日收盘净值确认，**单日数字不可交易**（看到今天涨再买＝按涨完的价成交），
# 所以只有多日确认过的信号才允许改变仓位。
RULES = [
    {"id": "S1", "side": "sell", "name": "散户接盘", "needs": 1, "confirm": "单日",
     "desc": "主力净流出而小单净流入：机构在兑现、散户在接盘，是最常见的出货结构。"},
    {"id": "S2", "side": "sell", "name": "超大单撤离", "needs": 1, "confirm": "单日",
     "desc": "超大单（机构/游资级）单日净流出超警戒线，其信号意义强于小单；"
             "若窗口累计也为净流出则升级为多日确认。"},
    {"id": "S3", "side": "sell", "name": "连续净流出", "needs": 3, "confirm": "多日",
     "desc": "主力连续 3 个及以上交易日净流出，属持续撤离而非单日噪音。"},
    {"id": "S4", "side": "sell", "name": "流入转流出", "needs": 2, "confirm": "多日",
     "desc": "前一日净流入、当日转为净流出且价格走弱：资金方向已翻。"},
    {"id": "S5", "side": "sell", "name": "高位滞涨资金走弱", "needs": 2, "confirm": "多日",
     "desc": "前一日大涨、当日横盘但主力已流出，常是趋势末端。"},
    {"id": "S6", "side": "sell", "name": "市场普跌拖累", "needs": 1, "confirm": "单日",
     "desc": "全市场大幅净流出且上涨行业占比过低，个股/板块信号可信度整体下降。"},
    {"id": "S7", "side": "sell", "name": "拉高派发", "needs": 1, "confirm": "单日",
     "desc": "股价上涨而主力净流出、小单净流入：上涨中派发，散户在追高接盘；"
             "涨幅越大越值得警惕（>1.5% 标记为高风险）。"},
    {"id": "B1", "side": "buy", "name": "多日趋势主线", "needs": 3, "confirm": "多日",
     "desc": "近 %d 日累计主力净流入达门槛、其中多数交易日为正、区间收涨："
             "资金持续进场成主线。**不再看当日涨跌**——单日流入只值一提，不构成买入理由。"
             % CFG["trend_win"]},
    {"id": "B2", "side": "buy", "name": "趋势中回调（T+1 最佳买点）", "needs": 3, "confirm": "多日",
     "desc": "满足 B1 的趋势条件下，当日回调（涨跌 ≤ 0）：区间在涨、今天回一口气，"
             "而场外基金按当日收盘净值确认——在回调日买比在放量上涨日买成本更低。"},
    {"id": "B3", "side": "buy", "name": "流出转大幅流入", "needs": 2, "confirm": "多日",
     "desc": "窗口前期持续净流出、近端转为大幅净流入且价格转强：资金重新集结的初段，"
             "趋势尚未确立，只作观察不追高。"},
]

UNSUPPORTED = [
    "龙头股放量突破/滞涨、板块内补涨扩散：需「板块→成分股」映射，"
    "本机东财成分股接口被限流、akshare 1.18.88 无同花顺成分股函数，故暂不实现。",
    "资金与价格底背离/顶背离（MFI 等）：需板块指数价格序列，当前板块历史通道不稳定。",
]

NOTES = [
    "**交易规则决定看什么**:场外基金 T 日 15:00 前下单按 T 日收盘净值确认、T+1 才生效。"
    "等看到「今天涨了」再买，成交价已经是涨完的价 —— 所以单日涨跌对基金买点没有可操作性，"
    "本引擎一律用**多日趋势**决定方向、单日涨跌只决定介入时机（回调买、追高等回踩）。",
    "资金流向有滞后性：股价常先于资金数据变动，它更适合【确认趋势】而非抄底。",
    "单日数据噪音大：本引擎要求 3 日以上的持续流入/流出才给「可买/减仓」结论，"
    "单日信号只作提醒、不作动手依据。",
    "须多维验证：资金流只是辅助，请结合技术面、产业逻辑与整体环境，勿单指标决策。",
    "本页面为规则化提示，不构成投资建议。",
]

# 结论卡底部恒定展示的交易口径。写成常量而不是散在各分支里拼，是为了保证
# 「买入/卖出/持有」任何结论下用户都能看到同一条前提——它就是"为什么不能看单日"的答案。
T1_NOTE = ("交易口径：场外基金 T 日 15:00 前下单按 T 日收盘净值确认、T+1 生效，"
           "当日涨跌不构成买卖理由 —— 本结论只看多日趋势（资金 + 价格），单日信号仅作提示。")


def _yi(v):
    """元 -> 亿。非数值返回 0。"""
    try:
        return float(v) / 1e8
    except (TypeError, ValueError):
        return 0.0


def _r2(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


def _r4(v):
    """金额换算保留 4 位小数：_comb 会把「亿」再乘回 1e8，
    若中途只留 2 位会引入约 50 万元的量化误差，影响阈值比较。"""
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return 0.0


def _num1(v):
    """展示用：保留 1 位小数，缺失/非法返回「—」（别把 None 打印成 0.0）。"""
    try:
        return "%.1f" % float(v)
    except (TypeError, ValueError):
        return "—"


# 交易日缓存：key=自然日，val=该日或之前最近的交易日。
# 用多键而非单键——盘前推算会同时问「今天」和「昨天」，单键会被互相顶掉，白查一遍库。
_TRADE_DATE_CACHE = {}
_TRADE_DATE_CACHE_MAX = 64


def _trade_date(d=None):
    """返回 <= d 的最近交易日（YYYY-MM-DD）。日历不可用时回落自然日。

    为什么不能直接用 date.today()：周末/节假日/盘前访问时，行情接口返回的是
    上一个交易日的收盘数据。若按自然日归档，周五的数据会被写成「周六」，
    又因收盘后 settled=1 被永久固化 —— 多日序列因此错位、天数虚高。
    查 trade_calendar（ETL 已灌满、含未来日期），拿不到才回落。
    """
    day = d or datetime.date.today()
    key = day.isoformat()
    hit = _TRADE_DATE_CACHE.get(key)
    if hit:
        return hit
    got = None
    if hdb is not None:
        try:
            conn = hdb.connect()
            try:
                got = hdb.last_trade_date(conn, key)
            finally:
                conn.close()
        except Exception:
            got = None
    if not got:
        # 只缓存查得到的结果：若因库异常回落自然日，绝不能把这个错误结果
        # 缓存一整天——那正是「周五数据写成周六并永久固化」的成因。
        return key
    if len(_TRADE_DATE_CACHE) >= _TRADE_DATE_CACHE_MAX:
        _TRADE_DATE_CACHE.clear()
    _TRADE_DATE_CACHE[key] = got
    return got


def _today():
    """当前应归档的交易日（= 最近交易日）。周末访问也不会写成周末。

    只做「自然日 → 最近交易日」的日期映射，**不含时间语义**，因此与运行时刻无关，
    可以安全地作为测试数据的基准。盘前的时间修正见 _snapshot_date()。
    """
    return _trade_date()


def _snapshot_date(now=None):
    """这份实时资金快照实际代表哪个交易日。

    与 _today() 的差别：_today() 只做日期映射；但开盘前(09:30 前)行情接口返回的
    仍是【上一交易日】的收盘快照，此时它归属上一交易日而非今天。若按今天归档，
    昨天的数字会被记成今天，整条多日序列错位一天。

    归档、历史排除、当日拼接三处必须同用本函数——只改其中一处会让同一天被算两次
    （既进历史序列、又以「当日」身份拼一次），连续净流入信号因此虚增。
    """
    now = now or datetime.datetime.now()
    today = now.date()
    td = _trade_date(today)
    if td != today.isoformat():
        return td                                    # 周末/节假日：最近交易日已是过去
    if (now.hour, now.minute) < (9, 30):
        return _trade_date(today - datetime.timedelta(days=1)) or td
    return td


def _is_settled_now(now=None):
    """快照归属的那个交易日是否已收盘定型，可写 settled=1。

    归属日早于今天（盘前/周末/节假日）时，那天当然已经收盘；
    否则看现在是否已过 15:00。这样盘前、周末、节假日都能把上一交易日正确定型，
    而不会把昨天的收盘值以 settled=0 挂在今天名下。
    """
    now = now or datetime.datetime.now()
    sd = _snapshot_date(now)
    if sd != now.date().isoformat():
        return True
    return now.hour >= 15


def is_trading_now(now=None):
    """现在是否处于连续竞价时段（交易日 09:10–15:05，覆盖集合竞价到收盘）。

    用途：让资金流向的高频轮询只在盘中开启，休市/周末降到分钟级。休市时板块数字本来就是
    静止的收盘值，按 10 秒猛抓不会多出任何信息，只会白耗东方财富的限额 —— 本机 9/21 夜里
    被限流、导致整晚板块数据全空，就是这么来的。
    """
    now = now or datetime.datetime.now()
    if _trade_date(now.date()) != now.date().isoformat():
        return False                      # 周末/节假日
    return (9, 10) <= (now.hour, now.minute) <= (15, 5)


def load_funds():
    if json_util:
        d = json_util.safe_load_json(_FUNDS_PATH, default={})
    else:
        try:
            with open(_FUNDS_PATH, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
    return d.get("funds") or []


def _board_index(capital):
    """把行业+概念板块合成一张按名称可查的表。同名以行业口径优先（数值更可信）。"""
    out = {}
    for kind, key in (("industry", "industries"), ("concept", "concepts")):
        for x in (capital.get(key) or []):
            nm = x.get("name")
            if not nm or nm in out:
                continue
            out[nm] = {"name": nm, "kind": kind,
                       "pct": _r2(x.get("pct")),
                       "main": _r4(_yi(x.get("main_net"))),
                       "xl": _r4(_yi(x.get("inst_net"))),
                       "big": _r4(_yi(x.get("big_net"))),
                       "mid": _r4(_yi(x.get("mid_net"))),
                       "small": _r4(_yi(x.get("small_net"))),
                       "total": _r4(_yi(x.get("total_net")))}
    return out


def _match_theme(concepts, board_names):
    """基金概念 -> 实际板块名。双向包含匹配（"PCB" 命中 "PCB概念"，"创新药" 命中 "创新药"）。"""
    hit = []
    for c in (concepts or []):
        c = str(c).strip()
        if not c:
            continue
        for bn in board_names:
            if c == bn or c in bn or bn in c:
                if bn not in hit:
                    hit.append(bn)
                break
    return hit


def board_funds_map(funds, board_names):
    """板块 -> 命中它的持仓基金列表（一对多）。

    为什么要反向表：持仓 4 只基金的概念是重叠的（016874 与 021528 都含
    CPO概念/光通信模块/PCB），卖出信号只报板块名时，用户不知道这条提醒该落到
    哪只基金上、还是都该动。有了这张表，每条信号就能说清「影响持仓：X、Y」。
    """
    m = {}
    for f in (funds or []):
        for bn in _match_theme(f.get("concepts"), board_names):
            m.setdefault(bn, []).append(f)
    return m


def _fund_ctx(fund, conn=None):
    """单只持仓的赎回成本画像。口径全部来自 fund_manager.switch_cost，本函数只做裁剪。

    缺买入日期 / 费率模块不可用时返回 None —— 宁可不显示费率，也不猜一个错数字。
    conn 给了就顺带挂上这只基金**自身**的净值趋势（板块资金只是主题代理，净值才是这笔钱）。
    """
    if fm is None or not fund:
        return None
    try:
        c = fm.switch_cost(fund)
    except Exception:
        return None
    if not c or c.get("days") is None:
        return None
    ctx = {"code": fund.get("code"), "name": fund.get("name"),
           "amount": fund.get("amount"), "days": c.get("days"),
           "fee_rate": c.get("fee_rate"), "fee_amount": c.get("fee_amount"),
           "need_gain": c.get("need_gain"), "warn": bool(c.get("warn"))}
    nav = _fund_nav_trend(conn, fund.get("code"))
    if nav:
        ctx["nav"] = nav
    return ctx


def _exit_of(ctxs):
    """把同一板块的多只受影响基金合成一条「现在动手要付多少」的结论。

    一个板块常同时命中 2 只基金，且两者持有天数可能不同（分批买入）。
    取最保守值：最短持有天数、最高费率（决定是否踩 1.5% 红线）、费率合计金额、
    最高的「需涨超」门槛 —— 只要有一只处在惩罚性费率区，就必须给出警告。
    """
    ctxs = [c for c in (ctxs or []) if c]
    if not ctxs:
        return None
    ds = [c["days"] for c in ctxs if c.get("days") is not None]
    rates = [c["fee_rate"] for c in ctxs if c.get("fee_rate") is not None]
    fees = [c["fee_amount"] for c in ctxs if c.get("fee_amount") is not None]
    gains = [c["need_gain"] for c in ctxs if c.get("need_gain") is not None]
    if not ds:
        return None
    lo, hi = min(ds), max(ds)
    return {"n": len(ctxs), "days": lo,
            "days_range": (str(lo) if lo == hi else "%d~%d" % (lo, hi)),
            "fee_rate": max(rates) if rates else None,
            "fee_amount": _r2(sum(fees)) if fees else None,
            "need_gain": max(gains) if gains else None,
            "warn": any(c.get("warn") for c in ctxs)}


def attach_exit(items, board_funds, conn=None):
    """给每条卖出信号挂上「影响哪些持仓 + 赎回/调仓成本」，就地改并返回 items。

    只在算得出持有天数时才挂 exit；挂不上时字段整体缺失，前端「缺失即不渲染」。
    """
    if not board_funds:
        return items
    for s in items:
        fs = board_funds.get(s.get("board")) or []
        ctxs = [c for c in (_fund_ctx(f, conn) for f in fs) if c]
        if not ctxs:
            continue
        s["funds"] = ctxs
        ex = _exit_of(ctxs)
        if ex:
            s["exit"] = ex
    return items


def record_daily_flow(capital, db=None):
    """把当日板块资金落进 board_flow_daily，供多日信号使用。

    页面每次访问都可安全调用：盘中值互相覆盖刷新，收盘后写的 settled=1 不再被回退。
    归档日期用 _snapshot_date() 而非自然日——盘前/周末落的是上一交易日，
    与 _series/_comb 的排除口径同源，避免同一天被算两次。
    返回 (行业写入数, 概念写入数)；任何异常都不外抛，落盘失败只是少一天样本。
    """
    if hdb is None or not capital:
        return (0, 0)
    # 降级数据（从 board_flow_daily 读回来的收盘快照）绝不能回写。
    # 它带着上一交易日的数字，回写会把那天记到今天名下；而收盘后 settled=1 一旦写死
    # 就是永久脏数据，只能手动删库 —— 与「禁用自然日归档」是同一类事故。
    if capital.get("fallback"):
        return (0, 0)
    try:
        conn = hdb.connect(db)
        try:
            now = datetime.datetime.now()
            settled = _is_settled_now(now)
            d = _snapshot_date(now)
            a = hdb.upsert_board_flow(conn, d, "industry", capital.get("industries") or [], settled)
            b = hdb.upsert_board_flow(conn, d, "concept", capital.get("concepts") or [], settled)
            return (a, b)
        finally:
            conn.close()
    except Exception:
        return (0, 0)


def record_live_flow(db=None):
    """现取一份资金快照并落盘，返回 (行业写入数, 概念写入数)，失败返回 (0, 0)。

    为什么要单独一个入口：夜间任务(fund_nightly / etl_backfill --only flow)与页面
    惰性落盘(maybe_record)走同一段取数+归档逻辑。没有页面访问的日子靠夜间任务补，
    否则 board_flow_daily 断档，「连续 N 日」这类多日信号永远攒不齐。
    """
    try:
        cap = _live_capital(db)
    except Exception:
        cap = None
    if not cap:
        return (0, 0)
    return record_daily_flow(cap, db=db)


def market_regime(capital, conn=None):
    """市场层面环境：广度、全市场主力净额、量能扩张还是萎缩（增量 vs 存量）。"""
    m = {"breadth": None, "main_net_yi": None, "vol_ratio": None, "volume_state": None}
    try:
        b = capital.get("breadth")
        m["breadth"] = round(b, 4) if isinstance(b, (int, float)) else None
        m["main_net_yi"] = _r2(_yi(capital.get("market_main_net")))
    except Exception:
        pass
    # 量能：用已落库的沪深300成交额算 5 日 / 20 日均值，不依赖板块资金积累
    if hdb is None:
        return m
    try:
        close_conn = False
        if conn is None:
            conn = hdb.connect()
            close_conn = True
        try:
            # 指数日线只有 volume 没有 amount（新浪通道），个股两者都有，故 COALESCE。
            rows = conn.execute(
                "SELECT COALESCE(amount, volume) AS v FROM bar_daily "
                "WHERE code='sh000300' AND COALESCE(amount, volume) IS NOT NULL "
                "ORDER BY date DESC LIMIT 20").fetchall()
        finally:
            if close_conn:
                conn.close()
        if len(rows) >= 10:
            amts = [r["v"] for r in rows]              # 最新在前
            short, rest = amts[:5], amts[5:]           # 分子/分母不重叠，否则比率天然回归到 1
            ratio = (sum(short) / len(short)) / (sum(rest) / len(rest)) if rest and sum(rest) else None
            if ratio:
                m["vol_ratio"] = _r2(ratio)
                if ratio >= CFG["vol_expand"]:
                    m["volume_state"] = "增量"
                elif ratio <= CFG["vol_shrink"]:
                    m["volume_state"] = "存量"
                else:
                    m["volume_state"] = "平量"
    except Exception:
        pass
    return m


def _sig(rule_id, side, board, title, evidence, sev="info", strength=None,
         trend=None, confirmed=None):
    r = {"rule": rule_id, "side": side, "board": board.get("name"), "kind": board.get("kind"),
         "title": title, "evidence": evidence, "sev": sev,
         "pct": board.get("pct"), "main": board.get("main")}
    if strength is not None:
        r["strength"] = strength
    if trend is not None:
        # 只下发前端要用的三项多日口径：窗口天数、区间累计涨幅、区间累计净流入(亿)。
        # 有了它们，条目才能显示"近 3 日 +5.7%"而不是只有当日涨跌——这正是把结论从
        # 单日搬到多日的关键证据。
        r["win_days"] = trend.get("cnt")
        r["win_pct"] = trend.get("cum_pct")
        r["win_main"] = trend.get("cum_main")
    if confirmed is not None:
        r["confirmed"] = bool(confirmed)
    return r


def _series(conn, board, limit=12):
    """该板块的历史资金流，按日期升序，且【排除当次快照归属日】。

    为什么排除：当次值我们用的是实时快照，若把同一交易日已落盘的行也带进来，
    会出现「ser[-1] 有时是快照日、有时是前一天」的错位（取决于本次访问前是否落过盘）。
    多日判定统一用 _comb() 把实时快照拼到历史尾部。
    注意排除的日期必须与 _comb 的标注、以及落盘日期同源（都用 _snapshot_date），
    否则盘前会把上一交易日算两次。
    """
    if conn is None:
        return []
    try:
        rows = hdb.board_flow_series(conn, board["kind"], board["name"], limit=limit + 1)
    except Exception:
        return []
    snap = _snapshot_date()
    return [r for r in rows if r.get("date") != snap]


def _comb(board, ser):
    """历史 + 当次实时快照，组成一个连续序列（金额从元换算回亿口径统一由调用方处理）。"""
    today = {"date": _snapshot_date(), "main_net": board["main"] * 1e8, "pct": board["pct"],
             "xl_net": board["xl"] * 1e8, "small_net": board["small"] * 1e8}
    return (ser or []) + [today]


def _trend_of(hist, board, win=None):
    """把「历史（不含快照归属日）+ 当日实时快照」拼成趋势窗口，算出多日指标。

    为什么要有窗口：用户买的是场外基金，T 日 15:00 前下单按 T 日收盘净值确认、T+1 生效。
    单日涨跌因此不可交易（看到"今天涨"再买＝按涨完的价成交），能指导交易的只有
    「这几天资金到底是不是在持续进场」。窗口把当日实时值和已定型的收盘值放在同一条
    时间轴上，给出累计流入、为正天数占比、区间涨跌 —— 单日噪音被自然平均掉。

    hist 取自 hdb.board_flow_recent()（一次性取回全市场，避免逐板块查库）。
    样本不足时 cnt < win，由调用方（_trend_ok）决定是否够格谈趋势，这里不硬凑。
    """
    win = win or CFG["trend_win"]
    rows = (hist or {}).get(board.get("name")) or []
    snap = _snapshot_date()
    ser = [r for r in rows if r.get("date") != snap]
    win_hist = ser[-(win - 1):] if win > 1 else []
    pairs = [((r.get("pct") or 0.0), _yi(r.get("main_net"))) for r in win_hist]
    pairs.append((board.get("pct") or 0.0, board.get("main") or 0.0))
    cum_main = sum(m for _, m in pairs)
    hist_main = [m for _, m in pairs[:-1]]
    return {"win": win, "cnt": len(pairs),
            "cum_main": _r4(cum_main), "cum_pct": _r2(sum(p for p, _ in pairs)),
            "pos_days": sum(1 for _, m in pairs if m > 0),
            "neg_days": sum(1 for _, m in pairs if m < 0),
            "today_pct": board.get("pct") or 0.0,
            "hist_cnt": len(hist_main), "hist_cum_main": _r4(sum(hist_main))}


def _trend_thr(kind):
    """窗口累计净流入门槛：单日门槛 × 系数（行业体量大、概念体量小，沿用同一比例）。"""
    return (CFG["ind_in_yi"] if kind == "industry" else CFG["con_in_yi"]) * CFG["trend_cum_mult"]


def _trend_ok(b):
    """多日趋势成立：样本够 + 累计净流入达线 + 多数交易日为正 + 区间收涨。"""
    t = b.get("trend") or {}
    if t.get("cnt", 0) < CFG["trend_min_hist"] + 1:
        return False                       # 没攒够历史交易日，谈不上"趋势"
    if t.get("cum_main", 0.0) < _trend_thr(b.get("kind")):
        return False
    need_pos = max(2, int(math.ceil(t["cnt"] * CFG["trend_persist"])))
    if t.get("pos_days", 0) < need_pos:
        return False                       # 净流入不连续（靠单日巨额撑起来的累计不算趋势）
    return t.get("cum_pct", 0.0) > 0


def _reversal_ok(b):
    """资金反转初段：窗口历史部分为净流出、近端转为大幅净流入且价格转强。

    与 _trend_ok 互斥地覆盖另一种情形——趋势还没确立（历史累计仍是负的），
    但资金已经开始回流。这种只给"观察"，不给"可买"：单日转向很容易是假动作。
    """
    t = b.get("trend") or {}
    if not t or t.get("hist_cnt", 0) < 1:
        return False
    return (t.get("hist_cum_main", 0.0) < 0
            and (b.get("main") or 0.0) >= _trend_thr(b.get("kind")) / CFG["trend_cum_mult"]
            and (t.get("today_pct") or 0.0) > 0.6)


def _fund_nav_trend(conn, code, win=20):
    """持仓基金**自身**的净值趋势（history.db.fund_nav，每只 500~1200 天）。

    为什么除了板块资金还要看它：板块资金是"这只基金所在主题"的代理指标，而这笔钱
    实际跟的是这只基金的净值——基金经理、个股选择、费率都体现在净值里。两条都走坏
    才是真问题；只有板块走坏、基金自身还稳，就不该急着付赎回费。

    返回 {as_of, ret5, ret20, ma20_gap, dd60}（%）或 None。拿不到就返回 None，
    绝不猜一个数——前端「缺失即不渲染」。
    """
    if hdb is None or conn is None or not code:
        return None
    try:
        rows = hdb.nav_series(conn, code)
    except Exception:
        return None
    navs = [(d, float(n)) for d, n, _ in (rows or []) if n]
    navs = navs[-61:]
    if len(navs) < 6:
        return None
    cur = navs[-1][1]
    if not cur:
        return None

    def _ret(k):
        if len(navs) <= k:
            return None
        base = navs[-1 - k][1]
        return _r2((cur / base - 1.0) * 100.0) if base else None

    tail20 = [n for _, n in navs[-20:]]
    ma20 = sum(tail20) / len(tail20)
    hi60 = max(n for _, n in navs[-60:])
    return {"as_of": navs[-1][0], "ret5": _ret(5), "ret20": _ret(20),
            "ma20_gap": _r2((cur / ma20 - 1.0) * 100.0) if ma20 else None,
            "dd60": _r2((cur / hi60 - 1.0) * 100.0) if hi60 else None}


def sell_signals(boards, held_boards, conn, market, board_funds=None):
    """持仓主题的风险信号。只遍历持仓命中的板块，不遍历全市场，避免噪音。

    board_funds 为「板块 -> 持仓基金」反查表；给了就给每条信号补上
    「影响哪只基金 + 现在赎回要付多少」——否则用户看到"S7 算力概念"也不知道该动谁。

    每条信号带 confirmed（是否被多日数据确认）：S3/S4/S5 本身就是多日口径，
    S1/S2/S6/S7 是单日结构——只有**该板块窗口累计也在净流出**时才升级为多日确认。
    结论层据此决定能不能动手：场外基金按当日收盘净值确认，单日流出就减仓＝卖在
    回调低点还倒付赎回费，所以单日风险只提示、不动手（见 verdict）。
    """
    out = []
    mkt_weak = (market.get("breadth") is not None and market["breadth"] < CFG["breadth_weak"]) \
        or (market.get("main_net_yi") is not None and market["main_net_yi"] < -CFG["mkt_out_yi"])
    hist = {}
    if conn is not None and hdb is not None:
        try:
            hist = hdb.board_flow_recent(conn, limit_days=CFG["trend_win"])
        except Exception:
            hist = {}

    for name in held_boards:
        b = boards.get(name)
        if not b:
            continue
        main, small, xl, pct = b["main"], b["small"], b["xl"], b["pct"]
        w = _trend_of(hist, b, CFG["trend_win"])
        # 多日走坏：窗口样本够、且累计仍在净流出。单日结构与它同向 → 升级为"多日确认"
        mtf_down = (w["cnt"] >= CFG["trend_min_hist"] + 1 and w["cum_main"] < 0)

        # S1/S7：主力净流出 + 小单净流入 = 机构兑现、散户接盘。按价格分两种结构：
        #   横盘或下跌 → S1 散户接盘；上涨 → S7 拉高派发。
        # 原先只有 S1 且带 pct<=up_pct 门槛，会把「上涨中的主力流出」整类漏掉——
        # 而"股价还在涨、主力已在撤"恰恰是最需要提醒的形态（散户正被吸引进场）。
        if main < 0 and small > 0:
            if pct <= CFG["up_pct"]:
                out.append(_sig("S1", "sell", b, "散户接盘结构",
                                "主力净流出 %.1f 亿、小单净流入 %.1f 亿、涨跌 %.2f%%：机构兑现而散户接盘"
                                % (abs(main), small, pct), "danger",
                                trend=w, confirmed=mtf_down))
            else:
                out.append(_sig("S7", "sell", b, "拉高派发",
                                "股价涨 %.2f%% 而主力净流出 %.1f 亿、小单净流入 %.1f 亿："
                                "上涨中派发，散户在追高"
                                % (pct, abs(main), small),
                                "danger" if pct >= CFG["pump_pct"] else "warn",
                                trend=w, confirmed=mtf_down))
        if xl < -CFG["xl_out_yi"]:
            out.append(_sig("S2", "sell", b, "超大单大幅撤离",
                            "超大单净流出 %.1f 亿（阈值 %.0f 亿）、涨跌 %.2f%%"
                            % (abs(xl), CFG["xl_out_yi"], pct),
                            "danger" if xl < -CFG["xl_out_yi"] * 2 else "warn",
                            trend=w, confirmed=mtf_down))

        ser = _series(conn, b, limit=CFG["streak_need"] + 2)
        comb = _comb(b, ser)          # 历史(不含今日) + 当日实时 = 连续序列
        if len(comb) >= CFG["streak_need"]:
            tail = comb[-CFG["streak_need"]:]
            if all((r["main_net"] or 0) < 0 for r in tail):
                cum = sum((r["main_net"] or 0) for r in tail) / 1e8
                out.append(_sig("S3", "sell", b, "连续 %d 日主力净流出" % CFG["streak_need"],
                                "近 %d 个交易日累计净流出 %.1f 亿，且价格 %.2f%%：属持续撤离"
                                % (CFG["streak_need"], abs(cum), pct), "danger",
                                trend=w, confirmed=True))
        if len(comb) >= 2:
            prev, cur = comb[-2], comb[-1]
            if (prev["main_net"] or 0) > 0 and (cur["main_net"] or 0) < 0 and pct < 0:
                out.append(_sig("S4", "sell", b, "资金由流入转流出",
                                "前一日净流入 %.1f 亿 → 当日净流出 %.1f 亿，价格 %.2f%%"
                                % ((prev["main_net"] or 0) / 1e8, abs(cur["main_net"] or 0) / 1e8, pct),
                                "warn", trend=w, confirmed=True))
            if (prev.get("pct") or 0) > 1.0 and abs(pct) <= 0.5 and main < 0:
                out.append(_sig("S5", "sell", b, "高位滞涨且资金走弱",
                                "前一日涨 %.2f%%、今日仅 %.2f%% 但主力净流出 %.1f 亿"
                                % (prev.get("pct") or 0, pct, abs(main)), "warn",
                                trend=w, confirmed=True))
        if mkt_weak and main < 0:
            out.append(_sig("S6", "sell", b, "市场普跌叠加板块失血",
                            "全市场主力净流出 %.1f 亿、上涨行业占比 %s，本板块再流出 %.1f 亿"
                            % (abs(market.get("main_net_yi") or 0),
                               ("%.0f%%" % (market["breadth"] * 100)) if market.get("breadth") is not None else "—",
                               abs(main)), "warn", trend=w, confirmed=mtf_down))
    return attach_exit(out, board_funds, conn)


SELL_RULES = [
    ("S1", "散户接盘结构"), ("S2", "超大单撤离"), ("S3", "连续净流出"),
    ("S4", "流入转流出"), ("S5", "高位滞涨"), ("S6", "市场普跌"), ("S7", "拉高派发"),
]


def sell_scan(boards, held_boards, market, days, fired=None):
    """卖出规则逐条体检：说明每条为什么没触发、还差几天数据。

    为什么要它：卖出侧为空时，页面原来只剩一句笼统的"未命中风险信号"，用户分不清
    「确实没风险」和「功能坏了」。这里给出每条规则的实测状态与最接近的反例板块，
    页面照着渲染即可——把"为什么没报警"讲清楚，比多报一个假信号更有价值。

    口径与 sell_signals 完全一致：只体检持仓命中的板块。
    """
    fired = set(fired or [])
    held = [boards[n] for n in held_boards if n in boards]
    need = CFG["streak_need"]
    got = days + 1                      # 含当日实时快照，多日判定用的是这个长度
    rows = []

    def add(rule, name, reason):
        rows.append({"rule": rule, "name": name,
                     "status": "fired" if rule in fired else "off",
                     "reason": reason})

    if not held:
        # 两种原因都会让 held 为空，必须分开说：盘后/接口没更新时板块快照本身就是空的，
        # 这时候赖 funds.json 会把人骗去改配置（实际啥都不用改，开盘就好了）。
        if not boards:
            why = "当日板块资金快照为空（盘后或行情接口未更新），拿不到板块就无法体检持仓"
        else:
            why = "funds.json 里没有概念标签，持仓没能映射到板块"
        for rid, nm in SELL_RULES:
            add(rid, nm, why)
    else:
        n = len(held)
        out_b = [b for b in held if b["main"] < 0]                  # 主力净流出的板块
        base = [b for b in out_b if b["small"] > 0]                 # 且小单接盘
        down = [b for b in base if b["pct"] <= CFG["up_pct"]]
        up = [b for b in base if b["pct"] > CFG["up_pct"]]
        cond_s1 = "需涨幅 ≤ +%.1f%%" % CFG["up_pct"]
        cond_s7 = "需涨幅 > +%.1f%%" % CFG["up_pct"]
        if not base:
            if out_b:
                w = min(out_b, key=lambda x: x["main"])
                tail = ("；只有 %s 主力为负（%.1f 亿），但它小单也净流出（%.1f 亿）"
                        % (w["name"], w["main"], w["small"]))
            else:
                tail = "；%d 个持仓板块主力全部净流入，没有机构兑现结构" % n
            add("S1", "散户接盘结构", cond_s1 + tail)
            add("S7", "拉高派发", cond_s7 + tail)
        else:
            if not down:
                w = up[0]
                add("S1", "散户接盘结构",
                    cond_s1 + "；符合结构的 %d 个板块涨幅都超了（%s +%.2f%%），已按 S7 出提醒"
                    % (len(up), w["name"], w["pct"]))
            else:
                add("S1", "散户接盘结构", "")
            if not up:
                w = down[0]
                add("S7", "拉高派发",
                    cond_s7 + "；符合结构的 %d 个板块涨幅未达（%s %+.2f%%），已按 S1 出提醒"
                    % (len(down), w["name"], w["pct"]))
            else:
                add("S7", "拉高派发", "")

        w = min(held, key=lambda x: x["xl"])
        if w["xl"] < -CFG["xl_out_yi"]:
            add("S2", "超大单撤离", "")
        elif w["xl"] >= 0:
            add("S2", "超大单撤离",
                "%d 个持仓板块超大单均为净流入（最小 %s %+.1f 亿）" % (n, w["name"], w["xl"]))
        else:
            add("S2", "超大单撤离",
                "超大单流出最多的 %s 为 %.1f 亿，未到 -%.0f 亿门槛"
                % (w["name"], w["xl"], CFG["xl_out_yi"]))

        add("S3", "连续净流出", "" if got >= need else
            "需连续 %d 个交易日样本，当前只有 %d 个（当日值由实时快照补齐）" % (need, got))
        add("S4", "流入转流出", "" if got >= 2 else
            "需 %d 个交易日做前后对照，当前只有 %d 个" % (2, got))
        add("S5", "高位滞涨", "" if got >= 2 else
            "需 %d 个交易日做前后对照，当前只有 %d 个" % (2, got))

    br, mn = market.get("breadth"), market.get("main_net_yi")
    add("S6", "市场普跌", "" if "S6" in fired else
        "非普跌环境：上涨行业占比 %s、全市场主力 %s 亿"
        % (("%.0f%%" % (br * 100)) if br is not None else "—",
           ("%+.0f" % mn) if mn is not None else "—"))
    return rows


def buy_signals(boards, conn, market):
    """全市场买入候选：以【多日趋势】为主线，单日涨跌只决定介入时机。

    为什么不再拿当日涨跌当门槛（旧版 B1「资金与价格共振」正是"今天涨就让你加仓"的来源）：
    用户买的是场外基金，T 日 15:00 前下单按 T 日收盘净值确认、T+1 才生效。等看到"今天
    涨了"再买，成交价就是涨完的价 —— 单日涨跌对基金买点没有可操作性，只会系统性追高。
    能指导交易的只有「这几天资金到底有没有在持续进场」×「今天是回调还是追高」：
      B1 多日趋势主线  近 N 日累计净流入达线、多数交易日为正、区间收涨 → 方向可介入
      B2 趋势中回调    在 B1 之上当日回调（pct<=0）→ T+1 最好的买点（成本没被抬高）
      B3 资金反转初段  窗口前期净流出、近端转强 → 只作观察，趋势尚未确立
    注意候选必须扫**全市场**而非当日流入榜：趋势中回调的那天，板块当日净流入往往是负的，
    在当日榜上根本看不到 —— 只扫当日榜会恰好漏掉最该买的那些。
    """
    win = CFG["trend_win"]
    hist = {}
    if conn is not None and hdb is not None:
        try:
            hist = hdb.board_flow_recent(conn, limit_days=win)
        except Exception:
            hist = {}
    cands = []
    for b in boards.values():
        if b["kind"] == "concept" and df is not None:
            try:
                if df.is_meta_concept(b["name"]):
                    continue
            except Exception:
                pass
        b = dict(b)
        b["trend"] = _trend_of(hist, b, win)
        cands.append(b)

    ok = [b for b in cands if _trend_ok(b)]
    # 强度用「占窗口最大累计流入的比例」归一到 0~10，跨行情日可比；
    # 绝对门槛只决定"够不够格进榜"，不决定分数。
    top_cum = max([b["trend"]["cum_main"] for b in ok] + [0.0])

    def _score(b):
        t = b["trend"]
        st = (t["cum_main"] / top_cum * 8.0) if top_cum else 0.0
        st *= 0.85 + 0.3 * (t["pos_days"] / float(max(1, t["cnt"])))   # 持续性加权
        if b["xl"] > 0:
            st *= 1.15
        if b["small"] < 0:
            st *= 1.15
        if market.get("volume_state") == "存量":
            st *= 0.7   # 存量博弈下的板块流入更可能是轮动而非新增资金
        return min(10.0, st)

    ranked = sorted(ok, key=lambda b: -_score(b))
    picks = list(ranked[:CFG["top_n"]])
    # 回调位单独留名额：T+1 最好的买点常常是"趋势向上、但今天回了一口气"的板块，
    # 它的累计流入未必挤得进前 N（回调日本身净流入往往很小甚至为负），纯按排名会把它筛掉。
    for b in [x for x in ranked if x["trend"]["today_pct"] <= 0][:CFG["pullback_n"]]:
        if b not in picks:
            picks.append(b)

    out = []
    for b in picks:
        t = b["trend"]
        st = _score(b)
        ev = ("近 %d 日累计主力净流入 %.1f 亿（%d/%d 天为正）、区间 %+.2f%%："
              "属持续流入而非单日现象%s"
              % (t["cnt"], t["cum_main"], t["pos_days"], t["cnt"], t["cum_pct"],
                 "；市场存量博弈，已降权" if market.get("volume_state") == "存量" else ""))
        if t["today_pct"] <= 0:
            # 趋势中的回调：场外基金按当日收盘净值确认 → 回调日买入成本更低
            out.append(_sig("B2", "buy", b, "趋势中回调（T+1 最佳买点）",
                            ev + "；当日回调 %.2f%%，按今日收盘净值确认，成本比上涨日低"
                            % t["today_pct"], "ok",
                            strength=_r2(min(10.0, st * 1.15)), trend=t))
        elif t["today_pct"] >= CFG["verdict_pct_max"]:
            # 方向没问题，但今天已经涨太多：按今日收盘确认＝买在情绪高点，只提示等回踩。
            # 分数只小幅折让（结论层会跳过追高候选去找"当日回调"的那个），避免它从榜上消失。
            out.append(_sig("B1", "buy", b, "多日趋势主线（当日已追高）",
                            ev + "；但当日已涨 %.2f%%（≥%.1f%%），等回踩再介入"
                            % (t["today_pct"], CFG["verdict_pct_max"]), "info",
                            strength=_r2(min(10.0, st * 0.85)), trend=t))
        else:
            out.append(_sig("B1", "buy", b, "多日趋势主线",
                            ev + "；当日 %+.2f%% 未追高，可分批介入" % t["today_pct"], "ok",
                            strength=_r2(st), trend=t))

    for b in [x for x in cands if _reversal_ok(x)][:CFG["watch_n"]]:
        t = b["trend"]
        out.append(_sig("B3", "buy", b, "流出转为大幅流入",
                        "此前 %d 个交易日累计净流出 %.1f 亿 → 当日净流入 %.1f 亿、涨 %.2f%%："
                        "资金重新集结，趋势尚未确立，只作观察"
                        % (t["hist_cnt"], abs(t["hist_cum_main"]), b["main"], b["pct"]),
                        "info", trend=t))
    out.sort(key=lambda x: -(x.get("strength") or 0))
    return out


# 结论阶梯：前端「规则说明」折叠区直接引用，让用户能逐条对照"这个结论是怎么来的"。
VERDICT_LADDER = [
    ("减仓", "持仓主题命中**已被多日确认**的风险（danger 级，或多日确认风险 ≥%d 条），"
             "且受影响基金已过惩罚性费率区（赎回费 ≤%.1f%%）——此时减仓成本可接受。"
             "单日流出不算，因为场外基金按当日收盘净值赎回，等于卖在回调低点还倒付赎回费。"
             % (CFG["verdict_sell_n"], CFG["verdict_fee_ok"])),
    ("暂不动手", "风险已被多日确认，但受影响基金未满 7 日（赎回费 1.5%% 监管红线）、"
                 "或赎回费高于 %.1f%%：此时减仓本身就是负期望，先记下、等过费率线再说。"
                 % CFG["verdict_fee_ok"]),
    ("可买", "无多日确认的持仓风险，且候选板块是**多日趋势**（近 %d 日累计净流入达线、"
             "多数交易日为正、区间收涨），趋势强度 ≥%.1f 分、市场未普跌、非存量博弈、"
             "候选当日涨幅 <%.1f%%（不追高）。"
             % (CFG["trend_win"], CFG["verdict_buy_score"], CFG["verdict_pct_max"])),
    ("加仓", "同「可买」，且趋势最强的板块正是你已持有的主题——优先加已有持仓，"
             "不新开同名主题的第二只基金（重复暴露 + 多付一份空仓期成本）。"),
    ("等回踩", "趋势成立且够强，但候选板块当日已涨 ≥%.1f%%：按今日收盘净值确认＝买在情绪高点，"
               "结论降级为等回踩，等回调日再买（趋势中的回调才是 T+1 最好的买点）。"
               % CFG["verdict_pct_max"]),
    ("持有", "多日趋势不支持动手（无达线机会、或市场普跌/存量博弈），或只有**单日**信号"
             "（单日流入不买、单日流出不减）：不动就是最优解——短期方向准确率上限约 55%，"
             "频繁调仓只会确定性地付手续费。"),
]


def _overseas_vehicle(board_name):
    """板块名 → 可买的场外基金。

    用户只能买场外开放式基金。库与「决策」页同源（analysis.FUND_SECTOR_MAP），
    不在本模块另抄一份标的表。库里 code 为空 = 该行业暂未核验到场外标的，
    此时如实返回 None，绝不硬凑一个能买的代码出去。
    """
    if ac is None or not board_name:
        return None
    try:
        for kw, fname, code in ac.FUND_SECTOR_MAP:
            if kw in board_name and code:
                return {"name": fname, "code": code}
    except Exception:
        return None
    return None


def verdict(sell, buy, market, days=0, need_days=None, held_boards=None):
    """把信号收敛成一句可执行的动作：可买 / 加仓 / 减仓 / 暂不动手 / 等回踩 / 持有。

    判定是显式规则阶梯（阈值全在 CFG，可逐条对照 VERDICT_LADDER），不是打分黑箱。
    **两条贯穿全表的口径**：
      · 多日优先——场外基金 T 日下单按 T 日收盘净值确认、T+1 才生效，单日涨跌不可交易。
        所以「买」要求候选带多日趋势证据（B1/B2），「卖」要求风险被多日确认（confirmed）：
        **单日流入不买、单日流出不减，只提示**。这正是修掉"当天涨就加仓、当天跌就减仓"的地方。
      · 成本把关——踩 <7 日 1.5% 红线、或费率高于 verdict_fee_ok 时，减仓本身是负期望，
        结论降级为「暂不动手」，并把"等多久"讲清楚。
    阶梯顺序：
      1) 风险占优（danger 且多日确认，或多日确认风险 ≥ verdict_sell_n）→ 减仓 / 暂不动手
      2) 机会占优（候选是多日趋势 B1/B2 且强度达线、市场不普跌、非存量）→ 可买 / 加仓 / 等回踩
      3) 其余 →「持有」（含"只有单日信号"与"趋势样本未攒够"两种不同原因，理由里说明白）
    """
    sell = sell or []
    buy = buy or []
    market = market or {}
    held_boards = held_boards or []
    need_days = need_days or CFG["streak_need"]
    danger = [s for s in sell if s.get("sev") == "danger"]
    conf = [s for s in sell if s.get("confirmed")]              # 被多日数据确认过的风险
    conf_danger = [s for s in danger if s.get("confirmed")]
    chase_lim = CFG["verdict_pct_max"]

    def _chase(x):
        return (x.get("pct") or 0.0) >= chase_lim

    # 候选选择：优先"当日未追高"的（可动手），追高的只作为「等回踩」的说明对象——
    # 否则一个今天已涨 4% 的板块会盖过另一个正好回调、T+1 成本更低的买点。
    # buy_signals 已按强度降序，这里只做筛选不重排。
    top = (([x for x in buy if not _chase(x)]) or buy or [None])[0]
    top_score = (top or {}).get("strength") or 0.0
    top_pct = (top or {}).get("pct") or 0.0

    br, mn = market.get("breadth"), market.get("main_net_yi")
    mkt_weak = ((br is not None and br < CFG["breadth_weak"])
                or (mn is not None and mn < -CFG["mkt_out_yi"]))
    diluted = market.get("volume_state") == "存量"

    exits = [s["exit"] for s in sell if s.get("exit")]
    fee_max = max([e.get("fee_rate") or 0.0 for e in exits], default=None)
    need_gain = max([e.get("need_gain") or 0.0 for e in exits], default=None)
    penalty = any(e.get("warn") for e in exits)

    # 风险落到具体持仓：同一只基金被多条规则命中去重，保留最严重的那条（danger 优先）。
    # 只取「驱动结论的那批」信号——减仓是多日确认驱动的，若把单日信号也混进处置清单，
    # 就会出现"结论说按多日确认减仓、清单里却列着一条单日信号"的自相矛盾。
    # 单日信号照旧在信号列表里展示（前端带「单日」标记），只是不参与结论。
    drv = conf if conf else sell
    _seen = {}
    for s in drv:
        for c in (s.get("funds") or []):
            k = c.get("code")
            cur = _seen.get(k)
            if cur is None or (s.get("sev") == "danger" and cur["sev"] != "danger"):
                _seen[k] = dict(c, board=s.get("board"), rule=s.get("rule"),
                                title=s.get("title"), sev=s.get("sev"))
    hold_actions = sorted(_seen.values(),
                          key=lambda x: (x.get("sev") != "danger", -(x.get("fee_amount") or 0)))
    # 同一板块常命中 2 只基金，逐基金罗列会把同一条原因重复三遍 → 按 (板块, 规则) 去重
    worst_items, _wk = [], set()
    for h in hold_actions:
        k = (h.get("board"), h.get("rule"))
        if k in _wk:
            continue
        _wk.add(k)
        worst_items.append("%s %s（%s）" % (h.get("board"), h.get("title"), h.get("rule")))
    worst = "; ".join(worst_items[:3]) or "—"

    def _buy_item(b):
        v = _overseas_vehicle(b.get("board"))
        return {"board": b.get("board"), "kind": b.get("kind"),
                "strength": b.get("strength") or 0.0, "pct": b.get("pct"), "rule": b.get("rule"),
                "win_days": b.get("win_days"), "win_pct": b.get("win_pct"),
                "vehicle": v, "etf": (v or {}).get("name"), "code": (v or {}).get("code"),
                "held": b.get("board") in held_boards}

    reasons, buy_actions = [], []
    # 风险要"能被多日确认"才允许改变仓位：单日流出就减仓＝按今天跌完的净值赎回，还倒付赎回费
    risk_dominant = bool(conf_danger) or len(conf) >= CFG["verdict_sell_n"]

    if risk_dominant:
        days_known = [h["days"] for h in hold_actions if h.get("days") is not None]
        if penalty:
            action, tone = "暂不动手", "watch"
            headline = "风险已多日确认，但赎回费踩了 7 日红线——现在动手是负期望"
            reasons.append("持仓主题命中 %d 条风险提醒（其中 %d 条经多日确认）：%s"
                           % (len(sell), len(conf), worst))
            reasons.append("受影响基金未满 7 日（最短持有 %s 天），赎回费 1.5%% 属惩罚性费率"
                           % (min(days_known) if days_known else "?"))
            reasons.append("先记下风险，等持有满 7 日、费率落到 0.5% 档再决定是否减仓")
        elif fee_max is not None and fee_max > CFG["verdict_fee_ok"]:
            action, tone = "暂不动手", "watch"
            headline = "风险已多日确认，但赎回费 %.2f%% 高于可接受上限 %.2f%%" % (fee_max, CFG["verdict_fee_ok"])
            reasons.append("持仓主题命中 %d 条风险提醒：%s" % (len(sell), worst))
            reasons.append("当前赎回费 %.2f%%、调仓需涨超 %s%%：动手成本吃掉信号空间"
                           % (fee_max, ("%.2f" % need_gain) if need_gain is not None else "—"))
        else:
            action, tone = "减仓", "sell"
            headline = ("持仓出现 %d 条多日确认的风险提醒%s，动手成本可接受"
                        % (len(conf),
                           ("（涉及 %d 只持仓）" % len(hold_actions)) if hold_actions else ""))
            reasons.append("命中并已多日确认的风险提醒：%s" % worst)
            if fee_max is not None:
                reasons.append("受影响基金赎回费 %.2f%%（合计约 ¥%.2f），已过惩罚性费率区"
                               % (fee_max, sum(h.get("fee_amount") or 0 for h in hold_actions)))
            # 板块资金只是"这只基金所在主题"的代理，净值才是这笔钱：两条都走坏才该减。
            # 只有板块走坏、基金自身还稳，就提示可以再等等——避免主题轮动误伤个基。
            navs = [h["nav"] for h in hold_actions if h.get("nav")]
            if navs:
                lo = min(navs, key=lambda n: n.get("ret20") if n.get("ret20") is not None else 0.0)
                reasons.append("基金自身净值（截至 %s）：近 20 日 %+.2f%%，%s"
                               % (lo.get("as_of") or "—", lo.get("ret20") or 0.0,
                                  "与主题同向走坏，减仓理由更充分"
                                  if (lo.get("ret20") or 0.0) < 0 else "尚未走坏，可再观察一两天"))
            reasons.append("建议分批减、不要一次清仓：资金流有滞后，趋势也可能反转")
    elif (top and top_score >= CFG["verdict_buy_score"] and not mkt_weak and not diluted
          and top.get("rule") in ("B1", "B2")):
        v = _overseas_vehicle(top.get("board"))
        is_held = top.get("board") in held_boards
        trend_ev = ("近 %s 日累计净流入 %s 亿、区间 %+.2f%%"
                    % (top.get("win_days") or "?", _num1(top.get("win_main")), top.get("win_pct") or 0.0)) \
            if top.get("win_days") else "多日资金持续净流入"
        if _chase(top):
            action, tone = "等回踩", "watch"
            headline = "%s 趋势成立，但今日已涨 %.2f%%，不追高" % (top.get("board"), top_pct)
            reasons.append("趋势强度 %.1f/10；%s" % (top_score, trend_ev))
            reasons.append("当日已涨 %.2f%%（≥%.1f%%）：按今日收盘净值确认＝买在情绪高点，"
                           "等回踩再介入" % (top_pct, chase_lim))
        elif is_held:
            action, tone = "加仓", "buy"
            headline = "%s 多日趋势最强，且正是你已持有的主题" % top.get("board")
            reasons.append("趋势强度 %.1f/10；%s、今日 %.2f%%" % (top_score, trend_ev, top_pct))
            reasons.append("优先加已有持仓，不新开同名主题的第二只基金（重复暴露 + 多付一份空仓期成本）")
        else:
            action, tone = "可买", "buy"
            headline = "%s 多日趋势成立（强度 %.1f/10），可小仓分批介入" % (top.get("board"), top_score)
            reasons.append("%s、今日 %.2f%%：依据是多日趋势而非单日涨跌" % (trend_ev, top_pct))
            reasons.append("单一行业基金仓位建议不超过总仓 20%，分批而非一次打满")
        reasons.append("场外标的：%s(%s)" % (v["name"], v["code"]) if v
                       else "该板块暂未核验到可买的场外标的（买不了场内 ETF），别硬追")
        buy_actions.append(_buy_item(top))
    else:
        action, tone = "持有", "hold"
        single = [s for s in sell if not s.get("confirmed")]
        if sell and single and not conf:
            # 关键分支：有风险提醒但全是单日结构 —— 明确说"先不动"，而不是含糊的"未达条件"，
            # 否则用户会以为系统默认了单日流出可以减仓。
            headline = "只有 %d 条单日风险信号，未被多日确认——先按兵不动" % len(single)
            reasons.append("单日风险：" + "; ".join("%s %s" % (s.get("board"), s.get("title"))
                                                    for s in single[:3]))
            reasons.append("场外基金按当日收盘净值确认：拿单日流出去减仓＝卖在回调低点还要付赎回费。"
                           "多日是噪音与趋势的分界线，等出现「连续 %d 日净流出」再动手"
                           % CFG["streak_need"])
        elif sell:
            headline = "有 %d 条风险提醒，但未达减仓条件（费率或严重度不够）" % len(sell)
            reasons.append("风险提醒：" + "; ".join("%s %s" % (s.get("board"), s.get("title"))
                                                    for s in sell[:3]))
        elif top and top.get("rule") == "B3":
            headline = "%s 资金刚由流出转为流入，趋势尚未确立——先观察，不追第一天" % top.get("board")
            reasons.append("趋势强度 %.1f/10：单日转向很容易是假动作，等累计净流入转正再谈介入"
                           % top_score)
        elif mkt_weak:
            headline = "市场普跌/资金大幅流出，不动就是最优解"
            reasons.append("上涨行业占比 %s、全市场主力 %s 亿"
                           % (("%.0f%%" % (br * 100)) if br is not None else "—",
                              ("%+.0f" % mn) if mn is not None else "—"))
        elif diluted:
            headline = "存量博弈，板块流入多为轮动而非新增资金"
            reasons.append("5/20 日量能比显示存量：此时追板块流入很容易接在轮动高点")
        elif top:
            headline = "趋势强度不足（%.1f/10 < %.1f），暂不构成买入理由" % (top_score, CFG["verdict_buy_score"])
            reasons.append("最强候选 %s：强度 %.1f、%s、今日 %.2f%%"
                           % (top.get("board"), top_score,
                              ("近 %s 日 %+.2f%%" % (top.get("win_days") or "?", top.get("win_pct") or 0.0))
                              if top.get("win_days") else "多日趋势", top_pct))
        else:
            headline = "没有满足趋势门槛的板块，也无持仓风险"
            reasons.append("多日累计净流入达线且多数交易日为正的板块暂时没有——"
                           "单日流入不构成买入理由，不动就是最优解")
        if top:
            buy_actions.append(_buy_item(top))

    pending = max(0, need_days - 1 - (days or 0))
    note = T1_NOTE
    if pending:
        note += "　多日趋势信号还需 %d 个交易日样本才启用，当前结论只用已有天数的对照数据。" % pending
    return {"action": action, "tone": tone, "headline": headline, "reasons": reasons,
            "hold_actions": hold_actions, "buy_actions": buy_actions,
            "risk_n": len(sell), "danger_n": len(danger), "confirmed_n": len(conf),
            "fee_max": fee_max, "need_gain": need_gain, "pending_days": pending, "note": note}


def _pending(days, held_boards, has_boards=True):
    """说清哪些规则还没启用、为什么。当日值来自实时快照，所以历史只需 days 天。"""
    p = []
    if days < CFG["trend_min_hist"]:
        p.append({"rule": "B1/B2/S3", "title": "多日趋势信号（买入主线的唯一依据）",
                  "why": "已积累 %d/%d 个历史交易日（当日用实时值补齐，收盘后落盘定型）。"
                         "趋势成立至少要 %d 个交易日样本——单日流入不构成买入理由，"
                         "攒够即自动启用" % (days, CFG["trend_min_hist"], CFG["trend_min_hist"] + 1)})
    if days < 1:
        p.append({"rule": "B3/S4/S5", "title": "资金方向翻转与高位滞涨",
                  "why": "需要至少 1 个历史交易日作对照，当前 %d 天" % days})
    # 与 sell_scan 同口径：板块快照为空 ≠ 没配 concepts，别把人骗去改 funds.json。
    # 必须先判 has_boards：快照为空时 held_boards 可能**仍非空**（概念名匹配不到空的板块表，
    # _match_theme 会把概念原样留下），只判 held_boards 会漏报、说不出真正原因。
    if not has_boards:
        p.append({"rule": "S*", "title": "持仓主题卖出提醒",
                  "why": "当日板块资金快照为空（盘后或行情接口未更新），拿不到板块就无法体检持仓；"
                         "开盘后自动恢复"})
    elif not held_boards:
        p.append({"rule": "S*", "title": "持仓主题卖出提醒",
                  "why": "funds.json 里没有概念标签，无法把持仓映射到板块；给持仓补 concepts 后启用"})
    return p


def tips(db=None):
    """对外入口：取板块数据 → 按日落盘 → 算信号。server 的 /api/invest_tips 只调这一个。"""
    cap = _live_capital(db)
    if not cap:
        return {"ok": False, "error": "资金快照不可用", "as_of": time.strftime("%Y-%m-%d %H:%M:%S"),
                "days": 0, "need_days": CFG["streak_need"], "market": {}, "sell": [],
                "sell_scan": [], "buy": [], "verdict": {},
                "data_src": "none", "data_date": None, "stale": True,
                "pending": [], "unsupported": UNSUPPORTED, "notes": NOTES, "rules": RULES, "verdict_ladder": VERDICT_LADDER, "cfg": CFG}
    maybe_record(cap, db=db)
    return compute(cap, db=db)


# 落盘节流：一次要写 ~996 行，页面每 60s 轮询不该重复写；收盘定型值一天只写一次。
_REC = {"date": None, "settled": False, "ts": 0.0}


def maybe_record(capital, db=None):
    """把当次板块资金按日落盘。返回 True 表示本次真的写了。"""
    now_ts = time.time()
    now = datetime.datetime.now()
    snap = _snapshot_date(now)          # 节流键用快照归属日，与落盘日期一致
    settled = _is_settled_now(now)
    if _REC["date"] == snap and (_REC["settled"] or (not settled and now_ts - _REC["ts"] < 120)):
        return False
    a, b = record_daily_flow(capital, db=db)
    if a or b:
        _REC["date"] = snap
        _REC["settled"] = settled
        _REC["ts"] = now_ts
        return True
    return False


def _snapshot_from(ind, con):
    """把 data_fetcher.get_cached_capital() 的 (行业列表, 概念列表) 组装成快照形状。

    注意：get_cached_capital 返回的是二元组，且无数据时是 (None, None)——真值判断会误判，
    所以这里显式按列表处理，不拿它当 dict 用。
    """
    ind = ind or []
    con = con or []
    snap = {"industries": ind, "concepts": con, "market_main_net": 0.0,
            "market_total_net": 0.0, "breadth": None}
    if ind:
        snap["market_main_net"] = sum(x.get("main_net", 0) or 0 for x in ind)
        snap["market_total_net"] = sum(x.get("total_net", 0) or 0 for x in ind)
        up = sum(1 for x in ind if (x.get("pct") or 0) > 0)
        snap["breadth"] = round(up / len(ind), 4)
    return snap


def _boards_from_history(db=None):
    """实时板块拿不到时的降级源：库里最近一个「已收盘定型」交易日的全量板块。

    为什么要降级而不是留空：板块数据一空，整页提醒跟着全哑（买入候选、持仓体检、结论全空），
    用户只会以为功能坏了。而"最近收盘的资金格局"本身是有用信息，看趋势仍然成立。

    但降级数据**绝不冒充实时**：返回值带 fallback.as_of，由 compute() 判断它是
    「同一交易日」（盘前/盘后/周末读昨日收盘 = 就是当前快照本身，可正常用）还是
    「陈旧」——陈旧时只展示、不出买卖结论。
    """
    if hdb is None:
        return None
    try:
        conn = hdb.connect(db)
        try:
            d = hdb.board_flow_latest_settled_date(conn)
            if not d:
                return None
            data = hdb.load_board_flow(conn, d)
        finally:
            conn.close()
    except Exception:
        return None
    if not (data.get("industries") or data.get("concepts")):
        return None
    snap = _snapshot_from(data.get("industries"), data.get("concepts"))
    snap["fallback"] = {"kind": "history", "as_of": d}
    return snap


def _stale_verdict(as_of):
    """实时板块不可用、只能拿陈旧数据时的结论：明确「观望」，并说清为什么不出建议。

    宁可直说"没有实时数据"，也不能让用户拿着昨天的资金流今天下单 —— 那是拿本金试错。
    """
    return {"action": "观望", "tone": "watch",
            "headline": "板块实时数据不可用，页面展示的是 %s 收盘格局 —— 不据此下单" % as_of,
            "reasons": [
                "东方财富板块接口暂时不可用（限流或网络），实时资金流没取到",
                "当前显示的是 %s 已定型的收盘数据：可以看格局，但不代表今天的资金动向" % as_of,
                "用陈旧资金流算买卖点等于拿钱试错，因此结论与买卖信号已暂停，实时源恢复后自动启用",
            ],
            "hold_actions": [], "buy_actions": [], "risk_n": 0, "danger_n": 0,
            "fee_max": None, "need_gain": None, "pending_days": 0, "note": ""}


def _live_capital(db=None):
    """实时资金快照：先复用高频线程缓存 → 再现抓 → 都拿不到回退库里最近收盘数据。

    只有在「实时和降级都没有」时才返回 None。
    """
    if df is not None:
        try:
            ind, con = df.get_cached_capital(max_age=300)
            if ind or con:
                return _snapshot_from(ind, con)
        except Exception:
            pass
        s = None
        try:
            s = df.build_capital_snapshot()
        except Exception:
            s = None
        if s and (s.get("industries") or s.get("concepts")):
            return s
        # 实时为空：先用库里的收盘格局顶上；连历史都没有才把空快照交出去——
        # 空快照能走到「快照为空/开盘后自动恢复」那套自证文案，比一句"不可用"有用。
        return _boards_from_history(db) or s
    return _boards_from_history(db)


def _drop_conflicted(buy, sell):
    """同一板块不得既是「多日确认的风险」又是买入候选。就地不改，返回筛选后的列表。

    否则页面上会同时出现「光纤概念 资金由流入转流出（多日确认）」和
    「光纤概念 趋势中回调（T+1 最佳买点）」两张互相打架的卡——用户根本无从下手
    （线上就同时出现过）。多日确认的风险优先：趋势的最近一天已经翻向净流出、
    且是机构兑现/滞涨结构时，叫它"买点"是自欺。
    """
    bad = set(s.get("board") for s in (sell or []) if s.get("confirmed"))
    if not bad:
        return buy
    return [b for b in (buy or []) if b.get("board") not in bad]


def attach_held(items, board_funds, held_boards):
    """给买入候选标注「这个主题你已经持有」。就地改并返回 items。

    为什么要有：持仓 4 只基金的概念高度重叠（016874 与 021528 都含 CPO/光通信/PCB），
    如果候选正好命中已持有的主题，再开一只新基金 = 重复暴露 + 多付一份空仓期成本。
    结论层遇到这种情况给的是「加仓」（见 verdict()），信号层就必须把同一件事说清楚，
    否则「今日结论」说加仓、下面买入列表却像在推荐新标的，两张卡片自己打架。

    只在能拿到持仓信息时标注；一个持仓都没有时整块不标（前端缺失即不渲染），不猜。
    """
    hold = set(held_boards or [])
    if board_funds:
        hold.update(board_funds.keys())
    if not hold:
        return items
    for x in (items or []):
        bn = x.get("board")
        x["held"] = bn in hold
        if x["held"]:
            fs = (board_funds or {}).get(bn) or []
            if fs:
                x["held_funds"] = [{"code": f.get("code"), "name": f.get("name")}
                                   for f in fs]
    return items


def compute(capital=None, analysis=None, db=None):
    """主入口。任何异常都吞掉并返回空结果——提醒失败不能影响盯盘主流程。"""
    res = {"ok": False, "as_of": time.strftime("%Y-%m-%d %H:%M:%S"),
           "days": 0, "need_days": CFG["streak_need"], "settled": _is_settled_now(),
           "market": {}, "sell": [], "sell_scan": [], "buy": [], "pending": [],
           "verdict": {},
           "data_src": "live", "data_date": _snapshot_date(), "stale": False,
           "unsupported": UNSUPPORTED, "notes": NOTES, "rules": RULES, "verdict_ladder": VERDICT_LADDER, "cfg": CFG}
    try:
        if capital is None:
            capital = _live_capital(db)
        if not capital:
            res["error"] = "资金快照不可用"
            return res

        res["ok"] = True
        # 数据来源与新鲜度：这一页的结论全部建立在板块资金上，必须让用户知道
        # 「这是实时还是收盘、属于哪一天」。降级读回来的历史收盘数据，若其归属日
        # 等于当前快照归属日（盘前/盘后/周末），它就**等同**于实时快照本身，可正常用；
        # 只有日期对不上（盘中实时源断了、只剩旧收盘）才算陈旧，此时停止出建议。
        fb = capital.get("fallback") or {}
        cur = _snapshot_date()
        res["data_src"] = "history" if fb else "live"
        res["data_date"] = fb.get("as_of") or cur
        stale = bool(fb) and fb.get("as_of") != cur
        res["stale"] = stale
        boards = _board_index(capital)
        conn = hdb.connect(db) if hdb else None
        try:
            days = hdb.board_flow_days(conn, True, exclude_date=_snapshot_date()) if conn else 0
            res["days"] = days
            held = []
            funds = load_funds()
            for f in funds:
                held.extend(_match_theme(f.get("concepts"), boards.keys()))
            held = list(dict.fromkeys(held))
            res["held_boards"] = held
            market = market_regime(capital, conn)
            res["market"] = market
            # 卖出信号必须能落到具体持仓上，并带上赎回/调仓成本（小资金频繁调仓=负期望）
            bf = board_funds_map(funds, boards.keys())
            sell = sell_signals(boards, held, conn, market, board_funds=bf)
            res["sell"] = sell
            # 逐条体检：卖出为空时页面要能说清"为什么没报警"，而不是一句笼统的话
            res["sell_scan"] = sell_scan(boards, held, market, days,
                                        [x["rule"] for x in sell])
            # 买入候选若命中已持有主题必须标出来（与结论层的「加仓」口径一致）；
            # 同时剔除已出「多日确认风险」的板块——两张卡不能同时说它该买和该卖。
            res["buy"] = attach_held(
                _drop_conflicted(buy_signals(boards, conn, market), sell), bf, held)
            # 结论放在信号之后算：它是 sell/buy/market 三层信号的收敛，不是第四个信号源
            res["verdict"] = verdict(res["sell"], res["buy"], market, days,
                                     CFG["streak_need"], held)
            res["pending"] = _pending(days, held, bool(boards))
            if stale:
                # 陈旧板块数据不得驱动买卖建议：宁可直说"数据未更新"，也不拿昨天的资金流
                # 给今天下单。展示层仍保留板块格局（看趋势成立），但结论与信号一律停用，
                # 免得用户在"看起来没有风险信号"的页面上做出错误判断。
                res["sell"] = []
                res["sell_scan"] = []
                res["buy"] = []
                res["verdict"] = _stale_verdict(fb.get("as_of"))
                res["pending"].append({
                    "rule": "全部", "title": "板块实时数据不可用",
                    "why": "实时源（东方财富）暂时不可用，页面展示的是 %s 收盘数据；"
                           "为避免陈旧数据误导买卖，结论与买卖信号已暂停，实时源恢复后自动启用"
                           % (fb.get("as_of") or "—")})
        finally:
            if conn:
                conn.close()
    except Exception as e:
        res["error"] = "%s: %s" % (type(e).__name__, e)
    return res


if __name__ == "__main__":
    import sys
    cap = None
    try:
        cap = df.build_capital_snapshot()
    except Exception as e:
        print("快照失败:", e)
    if cap:
        print("落盘:", record_daily_flow(cap))
    r = compute(cap)
    print(json.dumps({k: r.get(k) for k in ("ok", "days", "settled", "market", "held_boards",
                                            "sell", "buy", "pending", "error")},
                     ensure_ascii=False, indent=2))
    sys.exit(0)
