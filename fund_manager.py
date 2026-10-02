# -*- coding: utf-8 -*-
"""
fund_manager.py —— A股盯盘系统 · 基金持仓管理

功能:
  - 用户录入基金代码 + 买入金额，自动匹配基金名称
  - 接入实时行情取当日涨跌，并按名称做防御型/权益型分类，给出调仓建议

数据源(按基金类型自动选择):
  - 上市基金(ETF/LOF): 东方财富行情接口 push2delay(secid)，分时实时价/涨跌幅
  - 场外开放式基金(未上市): 名称走 pingzhongdata(fS_name)；涨跌走基金净值接口
    (lsjz 的 JZZZL，即最新单位净值日涨跌%)。场外基金无分时实时价，盘中不更新，
    显示的是"最新净值涨跌"，通常滞后到前一交易日净值公布。
"""
import os
import re
import json
import time
import datetime
import threading

try:
    import requests
except Exception:
    requests = None

try:
    import json_util
except Exception:
    json_util = None

_BASE = os.path.dirname(os.path.abspath(__file__))
_FUND_FILE = os.path.join(_BASE, "funds.json")
_HISTORY_FILE = os.path.join(_BASE, "fund_history.json")
_lock = threading.Lock()
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 防御/稳健型关键词(用于自动分类)
_DEF_KW = ["货币", "国债", "债券", "短债", "纯债", "中债", "信用债", "高股息", "红利",
           "理财", "现金", "同业", "中短债", "可转债", "日利", "现金管理"]

# 基金 -> 所属板块关键词(用于"按板块实时资金流向"驱动调仓)。
# 基金是板块篮子而非单只股票，调仓建议应看它「跟踪对标 / 当日联动板块」的主力净流入方向。
# 关键词需能命中东方财富行业/概念板块名称(见 data_fetcher 的 industries/concepts)。
# 重要：此处的「板块」是【跟踪对标 / 当日联动板块】（用于捕捉该基金方向性资金信号），
#       并非基金官方行业分类，不代表基金真实持仓行业，亦不构成任何投资建议。
# 留空表示宽基/灵活配置类，无明确行业，则用全市场主力净流入作为信号。
FUND_SECTOR_MAP = {
    # 016874 广发远见智选混合C -> 科技成长主动基金，重仓电子/计算机/AI算力光模块，对标电子板块
    "016874": ["电子", "半导体", "人工智能", "计算机"],
    # 017811 东方人工智能主题混合C -> AI主题
    "017811": ["人工智能", "半导体", "电子"],
    # 021528 财通成长优选混合C -> 科技成长
    "021528": ["计算机", "电子", "半导体"],
    # 012738 广发创新药ETF联接C -> 医药/创新药
    "012738": ["创新药", "医药"],
}

# 基金 -> 概念板块(基于实际前十大持仓推导；名称需能命中东方财富概念板块)。
# 此「概念板块」为该基金重仓股所隶属的题材概念(如 CPO概念/光通信模块/PCB/半导体概念/
# 创新药)，用于展示其「概念敞口」与对应题材的实时净进场，不代表基金官方行业分类，
# 亦不构成任何投资建议。名称留空表示无明显题材聚焦。
# 说明：funds.json 现已支持持久化 concepts 字段(由本系统写入)；FUND_CONCEPTS 作为
# funds.json 缺失 concepts 时的回退默认值，二者命名需与东方财富概念板块一致。
FUND_CONCEPTS = {
    # 016874 广发远见智选混合C -> 前十大: 中际旭创/新易盛(CPO光模块)、亨通光电/中天科技(光纤光缆)、
    #   宏景科技/协创数据(算力租赁)、生益科技/建滔积层板(覆铜板/PCB)。对标平台「光纤·算力租赁」。
    "016874": ["CPO概念", "光通信模块", "光纤概念", "算力概念", "PCB"],
    # 017811 东方人工智能主题混合C -> 前十大: 北方华创/中微公司/拓荆科技/盛美上海/芯源微/中科飞测/
    #   华海清科/精测电子/富创精密(半导体设备链)、寒武纪(AI芯片)。对标平台「半导体材料设备」。
    "017811": ["半导体概念", "国产芯片", "中芯概念", "先进封装", "AI芯片"],
    # 021528 财通成长优选混合C -> 前十大: 新易盛/源杰科技(CPO光模块/光芯片)、南亚新材/华正新材/
    #   德福科技/鼎泰高科/大族数控(PCB链)、三环集团/风华高科/博迁新材(MLCC)。对标平台「PCB·CPO」。
    "021528": ["PCB", "CPO概念", "MLCC", "光通信模块"],
    # 012738 广发创新药产业ETF联接C -> 前十大: 恒瑞医药/科伦药业/百济神州/艾力斯(创新药)、
    #   药明康德/凯莱英/康龙化成/泰格医药(CXO/CRO)。对标平台「创新药」。
    "012738": ["创新药", "CRO"],
}


def fund_concepts(code, fund=None):
    """返回该基金的概念板块列表。

    优先取 funds.json 的 concepts 字段(由本系统管理、可手动编辑，
    通常由 refresh_fund_concepts 实时抓取写入)，回退到 FUND_CONCEPTS 默认值。
    名称须能命中东方财富概念板块(用于实时净进场映射)。
    """
    if fund and isinstance(fund.get("concepts"), list) and fund["concepts"]:
        return list(fund["concepts"])
    return list(FUND_CONCEPTS.get(str(code), []) or [])


# ---------------------------------------------------------------------------
# 实时获取基金「概念板块敞口」：前十大持仓 -> 个股所属概念 -> 聚合
# 说明：基金是股票篮子，其题材敞口由其重仓股所属概念决定。
#   1) 抓前十大持仓股票代码(fundf10 jjcc)
#   2) 逐只取个股所属全部板块(东财 slist，行业/概念/地域混合)
#   3) 剔除元概念(宽基/指数/外资/风格类)，按命中持仓数排序取前 N
# 该流程对任意新基金均适用，故添加基金时可「实时获取」概念板块，无需手填。
# ---------------------------------------------------------------------------
# 基金前十大持仓：东方财富 pingzhongdata/{code}.js 中的 stockCodesNew 字段，
# 形如 ["0.300308","1.600487",...]（market.code）。该文件由天天基金公开提供，稳定可用。
_STOCKNEW_RE = re.compile(r'stockCodesNew\s*=\s*\[(.*?)\]', re.S)
_SEC_RE = re.compile(r'"\d+\.(\d{6})"')


def fetch_fund_holdings(code, tries=2):
    """取基金前十大持仓股票代码(list[str]，6位代码)用于推导概念敞口。失败返回 []。"""
    if requests is None:
        return []
    for _ in range(tries):
        try:
            r = requests.get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
                             headers={"User-Agent": _UA}, timeout=15)
            m = _STOCKNEW_RE.search(r.text)
            if not m:
                continue
            codes = _SEC_RE.findall(m.group(1))
            if codes:
                return codes[:10]
        except Exception:
            time.sleep(0.4)
    return []


# 带权重的前十大持仓：天天基金 F10「基金持仓」(季报)HTML 表格，含「占净值比例」。
# 用于把盘中实时估算从「等权平均」升级为「按实际持仓权重加权」——
# 真实基金前十大权重差异极大(第一重仓可达 9%+，第十约 1%)，等权平均会显著失真。
# 注意：数据为季报，滞后于当前持仓，但权重结构相对稳定，仍远优于等权。
_F10_JJCC_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
_TD_RE = re.compile(r'<td.*?>(.*?)</td>', re.S)
_TAG_RE = re.compile(r'<.*?>', re.S)
_W_RE = re.compile(r'^[\d,]+\.?\d*%$')


def fetch_fund_holdings_weighted(code, tries=2):
    """取基金前十大重仓 [(股票代码, 占净值比例%)]，按权重降序(接口本身已排序)。

    返回 [] 表示抓取失败，调用方应回退到等权估算(fetch_fund_holdings)。
    """
    if requests is None:
        return []
    for _ in range(tries):
        try:
            r = requests.get(_F10_JJCC_URL,
                             params={"type": "jjcc", "code": str(code),
                                     "topline": "10", "year": "", "month": ""},
                             headers={"User-Agent": _UA,
                                      "Referer": "https://fundf10.eastmoney.com/"},
                             timeout=15)
            out = []
            for row in re.findall(r'<tr>(.*?)</tr>', r.text, re.S):
                tds = _TD_RE.findall(row)
                if len(tds) < 8:
                    continue
                sc = _TAG_RE.sub('', tds[1]).strip()
                if not re.fullmatch(r'\d{6}', sc):
                    continue
                # 占净值比例：从后往前找第一个「数字%」单元格。
                # 不按固定列号取，是因为个别行会缺列导致整体偏移，
                # 按位置取极易误取到「持股数/持仓市值」(曾取到 111252.03 这种值)。
                w = None
                for cell in reversed(tds):
                    v = _TAG_RE.sub('', cell).strip()
                    if not _W_RE.match(v):
                        continue
                    try:
                        w = float(v.replace('%', '').replace(',', ''))
                    except Exception:
                        w = None
                    break
                if w is None or not (0 < w <= 100):     # 合理性校验，剔除错位值
                    continue
                out.append((sc, w))
                if len(out) >= 10:      # 只取最新报告期的前十大
                    break
            if out:
                return out
        except Exception:
            time.sleep(0.4)
    return []


def stock_concepts(stock_code, tries=2):
    """取单只股票所属全部板块名(list[str])，含行业/概念/地域混合。失败返回 []。

    东财 slist 接口一次拿全个股归属板块，是实时获取「股票->概念」的可靠来源。
    """
    if requests is None:
        return []
    market = "1" if str(stock_code).startswith("6") else "0"
    for _ in range(tries):
        try:
            r = requests.get("https://push2.eastmoney.com/api/qt/slist/get", params={
                "fltt": "2", "invt": "2", "secid": f"{market}.{stock_code}",
                "spt": "3", "pi": "0", "pz": "200", "po": "1",
                "fields": "f12,f14,f3,f128"},
                headers={"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"},
                timeout=15)
            j = r.json()
            diff = (j.get("data") or {}).get("diff") or {}
            items = diff.values() if isinstance(diff, dict) else diff
            return [it.get("f14", "") for it in items if it.get("f14")]
        except Exception:
            time.sleep(0.4)
    return []


