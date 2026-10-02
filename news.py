"""
news.py —— A股大盘盯盘系统 · 实时新闻面
数据源：新浪财经滚动新闻(feed.mix.sina.com.cn)，免费、无需密钥
功能：抓取最新市场/个股新闻，按利好/利空关键词做情绪打分，输出新闻面评分与条目
"""
import time
import requests

SINA_URL = "https://feed.mix.sina.com.cn/api/roll/get"

BULL = ["降准", "降息", "利好", "增持", "回购", "新高", "大涨", "上调", "超预期", "宽松",
        "扶持", "政策", "放宽", "签约", "中标", "订单", "盈利", "增长", "突破", "回暖",
        "复苏", "扩产", "稳市场", "提振", "加码", "创新高"]
BEAR = ["加息", "减持", "利空", "大跌", "暴跌", "下跌", "处罚", "暴雷", "退市", "下修",
        "亏损", "风险", "收紧", "制裁", "调查", "召回", "诉讼", "下调", "违约", "冻结",
        "警示", "崩盘", "探底", "杀跌", "踩雷"]

_HDR = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}
_session = None


def _sess():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(_HDR)
    return _session


def _count(text, words):
    return sum(text.count(w) for w in words)


def fetch_news(num=25, tries=3):
    """返回 {items:[{time,title,sentiment,score}], score(0-10), bull, bear}"""
    items, score = [], 5.0
    bull_total = bear_total = 0
    for _ in range(tries):
        try:
            r = _sess().get(SINA_URL, params={
                "pageid": "153", "lid": "2509", "num": str(num),
                "page": "1", "r": str(int(time.time() * 1000))}, timeout=12)
            data = (r.json().get("result", {}).get("data") or [])[:num]
            for it in data:
                title = it.get("title") or ""
                intro = it.get("intro") or ""
                txt = title + " " + intro
                b = _count(txt, BULL)
                k = _count(txt, BEAR)
                bull_total += b
                bear_total += k
                if b > k:
                    sent = "利好"
                elif k > b:
                    sent = "利空"
                else:
                    sent = "中性"
                # 单条对总分的贡献(限幅)
                score += max(-0.6, min(0.6, (b - k) * 0.3))
                try:
                    t = time.strftime("%H:%M", time.localtime(int(it.get("ctime", 0))))
                except Exception:
                    t = "--:--"
                items.append({"time": t, "title": title, "sentiment": sent})
            break
        except Exception:
            time.sleep(1)
    score = max(0.0, min(10.0, round(score, 1)))
    return {"items": items[:num], "score": score, "bull": bull_total, "bear": bear_total}


# ---------------------------------------------------------------------------
# 赛道/主题情绪归集：把「全市场泛财经新闻」映射到用户赛道
# (AI算力/半导体/CPO光模块/PCB/创新药)，解决"新闻是全市场的、与我的持仓无关"的问题。
# ---------------------------------------------------------------------------
TOPIC_KEYWORDS = {
    "AI算力": ["算力", "人工智能", "AI", "大模型", "英伟达", "GPU", "服务器", "数据中心", "液冷"],
    "半导体": ["半导体", "芯片", "晶圆", "光刻", "存储", "封测", "国产替代", "中芯", "制程"],
    "CPO光模块": ["CPO", "光模块", "光通信", "光纤", "硅光", "800G", "1.6T"],
    "PCB": ["PCB", "覆铜板", "印制电路", "载板"],
    "创新药": ["创新药", "生物医药", "CXO", "CRO", "ADC", "GLP-1", "减肥药", "集采", "医保", "BD"],
}

# 概念(东财概念板块名) -> 赛道，用于按基金实际敞口过滤新闻
CONCEPT_TRACK = [
    ("算力", "AI算力"), ("人工智能", "AI算力"), ("AI", "AI算力"),
    ("CPO", "CPO光模块"), ("光通信", "CPO光模块"), ("光纤", "CPO光模块"),
    ("半导体", "半导体"), ("芯片", "半导体"), ("封装", "半导体"), ("中芯", "半导体"),
    ("PCB", "PCB"), ("覆铜板", "PCB"), ("MLCC", "PCB"),
    ("创新药", "创新药"), ("CRO", "创新药"), ("医药", "创新药"),
]


def concepts_to_tracks(concepts):
    """把基金概念敞口映射到赛道列表（去重，保持顺序）。"""
    out = []
    for c in concepts or []:
        for kw, tr in CONCEPT_TRACK:
            if kw in str(c) and tr not in out:
                out.append(tr)
    return out


def topic_sentiment(items, tracks=None):
    """按赛道归集新闻情绪。

    items: fetch_news() 的 items（含 title/sentiment）
    tracks: 指定赛道列表；None 则统计全部赛道
    返回 {tracks:{赛道:{n,bull,bear,score,heads}}, top:[...], matched:n}
    score ∈ 0-10（5=中性）。
    """
    names = list(tracks) if tracks else list(TOPIC_KEYWORDS.keys())
    out = {}
    for name in names:
        kws = [k.lower() for k in TOPIC_KEYWORDS.get(name, [])]
        if not kws:
            continue
        bull = bear = n = 0
        heads = []
        for it in items or []:
            t = (it.get("title") or "")
            tl = t.lower()
            if any(k in tl for k in kws):
                n += 1
                s = it.get("sentiment")
                if s == "利好":
                    bull += 1
                elif s == "利空":
                    bear += 1
                if len(heads) < 3:
                    heads.append({"title": t, "sentiment": s})
        if n:
            score = round(5.0 + (bull - bear) / max(1, n) * 5.0, 1)
            out[name] = {"n": n, "bull": bull, "bear": bear,
                         "score": score, "heads": heads}
    ranked = sorted(out.items(), key=lambda kv: (-kv[1]["n"], -kv[1]["score"]))
    return {"tracks": out, "top": [k for k, _ in ranked[:3]],
            "matched": sum(v["n"] for v in out.values())}


if __name__ == "__main__":
    n = fetch_news(10)
    print("news_score:", n["score"], "bull:", n["bull"], "bear:", n["bear"])
    for x in n["items"][:6]:
        print(f"  [{x['sentiment']}] {x['time']} {x['title'][:40]}")
