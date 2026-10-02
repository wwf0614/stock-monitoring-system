# -*- coding: utf-8 -*-
"""fund_forecast.py —— 基金级趋势预测（对准真实持仓，而非宽基指数）

【为什么需要它】
forecast.py 的研判对象是宽基指数(沪深300/上证/创业板/深证)，与用户持有的行业/主题
场外基金(CPO/半导体/PCB/创新药)相关度低。典型反例：沪深300 横盘时 CPO/半导体一根线
拉 15%，宽基研判会判"震荡"，与你持仓的真实方向相反。

【本模块做什么】
对【每只基金】用其自身净值序列(+主概念板块序列)独立研判，输出：
  - 方向(up/down/side) + 置信度
  - 概率化未来区间：未来 H 个交易日的期望涨跌幅、1σ 区间、上涨概率(正态近似)
  - 年化波动率 / 趋势效率 ER / 均线排列 / 波动倍数
  - 波动率目标仓位建议

【复用】
  trend.fetch_nav_history   基金净值升序序列
  trend.get_concept_trend   主概念板块趋势(确认信号)
  forecast.realized_vol / efficiency_ratio / vol_target_position / calibrate_confidence
保证与大盘研判同一套口径。

对外：
  forecast_fund(code, name=None, amount=None) -> dict
  forecast_funds(rows=None, use_cache=True)   -> dict
"""
import math
import time

try:
    import forecast as fc
except Exception:
    fc = None
try:
    import trend as tr
except Exception:
    tr = None
try:
    import fund_manager as fm
except Exception:
    fm = None
try:
    import forecast_track as _track
except Exception:
    _track = None

_HORIZON = 5                 # 预测期限(交易日)，与"未来 3-5 日"口径一致
_TRADING_DAYS = 242          # 年化交易日数(与 forecast.py 一致)

_FC_CACHE = {}               # code -> (ts, result)
_TTL = 120.0                 # 2 分钟缓存，避免看板反复刷

_DIR_LABEL = {"up": "偏多", "down": "偏空", "side": "震荡"}


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _sma(vals, n):
    if not vals or len(vals) < n or n <= 0:
        return None
    return sum(vals[-n:]) / n


def _ma_state(last, ma5, ma10, ma20):
    """均线排列 -> (标签, 得分)。与 forecast._analyze_index 同口径。"""
    if not (ma5 and ma10 and ma20):
        return "样本不足", 0.0
    if last > ma5 > ma10 > ma20:
        return "多头排列", 3.0
    if last < ma5 < ma10 < ma20:
        return "空头排列", -3.0
    if last >= ma20 and last >= ma5:
        return "短期偏强", 1.0
    if last <= ma20 and last <= ma5:
        return "短期偏弱", -1.0
    return "交叉震荡", 0.0


def _vol_ratio(closes):
    """短/长波动比：近 20 日 vs 近 60 日已实现波动，用于自适应窗口与置信度惩罚。"""
    if not fc:
        return None
    v20 = fc.realized_vol(closes, 20)
    v60 = fc.realized_vol(closes, 60)
    if v20 and v60 and v60 > 0:
        return v20 / v60
    return None


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _series_of(code):
    """基金净值升序 [(date, nav)]；失败回退 []。"""
    if not tr:
        return []
    try:
        return tr.fetch_nav_history(code, days=120) or []
    except Exception:
        return []


def _dir_of(score):
    if score >= 1:
        return "up"
    if score <= -1:
        return "down"
    return "side"


def _votes(score, r5, r10, ma_score, concept):
    """多信号方向一致性投票 -> (同向票数, 总票数)，供 calibrate_confidence 使用。"""
    d = _dir_of(score)
    target = 1 if d == "up" else (-1 if d == "down" else 0)
    sigs = []
    for v in (r5, r10):
        if v is not None:
            sigs.append(1 if v > 0 else (-1 if v < 0 else 0))
    if ma_score:
        sigs.append(1 if ma_score > 0 else -1)
    if concept and concept.get("ok"):
        cr = concept.get("ret_1m")
        if cr is not None:
            sigs.append(1 if cr > 0 else (-1 if cr < 0 else 0))
    if not sigs:
        return (0, 0)
    same = sum(1 for s in sigs if s == target)
    return (same, len(sigs))


