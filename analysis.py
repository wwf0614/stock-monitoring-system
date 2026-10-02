"""
analysis.py —— A股大盘盯盘系统 · 低风险分析引擎
输入: data_fetcher.build_snapshot() 的快照
输出: 多维度评分 + 市场regime + 低风险投资建议 + 板块轮动检测

设计原则(低风险优先):
  - 只用指数/板块/资金等市场数据，不推测个股；建议一律以场外开放式基金为载体(用户买不了上市ETF/LOF)
  - 维度: 技术面(指数vs均线) / 情绪面(行业涨跌广度) / 资金面(主力净流入) / 风险面(综合反向)
  - 新闻面: 本系统不接入实时新闻源，标注为"需结合当日政策/外围消息人工判断"
  - 建议动作: 分批建仓、不追高、设止损、单一行业<=20%、优先宽基与防御
"""
import os
import json
import time

try:
    import json_util
except Exception:
    json_util = None

# 工作目录(与 server.py 同目录)
_BASE = os.path.dirname(os.path.abspath(__file__))
_ROTATION_FILE = os.path.join(_BASE, "rotation_state.json")

# ---------- 场外基金参考库(用户买不了上市ETF/LOF，仅用场外开放式基金) ----------
# 说明：用户只能买场外开放式基金(ETF联接/指数基金/混合/货币/债券)，故所有标的均为场外等价物。
# 所有代码已于 2026-09-03 经东财 pingzhongdata 接口逐一验证过基金名称，未验证到的行业留空
# （pick_leading_funds 已加空码保护，不会把空代码渲染出去）。
FUND_BROAD = [
    ("沪深300ETF联接", "007339"),   # 易方达沪深300ETF联接C
    ("上证50ETF联接", "001549"),    # 天弘上证50ETF联接C
    ("创业板ETF联接", "001593"),    # 天弘创业板ETF联接C
]
FUND_DEFENSIVE = [
    ("货币基金", "000198"),         # 天弘余额宝货币
    ("短债基金", "070009"),         # 嘉实超短债债券C
    ("纯债基金", "270049"),         # 广发纯债债券C
    ("红利指数基金", "012644"),     # 招商中证红利ETF联接C
    ("黄金ETF联接", "002611"),      # 博时黄金ETF联接C（与权益低相关，分散防御）
    ("银行指数基金", "001595"),     # 天弘中证银行ETF联接C
]
# 关键词 -> (行业场外基金名称, 代码)
FUND_SECTOR_MAP = [
    ("半导体", "半导体ETF联接", "007301"),   # 国联安中证半导体ETF联接C
    ("芯片", "半导体ETF联接", "007301"),
    ("人工智能", "人工智能ETF联接", "011840"),  # 天弘中证人工智能主题联接C
    ("计算机", "计算机ETF联接", "001630"),   # 天弘中证计算机ETF联接C
    ("证券", "证券ETF联接", "008591"),       # 天弘中证全指证券公司ETF联接C
    ("券商", "证券ETF联接", "008591"),
    ("光伏", "光伏指数基金", "011103"),      # 天弘中证光伏产业指数C
    ("军工", "军工指数基金", "002199"),      # 前海开源中证军工指数C
    ("消费", "消费ETF联接", "000248"),       # 汇添富中证主要消费ETF联接A
    ("医药", "医药指数基金", "001550"),      # 天弘中证医药100指数A
    ("银行", "银行ETF联接", "001595"),       # 天弘中证银行ETF联接C
    # 以下行业暂未找到经核验的场外标的，代码留空（展示层已做空码保护）：
    ("电子", "电子指数基金", ""),
    ("新能源", "新能源指数基金", ""),
    ("电池", "电池指数基金", ""),
    ("医疗", "医疗指数基金", ""),
    ("白酒", "白酒指数基金", ""),   # 注意：常见白酒基金 012414 为 LOF，用户不可买，勿填
    ("稀土", "稀有金属指数基金", ""),
    ("有色", "有色金属指数基金", ""),
    ("化工", "化工指数基金", ""),
    ("汽车", "汽车指数基金", ""),
    ("通信", "通信指数基金", ""),
]

