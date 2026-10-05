# 基金盯盘与基金持仓监控系统

本地运行的 A 股大盘 + 场外基金盯盘监控系统：抓取指数/板块/主力资金/新闻，进行 regime 判定与三维评分（技术/情绪/资金），输出买卖提醒与后市量化研判，并对基金持仓做每日净值结算，配套 Web 仪表盘。

## 功能特性

- **行情监控**：东方财富/新浪免费接口抓取指数、板块、主力资金、滚动新闻（Session 复用 + 重试 + 板块限流熔断）
- **信号引擎**：regime 判定 + 技术/情绪/资金三维评分，输出卖出风险与买入候选提醒
- **后市研判**：基于历史 K 线动量/均线/波动率的量化研判，每日对账追踪命中率
- **基金持仓**：场外基金持仓管理、净值抓取、每日快照与金额滚动结算（支持 C 类费率）
- **夜间维护**：Windows 计划任务每日自动回填板块资金落库、净值历史续写、结算与命中率统计
- **告警推送**：本地落盘 + 可选邮件 / webhook / Server酱
- **Web 仪表盘**：单文件原生 JS，持仓/决策/资金/市场四视图，口令门鉴权，支持手机局域网访问
- **历史数据库**：SQLite（WAL）存储交易日历、K 线、板块资金流、净值历史等 14 张表

## 技术栈

- 后端：Python 3.9+ 标准库 `http.server`（ThreadingHTTPServer），无重型框架
- 数据源：东方财富 push2、新浪财经（免费接口）；AKShare（盘后 ETL，版本锁定 1.18.88）
- 存储：SQLite（WAL 模式）+ JSON 状态文件（原子写 + `.bak` 二级回退）
- 前端：单文件 `dashboard.html` 原生 JS，无框架依赖

## 项目结构

```
├── server.py            # HTTP 服务 + API 路由 + 后台刷新线程 + 看门狗
├── data_fetcher.py      # 东财/新浪行情抓取层
├── akshare_provider.py  # AKShare 长历史/横截面数据（盘后 ETL）
├── wind_provider.py     # 万得适配层（当前降级不启用）
├── analysis.py          # regime 判定 + 三维评分 + 板块轮动
├── signals.py           # 投资提醒信号引擎
├── forecast.py          # 大盘后市研判
├── fund_forecast.py     # 持仓基金研判
├── forecast_track.py    # 研判命中率记录与对账
├── fund_manager.py      # 基金持仓 CRUD 与每日结算
├── fund_nightly.py      # 夜间维护任务（计划任务 21:30 触发）
├── etl_backfill.py      # akshare 数据固化进 SQLite 的 ETL
├── history_db.py        # SQLite 历史库（14 张表）
├── alerts.py            # 告警引擎（邮件/webhook/Server酱）
├── news.py              # 滚动新闻情绪打分
├── trend.py             # 中期趋势（1-3 个月）
├── json_util.py         # JSON 原子写 + 二级回退
├── watchdog.py          # 服务探活
├── dashboard.html       # Web 仪表盘（单文件）
├── start_server.bat / launch.vbs  # 前台 / 后台静默启动
├── config.example.json  # 配置模板（复制为 config.json 使用）
├── docs/                # 研判校准文档
└── deliverables/        # 投资分析产出与 UI 设计稿
```

## 快速开始

```bash
# 1. 安装依赖（建议 Python 3.9+）
pip install -r requirements.txt

# 2. 生成配置
cp config.example.json config.json   # 修改 api_token 为你自己的访问口令

# 3. 启动
python server.py 8765
# Windows 后台静默启动：双击 launch.vbs
```

浏览器访问 <http://127.0.0.1:8765/?token=你的口令>。手机访问见 `MOBILE_ACCESS.md`。

### 可选配置

- **夜间结算**：将 `fund_nightly.py` 加入 Windows 计划任务（建议每日 21:30），自动完成净值结算与命中率回填
- **告警推送**：编辑 `config.json` 中的 `email` / `webhook` / `serverchan` 段并置 `enabled: true`
- **历史数据**：运行 `python etl_backfill.py` 将 AKShare 长历史数据固化进 `history.db`

## 说明

- `history.db`、`funds.json`、`fund_history.json` 等运行数据与 `config.json` 不入库（见 `.gitignore`），本地运行后自动生成
- 万得（Wind）终端接入方案见 `万得数据接入指南.md`（存档，当前未启用）
- 本项目仅用于个人学习与投资研究，不构成任何投资建议
