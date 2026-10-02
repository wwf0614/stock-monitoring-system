// 新增能力的前端回归：基金级预测(5日区间/上行概率)、调仓经济性、题材情绪、参数自校准
// 独立于既有套件，自带 mock DOM 垫片（appendChild 会记录子树，便于读回渲染结果）
const fs = require("fs");
const html = fs.readFileSync("D:/盯盘系统/dashboard.html", "utf8");
const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)];
let code = scripts.map(s => s[1]).sort((a, b) => b.length - a.length)[0];
code += "\n;globalThis.__S=S;globalThis.__renderFunds=renderFunds;"
      + "globalThis.__renderAct=renderAct;globalThis.__renderHit=renderHit;"
      + "globalThis.__netSwitchOf=netSwitchOf;globalThis.__srcTxt=srcTxt;"
      + "globalThis.__go=go;globalThis.__VIEW_KEY=VIEW_KEY;"
      + "globalThis.__tipItem=tipItem;globalThis.__exitHtml=exitHtml;globalThis.__heldHtml=heldHtml;"
      + "globalThis.__renderTipVerdict=renderTipVerdict;globalThis.__renderTips=renderTips;"
      + "globalThis.__renderVerdict=renderVerdict;globalThis.__feeOf=feeOf;";

const mkEl = () => {
  const e = {
    textContent: "", className: "", value: "", style: {},
    dataset: {}, onclick: null, _children: [],
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(n) { e._children.push(n); },
    querySelector: () => mkEl(), querySelectorAll: () => [],
    addEventListener() {}, focus() {}, remove() {},
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
  };
  let _html = "";
  // 与真实 DOM 一致：写 innerHTML 会替换掉全部子节点
  Object.defineProperty(e, "innerHTML", {
    get() { return _html; },
    set(v) { _html = v == null ? "" : String(v); e._children = []; },
  });
  return e;
};
function textOf(node) {
  if (!node) return "";
  let s = String(node.innerHTML || "") + String(node.textContent || "");
  (node._children || []).forEach(c => { s += textOf(c); });
  return s;
}
/* localStorage 用 Map 支撑：视图恢复逻辑需要在 eval 之前预置已存视图 */
const STORE = new Map([["ashare_token", "tok"]]);
global.localStorage = {
  getItem: k => (STORE.has(k) ? STORE.get(k) : null),
  setItem: (k, v) => STORE.set(k, String(v)),
  removeItem: k => STORE.delete(k),
};
/* 视图/页签元素：记录 classList 变更，用于断言「刷新后停在原页面」 */
function mkClsEl(id, v) {
  const e = mkEl();
  const set = new Set();
  e.id = id; e.dataset.v = v;
  e.classList = {
    add(c) { set.add(c); }, remove(c) { set.delete(c); },
    toggle(c, force) {
      if (force === undefined) { set.has(c) ? set.delete(c) : set.add(c); }
      else if (force) { set.add(c); } else { set.delete(c); }
    },
    contains(c) { return set.has(c); },
  };
  e._cls = set;
  return e;
}
const VIEW_ELS = ["hold", "act", "flow", "mkt"].map(v => mkClsEl("v-" + v, v));
const els = {};
const getEl = s => (els[s] = els[s] || mkEl());
global.document = {
  querySelector: getEl,
  querySelectorAll: sel => (sel === ".view" || sel === "#tabbar .tab" || sel === "#railNav .nav-i")
    ? VIEW_ELS : [],
  createElement: () => mkEl(), hidden: false, body: mkEl(),
  documentElement: { dataset: {} },
  addEventListener() {}, getElementById: () => mkEl(),
};
/* 预置「上次停在资金页」，验证启动即恢复 */
STORE.set("ashare_view", "flow");
global.fetch = () => Promise.reject(new Error("offline"));
global.addEventListener = () => {};
global.AbortController = class { constructor() { this.signal = {}; } abort() {} };
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.clearTimeout = () => {};
global.navigator = { userAgent: "node" };
global.window = global;
global.scrollTo = () => {};