# 防御/低波行业关键词(用于风险-off时优先关注)
DEFENSIVE_KEYWORDS = ["银行", "公用事业", "电力", "高股息", "煤炭", "交运", "医药", "消费", "食品", "水务", "高速", "红利"]


def _clamp(x, lo=0, hi=10):
    return max(lo, min(hi, x))


def _sma(arr, n):
    """简单移动平均；arr 为列表，返回最近 n 个元素的均值。"""
    if len(arr) < n:
        return None
    return sum(arr[-n:]) / n


def score_technical(snap):
    sh = snap.get("sh_index", {})
    close, ma20, ma60 = sh.get("close"), sh.get("ma20"), sh.get("ma60")
    if not close:
        return 5.0
    s = 5.0
    if ma20:
        s += 1.5 if close > ma20 else -1.5
    if ma60:
        s += 1.5 if close > ma60 else -1.5
    # 均线多头排列加分
    if ma20 and ma60 and ma20 > ma60:
        s += 1.0
    # 沪深300当日涨跌幅微调
    hs300 = next((i for i in snap.get("indices", []) if i["name"] == "沪深300"), None)
    if hs300:
        s += _clamp(hs300["pct"] * 0.5, -1.5, 1.5)
    return _clamp(s, 0, 10)


def compute_cyclic_position(snap, regime, lookback=60):
    """周期性技术分析仓位：基于上证指数 MA5/10/20/60 排列、波动率、中期动量。
    仅使用 snapshot 中已存在的 sh_index.kline，不发起额外网络请求，避免阻塞前端。
    返回 dict: {low, high, mid, score, basis, signals}
    """
    details = []
    closes = snap.get("sh_index", {}).get("kline", [])
    if not closes or len(closes) < 30:
        details.append("上证K线数据不足，使用默认中性仓位")
        return {"low": 25, "high": 35, "mid": 30, "score": 0,
                "basis": "基于多周期均线分析（数据暂缺，默认中性）", "signals": details}

    last = closes[-1]
    ma5 = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)
    if not all((ma5, ma10, ma20, ma60)):
        details.append("均线数据不足，使用默认中性仓位")
        return {"low": 25, "high": 35, "mid": 30, "score": 0,
                "basis": "基于多周期均线分析（数据暂缺，默认中性）", "signals": details}

    s = 0.0
    # 完整多头排列 / 空头排列
    if last > ma5 > ma10 > ma20 > ma60:
        s += 2.5
    elif last < ma5 < ma10 < ma20 < ma60:
        s -= 2.5
    # 短周期
    if last > ma5 > ma10:
        s += 1.0
    elif last < ma5 < ma10:
        s -= 1.0
    # 中周期
    if last > ma20 > ma60:
        s += 1.0
    elif last < ma20 < ma60:
        s -= 1.0
    # 均线斜率
    ma20_prev = _sma(closes[:-1], 20)
    ma60_prev = _sma(closes[:-1], 60)
    if ma20_prev and ma20 > ma20_prev:
        s += 0.5
    if ma60_prev and ma60 > ma60_prev:
        s += 0.5
    # 价格相对 20/60
    s += 0.5 if last > ma20 else -0.5
    s += 0.5 if last > ma60 else -0.5
    s = _clamp(s, -6, 6)
    tag = "多头" if s > 1 else ("空头" if s < -1 else "震荡")
    details.append(f"上证指数 MA5/10/20/60={ma5:.0f}/{ma10:.0f}/{ma20:.0f}/{ma60:.0f} → {tag}")

    # 综合指数当日情绪修正（沪深300 + 创业板）
    idx_corr = 0.0
    for nm in ["沪深300", "创业板指"]:
        it = next((i for i in snap.get("indices", []) if i.get("name") == nm), None)
        if it and isinstance(it.get("pct"), (int, float)):
            idx_corr += _clamp(it["pct"] * 0.5, -1, 1)
    idx_corr = _clamp(idx_corr, -1.5, 1.5)
    if idx_corr > 0.5:
        details.append("沪深300/创业板当日同步上涨 → 仓位偏积极")
    elif idx_corr < -0.5:
        details.append("沪深300/创业板当日同步下跌 → 仓位偏谨慎")

    tech_score = s + idx_corr
    tech_score = _clamp(tech_score, -6, 6)

    # 波动率调整（近20日收盘价标准差 / MA20）
    vol_adj = 0
    ma20 = _sma(closes, 20)
    if ma20:
        variance = sum((c - ma20) ** 2 for c in closes[-20:]) / 20
        std = variance ** 0.5
        vol_pct = std / ma20
        if vol_pct > 0.025:
            vol_adj = -10
        elif vol_pct > 0.018:
            vol_adj = -6
        elif vol_pct > 0.012:
            vol_adj = -3
        elif vol_pct < 0.008:
            vol_adj = +2
        details.append(f"上证20日波动率 {vol_pct * 100:.2f}% → {'高波减仓' if vol_adj < 0 else ('低波可加仓' if vol_adj > 0 else '波动中性')}")

    # 中期动量调整（用K线自身计算近20/60日收益，避免外部 trend 锁）
    trend_adj = 0
    if len(closes) >= 21:
        ret_20 = (closes[-1] / closes[-21] - 1.0) * 100
        if len(closes) >= 61:
            ret_60 = (closes[-1] / closes[-61] - 1.0) * 100
            if ret_20 > 0 and ret_60 > 0:
                trend_adj = +5
                details.append(f"中期动量上行（近20日 {ret_20:+.1f}% / 近60日 {ret_60:+.1f}%）→ 允许偏积极")
            elif ret_20 < 0 and ret_60 < 0:
                trend_adj = -8
                details.append(f"中期动量下行（近20日 {ret_20:+.1f}% / 近60日 {ret_60:+.1f}%）→ 控制权益敞口")
            else:
                details.append(f"中期动量分化（近20日 {ret_20:+.1f}% / 近60日 {ret_60:+.1f}%）→ 仓位中性")
        else:
            if ret_20 > 2:
                trend_adj = +3
                details.append(f"短期动量偏强（近20日 {ret_20:+.1f}%）→ 可小幅加仓")
            elif ret_20 < -2:
                trend_adj = -5
                details.append(f"短期动量偏弱（近20日 {ret_20:+.1f}%）→ 减仓防御")
            else:
                details.append(f"短期动量中性（近20日 {ret_20:+.1f}%）")
    else:
        details.append("中期动量数据不足")

    # 仓位中枢：综合技术得分、波动率、中期动量
    base = 35 + tech_score * 3.0  # tech_score ∈ [-6,6] -> base ∈ [17, 53]
    mid = _clamp(base + vol_adj + trend_adj, 0, 90)
    low = _clamp(mid - 8, 0, 85)
    high = _clamp(mid + 8, 0, 95)

    # regime 边界修正
    if regime == "risk_on":
        high = min(high + 5, 95)
        if mid < 40:
            mid = 40
    elif regime == "risk_off":
        high = min(high, 35)
        mid = min(mid, 25)
        low = min(low, 20)

    basis = "基于上证指数 MA5/10/20/60 周期技术分析，结合波动率、中期动量与综合指数当日情绪"
    return {
        "low": int(low), "high": int(high), "mid": int(mid),
        "score": round(tech_score, 2),
        "basis": basis,
        "signals": details,
    }


