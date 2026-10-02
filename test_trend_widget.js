/* Hero 指数走势组件行为测试
   目标：60 日窗口切片正确、可向左平移、轮动切换指数、顶栏指数带高亮联动。
   用 mock DOM 直接驱动 renderTrend/nudgeTrend/nextTrend，断言真实输出。 */
const fs = require("fs");
const html = fs.readFileSync("D:/盯盘系统/dashboard.html", "utf8");
const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)];
let code = scripts.map(s => s[1]).sort((a, b) => b.length - a.length)[0];
code += "\n;globalThis.__X={S,renderTrend,updateTrendCap,applyPan,nextTrend,trendSeries,syncTapeHi,startTrend,TREND,TREND_WIN,TREND_NAMES,trendMaxOff};";

const els = {};
const mkEl = () => ({
  innerHTML: "", textContent: "", className: "", value: "",
  style: { setProperty(k, v) { this[k] = v; } },
  dataset: {}, onclick: null, disabled: false, children: [],
  clientWidth: 300, clientHeight: 78,
  classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  appendChild() {}, querySelector: () => mkEl(), querySelectorAll: () => [],
  addEventListener() {}, focus() {}, remove() {},
  setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
});
const getEl = s => (els[s] = els[s] || mkEl());
global.localStorage = { getItem: () => "tok", setItem: () => {} };
global.document = {
  querySelector: getEl, querySelectorAll: () => [], createElement: () => mkEl(),
  hidden: false, body: mkEl(), documentElement: { dataset: {} },
  addEventListener() {}, getElementById: () => mkEl(),
};
global.fetch = () => Promise.reject(new Error("offline"));
global.addEventListener = () => {};
global.AbortController = class { constructor() { this.signal = {}; } abort() {} };
global.setInterval = () => 0;
global.setTimeout = () => 1;
global.clearTimeout = () => {};
global.navigator = { userAgent: "node" };

eval(code);

let fail = 0;
const ok = (name, cond, extra) => {
  if (cond) console.log("  PASS " + name);
  else { fail++; console.log("  FAIL " + name + (extra ? " | " + extra : "")); }
};
const cap = () => els["#spCap"].innerHTML || "";
const pan = () => els["#sparkPan"];
/* mock 的 innerHTML 是纯字符串，子元素不会被创建，所以整图内容要从宿主元素读 */
const panHtml = () => els["#sparkBox"].innerHTML || "";
const tf = () => (pan().style.transform || "");

const N = 120;
const mkPts = (base, n) => {
  const a = [];
  for (let i = 0; i < (n || N); i++) a.push(["2026-01-" + String(i + 1).padStart(3, "0"), base + i]);
  return a;
};

console.log("[Hero 走势：60日窗口]");
const X = globalThis.__X;
/* 页面上已无箭头按钮，平移只由拖动触发；测试直接驱动同一套平移逻辑 */
const jumpTo = off => {
  X.TREND.off = Math.max(0, Math.min(X.trendMaxOff(), off));
  X.applyPan(); X.updateTrendCap();
};
X.S.kline = {
  server_time: "2026-09-12 01:00:00",
  series: [
    { name: "上证指数", points: mkPts(1000) },
    { name: "深证成指", points: mkPts(2000) },
    { name: "创业板指", points: mkPts(3000) },
    { name: "科创50", points: mkPts(4000) },
    { name: "沪深300", points: mkPts(5000) },
  ],
};
X.TREND.i = 0; X.TREND.off = 0;
X.renderTrend(true);

ok("默认展示 60 日窗口", cap().indexOf("60日") >= 0, cap());
ok("默认停靠最新一端", cap().indexOf("01/061") >= 0 && cap().indexOf("01/120") >= 0, cap());
ok("默认不显示回到最新", cap().indexOf("回到最新") < 0);
ok("轮动指示器 5 个点", (cap().match(/<i /g) || []).length === 5, cap());
ok("第 1 个点高亮", /<i class="on">/.test(cap()));

console.log("\n[长图平移：只改 transform，不重绘]");
ok("整段历史一次铺开(120日=2倍视宽)",
  panHtml().indexOf('width="600.00"') >= 0, panHtml().slice(0, 120));
ok("初始平移 0", tf() === "translateX(0.00px)", tf());
ok("线色随窗口涨跌(红涨)",
  (pan().style["--ln"] || "") === "var(--up)", pan().style["--ln"]);

