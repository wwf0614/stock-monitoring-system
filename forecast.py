# -*- coding: utf-8 -*-
"""forecast.py —— 基于前几天(历史)行情，量化研判后续走势。

与「实时推演」不同，本模块不依赖实时快照是否就绪：直接抓取核心指数历史日K线
(新浪优先 + 东方财富兜底，复用 data_fetcher.fetch_kline)，计算短期动量 / 均线排列 /
波动率，给出短中期研判、建议仓位，并贴合用户持仓基金给出针对性操作建议。

对外：forecast_market(lookback=20) -> dict
"""
import os
import time
import math

try:
    import data_fetcher as df
except Exception:
    df = None

try:
    import fund_manager as fm
except Exception:
    fm = None

# 命中率记录：每次研判落盘，事后回填实际走势，用于校准置信度。
# 该模块不可用(缺失/异常)时不影响研判主流程。
try:
    import forecast_track as _track
except Exception:
    _track = None

_BASE = os.path.dirname(os.path.abspath(__file__))

# 核心研判指数(secid, 名称)；secid 同时兼容新浪(_SINA_MAP)与东方财富
_INDICES = [
    ("1.000300", "沪深300"),   # 大中小盘中枢
    ("1.000001", "上证指数"),
    ("0.399006", "创业板指"),   # 成长/赛道风向
    ("0.399001", "深证成指"),
]

# 同一 lookback 窗口的结果缓存 60s，避免每次点击都重新抓 K 线
_FC_CACHE = {}  # lookback -> (ts, result)
_FC_TTL = 60

# ---------------------------------------------------------------------------
# 波动率自适应参数
# 核心思路：行情波动越快，模型越应该「缩短记忆、降低仓位」，而不是用固定窗口硬扛。
# ---------------------------------------------------------------------------
_TARGET_VOL = 15.0      # 目标年化波动率(%): 波动率目标仓位的锚(沪深300长期约 15-20%)
_BASE_POSITION = 55     # 基准仓位(%): 当前波动恰等于目标波动时的建议仓位
_BASE_LOOKBACK = 20     # 基准回看窗口(交易日)
_MIN_LOOKBACK = 6       # 自适应窗口下限(高波动时快速响应)
_MAX_LOOKBACK = 40      # 自适应窗口上限(低波动时过滤噪音)
_BARS_15M_PER_DAY = 16  # A股每日 4 小时 = 16 根 15 分钟K
_TRADING_DAYS = 242     # 年化交易日数


def parkinson_vol(rows, bars_per_day=_BARS_15M_PER_DAY,
                  trading_days=_TRADING_DAYS):
    """Parkinson 波动率(年化%)：用 K 线 high/low 估计。

    相比收盘价标准差，用极差估计的效率约高 5 倍，样本需求更少、对日内跳变更敏感——
    这正是「行情波动太快」时最需要的度量。rows: [(time,o,h,l,c)]。
    """
    if not rows or len(rows) < 5:
        return None
    s = 0.0
    n = 0
    for _t, _o, h, l, _c in rows:
        if not (l > 0 and h > 0 and h >= l):
            continue
        s += math.log(h / l) ** 2
        n += 1
    if n < 5 or s <= 0:
        return None
    var = s / (4.0 * math.log(2.0) * n)
    per_bar = math.sqrt(var) * 100.0
    return per_bar * math.sqrt(bars_per_day * trading_days)


def realized_vol(closes, n=20):
    """日线已实现波动率(年化%)：收盘价对数收益率的标准差。

    用于确定波动率的「绝对水平」(驱动波动率目标仓位)。
    注意与 parkinson_vol 分工不同：Parkinson 基于分钟 high/low，对盘中变化极敏感，
    但会系统性低估(捕捉不到隔夜跳空)，适合算「相对变化」；本函数用日线收盘，
    是标准口径、跨周期可比，适合定「绝对水平」。
    """
    if not closes or len(closes) < 5:
        return None
    seg = [c for c in closes[-min(n + 1, len(closes)):] if c > 0]
    if len(seg) < 5:
        return None
    rets = [math.log(seg[i] / seg[i - 1]) for i in range(1, len(seg))]
    if len(rets) < 3:
        return None
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
    if var <= 0:
        return None
    return math.sqrt(var) * math.sqrt(_TRADING_DAYS) * 100.0