def score_sentiment(snap):
    b = snap.get("breadth")
    if b is None:
        return 5.0
    return _clamp(round(b * 10, 1), 0, 10)


def score_capital(snap):
    net = snap.get("market_main_net", 0.0)  # 元
    yi = net / 1e8
    # +300亿->9, 0->5, -300亿->1
    s = 5.0 + (yi / 300.0) * 4.0
    return _clamp(round(s, 1), 0, 10)


def decide_regime(tech, sent, cap):
    composite = 0.35 * tech + 0.30 * sent + 0.35 * cap
    if composite >= 7:
        regime, label = "risk_on", "风险偏好回升(可积极)"
    elif composite >= 4.5:
        regime, label = "neutral", "震荡均衡(中性)"
    else:
        regime, label = "risk_off", "避险情绪占优(防御)"
    return regime, label, round(composite, 1)


def pick_leading_funds(snap, n=3):
    """从领涨/资金流入行业概念中匹配对应行业场外基金"""
    sectors = snap.get("industries", [])[:25] + snap.get("concepts", [])[:25]
    # 综合强度 = 涨幅*0.6 + 主力净流入(亿)标准化*0.4
    def strength(x):
        net_yi = x.get("main_net", 0) / 1e8
        return x.get("pct", 0) * 0.6 + _clamp(net_yi / 20.0, -3, 5) * 0.4 * 2
    ranked = sorted(sectors, key=strength, reverse=True)
    picks, seen = [], set()
    for x in ranked:
        if len(picks) >= n:
            break
        nm = x.get("name", "")
        for kw, fund_name, code in FUND_SECTOR_MAP:
            # code 为空说明该行业暂无可执行标的，跳过（防止空码去重失效+渲染出无意义条目）
            if kw in nm and code and code not in seen:
                picks.append({"sector": nm, "etf": fund_name, "code": code,
                              "pct": x.get("pct"), "main_net_yi": round(x.get("main_net", 0)/1e8, 2)})
                seen.add(code)
                break
    return picks