let fail = 0, pass = 0;
function chk(name, out, must, mustNot) {
  const miss = (must || []).filter(s => out.indexOf(s) < 0);
  const leak = (mustNot || []).filter(s => out.indexOf(s) >= 0);
  if (miss.length || leak.length) {
    fail++;
    console.log("  FAIL " + name + (miss.length ? " | 缺: " + miss.join(" / ") : "")
      + (leak.length ? " | 多: " + leak.join(" / ") : ""));
  } else { pass++; console.log("  PASS " + name); }
}

eval(code);

const FUND = {
  code: "012738", name: "广发创新药ETF联接C", amount: 494,
  buy_date: "2026-07-27", market_value: 500, pct: 1.2, hold_pct: 1.2, hold_pnl: 6,
  concepts: ["创新药"], sector: "创新药", sector_net_yi: 1.2, nav_fresh: false,
};

console.log("[基金级预测：5日预期区间 + 上行概率]");
try {
  globalThis.__S.funds = [FUND];
  globalThis.__S.fundFc = { "012738": { code: "012738", exp_pct: 4.03, low_pct: -4.03, high_pct: 12.09, prob_up: 69 } };
  globalThis.__renderFunds();
  const out = textOf(els["#fundList"]);
  chk("卡片显示 5 日预期", out, ["5日", "+4.03%", "上行概率", "69%"]);
} catch (e) { fail++; console.log("  FAIL 基金级预测 | " + e.message); }

console.log("[保守渲染：无预测数据时不出现该行]");
try {
  globalThis.__S.fundFc = {};
  globalThis.__renderFunds();
  chk("无预测→不渲染", textOf(els["#fundList"]), [], ["5日", "上行概率"]);
} catch (e) { fail++; console.log("  FAIL 无预测渲染 | " + e.message); }

console.log("[netSwitchOf 取值]");
try {
  globalThis.__S.board = { fund_advice: { items: [
    { code: "012738", net_switch: { ok: true, days: 8, total_cost_pct: 3.1, breakeven_pct: 3.1, upside_pct: -2.21, net_pct: -5.31, verdict: "不划算" } }
  ] } };
  const ns = globalThis.__netSwitchOf("012738");
  chk("按代码取到调仓经济性", JSON.stringify(ns || {}), ["total_cost_pct", "不划算"]);
  chk("未知代码→null", String(globalThis.__netSwitchOf("999999")), ["null"]);
} catch (e) { fail++; console.log("  FAIL netSwitchOf | " + e.message); }

console.log("[调仓经济性展示在基金卡片明细]");
try {
  globalThis.__S.fundFc = { "012738": { code: "012738", exp_pct: 4.03, prob_up: 69 } };
  globalThis.__S.board = { fund_advice: { items: [
    { code: "012738", net_switch: { ok: true, total_cost_pct: 3.1, breakeven_pct: 3.1, upside_pct: -2.21, net_pct: -5.31, verdict: "不划算" } }
  ] } };
  globalThis.__renderFunds();
  const out = textOf(els["#fundList"]);
  chk("明细含调仓经济性", out, ["调仓经济性", "成本 3.1%", "打平 3.1%", "不划算"]);
} catch (e) { fail++; console.log("  FAIL 调仓经济性 | " + e.message); }

console.log("[题材情绪：消息面按主题轨道归拢]");
try {
  globalThis.__S.board = {
    confidence: "中", server_time: "2026-09-13 21:00:00",
    recommendation: { position_low: 20, position_high: 40, position_mid: 30, action: "观望", position_basis: "x", position_signals: [] },
    scores: { composite: 5, technical: 5, sentiment: 4, capital: 6, news: 3, risk: 5 },
    fund_advice: { items: [{ code: "012738", name: "广发创新药ETF联接C", pct: 1.2, suggest: "持有观察" }] },
    next_step: { headline: "等待企稳", bullets: ["不加仓"] },
    news_topic: { tracks: {
      "创新药": { n: 3, bull: 2, bear: 0, score: 5.0 },
      "半导体": { n: 2, bull: 0, bear: 1, score: -2.0 },
      "CPO光模块": { n: 0, bull: 0, bear: 0, score: 0.0 },
    } },
  };
  globalThis.__renderAct();
  const out = textOf(els["#adviceList"]);
  chk("渲染题材情绪块", out, ["题材情绪", "创新药", "半导体", "+5.00", "−2.00"]);
  chk("零条目不显示", out, [], ["CPO光模块"]);
} catch (e) { fail++; console.log("  FAIL 题材情绪 | " + e.message); }