def efficiency_ratio(vals, n=None):
    """Kaufman 趋势效率比 ER ∈ [0,1] = 净位移 / 路径总位移。

    ER 高(>0.35) → 趋势市，动量信号可信、可持仓；
    ER 低(<0.15) → 震荡市，价格来回拉锯，动量失效、宜降仓做区间。
    样本不足返回 None。
    """
    if not vals or len(vals) < 3:
        return None
    seg = vals[-n:] if (n and n < len(vals)) else vals
    if len(seg) < 3:
        return None
    net = abs(seg[-1] - seg[0])
    path = sum(abs(seg[i] - seg[i - 1]) for i in range(1, len(seg)))
    if path <= 0:
        return None
    return net / path


def adaptive_lookback(vol_ratio, base=_BASE_LOOKBACK):
    """波动率自适应窗口：波动越高窗口越短(快速响应)，波动越低窗口越长(过滤噪音)。

    vol_ratio = 当前波动 / 基准波动；波动翻倍时窗口减半。
    窗口以 base 为锚浮动，并限制在 [base/4, base*2] 内——
    这样既保留用户选择的回看周期(前端 5/20/60/120 日切换仍有意义)，
    又允许波动驱动的自适应伸缩。
    """
    if not vol_ratio or vol_ratio <= 0:
        return base
    lo = max(_MIN_LOOKBACK, int(base / 4))
    hi = max(int(base * 2), lo + 1)
    return int(_clamp(base / vol_ratio, lo, hi))


def vol_target_position(vol_annual, base=_BASE_POSITION, target=_TARGET_VOL):
    """波动率目标仓位(%)：波动越大仓位越低，把组合风险敞口稳定在目标水平。

    这是机构「风险平价 / vol targeting」的标准做法——不预测方向也能改善风险收益比。
    vol_annual 为 None 或非正时回退到基准仓位。
    """
    if not vol_annual or vol_annual <= 0:
        return base
    return int(_clamp(base * target / vol_annual, 10, 90))


def intraday_momentum(m15, m60):
    """日内多周期动量。返回 {'m15_ret', 'm60_ret', 'bar_vol'}。

    m15_ret: 近 8 根 15 分钟(约2小时)涨跌幅——捕捉日内即时动能；
    m60_ret: 近 4 根 60 分钟(约半日)涨跌幅——过滤分钟级噪音。
    日线动量对盘中变化无感，这两项是「行情波动太快」时的主要补位信号。
    """
    out = {"m15_ret": None, "m60_ret": None, "bar_vol": None}
    if m15 and len(m15) >= 9:
        c0, c1 = m15[-9][4], m15[-1][4]
        if c0 > 0:
            out["m15_ret"] = round((c1 / c0 - 1.0) * 100.0, 2)
    if m60 and len(m60) >= 5:
        c0, c1 = m60[-5][4], m60[-1][4]
        if c0 > 0:
            out["m60_ret"] = round((c1 / c0 - 1.0) * 100.0, 2)
    v = parkinson_vol(m15[-16:] if m15 else None)
    if v is not None:
        out["bar_vol"] = round(v, 2)
    return out