#  exogenous / 宽口径概念：非基金真正「题材方向」的板块(地域/风格/外资/重组/技术形态等)，
# 即便被个股隶属也会淹没核心题材，故在聚合时剔除，使概念敞口聚焦到 CPO/PCB/国产芯片/
# 创新药 等代表基金方向的题材。与 is_meta_concept(元概念) 互补。
BROAD_CONCEPT_DENY = {
    # 地域/区域
    "一带一路", "雄安新区", "大湾区", "长三角", "成渝", "京津冀", "自贸区", "特区", "湾区", "新区",
    "北京板块", "上海板块", "广东板块", "深圳板块", "浙江板块", "江苏板块", "山东板块", "四川板块",
    "湖北板块", "湖南板块", "河南板块", "福建板块", "安徽板块", "河北板块", "陕西板块", "辽宁板块",
    "天津板块", "重庆板块", "广西板块", "江西板块", "贵州板块", "云南板块", "海南板块", "山西板块",
    "黑龙江板块", "吉林板块", "甘肃板块", "内蒙古板块", "宁夏板块", "青海板块", "新疆板块", "西藏板块", "香港板块",
    # 风格/估值/分红
    "科技风格", "医药医疗风格", "消费风格", "金融风格", "周期风格", "成长风格", "价值风格",
    "高股息", "低估值", "高市净率", "低市净率", "高市盈率", "低市盈率", "红利", "破净",
    "低价股", "高价股", "百元股", "次新", "新股", "转融券",
    # 外资/机构/资金属性
    "融资融券", "深股通", "沪股通", "北向", "社保", "QFII", "保险重仓", "券商重仓", "信托重仓",
    "私募重仓", "公募重仓", "国家队", "证金", "汇金", "养老金", "基金重仓", "机构重仓",
    "券商金股", "证金持股", "汇金持股",
    # 龙头/权重/规模标签
    "行业龙头", "龙头股", "白马股", "核心资产", "蓝筹", "权重股", "大盘成长", "大盘价值",
    "中盘成长", "小盘成长", "隐形冠军", "小巨人", "专精特新", "独角兽",
    # 事件/资本运作/技术形态类(非题材方向)
    "华为概念", "通信技术", "创业成份", "员工持股", "股权激励", "回购", "增持", "减持",
    "举牌", "要约收购", "借壳", "重组", "并购", "资产注入", "整体上市", "分拆",
    "限售解禁", "解禁", "高质押", "股权质押", "质押", "退市风险", "风险警示", "AH股",
    "北交所", "新三板", "科创板", "创业板", "主板", "中小板",
    "沪股通", "深股通", "港股通", "沪港通", "深港通", "陆股通", "北向资金", "南向", "外资",
    "龙虎榜", "东方财富热股", "同花顺热股", "人气王", "热搜", "榜单", "排行",
    "涨停", "跌停", "换手", "量比", "振幅", "活跃", "人气", "热度", "关注度",
    "金叉", "死叉", "均线", "突破", "支撑", "压力", "背离", "超买", "超卖",
    "新高", "新低", "历史新低", "历史高位", "历史低位", "平台突破", "缺口",
    "岛形反转", "V形", "双底", "双顶", "头肩", "楔形", "旗形", "矩形", "箱体", "趋势",
    "洗盘", "出货", "吸筹", "建仓", "加仓", "减仓", "清仓", "满仓", "空仓", "半仓", "轻仓", "重仓",
    "仓位", "控盘", "庄家", "主力资金", "游资", "敢死队",
}


def aggregate_fund_concepts(code, max_concepts=6):
    """综合基金前十大持仓，聚合出概念敞口(题材级，剔除元概念/行业大类/宽口径概念)。
    返回 list[str]，按命中持仓数降序，最多 max_concepts 个。失败/无数据返回 []。
    说明：slist 返回的板块含行业(电子/半导体/通信设备)与概念(CPO概念/光模块)混合，
    这里只保留「概念板块(m:90+t:3)」级别的题材，剥离行业大类与地域/风格/资金属性/
    技术形态等宽口径概念，使敞口聚焦到 CPO/PCB/国产芯片/创新药 等真正代表基金方向的题材。"""
    try:
        from data_fetcher import is_meta_concept
    except Exception:
        def is_meta_concept(n):
            return False
    ind_names = _industry_names()
    holdings = fetch_fund_holdings(code)
    if not holdings:
        return []
    counter = {}
    for h in holdings:
        for c in stock_concepts(h):
            if is_meta_concept(c) or c in BROAD_CONCEPT_DENY:
                continue
            if c in ind_names:      # 剥离行业大类，只留题材级概念
                continue
            counter[c] = counter.get(c, 0) + 1
    if not counter:
        return []
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [name for name, _ in ranked[:max_concepts]]


# 行业板块名称缓存(m:90+t:2)，用于把 slist 返回的板块区分为「行业」与「概念」，
# 让基金概念敞口只保留题材级概念。模块级缓存，避免每次抓取都翻 6 页行业列表。
_IND_NAMES_CACHE = {"ts": 0, "names": set()}


def _industry_names():
    now = time.time()
    if _IND_NAMES_CACHE["names"] and (now - _IND_NAMES_CACHE["ts"]) < 21600:
        return _IND_NAMES_CACHE["names"]
    try:
        from data_fetcher import fetch_sectors
        items = fetch_sectors("m:90+t:2", 100, "f3", 1, 6)
        names = {it.get("name") for it in items if it.get("name")}
        if names:
            _IND_NAMES_CACHE["names"] = names
            _IND_NAMES_CACHE["ts"] = now
    except Exception:
        pass
    return _IND_NAMES_CACHE["names"]


def refresh_fund_concepts(code=None):
    """(重新)实时抓取基金概念敞口并写入 funds.json。
    code 指定时只更新该基金；否则全部。返回受影响(已写入概念)的基金代码列表。
    注意：抓取失败(返回空)时保留原有 concepts，不覆盖为空。"""
    funds = load_funds()
    if not funds:
        return []
    target = [f for f in funds if (code is None or f["code"] == str(code))]
    changed = []
    for f in target:
        # 人工校准表(FUND_CONCEPTS)优先：命中则用校准值，避免被自动聚合(较泛/含噪声)覆盖；
        # 未收录的新基金仍走实时自动聚合推导。
        cons = FUND_CONCEPTS.get(str(f["code"])) or aggregate_fund_concepts(f["code"])
        if cons:
            f["concepts"] = cons
            changed.append(f["code"])
    if changed:
        save_funds(funds)
    return changed


# ---------------------------------------------------------------------------
# 盘中实时估算涨幅：以前十大重仓股的实时涨跌幅，等权加权估算基金当日盘中涨幅。
# 场外开放式基金无分时价，东财只给"最新净值涨跌"(前一交易日、盘中不更新)，
# 此估算填补盘中实时空白，使基金卡涨幅像养基宝一样盘中滚动。
# ---------------------------------------------------------------------------
_INTRADAY_QUOTE_CACHE = {}      # 个股 secid/code -> (ts, pct)
_INTRADAY_QUOTE_LOCK = threading.Lock()
_INTRADAY_QUOTE_TTL = 30.0

_INTRADAY_CACHE = {}            # 基金 code -> (ts, pct_or_None)
_INTRADAY_LOCK = threading.Lock()
_INTRADAY_TTL = 30.0
# 估算来源溯源：'etf'=底层ETF实时价 / 'holdings'=持仓加权。用于前端如实标注，
# 避免出现"重仓股加权"里其实走的是底层ETF、或估算值被标成官方净值这类口径错位。
_INTRADAY_SRC = {}

_HOLD_CACHE = {}                # 基金 code -> (ts, [持仓6位代码])
_HOLD_LOCK = threading.Lock()
_HOLD_TTL = 86400.0             # 持仓每日才变，缓存1天

# ETF联接/指数联接基金 -> 跟踪的底层ETF secid(无个股持仓时，用底层ETF实时价估算盘中涨幅)
ETF_LINK = {
    "012738": "0.159992",   # 广发创新药ETF联接C -> 创新药ETF(159992)
}


def _batch_intraday_quotes(codes):
    """批量取多只股票实时涨跌幅(%)。codes=6位代码列表。返回 {code: pct}。失败项缺失。"""
    if requests is None or not codes:
        return {}
    secids = [_secid_of(c) for c in codes]
    secids = [s for s in secids if s]
    if not secids:
        return {}
    try:
        r = requests.get(
            "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
            params={"np": "1", "fltt": "2", "invt": "2",
                    "secids": ",".join(secids), "fields": "f12,f3"},
            headers={"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"},
            timeout=10)
        j = r.json()
        data = (j.get("data") or {}).get("diff") or []
    except Exception:
        return {}
    out = {}
    for it in data:
        c = it.get("f12")
        p = it.get("f3")
        if c and isinstance(p, (int, float)):
            out[str(c)] = p
    return out


def _etf_secid_pct(secid):
    """取单个ETF/股票 secid 的实时涨跌幅(%)。失败返回 None。"""
    if requests is None:
        return None
    try:
        r = requests.get(
            "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
            params={"np": "1", "fltt": "2", "invt": "2", "secids": secid, "fields": "f12,f3"},
            headers={"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"},
            timeout=8)
        j = r.json()
        diff = (j.get("data") or {}).get("diff") or []
        if diff:
            p = diff[0].get("f3")
            return float(p) if isinstance(p, (int, float)) else None
    except Exception:
        return None
    return None


def _get_holdings_cached(code):
    """取基金前十大持仓(带1天缓存，避免每次刷新都打天天基金)。"""
    with _HOLD_LOCK:
        c = _HOLD_CACHE.get(code)
        if c and (time.time() - c[0]) < _HOLD_TTL:
            return c[1]
    h = fetch_fund_holdings(code)
    with _HOLD_LOCK:
        _HOLD_CACHE[code] = (time.time(), h)
    return h


_HOLD_W_CACHE = {}              # 基金 code -> (ts, [(股票代码, 占净值比例%)])
_HOLD_W_LOCK = threading.Lock()


def _get_weighted_holdings_cached(code):
    """取带权重的前十大持仓(带1天缓存)。失败返回 []，调用方应回退等权。"""
    with _HOLD_W_LOCK:
        c = _HOLD_W_CACHE.get(code)
        if c and (time.time() - c[0]) < _HOLD_TTL:
            return c[1]
    h = fetch_fund_holdings_weighted(code)
    with _HOLD_W_LOCK:
        _HOLD_W_CACHE[code] = (time.time(), h)
    return h