console.log("[参数自校准 提示]");
try {
  const box = getEl("#hitBox");
  const base = { ok: true, ready: true, total: 42, settled: 35, pending: 7, hit_rate: 58.6,
                 strict_hit_rate: 45.7, partial_rate: 25.7, brier: 0.22, calibration: [] };
  box.innerHTML = ""; globalThis.__S.hit = Object.assign({}, base,
    { tuned: { applied: false, n: 1, note: "样本不足(需≥20)，沿用默认参数" } });
  globalThis.__renderHit();
  chk("样本不足→明示沿用默认", box.innerHTML, ["参数自校准", "沿用默认"]);
  box.innerHTML = ""; globalThis.__S.hit = Object.assign({}, base,
    { tuned: { applied: true, n: 25 } });
  globalThis.__renderHit();
  chk("样本充足→显示已微调", box.innerHTML, ["参数自校准", "已按实测命中率微调", "n=25"]);
  box.innerHTML = ""; globalThis.__S.hit = base;
  globalThis.__renderHit();
  chk("无 tuned→不渲染该行", box.innerHTML, [], ["参数自校准"]);
} catch (e) { fail++; console.log("  FAIL 参数自校准 | " + e.message); }

console.log("[估算来源如实标注：不能把估算值说成官方净值]");
try {
  const st = globalThis.__srcTxt;
  const cases = [
    [{ pct_src: "tick" }, "交易所实时价"],
    [{ pct_src: "nav", nav_date: "2026-09-11" }, "官方净值 2026-09-11"],
    [{ pct_src: "est", est_src: "concept" }, "盘中估算（概念板块）"],
    [{ pct_src: "est", est_src: "etf" }, "盘中估算（底层ETF）"],
    [{ pct_src: "est", est_src: "holdings" }, "盘中估算（重仓股加权）"],
    [{ pct_src: "est" }, "盘中估算"],
  ];
  cases.forEach(c => chk("srcTxt " + JSON.stringify(c[0]), st(c[0]), [c[1]]));
  chk("估算不得标成官方净值", st({ pct_src: "est", est_src: "concept" }), [], ["官方净值"]);
} catch (e) { fail++; console.log("  FAIL srcTxt | " + e.message); }

console.log("[视图保持：刷新后停在原页面，不跳回「持仓」]");
try {
  const active = () => VIEW_ELS.filter(e => e._cls.has("on")).map(e => e.dataset.v);
  chk("启动即恢复为存储的「资金」", active().join(","), ["flow"], ["hold"]);
  chk("首屏 html[data-view] 已同步", String(document.documentElement.dataset.view), ["flow"]);
  chk("副导航/页签同步高亮", String(VIEW_ELS.filter(e => e._cls.has("on")).length), ["1"]);
  globalThis.__go("mkt");
  chk("切到市场后仅市场高亮", active().join(","), ["mkt"], ["flow", "hold"]);
  chk("切换后写入本地存储", String(STORE.get("ashare_view")), ["mkt"]);
  chk("非持仓页隐藏 FAB", String(getEl("#fab").style.display), ["none"]);
  globalThis.__go("hold");
  chk("回持仓页显示 FAB", String(getEl("#fab").style.display), ["grid"]);
  globalThis.__go("bogus");
  chk("非法视图回落「持仓」", active().join(","), ["hold"], ["bogus"]);
  chk("非法值被规整后写入", String(STORE.get("ashare_view")), ["hold"]);
} catch (e) { fail++; console.log("  FAIL 视图保持 | " + e.message); }