def calibrate_confidence(direction_votes, er, vol_ratio):
    """统计口径的置信度(%)，替代原先「60 + composite*4」这类拍脑袋公式。

    direction_votes: (同向票数, 总票数)——多周期方向越一致，置信度越高；
    er: 趋势效率比——趋势市信号可信度更高；
    vol_ratio: 波动倍数——波动越大，同样信号的可靠性越低(噪音占比高)。
    """
    same, total = direction_votes
    if not total:
        return 50
    consistency = same / total                                  # 0~1
    conf = 40.0 + consistency * 25.0                            # 一致性 40~65
    if er is not None:
        conf += _clamp(er, 0.0, 1.0) * 12.0                     # 趋势效率 +0~12
    if vol_ratio and vol_ratio > 1.0:
        conf -= _clamp((vol_ratio - 1.0) * 6.0, 0.0, 12.0)      # 高波动惩罚 -0~12
    return int(_clamp(conf, 35, 88))


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _sma(vals, n):
    if len(vals) < n or n <= 0:
        return None
    return sum(vals[-n:]) / n


def _volatility(vals, n=20):
    if len(vals) < n:
        return None
    seg = vals[-n:]
    m = sum(seg) / n
    if m == 0:
        return None
    var = sum((x - m) ** 2 for x in seg) / n
    return math.sqrt(var) / m * 100.0  # 百分比


# ---------------------------------------------------------------------------
# 增强维度：概率区间 / 市场状态机 / 量价配合 / 跨资产 risk-off
# ---------------------------------------------------------------------------
# 跨资产 risk-off 代理：国债ETF(避险资金) + 黄金ETF(避险资产)。
_CROSS_ASSETS = [("1.511010", "国债ETF", "bond"), ("1.518880", "黄金ETF", "gold")]


def prob_range(composite, vol_annual, horizon=5, trading_days=_TRADING_DAYS):
    """概率化未来区间：期望涨跌幅 + ±1σ(约68%置信) 区间 + 上涨概率。

    价格随机游走近似：σ_H = 日波动 × sqrt(H)；方向仅做弱倾斜(drift)。
    把"点方向"转成"区间+概率"，对决策更有用（可据此判断赔率是否划算）。
    """
    if not vol_annual or vol_annual <= 0:
        return {"exp_pct": None, "low_pct": None, "high_pct": None, "prob_up": None}
    daily = vol_annual / math.sqrt(trading_days)
    sigma = daily * math.sqrt(horizon)
    drift = _clamp(composite / 10.0, -1.0, 1.0) * sigma * 0.5
    pu = (0.5 * (1.0 + math.erf((drift / sigma) / math.sqrt(2.0))) * 100.0
          if sigma > 0 else 50.0)
    return {"exp_pct": round(drift, 2), "low_pct": round(drift - sigma, 2),
            "high_pct": round(drift + sigma, 2), "prob_up": round(pu, 0)}


def detect_market_state(composite, er, vol_ratio, r20):
    """5 态市场状态机 -> (key, label, hint)。

    比单纯"趋势市/震荡市"更细：区分牛/熊/震荡/恐慌，恐慌态需优先控风险而非抄底。
    """
    v = vol_ratio or 1.0
    if v >= 2.0 and r20 is not None and r20 <= -3:
        return "panic", "恐慌(高波动急跌)", "波动骤放+加速下行，优先控风险、勿盲目抄底"
    if er is not None and er >= 0.35:
        if composite >= 3:
            return "bull", "趋势上行(牛市结构)", "趋势成立，可持有并逢回调参与"
        if composite <= -3:
            return "bear", "趋势下行(熊市结构)", "趋势向下，反弹宜降仓、不宜追高"
    if composite >= 3:
        return "weak_up", "弱趋势偏多", "偏多但趋势不强，控制仓位、分批参与"
    if composite <= -3:
        return "weak_down", "弱趋势偏空", "偏空，降低权益敞口、等企稳再动"
    return "range", "区间震荡", "多空均衡，高抛低吸、控制仓位"