def estimate_fund_intraday(code):
    """估算基金当日盘中涨幅(%)：按前十大重仓股的「占净值比例」加权平均。

    取不到权重(季报接口失败)时回退到等权平均；ETF联接等无个股持仓的基金，
    回退到底层ETF实时涨跌幅。全部失败返回 None。
    注意：权重按「成功取到实时行情的持仓」重新归一化，个别股票行情缺失不会拉偏估算。
    """
    # ETF联接/指数联接：基金直接持有底层ETF，用底层ETF实时价最准。
    # 季报里这类基金的个股持仓占比往往极小(如创新药ETF联接仅约 0.6%)，
    # 拿它们推算反而失真，故 ETF_LINK 优先于重仓股推算。
    secid = ETF_LINK.get(str(code))
    if secid:
        p = _etf_secid_pct(secid)
        if isinstance(p, (int, float)):
            _INTRADAY_SRC[str(code)] = "etf"
            return p
    wh = _get_weighted_holdings_cached(code)
    weights = {str(c): w for c, w in wh} if wh else {}
    holdings = [str(c) for c, _ in wh] if wh else _get_holdings_cached(code)
    if not holdings:
        return None
    now = time.time()
    pcts = {}
    need = []
    with _INTRADAY_QUOTE_LOCK:
        for c in holdings:
            c = str(c)
            e = _INTRADAY_QUOTE_CACHE.get(c)
            if e and (now - e[0]) < _INTRADAY_QUOTE_TTL:
                pcts[c] = e[1]
            else:
                need.append(c)
    if need:
        fresh = _batch_intraday_quotes(need)
        with _INTRADAY_QUOTE_LOCK:
            for c, p in fresh.items():
                _INTRADAY_QUOTE_CACHE[c] = (now, p)
                pcts[c] = p
    num = 0.0
    den = 0.0
    for c, p in pcts.items():
        if not isinstance(p, (int, float)):
            continue
        w = weights.get(str(c), 1.0)        # 无权重信息时退化为等权
        if w <= 0:
            continue
        num += w * p
        den += w
    if den <= 0:
        return None
    _INTRADAY_SRC[str(code)] = "holdings"
    return round(num / den, 2)


def intraday_src(code):
    """返回盘中估算的来源：'etf' / 'holdings' / None(尚无估算)。供前端如实标注口径。"""
    return _INTRADAY_SRC.get(str(code))


def estimate_all_intraday():
    """刷新所有持仓基金的盘中估算涨幅缓存。先批量取全部持仓个股+底层ETF实时行情，再逐基估算。"""
    funds = load_funds()
    if not funds:
        return
    all_codes = []
    for f in funds:
        # 加权持仓(季报)与等权持仓(pingzhongdata)代码可能不同，两者都预取，
        # 保证后续逐基估算时命中缓存、不再单只发起请求。
        all_codes.extend(str(c) for c, _ in _get_weighted_holdings_cached(f["code"]))
        all_codes.extend(_get_holdings_cached(f["code"]))
    if all_codes:
        fresh = _batch_intraday_quotes(list(dict.fromkeys(all_codes)))
        now = time.time()
        with _INTRADAY_QUOTE_LOCK:
            for c, p in fresh.items():
                _INTRADAY_QUOTE_CACHE[c] = (now, p)
    now = time.time()
    for f in funds:
        p = estimate_fund_intraday(f["code"])
        with _INTRADAY_LOCK:
            _INTRADAY_CACHE[f["code"]] = (now, p)


def get_cached_intraday(code):
    """读取基金盘中估算涨幅(缓存命中)；未命中则懒计算。返回 pct 或 None。"""
    with _INTRADAY_LOCK:
        e = _INTRADAY_CACHE.get(code)
        if e and (time.time() - e[0]) < _INTRADAY_TTL:
            return e[1]
    p = estimate_fund_intraday(code)
    if p is not None:
        with _INTRADAY_LOCK:
            _INTRADAY_CACHE[code] = (time.time(), p)
    return p



def _secid_of(code):
    """6位代码 -> 东方财富 secid。沪市(1.)首数字 1/5/6/9；深市(0.)首数字 0/2/3。"""
    code = str(code).strip()
    if not (code.isdigit() and len(code) == 6):
        return None
    first = code[0]
    if first in "1569":
        return "1." + code
    if first in "023":
        return "0." + code
    return None


def load_funds():
    if json_util is not None:
        d = json_util.safe_load_json(_FUND_FILE, default={"funds": []})
        return d.get("funds", []) if isinstance(d, dict) else []
    try:
        with open(_FUND_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d.get("funds", [])
    except Exception:
        return []


def save_funds(funds):
    with _lock:
        if json_util is not None:
            json_util.atomic_write_json(_FUND_FILE, {"funds": funds})
            return
        with open(_FUND_FILE, "w", encoding="utf-8") as f:
            json.dump({"funds": funds}, f, ensure_ascii=False, indent=2)


def add_fund(code, amount, category=None):
    """新增/更新一只基金。category 可显式指定 'defensive'/'equity'，否则自动分类。
    返回 (ok, fund_or_error)。"""
    code = str(code).strip()
    if not (code.isdigit() and len(code) == 6):
        return False, "基金代码需为6位数字"
    try:
        amount = float(amount)
    except Exception:
        return False, "买入金额需为数字"
    if amount <= 0:
        return False, "买入金额需大于0"
    if category not in (None, "", "defensive", "equity", "auto"):
        return False, "类型需为 防御/权益/自动"
    if category == "auto":
        category = None
    funds = load_funds()
    name = lookup_name(code)  # 实时匹配名称；失败则用已有或"未知"
    if name is None:
        name = "未知(行情暂不可用)"
    exist = next((x for x in funds if x["code"] == code), None)
    if exist:
        # 唯一金额口径：amount 就是持仓总值，不再维护本金/份额/成本
        exist["amount"] = amount
        exist.pop("principal", None)
        exist.pop("shares", None)
        # 老数据可能缺 buy_date(份额法与赎回费计算都依赖它)，补上当天
        if not exist.get("buy_date"):
            exist["buy_date"] = _bj_date()
        exist["name"] = name
        if category:
            exist["category"] = category
    else:
        # buy_date 决定「买入日净值」基准与持有天数/赎回费，新建时必须写入
        f = {"code": code, "name": name, "amount": amount, "buy_date": _bj_date()}
        if category:
            f["category"] = category
        funds.append(f)
    save_funds(funds)
    # 实时获取该基金的概念板块敞口(前十大持仓->个股概念->聚合)，写入 concepts 字段
    try:
        refresh_fund_concepts(code)
    except Exception as e:
        print(f"[add_fund] 概念抓取失败(不影响添加): {e}")
    return True, next((x for x in funds if x["code"] == code), None)


def set_amount(code, mv):
    """重设持仓「总值」（对账校准用）：直接把金额改成给定值。
    不走赎回费/持有天数校验、不折算份额——重设只是记账校准，不是真实申赎，不产生费用。
    buy_date 保持不动（持有天数与赎回费护栏仍按真实买入日算）。
    返回 (ok, dict_or_error)。"""
    code = str(code).strip()
    try:
        mv = float(mv)
    except Exception:
        return False, "金额需为数字"
    if mv <= 0:
        return False, "金额需大于 0"
    funds = load_funds()
    f = next((x for x in funds if x["code"] == code), None)
    if f is None:
        return False, "持仓中没有这只基金"
    f["amount"] = round(mv, 2)
    f.pop("principal", None)
    f.pop("shares", None)
    if not f.get("buy_date"):
        f["buy_date"] = _bj_date()
    save_funds(funds)
    return True, {"code": code, "amount": round(mv, 2)}


def remove_fund(code):
    code = str(code).strip()
    funds = load_funds()
    new = [x for x in funds if x["code"] != code]
    if len(new) == len(funds):
        return False
    save_funds(new)
    return True


def fund_fee_type(f):
    """场外基金费率分类：money/bond/index_etf/stock/qdii/fof + 份额 A/C。
    识别靠名称关键词，C 类以名称结尾 'C' 判定（如「广发远见智选混合C」）。"""
    name = f.get("name") or ""
    n = name + " " + str(f.get("kind") or "")
    is_c = name.strip().upper().endswith("C")
    if "货币" in n:
        return "money", ("C" if is_c else "?")
    if "QDII" in n.upper():
        return "qdii", "?"
    if "FOF" in n.upper():
        return "fof", "?"
    if "ETF联接" in n or "联接" in n:
        return "index_etf", ("C" if is_c else "A")
    if "债" in n:
        return "bond", ("C" if is_c else "A")
    return "stock", ("C" if is_c else "A")


def confirm_lag(ftype):
    """申购确认日滞后自然日数：货/股混/债/ETF联接 T+1；QDII/FOF T+2。
    申购时间未知，统一按 T日15:00前 估计。"""
    return 2 if ftype in ("qdii", "fof") else 1


def _fee_threshold(ftype, share):
    if ftype == "money":
        return 0
    if ftype == "stock" and share != "C":
        return 180          # 主动股混 A 类常需 ≥180 天
    if ftype in ("qdii", "fof"):
        return 365          # QDII/FOF 常需 ≥1 年
    return 30               # 债 / ETF联接 / 股混 C 类


def _fee_rate(buy_date, f=None):
    """场外基金赎回费率（分类感知，持有期从确认日 T+1 起算）：
    监管红线 <7 日 1.5%；其后按类型阶梯（股混A≥180天免 / QDII·FOF≥1年免 /
    债·ETF联接·股混C ≥30天免 / 货币全免）。无买入日→0。"""
    if not buy_date:
        return 0.0
    try:
        d = datetime.datetime.strptime(buy_date, "%Y-%m-%d")
    except Exception:
        return 0.0
    ftype, share = fund_fee_type(f or {})
    days = max(0, (datetime.datetime.now() - d).days - confirm_lag(ftype))
    if ftype == "money":
        return 0.0
    if days < 7:
        return 1.5
    if ftype == "stock" and share != "C":
        return 0.5 if days < 180 else 0.0
    if ftype in ("qdii", "fof"):
        return 0.5 if days < 365 else 0.0
    return 0.5 if days < 30 else 0.0


def buy_more(code, amount):
    """加仓：买入金额直接加到「总值」上（不重置、不折算份额）。
    建仓日保持不变。返回 (ok, fund_or_error)。"""
    code = str(code).strip()
    try:
        amount = float(amount)
    except Exception:
        return False, "买入金额需为数字"
    if amount <= 0:
        return False, "买入金额需大于0"
    funds = load_funds()
    f = next((x for x in funds if x["code"] == code), None)
    if not f:
        return False, "持仓不存在，请先添加"
    f["amount"] = float(f.get("amount", 0) or 0) + amount
    f.pop("principal", None)
    f.pop("shares", None)
    if not f.get("buy_date"):
        f["buy_date"] = _bj_date()
    save_funds(funds)
    return True, next((x for x in funds if x["code"] == code), None)


def redeem(code, amount, cur_value=None):
    """赎回：按赎回金额（市值口径）减仓，份额同比例扣减；赎回费分类感知（确认日起算）。
    cur_value 为前端传入的当前市值；赎回金额≈全部市值视为清仓（删除持仓）。
    返回 (ok, dict)。"""
    code = str(code).strip()
    try:
        amount = float(amount)
    except Exception:
        return False, "赎回金额需为数字"
    if amount <= 0:
        return False, "赎回金额需大于0"
    funds = load_funds()
    f = next((x for x in funds if x["code"] == code), None)
    if not f:
        return False, "持仓不存在"
    total = float(f.get("amount", 0) or 0)
    rate = _fee_rate(f.get("buy_date"), f)
    fee = amount * rate / 100.0
    # 全额赎回（>=99.9% 市值）→ 删除持仓
    if (cur_value and float(cur_value) > 0 and amount >= float(cur_value) * 0.999) \
            or amount >= total * 0.999:
        remove_fund(code)
        return True, {"removed": True, "fee": round(fee, 2), "rate": rate}
    new_total = max(0.0, total - amount)
    f["amount"] = new_total
    f.pop("principal", None)
    f.pop("shares", None)
    if not f.get("buy_date"):
        f["buy_date"] = _bj_date()
    save_funds(funds)
    return True, {"removed": False, "fee": round(fee, 2), "rate": rate,
                  "amount": round(new_total, 2)}


def lookup_name(code):
    """只取名称(添加基金时调用)。上市走行情接口，场外走 pingzhongdata。"""
    q = fetch_one(code)
    if q and q.get("name"):
        return q["name"]
    return _lookup_name_offexchange(code)


def fetch_one(code, tries=2):
    """取单只基金实时涨跌: {code,name,pct,price,src}。失败返回 None。
    src: 'tick'(上市基金分时实时价) / 'nav'(场外基金，取最新单位净值涨跌%)。
    场外开放式基金行情接口取不到，自动回落到基金净值接口。"""
    if requests is None:
        return None
    secid = _secid_of(code)
    if secid:
        for host in ("https://push2delay.eastmoney.com", "https://push2.eastmoney.com"):
            for _ in range(tries):
                try:
                    r = requests.get(
                        host + "/api/qt/stock/get",
                        params={"ut": "fa5fd1943c7b386f172d6893dbfba10b",
                                "secid": secid, "fields": "f12,f58,f43,f170,f46"},
                        headers={"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"},
                        timeout=8)
                    j = r.json()
                    d = j.get("data") or {}
                    name = d.get("f58")
                    if not name:
                        continue
                    f170 = d.get("f170")
                    pct = (f170 / 100.0) if isinstance(f170, (int, float)) else None
                    price = d.get("f43")
                    price = (price / 1000.0) if isinstance(price, (int, float)) else None
                    openp = d.get("f46")
                    openp = (openp / 1000.0) if isinstance(openp, (int, float)) else None
                    intraday = None
                    if isinstance(price, (int, float)) and isinstance(openp, (int, float)) and openp:
                        intraday = (price - openp) / openp * 100.0
                    return {"code": code, "name": name, "pct": pct, "price": price,
                            "open": openp, "intraday_pct": intraday, "src": "tick"}
                except Exception:
                    time.sleep(0.3)
    # 场外开放式基金兜底
    return _fetch_offexchange(code, tries)


def _lookup_name_offexchange(code, tries=2):
    """场外基金名称: 东方财富 pingzhongdata(fS_name)。"""
    if requests is None:
        return None
    for _ in range(tries):
        try:
            r = requests.get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
                             headers={"User-Agent": _UA}, timeout=10)
            m = re.search(r'var fS_name\s*=\s*"(.*?)";', r.text)
            if m and m.group(1):
                return m.group(1)
        except Exception:
            time.sleep(0.3)
    return None