console.log("[卖出信号：说清影响哪只持仓 + 调仓成本]");
try {
  const it = globalThis.__tipItem({
    rule: "S7", side: "sell", board: "算力概念", kind: "concept", pct: 0.92, sev: "warn",
    title: "拉高派发", evidence: "股价涨 0.92% 而主力净流出 3.2 亿",
    funds: [{ code: "016874", name: "广发远见智选混合C", amount: 1273.1, days: 15,
              fee_rate: 0.5, fee_amount: 6.37, need_gain: 3.1, warn: false }],
    exit: { n: 1, days: 15, fee_rate: 0.5, fee_amount: 6.37, need_gain: 3.1, warn: false },
  });
  chk("带出影响持仓与赎回成本", it,
    ["影响持仓", "广发远见智选混合C", "¥1273", "持有 15 天", "赎回费 0.5%", "≈¥6.37", "需涨超 3.1%"]);
  chk("未踩红线时不乱报警", it, [], ["监管红线"]);

  const it2 = globalThis.__tipItem({
    rule: "S1", side: "sell", board: "PCB", kind: "concept", pct: -1.0, sev: "danger",
    title: "散户接盘结构", evidence: "x",
    funds: [{ code: "A", name: "甲混合C", amount: 1000, days: 2,
              fee_rate: 1.5, fee_amount: 15, need_gain: 4.1, warn: true }],
    exit: { n: 1, days: 2, fee_rate: 1.5, fee_amount: 15, need_gain: 4.1, warn: true },
  });
  chk("未满 7 日明示红线与负期望", it2, ["1.5%", "监管红线", "负期望"]);

  const buy = globalThis.__tipItem({ rule: "B1", side: "buy", board: "半导体", kind: "concept",
    pct: 2.0, sev: "ok", title: "资金与价格共振", evidence: "y", strength: 9.2 });
  chk("买入信号不出现费率块（缺失即不渲染）", buy, [], ["影响持仓", "赎回费"]);
  chk("无 exit 字段时同样不渲染", globalThis.__exitHtml({ board: "X" }), [], ["影响持仓"]);
} catch (e) { fail++; console.log("  FAIL 卖出费率块 | " + e.message); }

console.log("[买入候选：命中已持有主题必须标出来（与结论层「加仓」同口径）]");
try {
  /* 持仓 4 只基金概念高度重叠。候选若正好命中已持有主题，再开新基金=重复暴露，
     所以必须标出来，否则「今日结论」说加仓、下面列表却像在推新标的，两张卡自相矛盾。 */
  const heldIt = globalThis.__tipItem({ rule: "B1", side: "buy", board: "半导体", kind: "concept",
    pct: 2.0, sev: "ok", title: "资金与价格共振", evidence: "y", strength: 9.2,
    held: true, held_funds: [{ code: "017811", name: "东方人工智能主题混合C" }] });
  chk("标出已持有并点名是哪只持仓", heldIt,
    ["已持有本主题", "东方人工智能主题混合C", "优先加已有持仓"]);
  chk("已持有时不冒充新标的（不出现费率块）", heldIt, [], ["影响持仓"]);

  const freshIt = globalThis.__tipItem({ rule: "B1", side: "buy", board: "创新药", kind: "concept",
    pct: 1.8, sev: "ok", title: "x", evidence: "y", strength: 8.1, held: false });
  chk("未持有的候选不出现该提示", freshIt, [], ["已持有本主题"]);

  chk("缺 held 字段时不渲染（旧后端兼容）",
      globalThis.__heldHtml({ board: "X" }), [], ["已持有"]);
  chk("held=false 同样不渲染",
      globalThis.__heldHtml({ board: "X", held: false }), [], ["已持有"]);
  chk("给了 held 但缺基金名时仍成句（不塌成半句）",
      globalThis.__heldHtml({ board: "X", held: true }), ["已持有本主题", "优先加已有持仓"]);
} catch (e) { fail++; console.log("  FAIL 买入已持有标注 | " + e.message); }