def volume_price_signal(secid="1.000001"):
    """量价配合：近5日涨跌 + 量能变化(5日均量/20日均量)。

    价涨量增=健康(资金认同)；价涨量缩=乏力(易回落)；价跌量增=恐慌抛压。
    返回 {ok, r5, vol_ratio, signal, label}。
    """
    if not df:
        return {"ok": False}
    try:
        rows = df.fetch_kline_full(secid, lmt=30) or []
    except Exception:
        rows = []
    if len(rows) < 21:
        return {"ok": False, "reason": "K线样本不足"}
    closes = [r[4] for r in rows]
    vols = [r[5] for r in rows]
    r5 = ((closes[-1] / closes[-6] - 1.0) * 100.0) if closes[-6] else None
    v5 = sum(vols[-5:]) / 5.0
    v20 = sum(vols[-20:]) / 20.0
    vratio = (v5 / v20) if v20 else None
    if r5 is None or vratio is None:
        return {"ok": False}
    if r5 > 0 and vratio >= 1.1:
        sig, lab = "healthy", "价涨量增(健康)"
    elif r5 > 0 and vratio < 0.9:
        sig, lab = "weak", "价涨量缩(乏力)"
    elif r5 < 0 and vratio >= 1.1:
        sig, lab = "selloff", "价跌量增(抛压)"
    else:
        sig, lab = "neutral", "量价平稳"
    return {"ok": True, "r5": round(r5, 2), "vol_ratio": round(vratio, 2),
            "signal": sig, "label": lab}


def cross_asset_riskoff():
    """跨资产 risk-off 信号：国债ETF(避险资金) + 黄金ETF(避险资产) 近5日表现。

    股债跷跷板 + 避险资产走强 → 风险偏好下降。score 越高越偏 risk-off。
    返回 {ok, score(0-10), label, bond_5d, gold_5d}。
    """
    if not df:
        return {"ok": False}
    vals = {}
    for secid, name, tag in _CROSS_ASSETS:
        try:
            kl = df.fetch_kline(secid, lmt=10) or []
        except Exception:
            kl = []
        if len(kl) >= 6:
            try:
                c0, c1 = float(kl[-6][1]), float(kl[-1][1])
                vals[tag] = round((c1 / c0 - 1.0) * 100.0, 2) if c0 else None
            except Exception:
                vals[tag] = None
        else:
            vals[tag] = None
    bond, gold = vals.get("bond"), vals.get("gold")
    if bond is None and gold is None:
        return {"ok": False}
    score = 5.0
    if bond is not None:
        score += _clamp(bond / 0.8, -1.5, 2.0)     # 国债涨 → risk-off
    if gold is not None:
        score += _clamp(gold / 2.0, -1.0, 1.5)     # 黄金涨 → risk-off
    score = round(_clamp(score, 0.0, 10.0), 1)
    label = ("明显避险" if score >= 7 else ("偏避险" if score >= 5.8 else
             ("偏风险偏好" if score <= 3.5 else "中性")))
    return {"ok": True, "score": score, "label": label,
            "bond_5d": bond, "gold_5d": gold}