def _fetch_offexchange(code, tries=2):
    """场外开放式基金: 取最新单位净值涨跌%(lsjz 的 JZZZL)作为当日涨跌代理。
    场外基金无分时实时价，净值每日收盘后公布，故显示的是"最新净值涨跌(通常为前一交易日)"。"""
    if requests is None:
        return None
    for _ in range(tries):
        try:
            r = requests.get("https://api.fund.eastmoney.com/f10/lsjz",
                             params={"fundCode": code, "pageIndex": 1, "pageSize": 1},
                             headers={"User-Agent": _UA, "Referer": "https://fundf10.eastmoney.com/"},
                             timeout=8)
            j = r.json()
            lst = (j.get("Data") or {}).get("LSJZList") or []
            if lst:
                row = lst[0]
                jz = row.get("JZZZL")
                if jz not in (None, ""):
                    pct = float(jz)
                    price = row.get("DWJZ")
                    price = float(price) if price not in (None, "") else None
                    return {"code": code, "name": None, "pct": pct, "price": price,
                            "open": None, "intraday_pct": None, "src": "nav",
                            "nav_date": row.get("FSRQ")}
        except Exception:
            time.sleep(0.3)
    return None


_quote_cache = {}            # code -> (timestamp, result_dict)
_quote_cache_lock = threading.Lock()
# TTL：上市基金(tick)20s。
# 场外基金(nav)：原来缓存 6h 过长——净值只在收盘后更新一次，6h 会让整个交易时段
# 都停在陈旧值上(实测出现过整天显示上一版净值涨跌)。改为 5 分钟：既能及时捕捉
# 「净值刚公布」的时刻，请求量也可忽略(每只基金每小时最多 12 次)。
_QUOTE_TTL = {"tick": 20.0, "nav": 300.0, None: 20.0}


def fetch_quotes(codes, use_cache=True):
    """批量取实时涨跌，返回 [{code,name,pct,price,src}]。带 TTL 缓存：
    上市基金缓存20s、场外基金(净值日内不变)缓存6h，让「我的基金」面板秒开。
    后台线程会定期用 use_cache=False 强制刷新缓存，保证数据近实时。"""
    out = []
    now = time.time()
    for c in codes:
        q = None
        if use_cache:
            with _quote_cache_lock:
                e = _quote_cache.get(c)
            if e:
                ts, res = e
                ttl = _QUOTE_TTL.get(res.get("src"), 20.0)
                if now - ts < ttl:
                    q = res
        if q is None:
            q = fetch_one(c)
            if q:
                with _quote_cache_lock:
                    _quote_cache[c] = (now, q)
            else:
                q = {"code": c, "name": "未知(行情暂不可用)", "pct": None,
                     "price": None, "open": None, "intraday_pct": None, "src": None,
                     "nav_date": None}
        out.append(q)
        time.sleep(0.08)  # 轻量节流避免触发限流
    return out


def refresh_fund_cache():
    """后台线程定期调用：强制刷新所有持仓基金的行情缓存(忽略 TTL)。"""
    funds = load_funds()
    if not funds:
        return
    fetch_quotes([f["code"] for f in funds], use_cache=False)


def classify(name, category=None):
    """防御型 / 权益型。category 显式指定时优先。"""
    if category in ("defensive", "equity"):
        return category
    n = (name or "")
    if any(k in n for k in _DEF_KW):
        return "defensive"
    return "equity"


_last_td_cache = {"ts": 0.0, "date": None}


def _bj_now():
    """当前北京时间(datetime, tz-aware)。"""
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8)))


def in_trading_session(now=None):
    """是否处于A股交易时段(周一~周五 09:15-15:05 北京时间)。
    只按星期粗判，节假日会误判为交易日；但收盘/休市后会自动回落到官方净值口径，
    且前端对估算值有「盘中估算」标注，影响可控。"""
    d = now or _bj_now()
    if d.weekday() >= 5:
        return False
    hm = d.hour * 60 + d.minute
    return (9 * 60 + 15) <= hm <= (15 * 60 + 5)


def _last_trade_date():
    """最近一个A股交易日(YYYY-MM-DD)，取上证指数日K最后日期，10分钟缓存。
    用于判断官方净值是否已覆盖最新交易日：净值日期>=最近交易日 → 净值涨幅即准确的当日涨幅。"""
    now = time.time()
    if now - _last_td_cache["ts"] < 600:
        return _last_td_cache["date"]
    d = None
    try:
        import data_fetcher as df
        rows = df.fetch_kline("1.000001", lmt=5) or []
        if rows:
            d = rows[-1][0]
    except Exception:
        d = None
    _last_td_cache["ts"] = now
    _last_td_cache["date"] = d
    return d


# ---------------------------------------------------------------------------
# 历史净值序列 + 真实持有收益
# ---------------------------------------------------------------------------
# 旧口径的 bug：merge_with_amounts 曾用「买入本金 × 当日涨跌幅」算市值，
# 且 funds.json 无 cost 字段(退化为 cost=amount)，导致
#   hold_pct = (amount*(1+pct/100) - amount)/amount*100 = pct
# 即「持有收益率」恒等于「当日涨跌幅」，36 天的累计盈亏被完全抹平。
# 正确口径：按份额法，买入日净值 → 最新净值 的累计涨幅。
# C 类份额无申购费，故 份额 = 本金/买入日净值，市值 = 份额 × 最新净值，精确成立。
_nav_hist_cache = {}         # code -> (ts, [(date, nav), ...])  倒序：最新在前
_nav_hist_lock = threading.Lock()
_NAV_HIST_TTL = 1800.0       # 净值历史日内基本不变，缓存 30 分钟