console.log("[多日口径必须显示：区间涨幅 / 单日-多日确认 / 持仓基金自身净值]");
try {
  /* T+1：场外基金按当日收盘净值确认，单日涨跌不可交易。所以页面上必须看得出
     「这条信号基于多日趋势还是单日」以及「区间涨了多少」——只显示当日涨跌，等于在
     暗示可以按单日操作，那正是本次要修掉的误导。 */
  const b1 = globalThis.__tipItem({ rule: "B1", side: "buy", board: "半导体", kind: "concept",
    pct: 1.2, sev: "ok", title: "多日趋势主线", evidence: "近 4 日累计主力净流入 110.0 亿",
    strength: 8.4, win_days: 4, win_pct: 5.1 });
  chk("买入条目显示区间涨幅（不只当日）", b1, ["近 4 日", "5.10%", "1.20%"]);

  const sMulti = globalThis.__tipItem({ rule: "S3", side: "sell", board: "创新药",
    kind: "concept", pct: -0.6, sev: "danger", title: "连续 3 日主力净流出", evidence: "x",
    win_days: 3, win_pct: -4.2, confirmed: true });
  chk("多日确认的卖出信号标「多日确认」", sMulti, ["多日确认", "近 3 日", "4.20%"]);

  const sSingle = globalThis.__tipItem({ rule: "S7", side: "sell", board: "PCB",
    kind: "concept", pct: 1.9, sev: "warn", title: "拉高派发", evidence: "x",
    win_days: 3, win_pct: 1.1, confirmed: false });
  chk("单日卖出信号标「单日」且不冒充多日确认", sSingle, ["单日"], ["多日确认"]);

  const noWin = globalThis.__tipItem({ rule: "S1", side: "sell", board: "X",
    kind: "concept", pct: 0.1, sev: "warn", title: "t", evidence: "e" });
  chk("缺多日字段时不渲染该块（旧后端兼容）", noWin, [], ["近 ", "多日确认", "单日"]);

  const withNav = globalThis.__exitHtml({ board: "X", funds: [
    { code: "A", name: "甲混合C", amount: 1000, days: 15, fee_rate: 0.5, fee_amount: 5,
      need_gain: 3.1, warn: false, nav: { as_of: "2026-09-18", ret20: -3.2, ma20_gap: -1.8 } }],
    exit: { n: 1, days: 15, fee_rate: 0.5, fee_amount: 5, need_gain: 3.1, warn: false } });
  chk("持仓基金自身净值趋势一并展示", withNav, ["净值近20日", "3.20%"]);
  chk("无 nav 字段时不显示净值行", globalThis.__exitHtml({ board: "X", funds: [
    { code: "A", name: "甲C", days: 15, fee_rate: 0.5, warn: false }],
    exit: { days: 15, fee_rate: 0.5, warn: false } }), [], ["净值近20日"]);
} catch (e) { fail++; console.log("  FAIL 多日口径展示 | " + e.message); }