def _prob_range(score, ann_vol, horizon=_HORIZON):
    """概率化未来区间：mean(期望涨跌幅) 与 ±1σ(约68%置信) 区间 + 上涨概率。

    价格随机游走近似：σ_H = 日波动 × sqrt(H)。方向仅做弱倾斜(drift)，
    不夸大方差——把"方向判断"转化为"区间+概率"，比单点方向更诚实。
    """
    if not ann_vol or ann_vol <= 0:
        return {"exp_pct": None, "low_pct": None, "high_pct": None, "prob_up": None}
    daily = ann_vol / math.sqrt(_TRADING_DAYS)          # 日波动(%)
    sigma = daily * math.sqrt(horizon)                   # 周期波动(%)
    drift = _clamp(score / 10.0, -1.0, 1.0) * sigma * 0.5  # 方向弱倾斜
    prob_up = _norm_cdf(drift / sigma) * 100.0 if sigma > 0 else 50.0
    return {
        "exp_pct": round(drift, 2),
        "low_pct": round(drift - sigma, 2),
        "high_pct": round(drift + sigma, 2),
        "prob_up": round(prob_up, 0),
    }


def _view_text(name, direction, score, prob, regime, ann_vol, vratio):
    d_txt = {"up": "短期动能偏强，未来3-5日偏多",
             "down": "短期动能转弱，未来3-5日偏空",
             "side": "多空均衡，未来3-5日以区间震荡为主"}[direction]
    vol_txt = ""
    if ann_vol:
        vol_txt = " 年化波动约%.0f%%" % ann_vol
        if vratio:
            vol_txt += "(%s基准)" % ("放大" if vratio >= 1.15 else ("收敛" if vratio <= 0.85 else "接近"))
    band = ""
    if prob.get("low_pct") is not None:
        band = " 未来%d日大概率落在 %+.1f%% ~ %+.1f%%(期望 %+.1f%%，上涨概率约 %d%%)。" % (
            _HORIZON, prob["low_pct"], prob["high_pct"], prob["exp_pct"], prob["prob_up"])
    return "%s：%s【%s市】。%s%s" % (name, d_txt, regime, vol_txt.strip(), band)