def fetch_nav_history(code, since_date=None, max_pages=6, tries=2):
    """拉取历史净值序列，返回 [(date_str, nav_float), ...]（倒序，最新在前）。
    东方财富 f10/lsjz 接口每页硬上限 20 条(实测 pageSize 传 200 也只返回 20 条)，
    要覆盖较早的买入日必须翻页。给定 since_date 时翻到覆盖该日期即停。"""
    if requests is None:
        return []
    now = time.time()
    with _nav_hist_lock:
        e = _nav_hist_cache.get(code)
    if e and now - e[0] < _NAV_HIST_TTL:
        cached = e[1]
        # 缓存区间已覆盖 since_date(最早一条 <= since_date) 才可直接复用
        if since_date is None or (cached and cached[-1][0] <= since_date):
            return cached
    rows = []
    for page in range(1, max_pages + 1):
        got = None
        for _ in range(tries):
            try:
                r = requests.get("https://api.fund.eastmoney.com/f10/lsjz",
                                 params={"fundCode": code, "pageIndex": page, "pageSize": 20},
                                 headers={"User-Agent": _UA,
                                          "Referer": "https://fundf10.eastmoney.com/"},
                                 timeout=8)
                lst = (r.json().get("Data") or {}).get("LSJZList") or []
                got = [(x.get("FSRQ"), float(x["DWJZ"])) for x in lst
                       if x.get("FSRQ") and x.get("DWJZ") not in (None, "")]
                break
            except Exception:
                time.sleep(0.3)
        if not got:
            break
        rows.extend(got)
        if since_date and got[-1][0] <= since_date:
            break
        if len(got) < 20:
            break
        time.sleep(0.08)     # 轻量节流，避免触发限流
    if rows:
        with _nav_hist_lock:
            _nav_hist_cache[code] = (now, rows)
    return rows


def hold_perf(code, buy_date, amount, shares=None):
    # ⚠️ 已停用（2026-09-09）：系统改为「只维护一个总值」口径，不再计算累计持有收益，
    # merge_with_amounts 不再调用本函数。保留仅供历史脚本/对账参考，勿在新逻辑中引用。
    """按『买入日净值 → 最新净值』计算真实持有收益(份额法)。
    传入 shares（buy_more 加仓时按最新净值折算累计的份额）时直接用其计市值，
    hold_pct 改按金额口径（市值/本金-1），兼容多次加仓、不受单一买入日净值限制。
    返回 dict: {ok, hold_pct, market_value, shares, base_nav, base_date,
                latest_nav, latest_date}；取不到时 {'ok': False}。"""
    out = {"ok": False}
    if not buy_date:
        return out
    rows = fetch_nav_history(code, since_date=buy_date)
    if not rows:
        return out
    latest_date, latest_nav = rows[0]
    try:
        amount = float(amount or 0)
    except Exception:
        amount = 0.0
    try:
        shares = float(shares) if shares else None
    except Exception:
        shares = None
    if shares and shares > 0 and amount > 0:
        mv = shares * latest_nav
        out.update({
            "ok": True,
            "hold_pct": round((mv / amount - 1.0) * 100.0, 2),
            "market_value": round(mv, 2),
            "shares": round(shares, 4),
            "base_nav": None, "base_date": buy_date,
            "latest_nav": latest_nav, "latest_date": latest_date,
        })
        return out
    # 买入日当天净值通常当晚才公布，故取 buy_date 之后(含)最早的一条作为成本基准；
    # 若买入日晚于全部已公布净值(如当天刚买入、净值未出)，退化为用最新净值 → 持有收益 0%，
    # 比误用最早一条净值(会凭空放大收益)合理。
    after = [x for x in rows if x[0] >= buy_date]
    base_date, base_nav = (after[-1] if after else rows[0])
    if not latest_nav or not base_nav:
        return out
    shares = amount / base_nav
    out.update({
        "ok": True,
        "hold_pct": round((latest_nav / base_nav - 1.0) * 100.0, 2),
        "market_value": round(shares * latest_nav, 2),
        "shares": round(shares, 4),
        "base_nav": base_nav, "base_date": base_date,
        "latest_nav": latest_nav, "latest_date": latest_date,
    })
    return out


def merge_with_amounts(use_cache=True):
    """读取持仓 + 实时行情，合并为展示用结构。use_cache=False 时强制实时抓取。
    涨幅取值优先级：
      上市基金(tick) → 交易所行情涨跌幅(盘中/盘后均准确)；
      场外基金 → 官方净值已公布到最近交易日时用净值涨跌(准确)；
                 净值滞后(盘中/未公布)时用重仓股加权盘中估算(标记"盘中估算")。"""
    funds = load_funds()
    if not funds:
        return []
    quotes = {q["code"]: q for q in fetch_quotes([f["code"] for f in funds], use_cache=use_cache)}
    last_td = _last_trade_date()
    rows = []
    for f in funds:
        q = quotes.get(f["code"], {})
        name = q.get("name") or f.get("name") or f["code"]
        nav_pct = q.get("pct")                       # 最新净值涨跌(JZZZL)
        intraday = get_cached_intraday(f["code"])    # 盘中实时估算涨幅(持仓加权)
        nav_date = q.get("nav_date")
        # 官方净值已覆盖最近交易日 → 净值涨跌即当日准确涨幅，优先于估算。
        # 但盘中例外：当日净值要收盘后(约19:00-22:00)才公布，此时若仍判定"净值新鲜"，
        # 涨跌幅与金额都会冻结在前一交易日收盘 → 表现为「涨跌幅在动/已结算，金额不更新」。
        today = _bj_date()
        nav_fresh = bool(nav_date and str(nav_date) >= today) or bool(
            nav_date and last_td and str(nav_date) >= str(last_td)
            and not in_trading_session())
        if q.get("src") == "tick":
            pct = q.get("pct")                       # 交易所行情涨跌幅
            pct_src = "tick"
            is_live = True
        elif nav_fresh:
            pct = nav_pct
            pct_src = "nav"
            is_live = False
        else:
            pct = intraday if isinstance(intraday, (int, float)) else nav_pct
            pct_src = "est" if isinstance(intraday, (int, float)) else "nav"
            is_live = pct_src == "est"
        # 金额口径(2026-09-09 定稿)：只维护一个「总值」amount，不再区分本金/成本/份额。
        # amount = 截止「已结算净值日」的持仓总值(收盘口径)，由 settle_daily 按净值涨跌每日滚动。
        amount = float(f.get("amount", 0) or 0)
        pct_ok = isinstance(pct, (int, float)) and abs(pct) < 99
        # 关键：判断「当前这个涨跌是否已经滚进 amount」——只认 last_settled_nav_date，
        # 不能只看 pct_src=nav。净值刚公布、但结算还没跑(夜间任务 21:30 或后台线程)时，
        # amount 仍停在上一个已结算净值日；此时若直接取 mv=amount，市值会「退回」到上一日，
        # 今天赚的钱在界面上凭空消失，而涨幅却显示官方新值 → 自相矛盾。
        settled_date = f.get("last_settled_nav_date")
        rolled = bool(nav_date and settled_date and str(nav_date) == str(settled_date))
        if pct_ok and (pct_src in ("est", "tick") or not rolled):
            # 涨跌还没滚进 amount → 展示市值 = 总值 ×(1+当日涨跌)
            prev_mv = amount
            mv = round(amount * (1 + pct / 100.0), 2)
        else:
            # 该净值日已结算：amount 就是当日收盘总值，再乘一次即重复计息
            mv = round(amount, 2)
            prev_mv = (mv / (1 + pct / 100.0)) if (pct_ok and (1 + pct / 100.0) != 0) else mv
        # 当日盈亏 = 今日市值 − 昨收市值
        day_pnl = round(mv - prev_mv, 2)
        est = day_pnl
        # 成本基准 + 真实持仓收益（优先用户录入，其次按买入日净值反推估算）
        try:
            cost, cost_src = fund_cost_estimate(f)
        except Exception:
            cost, cost_src = None, None
        hold_pnl = hold_pct = None
        if cost and mv is not None and cost > 0:
            hold_pnl = round(mv - cost, 2)
            hold_pct = round((mv / cost - 1.0) * 100.0, 2)
        rows.append({
            "code": f["code"], "name": name, "amount": amount,
            "pct": pct, "nav_pct": nav_pct, "is_live": is_live,
            "pct_src": pct_src, "nav_fresh": nav_fresh,
            "est_pnl": est, "day_pnl": day_pnl,
            "market_value": (round(mv, 2) if mv is not None else None),
            "kind": classify(name, f.get("category")),
            "concepts": f.get("concepts"),
            "buy_date": f.get("buy_date"),
            "src": q.get("src"),
            "nav_date": q.get("nav_date"),
            "price": q.get("price"),
            "open": q.get("open"),
            "intraday_pct": intraday,
            # 估算来源溯源：'etf'(底层ETF) / 'holdings'(持仓加权) / 由 server 补 'concept'
            "est_src": (intraday_src(f["code"]) if isinstance(intraday, (int, float)) else None),
            "cost": cost, "cost_src": cost_src,
            "hold_pnl": hold_pnl, "hold_pct": hold_pct,
        })
    return rows


# ---------------------------------------------------------------------------
# 夜间/收盘后涨幅同步：把每只基金当日最终涨跌快照落盘，便于跨日对账与展示
# ---------------------------------------------------------------------------
def _bj_date():
    """返回北京时间日期字符串 YYYY-MM-DD。"""
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def total_day_pnl(rows):
    """今日浮动合计（元）——**必须与 sum(market_value) 同源**。

    为什么不能直接用 sum(est_pnl)：merge 阶段 `est_pnl ≡ day_pnl`（见
    merge_with_amounts 里 `est = day_pnl`），但 server 拿到概念板块实时涨幅后
    会用【覆盖后的 pct】重算 market_value 与 day_pnl，**est_pnl 留在覆盖前**。
    此时若合计仍读 est_pnl，就会出现「市值按今天算、浮动按上一交易日净值算」
    两个时钟并存的假象——2026-09-23 老大从看板上发现
    （市值 3375.08 比成本 +26.40，却同时显示今日浮动 −33.56）。

    取 day_pnl、缺失才回落 est_pnl，保证任何调用方口径一致。
    """
    tot = 0.0
    for r in rows or []:
        v = r.get("day_pnl")
        if v is None:
            v = r.get("est_pnl")
        if v:
            try:
                tot += float(v)
            except (TypeError, ValueError):
                pass
    return round(tot, 2)