def _analyze_index(series, m15=None, m60=None):
    """series: 升序 [(date, close)] -> 单指数研判 dict。

    m15/m60: 15/60 分钟K线 [(time,o,h,l,c)]，可选。缺失(接口失败或未映射标的)时
    日内指标为 None，仅用日线研判——保证行情源异常时优雅降级而非整体失败。
    """
    if not series or len(series) < 6:
        return None
    dates = [d for d, _ in series]
    closes = [float(c) for _, c in series]
    n = len(closes)
    last = closes[-1]

    def _ret(k):
        if n <= k:
            return None
        return (last / closes[-1 - k] - 1.0) * 100.0

    r5 = _ret(5)
    r10 = _ret(10)
    r20 = _ret(20)
    ma5 = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    vol = _volatility(closes, min(20, n))

    # 均线多空排列得分
    ma_score = 0.0
    if ma5 and ma10 and ma20:
        if last > ma5 > ma10 > ma20:
            arrange = "多头排列"
            ma_score = 3.0
        elif last < ma5 < ma10 < ma20:
            arrange = "空头排列"
            ma_score = -3.0
        elif last >= ma20 and last >= ma5:
            arrange = "短期偏强"
            ma_score = 1.0
        elif last <= ma20 and last <= ma5:
            arrange = "短期偏弱"
            ma_score = -1.0
        else:
            arrange = "交叉震荡"
            ma_score = 0.0
    else:
        arrange = "样本不足"

    # 日线子评分(0 附近中性，约 -10~+10)
    score = 0.0
    if r5 is not None:
        score += _clamp(r5, -6, 6) * 1.0
    if r10 is not None:
        score += _clamp(r10, -10, 10) * 0.5
    score += ma_score

    # 趋势效率比 ER：判断当前是趋势市还是震荡市(决定动量信号可不可信)
    er = efficiency_ratio(closes, n=min(20, n))
    er_val = round(er, 3) if er is not None else None

    # 日内多周期动量：日线对盘中变化无感，靠分钟线补位
    im = intraday_momentum(m15, m60)
    intr_score = 0.0
    if im["m15_ret"] is not None:
        intr_score += _clamp(im["m15_ret"], -2.0, 2.0) * 1.0
    if im["m60_ret"] is not None:
        intr_score += _clamp(im["m60_ret"], -3.0, 3.0) * 0.6

    if r20 is not None:
        trend = "上行" if r20 > 3 else ("下行" if r20 < -3 else "震荡")
    else:
        trend = "—"

    return {
        "name": None,
        "last": round(last, 2),
        "r5": round(r5, 2) if r5 is not None else None,
        "r10": round(r10, 2) if r10 is not None else None,
        "r20": round(r20, 2) if r20 is not None else None,
        "ma5": round(ma5, 2) if ma5 else None,
        "ma10": round(ma10, 2) if ma10 else None,
        "ma20": round(ma20, 2) if ma20 else None,
        "vol": round(vol, 2) if vol else None,
        "arrange": arrange,
        "trend": trend,
        "score": round(score, 2),
        "er": er_val,
        "intraday": im,
        "intr_score": round(intr_score, 2),
        "first_date": dates[0],
        "last_date": dates[-1],
        "n": n,
    }


def _build_view(regime, direction, conf, indices, vol_now=None, vol_ratio=None,
                regime_type=None, eff_lookback=None):
    core = None
    for it in indices:
        if it["name"] in ("沪深300", "上证指数"):
            core = it
            break
    if not core:
        core = indices[0]
    r5 = core.get("r5")
    r20 = core.get("r20")
    r5s = ("近5日%s%.2f%%" % ("涨" if r5 >= 0 else "跌", abs(r5))) if r5 is not None else "近5日数据不足"
    r20s = ("，近20日%s%.2f%%" % ("涨" if r20 >= 0 else "跌", abs(r20))) if r20 is not None else ""
    if direction == "up":
        short = "短期动能偏强，未来3-5个交易日大概率延续震荡上行，回调可视为低吸机会。"
    elif direction == "down":
        short = "短期动能转弱，未来3-5个交易日或延续弱势，反弹宜降仓而非追高。"
    else:
        short = "多空力量均衡，未来3-5个交易日以区间震荡为主，宜高抛低吸、控制仓位。"
    base = "核心指数(%s)%s%s。%s" % (core["name"], r5s, r20s, short)

    # 波动状态：这是「行情波动太快」时用户最需要看到的判断依据
    vol_txt = ""
    if vol_now and vol_ratio:
        if vol_ratio >= 1.5:
            vt = "波动显著放大(%.1f倍基准)"
        elif vol_ratio >= 1.15:
            vt = "波动偏高(%.1f倍基准)"
        elif vol_ratio >= 0.85:
            vt = "波动正常(%.1f倍基准)"
        else:
            vt = "波动收敛(%.1f倍基准)"
        vol_txt = (" 当前年化波动率约%.0f%%，%s，回看窗口已自适应调整为%d日，"
                   "建议仓位按波动率目标给出。" % (vol_now, vt % vol_ratio, eff_lookback or 0))
    elif vol_now:
        vol_txt = " 当前年化波动率约%.0f%%。" % vol_now
    type_txt = ("市场处于%s，" % regime_type) if (regime_type and regime_type != "—") else ""
    return "%s %s研判置信度约%.0f%%。%s" % (base, type_txt, conf, vol_txt)


