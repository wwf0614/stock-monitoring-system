// 用 mock DOM 跑一遍 renderHit，验证模板拼接在各种数据形态下都不崩
const fs = require("fs");
const path = "D:/盯盘系统/dashboard.html";
const html = fs.readFileSync(path, "utf8");
const scripts=[...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)];
let code=scripts.map(s=>s[1]).sort((a,b)=>b.length-a.length)[0];
code += "\n;globalThis.__S=S;globalThis.__renderHit=renderHit;globalThis.__renderFunds=renderFunds;";

const store = {};
const mkEl = () => ({
  innerHTML: "", textContent: "", className: "", value: "", style: {},
  dataset: {}, onclick: null,
  classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  appendChild() {}, querySelector: () => mkEl(), querySelectorAll: () => [],
  addEventListener() {}, focus() {}, remove() {},
  setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
});
global.localStorage = { getItem: () => "tok", setItem: () => {} };
const els = {};   // 按选择器缓存，便于读回 renderHit 写入的 innerHTML
const getEl = s => (els[s] = els[s] || mkEl());
global.document = {
  querySelector: getEl, querySelectorAll: () => [],
  createElement: () => mkEl(), hidden: false, body: mkEl(),
  documentElement: { dataset: {} },
  addEventListener() {}, getElementById: () => mkEl(),
};
global.fetch = () => Promise.reject(new Error("offline"));
global.addEventListener = () => {};
global.AbortController = class { constructor() { this.signal = {}; } abort() {} };
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.clearTimeout = () => {};
global.navigator = { userAgent: "node" };
global.window = global;
global.scrollTo = () => {};

let fail = 0;
function run(name, data, must, mustNot) {
  const box = getEl("#hitBox");
  box.innerHTML = "";
  try {
    globalThis.__S.hit = data;
    globalThis.__renderHit();
    const out = box.innerHTML || "";
    const miss = (must || []).filter(s => out.indexOf(s) < 0);
    const leak = (mustNot || []).filter(s => out.indexOf(s) >= 0);
    if (miss.length) throw new Error("缺少片段: " + miss.join(" / "));
    if (leak.length) throw new Error("不该出现: " + leak.join(" / "));
    console.log("  PASS " + name + " (" + out.length + " 字符)");
  } catch (e) {
    fail++;
    console.log("  FAIL " + name + " | " + e.message);
  }
}