def record_daily_snapshot(date_str=None, use_cache=True):
    """记录当日各基金涨幅快照到 fund_history.json(按日期去重，保留最近120天)。
    快照日期取各基金最新净值日期(FSRQ)的最大值，而非运行日——
    这样即使净值公布后(约19:00-22:00)才跑，也能把快照正确归档到『净值所属交易日』，
    避免白天跑出运行日=今日、却记的是昨日净值的混乱。Nightly 同步调用。"""
    rows = merge_with_amounts(use_cache=use_cache)
    if not rows:
        return None
    if date_str is None:
        nav_dates = [r.get("nav_date") for r in rows if r.get("nav_date")]
        nav_max = max(nav_dates) if nav_dates else None
        # 涨幅归属哪一天，取决于 pct 口径：
        #   est/tick = 当日盘中估算 → 归属今天(当前交易日)
        #   nav      = 官方净值涨跌 → 归属净值日期(夜间跑时才成立)
        if any(r.get("pct_src") in ("est", "tick") for r in rows):
            date_str = _bj_date()
        else:
            date_str = nav_max or _bj_date()
    snap = {
        "date": date_str,
        "rows": [
            {"code": r["code"], "name": r["name"],
             # pct 与 est_pnl 必须同源(都取 merge 的当日口径)，否则会出现
             # 「涨幅记的是净值、盈亏记的是盘中估算」的自相矛盾快照。
             # 夜间跑时 pct 本身就是官方净值涨跌，无需单独回退 nav_pct。
             "pct": r["pct"], "amount": r["amount"], "est_pnl": r["est_pnl"],
             "market_value": r.get("market_value"),
             "kind": r["kind"], "src": r.get("src"), "nav_date": r.get("nav_date")}
            for r in rows
        ],
        "total_amount": sum(r["amount"] for r in rows),
        # 与 total_market_value 同源；此处 merge 未做概念覆盖，两者等价，
        # 但统一走 total_day_pnl 以免日后覆盖逻辑变化时又出现两个时钟。
        "total_pnl": total_day_pnl(rows),
    }
    hist = []
    if json_util is not None:
        hist = json_util.safe_load_json(_HISTORY_FILE, default=[])
    else:
        try:
            with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
                hist = json.load(f)
        except Exception:
            hist = []
    if not isinstance(hist, list):
        hist = []
    hist = [h for h in hist if h.get("date") != date_str]  # 同日覆盖
    hist.append(snap)
    hist = sorted(hist, key=lambda x: x.get("date", ""))[-120:]
    with _lock:
        if json_util is not None:
            json_util.atomic_write_json(_HISTORY_FILE, hist)
        else:
            with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(hist, f, ensure_ascii=False, indent=2)
    return snap


# 结算脏数据护栏的待确认签名：code -> (nav_date, pct)。
# 回退路径(f10 尚未收录当日净值)必须连续两轮观测一致才落账，见 settle_daily。
_SETTLE_PENDING = {}

# 单日滚动与官方单日涨跌允许的最大偏差(pp)。超过即判脏数据、跳过本轮待下轮重试。
_SETTLE_XCHECK_PP = 1.2
# 回退路径单日涨跌的绝对上限(%)：场外股混基金单日几乎不可能超过 9%，
# 超过必是接口过渡态脏数据（2026-09-24 曾把 -3.01% 结算成 -8.98%、-3.26% 成 -13.94%）。
_SETTLE_FALLBACK_MAX = 9.0


def settle_daily(use_cache=False):
    """每日收盘/净值公布后结算：把每只基金金额更新为最新市值(上一基准 × 当日涨跌)，
    实现复利滚动跟踪。已结算过同一净值日期的基金自动跳过，避免夜间多次运行重复结算。
    返回结算明细 [{'code','name','pct','from','to'}]。

    脏数据护栏（2026-09-24 事故后新增）：官方净值刚公布的几分钟里，行情/f10 接口
    常处于半更新状态，可能返回过渡态脏数据；一旦按脏数据结算并写入
    last_settled_nav_date，错误金额就被幂等逻辑永久固化（9/22、9/24 各发生一次，
    单日错滚 +6.95% ~ -17.67%）。所以可疑数据宁可跳过本轮、下个周期(20s)重试，
    也绝不落账：
      G1 比率路径·单日滚动：必须与官方单日涨跌 nav_pct 互相印证(偏差≤1.2pp)，
         否则跳过——两个独立数据源打架时，等它们稳定比抢快重要；
      G2 回退路径(f10 未收录当日/基准缺失)：单日涨跌 |pct|>9% 直接判脏；
         且同一 (nav_date, pct) 必须连续两轮观测一致才结算（防瞬时脏值）。
    """
    rows = merge_with_amounts(use_cache=use_cache)
    if not rows:
        return []
    funds = load_funds()
    by_code = {f["code"]: f for f in funds}
    settled, changed = [], False
    for r in rows:
        code = r["code"]
        # 结算只认官方净值涨跌：盘中估算不能用来滚总值，否则夜间再滚一次会重复计息
        nav_date = r.get("nav_date")
        f = by_code.get(code)
        if not f or not nav_date:
            continue
        last_settled = f.get("last_settled_nav_date")
        if last_settled == nav_date:
            continue  # 当日已结算，跳过
        old = float(f.get("amount", 0) or 0)
        # 用净值比率做累计滚动：可正确处理"漏跑若干交易日"的情况，
        # 不再把缺失的多天压成单日涨跌(旧实现的隐患)。
        hist = fetch_nav_history(code) or []  # [(date, nav)] 倒序
        base_nav = latest_nav = None
        idx_base = idx_latest = None
        for i, (d, n) in enumerate(hist):
            if d == nav_date:
                latest_nav, idx_latest = n, i
            if last_settled and d == last_settled:
                base_nav, idx_base = n, i
        # hist 倒序：基准日在后，索引差恰为 1 → 本次滚动只跨 1 个交易日
        single_day = (idx_base is not None and idx_latest is not None
                      and idx_base - idx_latest == 1)
        if base_nav and latest_nav and base_nav > 0 and latest_nav != base_nav:
            ratio = latest_nav / base_nav
            pct = round((ratio - 1.0) * 100.0, 2)
            # G1：单日滚动必须得到官方单日涨跌印证；数据打架 → 跳过本轮待重试
            nav_pct = r.get("nav_pct")
            if single_day:
                if not isinstance(nav_pct, (int, float)) \
                        or abs(pct - nav_pct) > _SETTLE_XCHECK_PP:
                    print(f"[settle] {code} 数据未稳定：比率 {pct}% vs 官方单日 "
                          f"{nav_pct}%（>{_SETTLE_XCHECK_PP}pp 或缺失），跳过本轮待重试")
                    continue
            new = round(old * ratio, 2)
        else:
            pct = r.get("nav_pct")  # 回退：无净值历史可比时用单日官方涨跌
            if not isinstance(pct, (int, float)):
                continue
            # G2a：回退路径是单一数据源，超 9% 必为过渡态脏值
            if abs(pct) > _SETTLE_FALLBACK_MAX:
                print(f"[settle] {code} 可疑脏数据：回退涨跌 {pct}% 超 "
                      f"{_SETTLE_FALLBACK_MAX}%，跳过本轮待重试")
                continue
            # G2b：同一签名连续两轮一致才落账（防净值公布瞬间的瞬时脏值）
            sig = (nav_date, round(float(pct), 4))
            if _SETTLE_PENDING.get(code) != sig:
                _SETTLE_PENDING[code] = sig
                print(f"[settle] {code} 回退路径待二次确认 {sig}，下轮一致才结算")
                continue
            _SETTLE_PENDING.pop(code, None)
            new = round(old * (1 + pct / 100.0), 2)
        f["amount"] = new
        f["last_settled_nav_date"] = nav_date
        settled.append({"code": code, "name": r.get("name"), "pct": pct,
                        "from": old, "to": new})
        changed = True
    if changed:
        save_funds(funds)
    return settled


def latest_snapshot():
    """返回最近一次快照(看板展示"最近同步"用)。"""
    try:
        with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
            hist = json.load(f)
        if hist:
            return hist[-1]
    except Exception:
        pass
    return None


def resolve_fund_sector(code, sector_map_yi, overall_yi):
    """把基金解析到其【跟踪对标 / 当日联动板块】，并返回该板块的主力净流入(亿元)。
    sector_map_yi: {板块名: 主力净流入_亿元}（来自 15s 实时资本通道）。
    返回 {sector, net_yi, broad}。此「板块」为对标/联动参考，非基金官方行业分类，不构成投资建议。
    """
    kws = FUND_SECTOR_MAP.get(code)
    if not kws:
        return {"sector": "大盘(沪深300)", "net_yi": round(overall_yi, 1), "broad": True}
    matched = []
    for kw in kws:
        for name, net in sector_map_yi.items():
            if kw in name:
                matched.append((name, net))
    if not matched:
        return {"sector": "大盘(沪深300)", "net_yi": round(overall_yi, 1), "broad": True}
    # 取主力净流入绝对值最大的板块作为该基金的代表板块
    matched.sort(key=lambda t: abs(t[1]), reverse=True)
    name, net = matched[0]
    return {"sector": name, "net_yi": round(net, 1), "broad": False}


def attach_sector(rows, sector_map_yi, overall_yi):
    """给持仓行附加所属板块与板块主力净流入(亿元)，供看板/报告直接展示。"""
    for r in rows:
        sig = resolve_fund_sector(r["code"], sector_map_yi, overall_yi)
        r["sector"] = sig["sector"]
        r["sector_net_yi"] = sig["net_yi"]
        r["sector_broad"] = sig["broad"]
    return rows


def _equity_suggest(regime, sector, net, pct):
    """权益基金建议：以所属板块的实时主力净流入为首要信号(基金=板块篮子)。"""
    pct_txt = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "—"
    # 板块主力净流入分档：>=15亿 强流入；0~15 小幅流入；-15~0 小幅流出；<=-15 强流出
    if net >= 15:
        if regime == "risk_off":
            base = f"{sector}板块主力净流入 {net:.0f}亿、资金仍在进场，但大盘避险情绪占优，可持有底仓、不追高"
        else:
            base = f"{sector}板块主力净流入 {net:.0f}亿、资金持续进场，方向偏多，可持有并逢回调小幅加仓（单一行业≤20%仓）"
    elif net > 0:
        if regime == "risk_off":
            base = f"{sector}板块主力小幅净流入 {net:.0f}亿，大盘偏弱，先持有观察、不加仓"
        else:
            base = f"{sector}板块主力小幅净流入 {net:.0f}亿，可持有，等放量确认再加仓"
    elif net > -15:
        if regime == "risk_off":
            base = f"{sector}板块主力小幅净流出 {net:.0f}亿，弱势下不补仓，观望等待企稳"
        else:
            base = f"{sector}板块主力小幅净流出 {net:.0f}亿，中性，持有为主、控制仓位"
    else:
        if regime == "risk_off":
            base = f"{sector}板块主力净流出 {net:.0f}亿、资金撤离，建议减仓/观望，回避弱势板块"
        else:
            base = f"{sector}板块主力净流出 {net:.0f}亿、短期承压，建议减仓或观望，不接飞刀"
    # 叠加该基金自身当日涨跌的边界提示(避免追高/接飞刀)
    if isinstance(pct, (int, float)):
        if pct > 2 and net >= 0:
            base += f"；今日已{pct_txt}，不追高"
        elif pct < -2 and net <= 0:
            base += f"；今日{pct_txt}，企稳前不接飞刀"
    return base


