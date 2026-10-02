# A股盯盘系统 · 腾讯轻量云(Lighthouse)部署指南

> ⚠️ **状态：方案已中止（2026-08-22 决定，2026-09-21 归档）**
> 当时 Lighthouse 未连接、用户决定暂不上云，流程中止。随本次清理，配套的本地部署物料
> `deploy.sh`、`ashare-monitor.service` 已删除（内容都很短，需要时按本文档第 4 节重建即可）。
> **本文档保留下来的是第 0 节的代码审计结论 —— 它仍然成立**：系统纯 Python 3 + `requests`，
> 无 iFinD / 无 Windows 专属 API / 无数据库依赖，真要上 Linux 云是零改造的。
> 当前实际运行方式见 `MEMORY.md`「运行部署」节（Windows 计划任务 + pythonw）。

## 0. 可行性结论（已审计代码）

整套系统**无需任何代码改造即可部署到 Linux 轻量云**：

| 维度 | 现状 | 上云影响 |
|------|------|----------|
| 语言 | 纯 Python 3（标准库 + `requests`） | 轻量云自带 Python3，装 `requests` 即可 |
| 行情源 | 东方财富 + 新浪**免费接口** | 跨平台、云端可用，**不依赖 iFinD / Windows** |
| 存储 | 本地 JSON（`funds.json` 等） | 无数据库，数据落在实例磁盘，重启不丢 |
| 鉴权 | `dashboard.html` token 网关 | 上云即用，输入 `config.json` 的 `api_token` 进入 |
| 监听 | `server.py` 已绑定 `0.0.0.0:8765` | 公网可直接访问 |

> 注：项目早期记忆曾记录"依赖 iFinD"，但当前代码已全面改用东方财富/新浪免费接口，iFinD 依赖**已不存在**。

---

## 1. 前置：连接 Lighthouse 连接器（用户动作）

当前会话未连接「腾讯轻量应用服务器(Lighthouse)」连接器，无法对云做实操。
请在 WorkBuddy 左侧「连接器」中搜索 **轻量应用服务器 / Lighthouse**，完成连接与 OAuth 授权。
连好后，下列「云上操作」可由我直接执行（创建实例、配置防火墙、上传、启动）。

---

## 2. 创建轻量云实例

- **镜像**：Ubuntu 22.04 LTS / TencentOS / Debian（任选 Linux）
- **套餐**：最低 **2 核 2G** 足够（纯 Python 轻量服务，内存占用小）
- **地域**：就近（如上海 `ap-shanghai`）
- **系统盘**：默认即可（代码 + JSON 数据很小）

> 若想保留本地现有基金持仓，把本地的 `funds.json` 一并上传即可；否则首次运行后通过页面录入。

---

## 3. 防火墙开放端口

在 Lighthouse 控制台的**防火墙**中添加规则（实例级）：
- 协议 `TCP`，端口 `8765`，来源 `0.0.0.0/0`（若走 Nginx 则开放 `80/443`，见第 7 节）

---

## 4. 上传代码

将整个项目目录（除 `__pycache__/`、`server.log`、`*.pyc` 外）上传到服务器，例如 `/opt/ashare-monitor`：

```bash
# 本地（Windows Git Bash）示例：
scp -r "D:\盯盘系统" root@<公网IP>:/opt/ashare-monitor
```

需上传的核心文件：
`server.py` `data_fetcher.py` `analysis.py` `news.py` `alerts.py` `fund_manager.py`
`trend.py` `config.json` `dashboard.html` `requirements.txt` `ashare-monitor.service`
以及数据文件 `funds.json` `fund_history.json` `alerts.json` `rotation_state.json` `last_report.json`

---

## 5. 运行部署脚本

```bash
ssh root@<公网IP>
cd /opt/ashare-monitor
bash deploy.sh
```

脚本会：装依赖 → 注册 systemd 开机自启（替代 Windows 任务计划程序）→ 启动 → 健康检查。

---

## 6. 验证

- 浏览器访问 `http://<公网IP>:8765`，输入 `config.json` 里 `api_token` 对应的口令（以配置文件为准；改完需重启服务生效）进入。
- 命令行：`curl http://127.0.0.1:8765/api/health`

运维命令：
```bash
systemctl status ashare-monitor     # 状态
journalctl -u ashare-monitor -f     # 日志
systemctl restart ashare-monitor    # 重启
```

---

## 7. （可选）Nginx 反代 + HTTPS + 域名

若希望用 80/443 + 域名访问，安装 Nginx 并反代 8765：

```nginx
server {
    listen 80;
    server_name your.domain.com;
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

此时防火墙只需开放 `80/443`，`8765` 可改为仅监听 `127.0.0.1`（编辑 `server.py` 的 `("0.0.0.0", port)` 为 `("127.0.0.1", port)`）。

---

## 8. 注意事项

- **行情源限流**：东方财富/新浪免费接口对公网 IP 偶有风控，代码已带 `User-Agent` + 重试 + 熔断冷却，一般可用；若长期被限流，可考虑加出口代理。
- **dashboard.html 小瑕疵**：第 325 行有个 `http://localhost:8765` 的"打开实时盯盘"按钮链接，上云后无意义（不影响核心功能），可后续清理。
- **数据安全**：持仓与历史均在 JSON 文件，建议定期对 `/opt/ashare-monitor/*.json` 做快照备份。