def _advise_funds(direction, composite, indices):
    out = []
    if not fm:
        return out
    try:
        funds = fm.load_funds()
    except Exception:
        return out
    for f in funds:
        code = f.get("code")
        name = f.get("name", code or "")
        kind = (f.get("kind") or f.get("category") or "equity").lower()
        if "货币" in str(name) or kind in ("money", "货币"):
            advice = "持有"
            impact = "≈0%"
            reason = "货币基金收益稳定，与股市方向无关，作为现金管理底仓。"
        elif "债" in str(name) or kind in ("bond", "债券"):
            impact = "-0.3%" if direction == "up" else "+0.3%"
            advice = "持有" if direction != "down" else "可小幅加仓"
            reason = "债市与股市多呈跷跷板，市场走弱时债基更具配置与避险价值。"
        else:  # 权益 / 混合：跟随大盘
            exp = round(composite * 0.4, 1)
            impact = ("+%.1f%%" % exp) if exp >= 0 else ("%.1f%%" % exp)
            if direction == "up" and composite >= 2:
                advice = "持有/小幅加仓"
            elif direction == "down" and composite <= -2:
                advice = "建议减仓防御"
            else:
                advice = "持有观望"
            reason = "权益类随大盘方向，当前研判%s。" % (
                "偏多" if direction == "up" else ("偏空" if direction == "down" else "震荡"))
        out.append({
            "code": code, "name": name, "kind": kind,
            "advice": advice, "impact": impact, "reason": reason,
        })
    return out


_INTR_MAX = 3.8   # intr_score 的理论最大绝对值(2.0*1.0 + 3.0*0.6)，用于归一化到 ±10