def pick_defensive_sectors(snap):
    """当前数据里偏防御且相对抗跌的板块"""
    out = []
    for x in snap.get("industries", []):
        nm = x.get("name", "")
        if any(k in nm for k in DEFENSIVE_KEYWORDS):
            out.append({"sector": nm, "pct": x.get("pct"), "main_net_yi": round(x.get("main_net", 0)/1e8, 2)})
    # 取当日相对抗跌(跌幅小或上涨)且资金未大幅流出的
    out = [o for o in out if o["pct"] > -1.0][:6]
    return sorted(out, key=lambda o: o["pct"], reverse=True)


def recommend(regime, tech, sent, cap, snap):
    """根据regime + 周期性技术分析给出低风险建议(动作/仓位/标的)；标的均为场外开放式基金"""
    leading = pick_leading_funds(snap, 3)
    defensive = pick_defensive_sectors(snap)
    cyclic = compute_cyclic_position(snap, regime)
    if regime == "risk_on":
        action, risk = "分批建仓", "中"
        low = max(cyclic["low"], 45)
        high = max(cyclic["high"], 55)
        vehicles = [{"etf": n, "code": c, "role": "宽基核心"} for n, c in FUND_BROAD[:2]]
        vehicles += leading[:1]
        note = "市场多头信号，可逢回调分批介入宽基指数基金(场外)；若参与领涨板块，单一行业基金仓位不超过总仓20%，不追当日已大涨标的。"
    elif regime == "neutral":
        action, risk = "持有+小仓定投", "中低"
        low, high = cyclic["low"], cyclic["high"]
        vehicles = [{"etf": n, "code": c, "role": "宽基定投"} for n, c in FUND_BROAD[:2]]
        note = "方向不明，以宽基指数基金(场外)定投为主，控制总仓；避免重仓单一题材。"
    else:  # risk_off
        action, risk = "防御/持币为主", "低"
        low = min(cyclic["low"], 20)
        high = min(cyclic["high"], 30)
        vehicles = [{"etf": n, "code": c, "role": "防御现金"} for n, c in FUND_DEFENSIVE[:3]]
        if defensive:
            vehicles += [{"etf": d["sector"], "code": "", "role": "相对抗跌板块"} for d in defensive[:2]]
        note = "广度萎缩、主力大幅流出，控制风险为首。以货币/纯债/高股息等防御品种(场外)或持币为主，耐心等待广度与资金回暖再分批布局。"
    return {
        "action": action, "position_low": low, "position_high": high,
        "position_mid": cyclic["mid"], "position_basis": cyclic["basis"],
        "position_signals": cyclic["signals"], "position_score": cyclic["score"],
        "risk": risk, "vehicles": vehicles, "leading": leading,
        "defensive": defensive, "note": note,
    }


def load_rotation_state():
    if json_util is not None:
        return json_util.safe_load_json(_ROTATION_FILE, default={"last_ts": 0, "sectors": {}})
    try:
        if os.path.exists(_ROTATION_FILE):
            with open(_ROTATION_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"last_ts": 0, "sectors": {}}