def _sign_label(v):
    """把涨跌幅数值格式化为带符号百分比字符串。"""
    if not isinstance(v, (int, float)):
        return "—"
    return f"{v:+.2f}%"


def _med_classify(nav_t, concept_t):
    """综合基金净值趋势与(可选)主概念板块趋势，给中期方向定性。
    返回 (med_label, med_detail) ；med_label ∈ up / down / mixed / unknown。"""
    # 优先用主概念板块趋势(更贴近"盘面/板块"语境)，回退到基金净值趋势
    src = concept_t if (concept_t and concept_t.get("ok")) else nav_t
    if not (src and src.get("ok")):
        return "unknown", ""
    r1 = src.get("ret_1m")
    r3 = src.get("ret_3m")
    if r1 is not None and r3 is not None:
        if r1 > 0 and r3 > 0:
            med = "up"
        elif r1 < 0 and r3 < 0:
            med = "down"
        else:
            med = "mixed"
    elif r1 is not None:
        med = "up" if r1 > 0 else "down"
    else:
        med = "unknown"
    parts = []
    if r1 is not None:
        parts.append(f"近1月 {_sign_label(r1)}")
    if r3 is not None:
        parts.append(f"近3月 {_sign_label(r3)}")
    if src.get("max_dd") is not None:
        parts.append(f"最大回撤 {_sign_label(src.get('max_dd'))}")
    src_name = "主概念板块" if (concept_t and concept_t.get("ok")) else "基金净值"
    detail = f"{src_name}：" + "，".join(parts)
    return med, detail


def _equity_suggest_v2(regime, sector, net, pct, nav_t, concept_t):
    """权益基金建议(融入 1-3 个月中期趋势)：
    - 今日信号：regime + 板块主力净流入(net) + 当日涨跌(pct)
    - 中期信号：基金净值趋势 / 主概念板块趋势(近1月/近3月/最大回撤)
    目标：避免"下跌趋势里追一日反弹"与"上行趋势里追当日高点"。
    """
    pct_txt = _sign_label(pct)
    med, med_detail = _med_classify(nav_t, concept_t)
    base = ""
    # 中线定性主导大方向
    if med == "up":
        if net >= 0:
            base = (f"{sector}板块与基金近1-3月中期趋势向上（{med_detail}），"
                    f"中线方向偏多，可持有并逢回调分批加仓（单一行业≤20%仓）")
        else:  # 中线向上但板块当日资金小幅流出
            base = (f"基金近1-3月中期向上（{med_detail}），但所属 {sector} 板块当日主力净流出，"
                    f"短线有波动，持有为主、等资金回流再加")
    elif med == "down":
        if pct is not None and pct > 0 and net <= 0:
            base = (f"基金近1-3月仍处下行（{med_detail}），今日反弹多为下跌中继，"
                    f"不追高、等企稳；{sector}板块资金未回流前不接飞刀")
        elif net < 0:
            base = (f"基金近1-3月中期走弱（{med_detail}）且 {sector} 板块资金净流出，"
                    f"建议减仓/观望，回避弱势板块")
        else:
            base = (f"基金近1-3月中期偏弱（{med_detail}），暂持有观察、不加仓，"
                    f"等待趋势扭转信号")
    elif med == "mixed":
        base = (f"基金近1-3月趋势分化（{med_detail}），中线方向不明，"
                f"以持有+结构微调为主，不追单日波动")
    else:
        # 无中期数据：回退到原"当日信号"逻辑
        return _equity_suggest(regime, sector, net, pct)
    # 叠加当日涨跌边界提示(避免追高/接飞刀)
    if isinstance(pct, (int, float)):
        if pct > 2 and net >= 0:
            base += f"；今日已{pct_txt}，不追高，等回落"
        elif pct < -2 and net <= 0:
            base += f"；今日{pct_txt}，企稳前不接飞刀"
    # 盘面中期语境(来自沪深300)
    return base


# ---------------------------------------------------------------------------
# 调仓成本：场外基金赎回费率约束
#
# 为什么必须先过这一关：证监会规定「持续持有期少于 7 日」须收取不低于 1.5% 的赎回费。
# 对小资金 + 场外基金的组合，频繁调仓的费率成本会系统性吃掉方向判断的期望收益——
# 短期方向准确率上限约 55%，而一次 7 日内调仓的确定成本就是 1.5%，属负期望交易。
# 因此调仓建议应先回答「费率划不划算」，再谈方向。
#
# 费率档位：C 类份额(无申购费、按日计提销售服务费)通常持有满 30 日即免赎回费；
# A 类份额通常需满 1-2 年才免。此处按份额类别取常见保守档位估算，非精确值。
# 注意：赎回按「先进先出(FIFO)」计，本系统不追踪多笔买入，故以 buy_date 单次计时；
#       若期间有加仓，实际被赎回的可能是更早的份额，故此估算偏保守。
# ---------------------------------------------------------------------------
REDEEM_FEE_C = [(7, 1.5), (30, 0.5), (10 ** 9, 0.0)]                # C 类份额
REDEEM_FEE_A = [(7, 1.5), (365, 0.5), (730, 0.25), (10 ** 9, 0.0)]  # A 类份额

# 调仓在途成本（2026-09-03 大师体检后新增）：场外赎回 T+1~T+3 到账 + 再申购
# T+1 确认，一次"卖旧买新"约 3 个交易日完全空仓，期间承担市场波动；
# C 类另有按日计提的销售服务费（持有越久越贵）。免赎回费 ≠ 零成本。
OUT_OF_MARKET_DAYS = 3         # 一次调仓的典型空仓交易日数
OUT_OF_MARKET_DAILY_VOL = 1.5  # 主题/成长类场外基金典型单日波动(%)


def out_of_market_cost_pct():
    """空仓期波动成本估算(%)：日波动 × sqrt(空仓交易日)。"""
    return OUT_OF_MARKET_DAILY_VOL * (OUT_OF_MARKET_DAYS ** 0.5)


def is_c_class(name):
    """判断是否为 C 类份额(如「广发远见智选混合C」)。名称以 C/E 结尾或带 (C) 标记。"""
    s = str(name or "").strip()
    return bool(re.search(r'[CcEe][）)]?\s*$', s))


def holding_days(fund, today=None):
    """持有天数（按 buy_date，且从【份额确认日】起算）。缺买入日期返回 None。

    为什么必须扣确认日：持有期不是从"下单日"而是从"份额确认日"(T+1，QDII/FOF T+2)
    起算。少扣这一天，在 7 日/30 日免赎线上会把 1.5% 报成 0.5%(3 倍费率差)——
    而这条线正是「小资金频繁调仓=负期望」整个判断的基础。
    口径与 _fee_rate()/redeem() 一致，前端 feeOf() 亦同（daysSince - confirmLag）。
    """
    d = (fund or {}).get("buy_date")
    if not d:
        return None
    try:
        d0 = datetime.date.fromisoformat(str(d)[:10])
    except Exception:
        return None
    try:
        ftype, _share = fund_fee_type(fund or {})
        lag = confirm_lag(ftype)
    except Exception:
        lag = 1
    return max(0, ((today or datetime.date.today()) - d0).days - lag)


def redemption_fee_rate(days, name=None):
    """按持有天数返回赎回费率(%)。days 为 None 时返回 None(不确定)。"""
    if days is None:
        return None
    for limit, fee in (REDEEM_FEE_C if is_c_class(name) else REDEEM_FEE_A):
        if days < limit:
            return fee
    return 0.0


def switch_cost(fund):
    """计算单只基金的调仓成本画像。

    返回 {days, fee_rate, fee_amount, need_gain, warn, note}:
      days       持有天数(None=未知)
      fee_rate   赎回费率%(None=未知)
      fee_amount 按当前金额估算的赎回费(元)
      need_gain  换仓需涨超多少才划算(%)，= 赎回费率 + 空仓期波动成本
      warn       是否处于惩罚性费率区(<7日)
      note       可直接展示的提示文案(无提示时为空串)
    """
    name = (fund or {}).get("name")
    days = holding_days(fund)
    rate = redemption_fee_rate(days, name)
    amount = float((fund or {}).get("amount") or 0)
    if days is None or rate is None:
        return {"days": None, "fee_rate": None, "fee_amount": None,
                "need_gain": None, "warn": False, "note": ""}
    fee_amount = round(amount * rate / 100.0, 2)
    drift = round(out_of_market_cost_pct(), 2)
    need_gain = round(rate + drift, 2)
    if rate >= 1.5:
        note = (f"持有仅 {days} 天，未满 7 天赎回收 1.5% 惩罚性费率"
                f"（约 ¥{fee_amount}）— 此时调仓为负期望，建议至少持有满 7 天")
    elif rate > 0:
        note = (f"持有 {days} 天，赎回费约 {rate}%（约 ¥{fee_amount}），"
                f"加上约 {OUT_OF_MARKET_DAYS} 个交易日空仓的波动成本，"
                f"调仓需涨超 {need_gain}% 才划算")
    else:
        note = (f"持有 {days} 天，已免赎回费；但调仓仍需空仓约 "
                f"{OUT_OF_MARKET_DAYS} 个交易日，波动成本约 {drift}%，"
                f"需涨超 {need_gain}% 才划算（C 类另计销售服务费）")
    return {"days": days, "fee_rate": rate, "fee_amount": fee_amount,
            "need_gain": need_gain, "warn": rate >= 1.5, "note": note}


def fee_suffix(fund):
    """生成附加到调仓建议末尾的费率提示；无需提示时返回空串。"""
    c = switch_cost(fund)
    return ("；【费率】" + c["note"]) if c.get("note") else ""