def forecast_market(lookback=20):
    """多周期融合 + 波动率自适应的量化研判。带 60s 缓存。

    相比纯日线版本，主要改进：
      1) 接入 15/60 分钟线，补足「日内即时动能」，解决日线对盘中变化无感的问题；
      2) 波动率自适应窗口——波动越大自动缩短回看窗口，快速响应而非用固定窗口硬扛；
      3) 波动率目标仓位——波动放大时自动降仓，不依赖方向预测即可改善风险收益比；
      4) 置信度改为统计口径(多周期一致性 + 趋势效率 + 波动惩罚)，不再是拍脑袋公式。
    行情源异常时自动降级为纯日线研判，不会整体失败。
    """
    lookback = max(5, min(120, int(lookback)))
    now = time.time()
    cached = _FC_CACHE.get(lookback)
    if cached and (now - cached[0]) < _FC_TTL:
        return cached[1]

    # ---- 1) 抓取：日线按最大窗口一次抓够(供自适应裁剪复用)，并抓分钟线 ----
    raw = []
    for secid, name in _INDICES:
        try:
            series = df.fetch_kline(secid, lmt=max(lookback, _MAX_LOOKBACK) + 5) if df else []
        except Exception:
            series = []
        try:
            m15 = df.fetch_kline_minute(secid, scale=15, lmt=80) if df else []
        except Exception:
            m15 = []
        try:
            m60 = df.fetch_kline_minute(secid, scale=60, lmt=30) if df else []
        except Exception:
            m60 = []
        raw.append((secid, name, list(series or []), list(m15 or []), list(m60 or [])))
    if not any(r[2] for r in raw):
        return {"ok": False, "reason": "历史行情抓取失败(网络/接口不可用)", "lookback": lookback}

    # ---- 2) 波动状态 ----
    # 绝对水平用日线已实现波动率(标准口径，驱动波动率目标仓位)；
    # 相对变化用分钟 Parkinson 短/长比(对盘中变化敏感，驱动自适应窗口)。
    # 二者分工的原因：分钟 Parkinson 会系统性低估(捕捉不到隔夜跳空)，
    # 直接拿它定仓位会把仓位算得过高。
    vol_now = vol_base = vol_annual = None
    for secid, name, series, m15, m60 in raw:
        if name not in ("沪深300", "上证指数"):
            continue
        if series:
            vol_annual = realized_vol([c for _, c in series])
        if len(m15) >= 21:
            vol_now = parkinson_vol(m15[-16:])                 # 约最近 1 个交易日
            vol_base = parkinson_vol(m15[:-16])                # 此前一段，作为基准
        break
    vol_ratio = (vol_now / vol_base) if (vol_now and vol_base and vol_base > 0) else None
    # 以用户选择的 lookback 为锚做自适应，保留前端窗口切换的语义
    eff_lookback = adaptive_lookback(vol_ratio, base=lookback)

    # ---- 3) 按自适应窗口裁剪日线后研判 ----
    indices = []
    for secid, name, series, m15, m60 in raw:
        if not series:
            continue
        seg = series[-eff_lookback:] if len(series) > eff_lookback else series
        a = _analyze_index(seg, m15=m15, m60=m60)
        if a:
            a["name"] = name
            a["secid"] = secid
            indices.append(a)
    if not indices:
        return {"ok": False, "reason": "历史行情样本不足", "lookback": lookback}

    weights = {"沪深300": 1.2, "上证指数": 1.0, "创业板指": 1.0,
               "中证500": 0.7, "深证成指": 0.7}

    # ---- 4) 多周期融合：日线分 + 日内分 ----
    # 波动越高，日内(短周期)权重越大——此时日线信号已滞后，短周期更能代表当前状态。
    intr_w = _clamp(0.25 + (vol_ratio - 1.0) * 0.25, 0.15, 0.55) if vol_ratio else 0.25
    tw = ts = tiw = tis = 0.0
    for it in indices:
        w = weights.get(it["name"], 0.6)
        ts += it["score"] * w
        tw += w
        if it.get("intraday", {}).get("m15_ret") is not None:
            tis += _clamp(it["intr_score"] / _INTR_MAX * 10.0, -10.0, 10.0) * w
            tiw += w
    day_score = ts / tw if tw else 0.0
    intr_score = tis / tiw if tiw else None
    if intr_score is None:
        composite = day_score                       # 无分钟数据时纯日线(优雅降级)
        intr_w = 0.0
    else:
        composite = day_score * (1 - intr_w) + intr_score * intr_w

    # ---- 5) 趋势效率(ER)：区分趋势市 / 震荡市 ----
    ers = [it["er"] for it in indices if it.get("er") is not None]
    er_avg = sum(ers) / len(ers) if ers else None
    if er_avg is None:
        regime_type = "—"
    elif er_avg >= 0.35:
        regime_type = "趋势市"
    elif er_avg >= 0.15:
        regime_type = "弱趋势"
    else:
        regime_type = "震荡市"

    # ---- 6) 方向判定(沿用原阈值，保持对外语义稳定) ----
    if composite >= 4:
        regime, direction = "强势上行", "up"
    elif composite >= 1:
        regime, direction = "震荡偏多", "up"
    elif composite > -1:
        regime, direction = "窄幅震荡", "side"
    elif composite > -4:
        regime, direction = "震荡偏空", "down"
    else:
        regime, direction = "弱势下行", "down"

    # ---- 7) 置信度：多周期方向一致性 + 趋势效率 - 高波动惩罚 ----
    same = total = 0
    for it in indices:
        d = 1 if it["score"] > 0 else (-1 if it["score"] < 0 else 0)
        m = (it.get("intraday") or {}).get("m60_ret")
        if m is None or d == 0:
            continue
        mv = 1 if m > 0 else (-1 if m < 0 else 0)
        if mv == 0:
            continue
        total += 1
        if d == mv:
            same += 1
    conf = calibrate_confidence((same, total), er_avg, vol_ratio)
    # 反向重校准：用实测命中率对置信度整体缩放(样本不足时系数=1，等同不改)
    _tp = _track.tuned_params() if _track is not None else {}
    _tpar = (_tp or {}).get("params", {}) if isinstance(_tp, dict) else {}
    _cscale = float(_tpar.get("conf_scale", 1.0) or 1.0)
    if _cscale != 1.0:
        conf = int(_clamp(conf * _cscale, 35, 88))

    # ---- 8) 波动率目标仓位：先看风险，再叠方向偏好 ----
    pos_vol = vol_target_position(vol_annual,
                                  base=int(_tpar.get("base_position", _BASE_POSITION)),
                                  target=float(_tpar.get("target_vol", _TARGET_VOL)))
    dir_bias = _clamp(composite / 6.0, -1.0, 1.0)
    suggested = int(_clamp(pos_vol * (1.0 + 0.35 * dir_bias), 10, 90))

    # ---- 8b) 增强维度：状态机 / 概率区间 / 量价 / 跨资产 ----
    _core = next((it for it in indices if it["name"] in ("沪深300", "上证指数")), indices[0])
    mstate, mstate_label, mstate_hint = detect_market_state(
        composite, er_avg, vol_ratio, _core.get("r20"))
    prob = prob_range(composite, vol_annual)
    volsig = volume_price_signal("1.000001")
    xasset = cross_asset_riskoff()

    view = _build_view(regime, direction, conf, indices,
                       vol_now=vol_annual, vol_ratio=vol_ratio,
                       regime_type=regime_type, eff_lookback=eff_lookback)
    funds = _advise_funds(direction, composite, indices)

    if vol_ratio is None:
        vol_state = "波动数据不足"
    elif vol_ratio >= 1.5:
        vol_state = "显著放大"
    elif vol_ratio >= 1.15:
        vol_state = "偏高"
    elif vol_ratio >= 0.85:
        vol_state = "正常"
    else:
        vol_state = "收敛"

    res = {
        "ok": True,
        "lookback": lookback,
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
        "market": {
            "regime_label": regime,
            "direction": direction,
            "confidence": round(conf, 0),
            "suggested_position": suggested,
            "composite_score": round(composite, 2),
            "short_term_view": view,
            # —— 新增：波动率自适应相关 ——
            "vol_annual": round(vol_annual, 2) if vol_annual else None,
            "vol_intraday": round(vol_now, 2) if vol_now else None,
            "vol_base": round(vol_base, 2) if vol_base else None,
            "vol_ratio": round(vol_ratio, 3) if vol_ratio else None,
            "vol_state": vol_state,
            "vol_target_position": pos_vol,
            "eff_lookback": eff_lookback,
            "intraday_weight": round(intr_w, 3),
            "er": round(er_avg, 3) if er_avg is not None else None,
            "regime_type": regime_type,
            "day_score": round(day_score, 2),
            "intr_score": round(intr_score, 2) if intr_score is not None else None,
            # —— 增强维度 ——
            "state": mstate,
            "state_label": mstate_label,
            "state_hint": mstate_hint,
            "prob": prob,
            "volume": volsig,
            "cross_asset": xasset,
            "tuned": {"applied": bool((_tp or {}).get("applied")),
                      "note": (_tp or {}).get("note")} if _tp else None,
        },
        "indices": indices,
        "funds": funds,
        "note": ("融合日线(自适应窗口%d日)+15/60分钟线的多周期量化推演；"
                 "仓位按波动率目标给出，仅供参考、非投资建议。" % eff_lookback),
    }

    # ---- 9) 落盘：记录本次研判与锚点价格，供事后回填命中率 ----
    # 只在真正重算(非缓存命中)时记录，避免刷新页面刷出大量重复样本。
    try:
        if _track is not None:
            _track.record_forecast(res)
    except Exception:
        pass

    _FC_CACHE[lookback] = (time.time(), res)
    return res