console.log("[今日结论：该买还是该卖]");
try {
  const box = getEl("#tipVerdict");
  /* renderVerdict 写的是各子节点的 textContent/innerHTML，不是 box.innerHTML，
     所以要把子节点串起来看 */
  const vTxt = () => ["#tvGlyph", "#tvAction", "#tvHead", "#tvReasons", "#tvActions", "#tvNote"]
    .map(s => textOf(getEl(s))).join(" ");

  globalThis.__renderTipVerdict({
    action: "减仓", tone: "sell", headline: "持仓出现 2 条风险提醒（涉及 1 只持仓），动手成本可接受",
    reasons: ["命中风险提醒：半导体 资金由流入转流出（S4）", "建议分批减、不要一次清仓"],
    hold_actions: [{ code: "017811", name: "东方人工智能主题混合C", amount: 958.34,
                     days: 15, fee_rate: 0.5, rule: "S4" }],
    buy_actions: [], note: "连续多日信号还需 1 个交易日样本才启用",
  });
  chk("结论卡显示动作与理由", vTxt(),
    ["卖", "减仓", "持仓出现 2 条风险提醒", "分批减", "东方人工智能主题混合C", "¥958", "持有15天"]);
  chk("减仓走风险配色（不用红绿）", String(box.className), ["tone-sell"]);
  chk("数据不足只提示不阻断", vTxt(), ["还需 1 个交易日样本"]);

  globalThis.__renderTipVerdict({
    action: "可买", tone: "buy", headline: "半导体 资金强度 9.2/10，可小仓分批介入",
    reasons: ["无持仓风险信号", "场外标的：半导体ETF联接(007301)"],
    hold_actions: [], buy_actions: [{ board: "半导体", strength: 9.2,
      etf: "半导体ETF联接", code: "007301", held: false }],
  });
  chk("可买给到场外标的", vTxt(), ["买", "可买", "半导体ETF联接", "007301"]);
  chk("买入走机会配色", String(box.className), ["tone-buy"]);
  chk("可买时不再显示持仓处置", vTxt(), [], ["持有15天"]);

  globalThis.__renderTipVerdict({
    action: "暂不动手", tone: "watch", headline: "风险信号已出，但赎回费踩了 7 日红线",
    reasons: ["未满 7 日"], hold_actions: [], buy_actions: [],
  });
  chk("暂不动手也渲染（不静默）", vTxt(), ["暂不动手", "7 日红线"]);

  globalThis.__renderTipVerdict(null);
  chk("无结论时整块收起", String(box.className), ["off"]);
  globalThis.__renderTipVerdict(undefined);
  chk("未定义同样收起（旧后端兼容）", String(box.className), ["off"]);
  globalThis.__renderTipVerdict({});
  chk("空对象不炸且收起", String(box.className), ["off"]);
  /* 关键回归：收起后再次拿到结论必须能恢复显示（曾因 [hidden] 属性被重置而永久不显示） */
  globalThis.__renderTipVerdict({ action: "持有", tone: "hold", headline: "不动就是最优解",
                               reasons: ["无达线信号"], hold_actions: [], buy_actions: [] });
  chk("收起后能恢复显示且清掉 off", String(box.className), ["tone-hold"]);
  chk("恢复显示时不再带 off", String(box.className.indexOf("off") < 0), ["true"]);

  /* --- 公共类隔离守卫 ---
     .verdict 是公共类：持仓页决策条 #verdict（renderVerdict，无参）也在用。
     本页专属样式必须挂在 .verdict.tipv 上，否则会串改持仓页决策条的段落间距
     （踩过：全局 .verdict p 覆盖了基础规则的 var(--sp3)）。 */
  chk("结论卡带 tipv 作用域标记（防样式串页）", String(box.className), ["tipv"]);
  chk("结论卡专属样式已按 .tipv 隔离", html, ["verdict.tipv p", "verdict.tipv .bd>div"]);
  const unscopedP = (html.match(/\.verdict\s+p\s*\{/g) || []).length;
  chk("无作用域的 .verdict p 规则只剩基础那一条（新增覆盖即串页）",
      String(unscopedP), ["1"]);
} catch (e) { fail++; console.log("  FAIL 今日结论 | " + e.message); }

console.log("[半成品响应：后端 ok=true 但中途抛错时必须显出来]");
try {
  const box = getEl("#tipVerdict");
  globalThis.__renderTips({ ok: true, as_of: "2026-09-21 14:00:00", days: 1, need_days: 3,
    market: { breadth: 0.8, main_net_yi: 100 }, sell: [], sell_scan: [], buy: [],
    pending: [], rules: [], unsupported: [], notes: [], error: "KeyError: 'verdict'" });
  chk("把 error 显性渲染出来", textOf(getEl("#tipMarket")), ["部分数据计算失败", "KeyError"]);
  chk("同时结论卡收起（不显示半成品结论）", String(box.className), ["off"]);

  globalThis.__renderTips({ ok: true, as_of: "2026-09-21 14:00:00", days: 1, need_days: 3,
    market: { breadth: 0.8, main_net_yi: 100 }, sell: [], sell_scan: [], buy: [],
    pending: [], unsupported: [], notes: [],
    rules: [{ id: "S1", side: "sell", name: "散户接盘", desc: "机构兑现" }],
    verdict: { action: "持有", tone: "hold", headline: "无达线信号", reasons: ["x"],
               hold_actions: [], buy_actions: [] }, verdict_ladder: [["持有", "说明"]] });
  chk("正常响应不出现错误提示", textOf(getEl("#tipMarket")), [], ["部分数据计算失败"]);
  chk("正常响应结论卡展开", String(box.className), ["tone-hold"]);
  chk("规则说明含结论阶梯", textOf(getEl("#tipRules")), ["结论是怎么得出的", "持有"]);
} catch (e) { fail++; console.log("  FAIL 半成品响应 | " + e.message); }

console.log("[命名冲突守卫：决策页结论条 vs 投资提醒结论卡]");
try {
  /* 曾把投资提醒的结论卡也命名成 renderVerdict，与 1592 行「决策页结论条」撞名：
     后定义的覆盖前者 → 决策页结论条永不渲染，且它每次无参调用都把结论卡收起
     （症状：卡片有内容却始终不可见）。这里把两个名字钉死。 */
  chk("两个函数不是同一个（同名覆盖会静默改坏对方）",
      String(globalThis.__renderVerdict === globalThis.__renderTipVerdict), ["false"]);
  chk("决策页 renderVerdict 必须是不带参的",
      String(globalThis.__renderVerdict.length), ["0"]);
  chk("投资提醒 renderTipVerdict 必须自带参数",
      String(globalThis.__renderTipVerdict.length), ["1"]);

  const vbox = getEl("#tipVerdict");
  globalThis.__renderTipVerdict({ action: "减仓", tone: "sell", headline: "h", reasons: ["r"],
                                  hold_actions: [], buy_actions: [] });
  const before = String(vbox.className);

  globalThis.__S.funds = [FUND];
  globalThis.__S.board = {
    regime_label: "中性",
    summary: { headline: "大盘震荡", bullets: ["【仓位】保持", "仓位维持不变"] },
    recommendation: { action: "持有+小仓定投", position_low: 30, position_high: 40,
                      position_mid: 35, risk: "中低" },
  };
  globalThis.__renderVerdict();
  chk("决策页结论条渲染到 #verdict", textOf(getEl("#verdict")),
    ["持有+小仓定投", "建议仓位 30% – 40%", "大盘震荡"]);
  chk("调决策页结论条不得影响投资提醒卡片",
      String(vbox.className) === before ? "same" : ("changed:" + vbox.className), ["same"]);
} catch (e) { fail++; console.log("  FAIL 命名冲突守卫 | " + e.message); }

console.log("[降级/陈旧数据：不得被显示成「今天没风险」]");
try {
  /* 实时板块取不到时会回退到库里最近收盘格局并带 stale=true。此时若让买卖两栏落到
     「未命中持仓主题的风险信号」「当日无满足门槛的板块」这类**看起来正常**的空态，
     用户会把「我们没数据」读成「今天安全 / 没机会」——这是这一页最危险的误解。 */
  const tStale = {
    ok: true, as_of: "2026-09-22 10:00:00",
    data_src: "history", data_date: "2026-09-18", stale: true,
    days: 1, need_days: 3, market: {}, sell: [], sell_scan: [], buy: [],
    verdict: { action: "观望", tone: "watch", headline: "板块实时数据不可用",
               reasons: ["r"], hold_actions: [], buy_actions: [] },
    pending: [{ rule: "全部", title: "板块实时数据不可用", why: "w" }],
    unsupported: [], notes: [], rules: [], verdict_ladder: [],
  };
  globalThis.__renderTips(tStale);
  chk("数据日期标出「收盘 · 实时源不可用」", textOf(getEl("#tipMarket")),
      ["数据日期", "2026-09-18 收盘 · 实时源不可用"]);
  chk("状态标签显示数据未更新", String(getEl("#tipState").textContent), ["数据未更新"]);
  chk("卖出栏不得伪装成「未命中风险信号」", textOf(getEl("#tipSell")),
      ["板块实时数据不可用"], ["未命中持仓主题的风险信号"]);
  chk("买入栏不得伪装成「无满足门槛板块」", textOf(getEl("#tipBuy")),
      ["板块实时数据不可用"], ["没有满足趋势门槛的板块"]);
  chk("陈旧时结论卡为观望配色", String(getEl("#tipVerdict").className), ["tone-watch"]);
} catch (e) { fail++; console.log("  FAIL 陈旧数据展示 | " + e.message); }

console.log("[实时数据不得被误标成降级]");
try {
  const tLive = {
    ok: true, as_of: "2026-09-22 10:00:00",
    data_src: "live", data_date: "2026-09-22", stale: false,
    days: 1, need_days: 3, market: {}, sell: [], sell_scan: [], buy: [],
    verdict: { action: "持有", tone: "hold", headline: "h", reasons: [],
               hold_actions: [], buy_actions: [] },
    pending: [], unsupported: [], notes: [], rules: [], verdict_ladder: [],
  };
  globalThis.__renderTips(tLive);
  chk("实时数据标为「实时」", textOf(getEl("#tipMarket")), ["2026-09-22 实时"], ["实时源不可用"]);
  chk("实时且无信号 → 保留正常空态文案", textOf(getEl("#tipSell")),
      ["未命中持仓主题的风险信号"]);
  chk("状态标签回到正常口径", String(getEl("#tipState").textContent), ["当前无风险信号"]);
} catch (e) { fail++; console.log("  FAIL 实时展示 | " + e.message); }

console.log("\n通过 " + pass + " 项，失败 " + fail + " 项");
process.exit(fail ? 1 : 0);