# ---------------------------------------------------------------------------
# 成本基准与真实持仓收益 + 调仓净收益估算
#
# 背景：系统原以「单字段 amount=当前总值」为唯一口径，不维护本金/成本，
# 因此只能看「当日盈亏」，看不到「累计持有收益」，调仓也只知道费率、算不出净差。
# 本段补回这块：优先用户录入 cost；未录入时用「买入日净值 → 最新净值」反推估算。
# ---------------------------------------------------------------------------
def _base_nav_at(code, buy_date):
    """买入日(含)最早的已公布净值 -> (date, nav)；取不到返回 (None, None)。"""
    rows = fetch_nav_history(code, since_date=buy_date)   # 降序，最新在前
    if not rows:
        return None, None
    after = [x for x in rows if x[0] >= buy_date]
    if not after:
        return None, None
    return after[-1]     # 降序里最后一条 = 离买入日最近的一条


def fund_cost_estimate(f):
    """估算/读取基金「买入成本(本金)」。返回 (cost, source)。

    source: 'input'(用户录入) / 'nav_est'(按买入日净值反推) / None(无法确定)。
    反推原理：amount 为当前市值、buy_date 为买入日；单次买入下
        cost = amount × 买入日净值 / 最新净值。
    若期间有加仓，buy_date 不更新会低估成本，故标注为「估算」。
    """
    c = (f or {}).get("cost")
    if c not in (None, "", 0):
        try:
            c = float(c)
            if c > 0:
                return round(c, 2), "input"
        except Exception:
            pass
    code = (f or {}).get("code")
    buy = (f or {}).get("buy_date")
    amount = float((f or {}).get("amount") or 0)
    if not code or not buy or amount <= 0:
        return None, None
    rows = fetch_nav_history(code, since_date=buy)
    if not rows:
        return None, None
    latest_nav = rows[0][1]
    _bd, base_nav = _base_nav_at(code, buy)
    if latest_nav and base_nav and latest_nav > 0:
        return round(amount * base_nav / latest_nav, 2), "nav_est"
    return None, None


def set_cost(code, value):
    """录入/更新基金买入成本(本金)，用于精确计算真实持仓收益。返回 (ok, dict_or_error)。"""
    code = str(code).strip()
    try:
        value = float(value)
    except Exception:
        return False, "成本需为数字"
    if value <= 0:
        return False, "成本需大于 0"
    funds = load_funds()
    f = next((x for x in funds if x["code"] == code), None)
    if f is None:
        return False, "持仓中没有这只基金"
    f["cost"] = round(value, 2)
    save_funds(funds)
    return True, {"code": code, "cost": round(value, 2)}


def net_switch_estimate(f, upside_pct=None):
    """调仓净收益估算：赎回费 + 空仓期波动成本 vs 换仓潜在上行。

    返回 {ok, days, fee_rate, fee_amount, out_cost_pct, total_cost_pct,
          breakeven_pct, upside_pct, net_pct, verdict, note}。
    upside_pct: 预期换仓潜在上行(%)（可选，来自基金级预测的期望涨跌）。
    """
    sc = switch_cost(f)
    if sc.get("fee_rate") is None:
        return {"ok": False, "note": ""}
    out_cost = round(out_of_market_cost_pct(), 2)
    total = round(sc["fee_rate"] + out_cost, 2)
    net = (round(upside_pct - total, 2)
           if isinstance(upside_pct, (int, float)) else None)
    verdict = ("划算" if net > 0 else "不划算") if net is not None else None
    return {
        "ok": True,
        "days": sc.get("days"),
        "fee_rate": sc["fee_rate"],
        "fee_amount": sc.get("fee_amount"),
        "out_cost_pct": out_cost,
        "total_cost_pct": total,
        "breakeven_pct": total,
        "upside_pct": (round(upside_pct, 2)
                       if isinstance(upside_pct, (int, float)) else None),
        "net_pct": net,
        "verdict": verdict,
        "note": ("调仓总成本约 %.2f%%（赎回费 %.2f%% + 空仓波动 %.2f%%），"
                 "换仓需涨超 %.2f%% 才划算" % (total, sc["fee_rate"], out_cost, total)),
    }


def advise(regime, rows, sector_map_yi=None, overall_yi=0.0,
           trends=None, news_label=None, concept_net=None,
           fund_forecast_map=None):
    """
    基于盘面 regime 与持仓，生成调仓建议。
    权益基金以「所属板块的实时主力净流入」为首要信号(基金=板块篮子，不是单只股票)。
    sector_map_yi: {板块名: 主力净流入_亿元}(来自 15s 实时资本通道)；overall_yi: 全市场主力净流入。
    trends: 可选，trend.get_trends_for_funds(rows) 的输出 {market, funds:{code:{nav,concept,main_concept}}}，
            提供 1-3 个月中期趋势，使建议不被"一日游"行情误导。
    news_label: 可选，消息面标签(偏利好/中性/偏利空)，作为方向判断的辅助信号之一。
    concept_net: 可选，{概念板块名:{net,pct}} 实时概念资金流；命中基金主概念时优先用作板块信号(题材级，更贴近真实方向)。
    返回 {summary, market_trend, items:[{...,suggest, med_label, med_detail, ret_1m, ret_3m, max_dd}]}
    """
    sector_map_yi = sector_map_yi or {}
    trend_funds = (trends or {}).get("funds", {}) if trends else {}
    market_t = (trends or {}).get("market") if trends else None
    items = []
    for r in rows:
        # 板块信号优先用「主概念题材」的实时主力净流入(与"我的基金"表主概念一致，更贴近真实方向)；
        # 无主概念、或该概念当日无资金数据时，回退到行业板块信号(FUND_SECTOR_MAP)。
        cons = fund_concepts(r["code"], r)
        main_c = cons[0] if cons else None
        cn = (concept_net or {}).get(main_c) if main_c else None
        if cn is not None and cn.get("net") is not None:
            sector, net = main_c, round(cn["net"] / 1e8, 1)
        else:
            sig = resolve_fund_sector(r["code"], sector_map_yi, overall_yi)
            sector, net = sig["sector"], sig["net_yi"]
        pct = r.get("pct")
        pct_txt = _sign_label(pct)
        tf = trend_funds.get(r["code"], {}) or {}
        nav_t = tf.get("nav")
        concept_t = tf.get("concept")
        if r["kind"] == "defensive":
            if regime == "risk_off":
                sug = "持有作为防御底仓，继续充当资金避风港"
            elif regime == "risk_on":
                sug = "可保留做现金管理，权益机会来临时再调配"
            else:
                sug = "持有，作为组合的稳定器"
            med, med_detail = _med_classify(nav_t, concept_t)
        else:  # 权益型 -> 当日信号 + 中期趋势
            sug = _equity_suggest_v2(regime, sector, net, pct, nav_t, concept_t)
            med, med_detail = _med_classify(nav_t, concept_t)
        # 中期趋势要点(用于展示)
        src = concept_t if (concept_t and concept_t.get("ok")) else nav_t
        ret_1m = (src or {}).get("ret_1m")
        ret_3m = (src or {}).get("ret_3m")
        max_dd = (src or {}).get("max_dd")
        fee = switch_cost(r)
        # 调仓净收益：成本(赎回费+空仓波动) vs 基金级预测的期望上行
        _ff = (fund_forecast_map or {}).get(r["code"]) or {}
        nsw = net_switch_estimate(r, _ff.get("exp_pct"))
        # 持仓研判聚焦「行情·资金流向·主力·消息面」，不再以赎回费率作为调仓约束/提示。
        # (费率仍在基金卡片「赎回费」处如实展示，供交易时自行参考，但不影响此处方向判断。)
        if r["kind"] != "defensive" and news_label in ("偏利好", "偏利空"):
            sug = sug + ("；消息面偏暖，留意资金流入的题材催化" if news_label == "偏利好"
                         else "；消息面偏空，注意规避利空扰动、控制追高")
        items.append({**r, "pct_txt": pct_txt, "sector": sector,
                      "sector_net_yi": net, "suggest": sug,
                      "med_label": med, "med_detail": med_detail,
                      "ret_1m": ret_1m, "ret_3m": ret_3m, "max_dd": max_dd,
                      "main_concept": tf.get("main_concept"),
                      "fee": fee, "net_switch": nsw})

    # 综述
    def_n = sum(1 for r in rows if r["kind"] == "defensive")
    failed = [it for it in items if it["kind"] == "equity" and it["med_label"] == "down"]
    up_n = [it for it in items if it["kind"] == "equity" and it["med_label"] == "up"]
    eq_n = len(rows) - def_n
    eq_items = [it for it in items if it["kind"] == "equity"]
    inflow_n = sum(1 for it in eq_items if (it.get("sector_net_yi") or 0) > 0)
    outflow_n = sum(1 for it in eq_items if (it.get("sector_net_yi") or 0) < 0)
    # 盘面中期语境
    mkt_txt = ""
    if market_t and market_t.get("ok"):
        m1 = market_t.get("ret_1m"); m3 = market_t.get("ret_3m")
        mkt_txt = (f"盘面(沪深300)近1月 {_sign_label(m1)}、近3月 {_sign_label(m3)}，"
                   f"呈「{market_t.get('label','—')}」；")
    sector_note = ""
    if eq_items:
        sector_note = (f"你的权益基金所属板块中，{inflow_n} 个板块主力净流入、{outflow_n} 个净流出；"
                       f"调仓以板块资金流向为首要信号（基金是板块篮子，看板块主力进出比看单只价格更代表方向）。")
    if regime == "risk_off":
        summary = (f"当前为防御市，{mkt_txt}你的持仓含 {def_n} 只防御型、{eq_n} 只权益型基金；"
                   f"建议权益类逢高减仓、把资金向货币/国债/高股息等防御品种集中，"
                   f"总仓控制在低位、不追当日大涨。{sector_note}")
    elif regime == "risk_on":
        summary = (f"当前为积极市，{mkt_txt}持仓含 {def_n} 只防御型、{eq_n} 只权益型基金；"
                   f"可逢回调小幅加仓权益类，但仍需分批、避免单行业过重。{sector_note}")
    else:
        summary = (f"当前为中性震荡市，{mkt_txt}持仓含 {def_n} 只防御型、{eq_n} 只权益型基金；"
                   f"维持现有结构，做防御与卫星仓的微调即可。{sector_note}")
    if failed:
        summary += (f" 其中 {len(failed)} 只权益基金近1-3月中期仍处下行，"
                    f"反弹不追高、以等待企稳为主。")
    # 消息面(利好/利空)语境：与行情/资金/主力并列的方向辅助信号。
    if news_label == "偏利好":
        summary += " 消息面偏暖，可顺势关注资金流入的主线题材。"
    elif news_label == "偏利空":
        summary += " 消息面偏空，注意规避利空扰动、控制追高。"
    return {"summary": summary, "market_trend": market_t, "items": items}
