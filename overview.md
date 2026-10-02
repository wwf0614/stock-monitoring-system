# A股盯盘系统 · 工作概览（截至2026-09-03）

## 本次新增交付

- 新增 `deliverables/2026下半年主线与场外基金建议.html`：梳理2026年下半年AI硬科技、创新药、周期修复、红利低波四类主线，给出场外基金映射，并结合现有四只C类持仓提示主题重合与费率护栏。
- 核心结论：不宜继续无差别叠加AI硬件；优先核对持仓穿透，新增资金优先补低相关红利/纯债底仓，周期和新能源等待景气确认。

# A股盯盘系统 · 优化总结（2026-08-22）

围绕用户确认的四个维度完成了系统优化，并通过单测 + 实跑冒烟验证。

## 一、稳定性 / 运维（最高优先级）

**核心隐患（已修复）**：原系统 `funds.json`、`fund_history.json`、`rotation_state.json`、`alerts.json`、`last_report.json` 全部用 `open(path,"w")` 直接覆盖写。一旦写的过程中进程被中断（崩溃 / 强杀 / 断电），文件会被截断成半截而损坏，基金持仓将丢失。

**修复方案**：新增 `json_util.py` 安全读写模块：
- `atomic_write_json` / `atomic_write_text`：先写临时文件 → `fsync` → `os.replace` 原子覆盖（同盘内原子，杜绝写一半）。写前自动把旧文件备份为 `.bak`，提供二级回退。
- `safe_load_json`：读取时主文件损坏或缺失 → 自动回退 `.bak` → 再回退 `default`，绝不因读取异常让进程崩溃；若主文件损坏还会用 `.bak` 自动修复主文件。

已将以上 5 个 JSON 的所有写入点 + 关键读取点改为原子/安全读写（覆盖 `fund_manager.py`、`analysis.py`、`alerts.py`、`daily_report.py`、`fund_direction_1450.py`）。

**日志轮转**：`server.py` 无控制台运行时（任务计划/后台启动）的 stdout 重定向，由追加写改为 `_RotatingLog` 可轮转流，`server.log` 超过 2MB 自动滚动为 `.1/.2/.3`，避免无限增长。

**工作线程看门狗**：新增 `_watchdog_worker`，每 30s 检测 `_refresh` / `_cap_refresh` / `_fund_refresh` 三个后台线程，发现退出自动拉起新实例（原代码三线程为 `while True` + 内部 try，正常不会死，看门狗为极端情况兜底）。

## 二、看板 UI / 体验

`dashboard.html` 的「打开实时盯盘」按钮原本写死 `http://localhost:8765`，在手机 / 局域网访问时会失效。已改为按 `location.hostname` 动态拼出 `<host>:8765`，跨设备访问也能正确跳转。

## 三、性能 / 抓取

经审计，`data_fetcher.py` 与 `news.py` 已使用 `requests.Session()` 复用连接 + 板块限流熔断 / TTL 缓存，该维度无需改动，保持现状即可。

## 四、分析 / 调仓质量

分析引擎（regime 判定 + 技术/情绪/资金三维评分 + recommend + 板块轮动 + 中期趋势 `trend.py`）已较为完善。本次重点是消除「`rotation_state.json` 损坏导致 `compute_rotation` 崩溃」的隐患 —— 通过 `safe_load_json` 回退，损坏时返回空 sectors 而非抛异常中断评分。

## 验证结果

- `json_util` 单测：原子写 + 损坏回退 `.bak` 均通过。
- `fund_manager` 集成：存读往返一致；主文件截断后从 `.bak` 成功恢复持仓。
- `server` 实跑：`/api/health` 返回 `{"ok":true,"ready":true}`，4 只基金中期趋势预热正常，启动无报错。
- 全部改动文件 `py_compile` 通过，所有模块导入正常。

## 五、新增：后市研判 · 基于历史行情（替换 what-if 实时推演）

用户反馈「量化推演」面板一直「推演失败/加载太慢」。根因是：新增 `/api/simulate` 路由必须重启 `server.py` 才能生效，且 what-if 推演依赖实时快照就绪。因此改为**不依赖实时行情、直接抓取前几天历史 K 线**的「后市研判」功能。

- **后端**：新增 `forecast.py`，直接抓取核心指数（沪深300 / 上证指数 / 创业板指 / 深证成指）前 N 个交易日日K线（复用 `data_fetcher.fetch_kline`，新浪优先 + 东方财富兜底），计算：
  - 近 5 / 10 / 20 日动量
  - MA5 / MA10 / MA20 多空排列
  - 波动率
  综合给出 regime_label、short_term_view（未来 3-5 个交易日研判）、置信度、建议仓位、持仓基金后市建议。结果按 `lookback` 缓存 60s，避免每次点击都重新抓 K 线。
- **API**：`server.py` 新增 `POST /api/forecast`（token 保护），调用 `forecast_mod.forecast_market`。
- **前端**：`dashboard.html` 把「量化推演」面板整体替换为「📈 后市研判 · 基于历史行情」，包含：
  - 时间窗口按钮：前 5 / 10 / 20 / 60 天；
  - 「分析后市」按钮；
  - 研判结果卡：市场状态（涨跌色大标签）、短线观点、综合评分、置信度、建议仓位进度条；
  - 核心指数短期表现表；
  - 持仓基金后市建议表。
- **验证**：`/api/forecast` 返回正常，示例：近 20 日研判为「震荡偏空」，4 只持仓基金建议已生成；dashboard 根路径 HTML 已包含新面板元素，JS 语法检查通过。

## 改动文件清单

- 新增：`json_util.py`、`forecast.py`（`simulation.py` 仍保留在目录中，但面板已不再调用）
- 修改：`fund_manager.py`、`analysis.py`、`alerts.py`、`daily_report.py`、`fund_direction_1450.py`、`server.py`、`dashboard.html`
- 测试产生的临时文件均已清理，未触碰用户真实 `funds.json` 等数据。

## 备注

所有改动均为**增量、向后兼容**：每个调用点都保留了 `json_util` 不可用时（极少情况）的原有 `open` 直写兜底，模块导入用 `try/except` 包裹，不会因新模块缺失而拖垮旧逻辑。