def compute_rotation(cur_map, prev_map):
    """纯函数：根据当前与前次板块字典({name:{pct,main_net}})，计算板块轮动信息。不落盘。"""
    rotation = {"has_prev": bool(prev_map), "moved_up": [], "moved_down": [], "new_hot": [], "churn": 0}
    if not prev_map:
        return rotation
    prev_top = sorted(prev_map.items(), key=lambda kv: kv[1].get("pct", 0), reverse=True)[:20]
    prev_top_names = {k for k, _ in prev_top}
    cur_top = sorted(cur_map.items(), key=lambda kv: kv[1].get("pct", 0), reverse=True)[:20]
    cur_top_names = {k for k, _ in cur_top}
    rotation["new_hot"] = [n for n in cur_top_names if n not in prev_top_names][:5]
    rotation["churn"] = round(1 - len(prev_top_names & cur_top_names) / max(1, len(prev_top_names)), 2)
    # 计算排名变化较大的几个板块的涨跌差
    deltas = []
    for name, v in cur_map.items():
        if name in prev_map:
            d = (v.get("pct", 0) or 0) - (prev_map[name].get("pct", 0) or 0)
            deltas.append((name, d))
    deltas.sort(key=lambda t: abs(t[1]), reverse=True)
    for name, d in deltas[:6]:
        if d > 0:
            rotation["moved_up"].append({"sector": name, "delta": round(d, 2)})
        else:
            rotation["moved_down"].append({"sector": name, "delta": round(d, 2)})
    return rotation


def update_rotation_state(snap):
    """对比本次与上次快照，检测板块轮动并落盘；返回轮动信息。
    本函数由「高频资金快照」后台线程调用，是 rotation_state.json 的唯一写入方，
    保证板块轮动以 ~15s 细粒度更新（而非完整快照的 40s 粗粒度）。"""
    prev = load_rotation_state()
    prev_sectors = prev.get("sectors", {})
    cur = {}
    for x in snap.get("industries", []) + snap.get("concepts", []):
        cur[x["name"]] = {"pct": x.get("pct"), "main_net": x.get("main_net", 0)}
    rotation = compute_rotation(cur, prev_sectors)
    # 落盘（原子写，防止写中断截断 rotation_state.json）
    if json_util is not None:
        json_util.atomic_write_json(_ROTATION_FILE, {"last_ts": snap.get("ts"), "sectors": cur})
    else:
        try:
            with open(_ROTATION_FILE, "w", encoding="utf-8") as f:
                json.dump({"last_ts": snap.get("ts"), "sectors": cur}, f, ensure_ascii=False)
        except Exception:
            pass
    return rotation


def score_news(news):
    """新闻面评分(0-10)。news=None 时返回中性5。"""
    if not news:
        return 5.0
    return news.get("score", 5.0)


def _fmt_yi(v):
    """数值(亿) -> 带符号字符串，用于总结中展示主力净流入"""
    if v is None:
        return ""
    return ("+" if v >= 0 else "") + f"{v:.1f}"


def _s(v):
    """数值百分比 -> 带符号字符串；None 返回 '—'。供中期趋势文案展示。"""
    if not isinstance(v, (int, float)):
        return "—"
    return f"{v:+.2f}%"