def forecast_fund(code, name=None, amount=None, use_cache=True):
    """单只基金趋势研判(含概率化未来区间)。"""
    code = str(code).strip()
    now = time.time()
    if use_cache:
        c = _FC_CACHE.get(code)
        if c and (now - c[0]) < _TTL:
            return c[1]

    res = {"ok": False, "code": code, "name": name or code}
    series = _series_of(code)
    if len(series) < 6:
        res["reason"] = "基金净值样本不足(%d 条)" % len(series)
        _FC_CACHE[code] = (now, res)
        return res

    dates = [d for d, _ in series]
    closes = [float(v) for _, v in series]
    n = len(closes)
    last = closes[-1]
    ma5, ma10, ma20 = _sma(closes, 5), _sma(closes, 10), _sma(closes, 20)

    def ret(k):
        return ((last / closes[-1 - k] - 1.0) * 100.0) if n > k else None

    r5, r10, r20 = ret(5), ret(10), ret(20)
    arrange, ma_score = _ma_state(last, ma5, ma10, ma20)

    score = 0.0
    if r5 is not None:
        score += _clamp(r5, -6, 6) * 1.0
    if r10 is not None:
        score += _clamp(r10, -10, 10) * 0.5
    score += ma_score

    er = fc.efficiency_ratio(closes, n=min(20, n)) if fc else None
    ann_vol = fc.realized_vol(closes) if fc else None
    vratio = _vol_ratio(closes)

    # 主概念板块趋势(确认信号)
    concept, main_concept = None, None
    try:
        cons = fm.fund_concepts(code) if fm else []
        main_concept = cons[0] if cons else None
        if main_concept and tr:
            concept = tr.get_concept_trend(main_concept, use_cache=use_cache)
    except Exception:
        concept = None

    direction = _dir_of(score)
    votes = _votes(score, r5, r10, ma_score, concept)
    conf = fc.calibrate_confidence(votes, er, vratio) if fc else 50
    prob = _prob_range(score, ann_vol, _HORIZON)
    pos = fc.vol_target_position(ann_vol) if fc else 55
    dir_bias = _clamp(score / 6.0, -1.0, 1.0)
    sugg = int(_clamp(pos * (1.0 + 0.35 * dir_bias), 10, 90))
    regime = "趋势" if (er or 0) >= 0.35 else ("弱趋势" if (er or 0) >= 0.15 else "震荡")

    res = {
        "ok": True,
        "code": code,
        "name": name or code,
        "amount": amount,
        "as_of": dates[-1],
        "n": n,
        "last_nav": round(last, 4),
        "r5": round(r5, 2) if r5 is not None else None,
        "r10": round(r10, 2) if r10 is not None else None,
        "r20": round(r20, 2) if r20 is not None else None,
        "ma_state": arrange,
        "score": round(score, 2),
        "direction": direction,
        "direction_label": _DIR_LABEL[direction],
        "confidence": int(conf),
        "er": round(er, 3) if er is not None else None,
        "regime": regime,
        "vol_annual": round(ann_vol, 2) if ann_vol else None,
        "vol_ratio": round(vratio, 2) if vratio else None,
        "exp_pct": prob["exp_pct"],
        "low_pct": prob["low_pct"],
        "high_pct": prob["high_pct"],
        "prob_up": prob["prob_up"],
        "horizon": _HORIZON,
        "suggest_position": sugg,
        "main_concept": main_concept,
        "concept_trend": (concept or {}).get("label") if concept else None,
        "concept_ret_1m": (concept or {}).get("ret_1m") if concept else None,
        "view": _view_text(name or code, direction, score, prob, regime, ann_vol, vratio),
    }
    _FC_CACHE[code] = (now, res)
    try:
        if _track is not None:
            _track.record_fund_forecast(res)
    except Exception:
        pass
    return res


def forecast_funds(rows=None, use_cache=True):
    """对全部持仓基金做趋势研判，并给出组合加权期望。

    返回 {ok, funds:[forecast_fund...], portfolio:{total_amount, exp_pct, prob_up, ...}}
    """
    if rows is None:
        rows = fm.merge_with_amounts() if fm else []
    rows = rows or []
    out = []
    for r in rows:
        out.append(forecast_fund(r.get("code"), r.get("name"),
                                 r.get("amount"), use_cache=use_cache))
    tot = exp = wsum = up_w = 0.0
    for r, f in zip(rows, out):
        amt = float(r.get("amount") or 0)
        tot += amt
        if not f.get("ok") or f.get("exp_pct") is None:
            continue
        exp += amt * f["exp_pct"]
        wsum += amt
        if f.get("prob_up") is not None:
            up_w += amt * f["prob_up"]
    portfolio = {
        "total_amount": round(tot, 2),
        "exp_pct": round(exp / wsum, 2) if wsum else None,
        "prob_up": round(up_w / wsum, 0) if wsum else None,
    }
    return {
        "ok": any(f.get("ok") for f in out),
        "funds": out,
        "portfolio": portfolio,
        "horizon": _HORIZON,
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


if __name__ == "__main__":
    import json
    r = forecast_funds()
    print(json.dumps(r.get("portfolio"), ensure_ascii=False, indent=2))
    for f in r.get("funds", []):
        print("  %-8s %-10s %s" % (f.get("code"), f.get("direction_label"),
                                   f.get("view", f.get("reason", ""))))