eval(code);
console.log("[响应式静态检查]");
const responsiveChecks = [
  ["viewport-fit", /viewport-fit=cover/],
  ["320px 安全宽度", /html\{min-width:320px/],
  ["横向裁切", /overflow-x:clip/],
  ["小屏断点", /@media \(max-width:374px\)/],
  ["手机断点", /@media \(min-width:640px\)/],
  ["平板断点", /@media \(min-width:768px\)/],
  ["桌面断点", /@media \(min-width:1024px\)/],
  ["宽屏断点", /@media \(min-width:1536px\)/],
  ["安全区适配", /safe-area-inset-(?:left|right|bottom)/],
  ["触控热区", /button\{min-height:44px/],
  ["键盘焦点", /:focus-visible/],
  ["网格防溢出", /minmax\(0,1fr\)/],
  ["弹层限高", /max-height:min\(88dvh,720px\)/],
  ["移动操作区", /class=\"f-actions\"/],
  ["减少动效", /prefers-reduced-motion:reduce/],
];
responsiveChecks.forEach(([name, pattern]) => {
  if (!pattern.test(html)) {
    fail++;
    console.log("  FAIL " + name);
  } else {
    console.log("  PASS " + name);
  }
});

console.log("\n[主题切换静态检查]");
const themeChecks = [
  ["浅色主题 token", /\[data-theme="light"\]/],
  ["防闪烁读取主题", /localStorage\.getItem\("ashare_theme"\)/],
  ["切换函数", /function toggleTheme/],
  ["主题持久化", /localStorage\.setItem\("ashare_theme"/],
  ["按钮同步", /function syncThemeBtn/],
  ["logo 反白", /stroke="#fff"/],
  ["走势图用变量", /var\(--up\)/],
];
themeChecks.forEach(([name, pattern]) => {
  if (!pattern.test(html)) { fail++; console.log("  FAIL " + name); }
  else console.log("  PASS " + name);
});
if (/#06131a/.test(html)) { fail++; console.log("  FAIL 残留硬编码#06131a"); }
else console.log("  PASS 无残留硬编码#06131a");

console.log("\n[图标静态检查]");
const iconChecks = [
  ["新蜡烛影线", /x1="8" y1="6\.5"/],
  ["新蜡烛实体", /rect x="6\.4"/],
  ["新趋势线", /M6\.5 16 L12 11 L16 9/],
  ["焦点圆点", /circle cx="16" cy="9"/],
];
iconChecks.forEach(([name, pattern]) => {
  if (!pattern.test(html)) { fail++; console.log("  FAIL " + name); }
  else console.log("  PASS " + name);
});
if (/M3 17l4\.5-5 3\.5 3 5-6\.5/.test(html)) { fail++; console.log("  FAIL 旧折线箭头残留"); }
else console.log("  PASS 旧折线箭头已清除");

console.log("\n[删除功能静态检查]");
const delChecks = [
  ["jdel token 拼接修正(&)", /sep=path\.indexOf\(/],
  ["delFund 护栏函数", /function delFund/],
  ["护栏<7天强警示", /赎回费 ≥1\.5%/],
  ["卡片删除入口", /data-act="del"/],
];
delChecks.forEach(([name, pattern]) => {
  if (!pattern.test(html)) { fail++; console.log("  FAIL " + name); }
  else console.log("  PASS " + name);
});

console.log("\n[买入/卖出功能静态检查]");
const py = fs.readFileSync("D:/盯盘系统/fund_manager.py", "utf8");
const tradeChecks = [
  ["交易抽屉 DOM", /id="tradeSheet"/],
  ["openTrade 函数", /function openTrade/],
  ["confirmTrade 函数", /function confirmTrade/],
  ["updateTradeFee 函数", /function updateTradeFee/],
  ["卡片买入入口", /data-act="buy"/],
  ["卡片卖出入口", /data-act="sell"/],
  ["买入走 buy 动作", /action:"buy"/],
  ["卖出走 sell 动作", /action:"sell"/],
  ["C类免申购费提示", /C类免申购费/],
  ["赎回费率自动计算", /预估赎回费/],
  ["后端 赎回按市值口径", /市值口径/],
  ["服务费口径提示", /销售服务费约/],
  ["后端 buy_more", /def buy_more\(/],
  ["后端 redeem", /def redeem\(/],
  ["后端 _fee_rate", /def _fee_rate\(/],
];
tradeChecks.forEach(([name, pattern]) => {
  const src = (name.indexOf("后端") === 0) ? py : html;
  if (!pattern.test(src)) { fail++; console.log("  FAIL " + name); }
  else console.log("  PASS " + name);
});

console.log("\n[详情窗口刷新保持展开静态检查]");
const keepChecks = [
  ["刷新前捕获展开/输入", /const pending=\{\}/],
  ["保存 open 状态", /FUND_OPEN===f\.code/],
  ["恢复 open 状态", /"ft-row"\+\(isOpen\?" open":""\)/],
  ["回灌输入焦点", /data-code="/],
  ["光标归尾", /setSelectionRange/],
];
keepChecks.forEach(([name, pattern]) => {
  if (!pattern.test(html)) { fail++; console.log("  FAIL " + name); }
  else console.log("  PASS " + name);
});

console.log("\n[市值透明化静态检查]");
const mvChecks = [
  ["卡片主数字标市值", /label:"市值"/],
  ["详情当前市值行", /<div class="k">实时市值<\/div>/],
  ["市值标签CSS", /\.ft-num b\{/],
  ["市值顶框位", /class="ft-cell ft-num"><b/],
];
mvChecks.forEach(([name, pattern]) => {
  if (!pattern.test(html)) { fail++; console.log("  FAIL " + name); }
  else console.log("  PASS " + name);
});

console.log("\n[场外基金规则对齐静态检查]");
const py2 = fs.readFileSync("D:/盯盘系统/fund_manager.py", "utf8");
const ruleChecks = [
  ["分类费率 fundFeeType", /function fundFeeType/],
  ["免赎阈值 feeThreshold", /function feeThreshold/],
  ["确认日滞后 confirmLag", /function confirmLag/],
  ["15:00边界提示", /function settleNavLabel/],
  ["交易弹层显示成交净值", /settleNavLabel\(\)/],
  ["持有进度动态阈值", /fee\.threshold\|\|30/],
  ["重设总值文案", /重设总值/],
  ["后端 fund_fee_type", /def fund_fee_type\(/],
  ["后端确认日滞后", /def confirm_lag\(/],
  ["后端加仓累加总值", /f\["amount"\] = float\(f\.get\("amount", 0\) or 0\) \+ amount/],
  ["后端重设本金清份额", /exist\.pop\("shares", None\)/],
  ["后端赎回按金额减仓", /new_total = max\(0\.0, total - amount\)/],
];
ruleChecks.forEach(([name, pattern]) => {
  const src = (name.indexOf("后端") === 0) ? py2 : html;
  if (!pattern.test(src)) { fail++; console.log("  FAIL " + name); }
  else console.log("  PASS " + name);
});

console.log("\n[Hero 走势图(60日窗口/可平移/30s轮动)静态检查]");
const trendChecks = [
  ["走势窗口 60 日", /const TREND_WIN=60/],
  ["轮动间隔 30 秒", /const TREND_ROT=30000/],
  ["五指数轮动序列", /TREND_NAMES=\["上证指数","深证成指","创业板指","科创50","沪深300"\]/],
  ["取一年历史供平移", /index_kline\?lmt=250/],
  ["平移只改 transform", /function applyPan/],
  ["拖动平移(无箭头按钮)", /function bindTrend/],
  ["箭头按钮已清除", /sp-nav/],
  ["顶栏联动高亮", /function syncTapeHi/],
  ["顶栏点击切换到对应指数", /function selectTrendByName/],
  ["顶栏点击事件委托绑定", /function bindTape/],
  ["顶栏标签可点击", /\.tape\{cursor:pointer/],
  ["手动平移后暂停轮动", /function holdTrend/],
  ["轮动启动一次", /let TREND_STARTED=false/],
  ["走势区容器", /class="spark-body"/],
  ["旧版 40 日硬编码已清除", /slice\(-40\)/],
];
trendChecks.forEach(([name, pattern]) => {
  const hit = pattern.test(html);
  const want = name.indexOf("已清除") < 0;
  if (hit === want) console.log("  PASS " + name);
  else { fail++; console.log("  FAIL " + name); }
});

console.log("\n[视图保持(刷新后停在原页面)静态检查]");
const viewChecks = [
  ["视图键位 ashare_view", /ashare_view/],
  ["首屏按存储决定视图", /dataset\.view=v/],
  ["head 内校验白名单", /v!=="hold"&&v!=="act"&&v!=="flow"&&v!=="mkt"/],
  ["切换时持久化", /localStorage\.setItem\(VIEW_KEY,v\)/],
  ["启动恢复上次视图", /function restoreView/],
  ["非法视图回落持仓", /if\(VIEWS\.indexOf\(v\)<0\) v=VIEWS\[0\]/],
  ["首屏隐藏非当前视图", /html\[data-view\] \.view\{display:none\}/],
  ["首屏显示目标视图", /html\[data-view="flow"\] #v-flow/],
  ["FAB 仅持仓页显示", /\$\("#fab"\)\.style\.display=\(v==="hold"\)\?"grid":"none"/],
];
viewChecks.forEach(([name, pattern]) => {
  if (pattern.test(html)) console.log("  PASS " + name);
  else { fail++; console.log("  FAIL " + name); }
});
// 反向：页签点击绑定不能被误删（曾因一次编辑误删导致点击页签无反应）
if (/#tabbar \.tab, #railNav \.nav-i"\)\.forEach\(b=>b\.onclick=\(\)=>go\(b\.dataset\.v\)\)/.test(html)) {
  console.log("  PASS 页签/导航点击绑定存在");
} else { fail++; console.log("  FAIL 页签/导航点击绑定存在"); }

console.log("\n[renderFunds 冒烟(含买入/卖出按钮绑定)]");
try {
  globalThis.__S.funds = [{
    code: "012738", name: "广发创新药ETF联接C", amount: 494, principal: 494,
    buy_date: "2026-07-27", market_value: 500, pct: 1.2, hold_pct: 1.2, hold_pnl: 6,
    concepts: ["创新药"], sector: "创新药", sector_net_yi: 1.2, nav_fresh: false,
  }];
  globalThis.__renderFunds();
  console.log("  PASS renderFunds 执行无异常");
} catch (e) {
  fail++;
  console.log("  FAIL renderFunds | " + e.message);
}

console.log("\n[renderHit 冒烟]");

// 1) 空 / 未就绪 —— 硬约束：样本不足时绝不渲染任何"命中率"数字
run("未就绪(样本不足)", { ok: true, ready: false, total: 1, pending: 1, note: "已记录 1 次研判" },
    ["已记录 1 次研判", "待回填"], ["实测命中率", "BRIER", "校准"]);
run("null", null, [], ["实测命中率"]);
run("空对象", {}, [], ["实测命中率"]);

// 2) 完整数据
run("完整统计", {
  ok: true, ready: true, total: 42, settled: 35, pending: 7,
  hit_rate: 58.6, strict_hit_rate: 45.7, partial_rate: 25.7,
  brier: 0.238, brier_n: 35,
  calibration: [
    { bucket: "35-50", n: 3, avg_conf: 46.0, real: 33.3 },
    { bucket: "50-58", n: 9, avg_conf: 54.2, real: 61.1 },
    { bucket: "58-66", n: 14, avg_conf: 61.8, real: 64.3 },
    { bucket: "66-100", n: 9, avg_conf: 71.5, real: 44.4 },
  ],
  by_direction: {
    up: { n: 16, hit_rate: 56.3, avg_ret: 0.42 },
    down: { n: 12, hit_rate: 70.8, avg_ret: -0.55 },
    side: { n: 7, hit_rate: 42.9, avg_ret: 0.11 },
  },
  position_edge: { n: 35, low_pos_avg: 34.0, low_next_ret: -0.32, high_pos_avg: 62.0, high_next_ret: 0.48 },
  position_ic: 0.31,
  timing: { n: 35, open_hit: 51.2, close_hit: 58.6 },
  recent: [
    { date: "2026-09-02", actual_date: "2026-09-03", direction: "down", confidence: 60, position: 43, ret: -0.62, hit: 1.0, grade: "命中" },
    { date: "2026-09-01", actual_date: "2026-09-02", direction: "up", confidence: 71, position: 55, ret: 0.85, hit: 1.0, grade: "命中" },
    { date: "2026-08-31", actual_date: "2026-09-01", direction: "side", confidence: 52, position: 48, ret: 0.12, hit: 1.0, grade: "命中" },
    { date: "2026-08-28", actual_date: "2026-08-31", direction: "up", confidence: 68, position: 61, ret: -1.24, hit: 0.0, grade: "反向" },
    { date: "2026-08-27", actual_date: "2026-08-28", direction: "up", confidence: 45, position: 39, ret: 0.08, hit: 0.5, grade: "部分" },
  ],
}, ["实测命中率", "58.6%", "BRIER", "0.238", "置信度校准", "分方向表现",
    "仓位建议有效性", "时点对比", "近期逐笔", "命中", "反向", "部分", "n=9"]);

// 3) 边界：字段缺失 / null / 空数组
run("缺 calibration", { ok: true, ready: true, total: 5, settled: 5, pending: 0, hit_rate: 60, strict_hit_rate: 40, partial_rate: 40, brier: null, recent: [] },
    ["实测命中率", "60.0%"], ["置信度校准"]);
run("calibration 全空档", { ok: true, ready: true, total: 5, settled: 5, pending: 0, hit_rate: 60, calibration: [{ bucket: "35-50", n: 0, avg_conf: null, real: null }], recent: [] },
    ["实测命中率"], ["cal-mark"]);
run("hit_rate 为 null", { ok: true, ready: true, total: 5, settled: 5, pending: 0, hit_rate: null, strict_hit_rate: null, partial_rate: null, brier: null, recent: [] },
    ["实测命中率", "—"], ["NaN", "undefined"]);
run("position_edge 缺失", { ok: true, ready: true, total: 5, settled: 5, pending: 0, hit_rate: 60, recent: [{ date: "2026-09-02", direction: "up", confidence: 60, ret: null, hit: null, grade: null }] },
    ["近期逐笔", "待定"], ["仓位建议有效性"]);
run("仓位有效性为负(低仓组更好)", { ok: true, ready: true, total: 12, settled: 12, pending: 0, hit_rate: 50, position_edge: { n: 12, low_pos_avg: 30, low_next_ret: 0.4, high_pos_avg: 60, high_next_ret: -0.2 }, recent: [] },
    ["仓位建议有效性", "降仓偏保守"]);
run("timing 样本不足(n<5)", { ok: true, ready: true, total: 5, settled: 5, pending: 0, hit_rate: 50, timing: { n: 3, open_hit: 33, close_hit: 66 }, recent: [] },
    ["实测命中率"], ["时点对比"]);
run("极端值(命中100%)", { ok: true, ready: true, total: 3, settled: 3, pending: 0, hit_rate: 100, strict_hit_rate: 100, partial_rate: 0, brier: 0, calibration: [{ bucket: "66-100", n: 3, avg_conf: 80, real: 100 }], recent: [] },
    ["100.0%", "0.000", "优于随机"], ["NaN", "undefined"]);
run("极端值(命中0%)", { ok: true, ready: true, total: 3, settled: 3, pending: 0, hit_rate: 0, strict_hit_rate: 0, partial_rate: 0, brier: 0.9, calibration: [{ bucket: "66-100", n: 3, avg_conf: 80, real: 0 }], recent: [] },
    ["0.0%", "不优于随机"], ["NaN", "undefined"]);

console.log("\n失败 " + fail + " 项");
process.exit(fail ? 1 : 0);