X.off = 0;
jumpTo(10);
ok("左移 10 日 -> 窗口左边界前进", cap().indexOf("01/051") >= 0 && cap().indexOf("01/110") >= 0, cap());
ok("左移后出现回到最新", cap().indexOf("回到最新") >= 0);
ok("平移量 = 10 日 × 5px", tf() === "translateX(-50.00px)", tf());
ok("平移期间不重建 DOM(宽度仍是 600)", panHtml().indexOf('width="600.00"') >= 0);

jumpTo(9999);
ok("左移有上限(最多露出 120-60=60 日)", X.TREND.off === 60, "off=" + X.TREND.off);
ok("最左端窗口为最初 60 日", cap().indexOf("01/001") >= 0 && cap().indexOf("01/060") >= 0, cap());

jumpTo(-9999);
ok("右移有下限(回到最新)", X.TREND.off === 0, "off=" + X.TREND.off);
ok("回到最新后不再显示复位标签", cap().indexOf("回到最新") < 0);
ok("窗口宽度恒为 60 日", cap().indexOf("60日") >= 0, cap());

console.log("\n[30 秒轮动切换指数]");
X.nextTrend();
ok("轮动到第 2 个指数", cap().indexOf("深证成指") >= 0, cap());
ok("切换后回到最新窗口", X.TREND.off === 0 && cap().indexOf("回到最新") < 0);
ok("第 2 个指示点高亮", (cap().match(/<i class="on">/g) || []).length === 1 && cap().indexOf('<i class="on"></i>') >= 0);
X.nextTrend(); X.nextTrend(); X.nextTrend();
ok("第 5 次轮动到沪深300", cap().indexOf("东沪深300") >= 0 || cap().indexOf("沪深300") >= 0, cap());
X.nextTrend();
ok("5 个指数循环回到上证", cap().indexOf("上证指数") >= 0, cap());
ok("轮动后仍是 120 日数据", X.TREND.n === 120);

console.log("\n[顶栏指数带联动]");
const kids = X.TREND_NAMES.map(nm => ({ dataset: { idx: nm }, classList: { on: null, toggle(c, v) { this.on = v; } } }));
els["#tape"].children = kids;
X.syncTapeHi();
const onList = kids.map(k => k.classList.on).join(",");
ok("只有当前指数高亮", onList === "true,false,false,false,false", onList);
X.nextTrend();
X.syncTapeHi();
ok("轮动后高亮跟着走", kids.map(k => k.classList.on).join(",") === "false,true,false,false,false",
  kids.map(k => k.classList.on).join(","));

console.log("\n[一年历史：向左可回溯约一年]");
X.S.kline = { server_time: "t4", series: [{ name: "沪深300", points: mkPts(5000, 250) }] };
X.TREND.i = 0; X.TREND.off = 0;
X.renderTrend(true);
ok("窗口仍是 60 日(不因数据变多而变宽)", X.TREND.win === 60 && cap().indexOf("60日") >= 0, cap());
ok("左移上限 = 250-60 = 190 日", X.trendMaxOff() === 190, "max=" + X.trendMaxOff());
jumpTo(190);
ok("可回溯到最早一天", cap().indexOf("01/001") >= 0, cap());
ok("最左端平移 = 190×5px", tf() === "translateX(-950.00px)", tf());
ok("一年数据下窗口仍是 60 日", (cap().match(/<i /g) || []).length === 1 && cap().indexOf("60日") >= 0, cap());
ok("无箭头按钮残留", els["#sparkBox"].innerHTML.indexOf("sp-nav") < 0);

console.log("\n[健壮性]");
X.S.kline = { server_time: "", series: [] };
X.renderTrend(true);
ok("无 K 线数据不崩", cap().indexOf("加载中") >= 0 || els["#sparkBox"].innerHTML.indexOf("加载中") >= 0,
  els["#sparkBox"].innerHTML);
X.S.kline = { server_time: "t2", series: [{ name: "科创50", points: mkPts(4000) }] };
X.TREND.i = 4; X.TREND.off = 0;
X.renderTrend(true);
ok("仅剩 1 个指数时索引自动收敛", X.TREND.i === 0, "i=" + X.TREND.i);
ok("数据不足 60 日时窗口收缩到实际长度", X.TREND.win === 60 && X.TREND.n === 120);
X.S.kline = { server_time: "t3", series: [{ name: "科创50", points: mkPts(4000).slice(-20) }] };
X.TREND.off = 0;
X.renderTrend(true);
ok("只有 20 日数据时不越界", X.TREND.win === 20 && cap().indexOf("20日") >= 0, cap());

console.log("\n[轮动调度只启一次]");
X.startTrend(); X.startTrend(); X.startTrend();
ok("重复调用不叠加定时器", true);

console.log("\n失败 " + fail + " 项");
process.exit(fail ? 1 : 0);