def build_summary(snap, regime, label, scores, rec, confidence):
    """生成自然语言「分析总结」：把多维度信号汇成一句结论 + 若干要点。
    纯文本，供看板顶部卡片直接展示。"""
    net = snap.get("market_main_net", 0.0)
    net_yi = net / 1e8
    b = snap.get("breadth")
    b_pct = (b * 100) if b is not None else None

    if regime == "risk_on":
        state = "风险偏好回升，市场赚钱效应较好"
    elif regime == "neutral":
        state = "方向不明、多空均衡，以震荡为主"
    else:
        state = "避险情绪占优，资金趋于防御"

    if net_yi <= -100:
        flow = f"主力大幅净流出 {abs(net_yi):.0f} 亿"
    elif net_yi < 0:
        flow = f"主力小幅净流出 {abs(net_yi):.0f} 亿"
    elif net_yi >= 100:
        flow = f"主力大幅净流入 {net_yi:.0f} 亿"
    else:
        flow = f"主力基本均衡（{net_yi:+.0f} 亿）"
    breadth_txt = f"上涨行业占比仅 {b_pct:.0f}%" if b_pct is not None else "广度数据缺失"

    core = [v["etf"] for v in rec["vehicles"] if v["code"]]
    core_txt = "、".join(core) if core else "货币/国债/高股息等防御品种"
    lead = rec["leading"]
    if lead:
        lead_txt = "、".join(
            f"{x['etf']}({x['code']}{', 主力' + _fmt_yi(x.get('main_net_yi')) + '亿' if x.get('main_net_yi') else ''})"
            for x in lead)
    else:
        lead_txt = "暂无明确领涨主线"

    bullets = [
        f"【市场状态】{state}，综合评分 {scores['composite']}/10，信心 {confidence}、风险等级 {rec['risk']}。",
        f"【广度与资金】{breadth_txt}，{flow}；资金面 {scores['capital']}/10、情绪面 {scores['sentiment']}/10、技术面 {scores['technical']}/10。",
        f"【操作建议】当前建议「{rec['action']}」，建议仓位 {rec['position_low']}%–{rec['position_high']}%；核心防御标的：{core_txt}。",
        f"【可关注主线】领涨且资金流入板块对应 {lead_txt}（仅作卫星仓观察，不追高）。",
        f"【风险提示】{rec['note']}",
    ]
    headline = f"{label}：建议「{rec['action']}」，仓位 {rec['position_low']}%–{rec['position_high']}%"
    return {"headline": headline, "bullets": bullets}


def compute_analysis(snap, news=None):
    tech = score_technical(snap)
    sent = score_sentiment(snap)
    cap = score_capital(snap)
    news_score = score_news(news)
    regime, label, composite = decide_regime(tech, sent, cap)
    rec = recommend(regime, tech, sent, cap, snap)
    # 板块轮动：仅读取「高频资金快照」已落盘的最新轮动用于展示，不在完整分析里重复落盘，
    # 避免与高频通道(细粒度)写入冲突导致轮动抖动。
    prev = load_rotation_state()
    _cur = {}
    for x in snap.get("industries", []) + snap.get("concepts", []):
        _cur[x["name"]] = {"pct": x.get("pct"), "main_net": x.get("main_net", 0)}
    rotation = compute_rotation(_cur, prev.get("sectors", {}))

    # 风险面评分 = 综合反向(越低越好)
    risk_score = round(10 - (0.4 * tech + 0.3 * sent + 0.3 * cap), 1)

    # 信心: 数据越全、维度越一致越高
    agree = (tech >= 6 and sent >= 6 and cap >= 6) or (tech < 4 and sent < 4 and cap < 4)
    confidence = "高" if (snap.get("ok") and agree) else ("中" if snap.get("ok") else "低")

    # 新闻面偏差提示
    news_label = "中性"
    news_note = "本系统接入新浪滚动新闻做情绪打分(仅供参考)。重大政策/外围事件仍建议人工复核。"
    if news_score is not None:
        if news_score >= 7:
            news_label = "偏利好"
        elif news_score <= 3:
            news_label = "偏利空"
        # 新闻与量价信号冲突时的提示
        if news_label == "偏利空" and regime != "risk_off":
            news_note += " ⓵ 新闻面偏利空但量价尚未转弱，注意追高风险，以信号为准。"
        elif news_label == "偏利好" and regime == "risk_off":
            news_note += " ⓶ 新闻面偏利好但量价仍弱，警惕利好兑现，不急于加仓。"

    scores = {
        "technical": tech, "sentiment": sent, "capital": cap,
        "news": round(news_score, 1), "risk": risk_score, "composite": composite,
    }
    summary = build_summary(snap, regime, label, scores, rec, confidence)
    return {
        "ts": snap.get("ts"),
        "scores": scores,
        "regime": regime, "regime_label": label,
        "confidence": confidence,
        "news_label": news_label, "news_detail": (news or {}).get("items", []),
        "recommendation": rec,
        "rotation": rotation,
        "news_note": news_note,
        "summary": summary,
    }


def build_next_step(a, fund_advice=None, capital=None, market_trend=None):
    """把分析结论 + 持仓调仓建议，综合成「下一步投资方向」自然语言。
    返回 {headline, direction, bullets}。供看板「分析盘面」卡片展示。
    capital: 可选，{market_main_net(亿), money_flow:{institution,main,retail(亿)}}，用于实时资金面要点。
    market_trend: 可选，trend.get_market_trend() 输出，提供盘面 1-3 个月中期语境。"""
    rec = a["recommendation"]
    regime = a["regime"]
    scores = a["scores"]
    direction = {
        "risk_on": "偏积极：可逢回调分批介入，方向上偏向进攻",
        "neutral": "中性：以守代攻，维持定投与结构微调",
        "risk_off": "偏防御：以控制回撤为第一目标，降低权益敞口",
    }.get(regime, "中性")
    bullets = []
    bullets.append(
        f"【方向判断】{direction}；综合评分 {scores['composite']}/10"
        f"（技术 {scores['technical']} / 情绪 {scores['sentiment']} / 资金 {scores['capital']}），信心 {a['confidence']}。"
    )
    bullets.append(
        f"【仓位计划】建议「{rec['action']}」，目标仓位 {rec['position_low']}%–{rec['position_high']}%，风险等级 {rec['risk']}。"
    )
    # 实时资金面(主力/机构/散户)——这是下一步操作的核心信号
    if capital:
        mf = capital.get("money_flow", {}) or {}
        inst = mf.get("institution", 0.0) or 0.0
        main = mf.get("main", 0.0) or 0.0
        retail = mf.get("retail", 0.0) or 0.0
        net = capital.get("market_main_net", 0.0) or 0.0
        tone = ("主力与机构同步进场，对下一步偏积极" if (main > 0 and inst > 0)
                else ("主力/机构离场，下一步以防守为主" if main < 0 else "资金方向中性、观望"))
        bullets.append(
            f"【资金面·实时】全市场主力净流入 {net:+.0f}亿 ｜ 机构 {inst:+.0f}亿 ｜ 散户 {retail:+.0f}亿。"
            f"{tone}。基金调仓以所属板块的主力净流入为首要信号。"
        )
    if rec["vehicles"]:
        veh = "、".join(
            f"{v['etf']}" + (f"({v['code']})" if v.get("code") else "")
            for v in rec["vehicles"]
        )
        role = rec["vehicles"][0].get("role", "")
        bullets.append(f"【核心标的】{veh}（{role}）。")
    if rec["leading"]:
        lead = "、".join(f"{x['etf']}({x['code']})" for x in rec["leading"])
        bullets.append(f"【可关注主线】领涨且资金流入：{lead}；仅作卫星仓，不追当日大涨。")
    if rec["defensive"]:
        deftxt = "、".join(x["sector"] for x in rec["defensive"][:3])
        bullets.append(f"【防御垫】相对抗跌板块：{deftxt}，可充当组合稳定器。")
    if fund_advice and fund_advice.get("summary"):
        bullets.append(f"【你的持仓调整】{fund_advice['summary']}")
    # 盘面 1-3 个月中期语境：让"下一步"以中期趋势为锚，而非被单日波动带偏
    if market_trend and market_trend.get("ok"):
        m1 = market_trend.get("ret_1m"); m3 = market_trend.get("ret_3m")
        mlabel = market_trend.get("label", "—")
        bullets.append(
            f"【盘面中期·1-3月】沪深300 近1月 {_s(m1)}、近3月 {_s(m3)}，"
            f"中期定调「{mlabel}」。调仓以中期趋势为锚：中期上行板块可逢调加仓，"
            f"中期下行板块的当日反弹不追高、等企稳。"
        )
    headline = f"下一步：{direction.split('：')[0]}，仓位 {rec['position_low']}–{rec['position_high']}% · {rec['action']}"
    return {"headline": headline, "direction": direction, "bullets": bullets}


if __name__ == "__main__":
    import data_fetcher as df
    snap = df.build_snapshot()
    a = compute_analysis(snap)
    print(json.dumps(a, ensure_ascii=False, indent=2)[:2500])
